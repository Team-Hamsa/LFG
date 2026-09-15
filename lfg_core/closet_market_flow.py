"""Closet Market state machines (#443).

Two machines, both driven inline by the service and retried by the 2-minute
settlement sweep; every step is idempotent and safe to re-enter.

Bid (closet_orders, side='bid'):
  pending_escrow --signed+validated EscrowCreate matches--> open --cross--> matched
  open --expiry / user cancel--> cancelling --> expired | cancelled
  A cancelling bid is returned by EscrowCancel after CancelAfter, else by
  EscrowFinish (backend holds the fulfillment) followed by a refund Payment.

Fill (closet_fills):
  funds_pending --funds in--> funded --asset move (1 sqlite tx)--> asset_moved
  --forward seller [+ overshoot buyer]--> paid --mirror both Closets--> mirrored
  refund_pending --refund--> refunded ; funds_pending --cannot proceed--> failed
  Any backend tx with an unknown outcome parks the fill in `indeterminate`
  with pending_phase/pending_lls. It resolves ONLY by finding the memo-tagged
  tx (landed) or by the validated ledger passing pending_lls with nothing
  found (absent -> retry). Absence alone is never failure.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
import traceback
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from lfg_core import closet_market_store as cms
from lfg_core import config, market_ops, memos, owner_lock
from lfg_core.xrpl_ops import (
    RIPPLE_EPOCH_OFFSET,
    TxNotSubmitted,
    TxOutcome,
    tx_entry_hash,
    tx_entry_result,
)

CANCEL_SLACK_SECONDS = (
    60  # ledger close time lags wall clock; EscrowCancel before CancelAfter is tecNO_PERMISSION
)
MAX_STEPS = 8


@dataclass
class ClosetMarketDeps:
    conn_factory: Callable[[], sqlite3.Connection]
    app_account: str
    fee_bps: int
    payload_status_fn: Callable[[str], Awaitable[dict[str, Any] | None]]
    get_tx_fn: Callable[[str], Awaitable[dict[str, Any]]]
    get_escrow_fn: Callable[[str, int], Awaitable[dict[str, Any] | None]]
    escrow_finish_fn: Callable[[str, int, str, str, str], Awaitable[TxOutcome]]
    escrow_cancel_fn: Callable[[str, int, str], Awaitable[TxOutcome]]
    payment_fn: Callable[[str, str, str, str], Awaitable[TxOutcome]]
    find_txs_fn: Callable[[str, int | None], Awaitable[list[dict[str, Any]]]]
    ledger_index_fn: Callable[[], Awaitable[int | None]]
    mirror_fn: Callable[[sqlite3.Connection, str], Awaitable[None]]
    unseal_fn: Callable[[str], str]
    records_dir: str = config.ECONOMY_RECORDS_DIR
    now_fn: Callable[[], float] = time.time
    signing_wait_seconds: int = 3600


def _journal(deps: ClosetMarketDeps, kind: str, row: dict[str, Any] | None) -> None:
    """Best-effort on-disk record of every transition (economy_flow._write_record posture)."""
    if row is None:
        return
    try:
        os.makedirs(deps.records_dir, exist_ok=True)
        path = os.path.join(deps.records_dir, f"closet-{kind}-{row['id']}.json")
        public = {k: v for k, v in row.items() if k != "fulfillment_enc"}
        with open(path, "w") as fh:
            json.dump(public, fh, indent=2, default=str)
    except Exception:
        logging.error(f"closet market journal write failed: {traceback.format_exc()}")


def _amount_equals(a: Any, b: dict[str, str]) -> bool:
    if not isinstance(a, dict):
        return False
    try:
        return (
            str(a.get("currency", "")).upper() == str(b["currency"]).upper()
            and a.get("issuer") == b["issuer"]
            and Decimal(str(a.get("value"))) == Decimal(b["value"])
        )
    except (InvalidOperation, ValueError):
        return False


def _tx_body(tx: dict[str, Any]) -> dict[str, Any]:
    body = tx.get("tx_json")
    return body if isinstance(body, dict) else tx


async def _submit(fn: Callable[..., Awaitable[TxOutcome]], *args: Any) -> TxOutcome | None:
    try:
        return await fn(*args)
    except TxNotSubmitted as exc:
        logging.warning(f"closet market tx not submitted: {exc}")
        return None


async def _phase_outcome(
    deps: ClosetMarketDeps, tag: str, lls: int | None
) -> tuple[str, str | None]:
    """("landed", hash) | ("absent", None) | ("wait", None) for an unknown tx."""
    try:
        entries = await deps.find_txs_fn(tag, lls)
    except Exception:
        logging.warning(f"closet market memo lookup failed for {tag}: {traceback.format_exc()}")
        return "wait", None
    for entry in entries:
        if tx_entry_result(entry) == "tesSUCCESS":
            return "landed", tx_entry_hash(entry) or cms.HASH_UNKNOWN
    index = await deps.ledger_index_fn()
    if index is not None and lls is not None and index > lls:
        return "absent", None
    return "wait", None


# --- bids ---------------------------------------------------------------------


def _bid_expired(order: dict[str, Any], deps: ClosetMarketDeps) -> bool:
    cancel_after = order["cancel_after"]
    return (
        cancel_after is not None
        and int(deps.now_fn()) - RIPPLE_EPOCH_OFFSET >= cancel_after + CANCEL_SLACK_SECONDS
    )


def _final_cancel_state(order: dict[str, Any]) -> str:
    return cms.EXPIRED if order["cancel_reason"] == "expired" else cms.CANCELLED


def _escrow_mismatch(
    fields: dict[str, Any], order: dict[str, Any], deps: ClosetMarketDeps
) -> str | None:
    if fields.get("Account") != order["owner"]:
        return "wrong account"
    if fields.get("Destination") != deps.app_account:
        return "wrong destination"
    if not _amount_equals(fields.get("Amount"), market_ops.brix_amount_dict(order["price_brix"])):
        return "wrong amount"
    if str(fields.get("Condition") or "").upper() != str(order["condition"]).upper():
        return "wrong condition"
    return None


def _cancel_unopened(
    conn: sqlite3.Connection, order: dict[str, Any], deps: ClosetMarketDeps, reason: str
) -> None:
    cms.update_order(conn, order["id"], state=cms.CANCELLED, cancel_reason="unopened", error=reason)
    _journal(deps, "order", cms.get_order(conn, order["id"]))


async def advance_bid(order_id: str, deps: ClosetMarketDeps) -> str | None:
    async with owner_lock.owner_lock(f"closet-order:{order_id}"):
        conn = deps.conn_factory()
        try:
            order = cms.get_order(conn, order_id)
            if order is None or order["side"] != cms.SIDE_BID:
                return None
            if order["state"] == cms.PENDING_ESCROW:
                return await _advance_pending_bid(conn, order, deps)
            if order["state"] == cms.OPEN:
                if not _bid_expired(order, deps):
                    fill = cms.cross_incoming(conn, order_id, fee_bps=deps.fee_bps)
                    return str(fill["id"]) if fill else None
                try:
                    cms.begin_cancel_bid(conn, order_id, owner=None, reason="expired")
                except cms.OrderError:
                    return None
            order = cms.get_order(conn, order_id)
            if order is not None and order["state"] == cms.CANCELLING:
                await _advance_cancelling_bid(conn, order, deps)
            return None
        finally:
            conn.close()


async def _advance_pending_bid(
    conn: sqlite3.Connection, order: dict[str, Any], deps: ClosetMarketDeps
) -> str | None:
    waited = deps.now_fn() - order["created_ts"]
    if not order["signed_txid"]:
        status = (
            await deps.payload_status_fn(order["payload_uuid"]) if order["payload_uuid"] else None
        )
        if status is None or not status.get("signed"):
            if (status or {}).get("expired") or waited > deps.signing_wait_seconds:
                _cancel_unopened(conn, order, deps, "signing request expired")
            return None
        if status.get("account") != order["owner"]:
            _cancel_unopened(
                conn, order, deps, "the bid was signed by a different account than yours"
            )
            return None
        txid = status.get("txid")
        if not txid:
            return None
        cms.update_order(conn, order["id"], signed_txid=txid)
        order = cms.get_order(conn, order["id"]) or order
    try:
        tx = await deps.get_tx_fn(order["signed_txid"])
    except Exception:
        logging.warning(f"closet bid {order['id']}: tx lookup failed: {traceback.format_exc()}")
        return None
    if not tx.get("validated"):
        if waited > deps.signing_wait_seconds:
            _cancel_unopened(conn, order, deps, "the escrow never validated")
        return None
    result = (tx.get("meta") or {}).get("TransactionResult")
    if result != "tesSUCCESS":
        _cancel_unopened(conn, order, deps, f"escrow create failed: {result}")
        return None
    body = _tx_body(tx)
    problem: str | None
    if body.get("TransactionType") != "EscrowCreate":
        problem = "not an EscrowCreate"
    elif body.get("CancelAfter") != order["cancel_after"] or body.get("FinishAfter") is not None:
        problem = "wrong expiry"
    else:
        problem = _escrow_mismatch(body, order, deps)
    seq_raw = body.get("Sequence") or body.get("TicketSequence")
    seq = seq_raw if isinstance(seq_raw, int) else None
    if problem is None and seq is None:
        problem = "no escrow sequence"
    if problem is None and seq is not None:
        try:
            node = await deps.get_escrow_fn(order["owner"], seq)
        except Exception:
            logging.warning(
                f"closet bid {order['id']}: escrow lookup failed: {traceback.format_exc()}"
            )
            return None
        problem = (
            "the escrow is no longer on-ledger"
            if node is None
            else _escrow_mismatch(node, order, deps)
        )
    if problem is not None or seq is None:
        logging.error(f"closet bid {order['id']}: validated escrow rejected ({problem})")
        _cancel_unopened(conn, order, deps, f"escrow did not match the bid ({problem})")
        return None
    cms.mark_bid_open(conn, order["id"], escrow_tx_hash=order["signed_txid"], escrow_owner_seq=seq)
    _journal(deps, "order", cms.get_order(conn, order["id"]))
    fill = cms.cross_incoming(conn, order["id"], fee_bps=deps.fee_bps)
    return str(fill["id"]) if fill else None


def _apply_order_outcome(
    conn: sqlite3.Connection,
    order: dict[str, Any],
    out: TxOutcome | None,
    *,
    phase: str,
    hash_field: str,
    complete: bool,
) -> str:
    if out is None:
        return "not_submitted"
    if out.state == "confirmed":
        fields: dict[str, Any] = {hash_field: out.tx_hash or cms.HASH_UNKNOWN, "error": None}
        if complete:
            fields["state"] = _final_cancel_state(order)
        cms.update_order(conn, order["id"], **fields)
    elif out.state == "unknown":
        cms.update_order(conn, order["id"], pending_phase=phase, pending_lls=out.last_ledger_seq)
    else:
        cms.update_order(conn, order["id"], error=f"{phase} failed; will retry")
    return out.state


async def _advance_cancelling_bid(
    conn: sqlite3.Connection, order: dict[str, Any], deps: ClosetMarketDeps
) -> None:
    oid = order["id"]
    if order["pending_phase"]:
        verdict, tx_hash = await _phase_outcome(
            deps, cms.memo_tag(order["pending_phase"], oid), order["pending_lls"]
        )
        if verdict == "wait":
            return
        fields: dict[str, Any] = {"pending_phase": None, "pending_lls": None}
        if verdict == "landed":
            if order["pending_phase"] == "cancel_finish":
                fields["cancel_finish_hash"] = tx_hash
            else:  # "cancel" (EscrowCancel) and "cancel_refund" both complete it
                fields["cancel_refund_hash"] = tx_hash
                fields["state"] = _final_cancel_state(order)
        cms.update_order(conn, oid, **fields)
        order = cms.get_order(conn, oid) or order
        if order["state"] != cms.CANCELLING:
            _journal(deps, "order", order)
            return
    seq = order["escrow_owner_seq"]
    if order["cancel_finish_hash"] is None:
        if seq is None:
            cms.update_order(conn, oid, state=_final_cancel_state(order))
            return
        try:
            node = await deps.get_escrow_fn(order["owner"], seq)
        except Exception:
            logging.warning(f"closet bid {oid}: escrow lookup failed: {traceback.format_exc()}")
            return
        if node is None:  # cancelled on-ledger after CancelAfter (anyone may): the BRIX is home
            cms.update_order(conn, oid, state=_final_cancel_state(order))
            _journal(deps, "order", cms.get_order(conn, oid))
            return
        if _bid_expired(order, deps):
            out = await _submit(
                deps.escrow_cancel_fn, order["owner"], seq, cms.memo_tag("cancel", oid)
            )
            _apply_order_outcome(
                conn, order, out, phase="cancel", hash_field="cancel_refund_hash", complete=True
            )
            _journal(deps, "order", cms.get_order(conn, oid))
            return
        out = await _submit(
            deps.escrow_finish_fn,
            order["owner"],
            seq,
            order["condition"],
            deps.unseal_fn(order["fulfillment_enc"]),
            cms.memo_tag("cancel_finish", oid),
        )
        if (
            _apply_order_outcome(
                conn,
                order,
                out,
                phase="cancel_finish",
                hash_field="cancel_finish_hash",
                complete=False,
            )
            != "confirmed"
        ):
            return
        order = cms.get_order(conn, oid) or order
    if order["cancel_refund_hash"] is None:
        out = await _submit(
            deps.payment_fn,
            order["owner"],
            order["price_brix"],
            cms.memo_tag("cancel_refund", oid),
            memos.ACTION_CLOSET_REFUND,
        )
        _apply_order_outcome(
            conn, order, out, phase="cancel_refund", hash_field="cancel_refund_hash", complete=True
        )
    _journal(deps, "order", cms.get_order(conn, oid))
