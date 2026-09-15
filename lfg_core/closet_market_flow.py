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

User txs (the buy Payment, the bid EscrowCreate) carry a pinned
LastLedgerSequence, recorded as `user_lls` on the row (PR #502 G1). A
"nothing landed" decision (fail a take fill / cancel an unopened bid) on a row
with user_lls set needs BOTH a validated ledger index, read before the
reconciliation lookup, past user_lls AND that lookup proving it covers
user_lls; anything short of that waits. Rows with user_lls NULL keep the
pre-G1 behavior.
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
from lfg_core import closet_token, config, market_ops, memos, owner_lock
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
    # Each backend-tx callable also takes a keyword-only `max_last_ledger_seq`
    # (Callable[..., ...] rather than a fixed positional signature, so both the
    # real xrpl_ops functions and the test fakes can accept it).
    escrow_finish_fn: Callable[..., Awaitable[TxOutcome]]
    escrow_cancel_fn: Callable[..., Awaitable[TxOutcome]]
    payment_fn: Callable[..., Awaitable[TxOutcome]]
    find_txs_fn: Callable[[str, int | None], Awaitable[list[dict[str, Any]]]]
    ledger_index_fn: Callable[[], Awaitable[int | None]]
    mirror_fn: Callable[[sqlite3.Connection, str], Awaitable[None]]
    unseal_fn: Callable[[str], str]
    records_dir: str = config.ECONOMY_RECORDS_DIR
    now_fn: Callable[[], float] = time.time
    signing_wait_seconds: int = 3600
    ledger_margin: int = 40
    # On-ledger reconciliation before a terminal "nothing arrived" decision
    # (#443 final review C2). None skips the lookup (the old behavior) — but a
    # row with user_lls set can then never prove absence, so it waits.
    # Both take a keyword `require_ledger` (the row's user_lls, or None).
    find_invoice_payments_fn: Callable[..., Awaitable[list[dict[str, Any]]]] | None = None
    find_escrow_by_condition_fn: Callable[..., Awaitable[dict[str, Any] | None]] | None = None


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


async def _submit(
    fn: Callable[..., Awaitable[TxOutcome]], *args: Any, **kwargs: Any
) -> TxOutcome | None:
    try:
        return await fn(*args, **kwargs)
    except TxNotSubmitted as exc:
        logging.warning(f"closet market tx not submitted: {exc}")
        return None


async def _begin_order_phase(
    conn: sqlite3.Connection, order: dict[str, Any], phase: str, deps: ClosetMarketDeps
) -> int | None:
    """Write-ahead intent: persist pending_phase/pending_lls BEFORE the backend
    tx is submitted, and bound the tx's own LastLedgerSequence to that same
    value (via max_last_ledger_seq). A crash between the tx landing on-ledger
    and this process recording the outcome then still leaves a resolvable
    trail: the next pass finds pending_phase already set and resolves it via
    _phase_outcome's memo lookup, bounded by the SAME lls that was durably
    recorded before anything was sent. Returns None (submit nothing) if the
    ledger index can't be read, matching xrpl_ops' own refuse-to-submit
    posture for an unpinnable deadline."""
    idx = await deps.ledger_index_fn()
    if idx is None:
        return None
    lls = idx + deps.ledger_margin
    cms.update_order(conn, order["id"], pending_phase=phase, pending_lls=lls)
    return lls


async def _phase_outcome(
    deps: ClosetMarketDeps, tag: str, lls: int | None
) -> tuple[str, str | None]:
    """("landed", hash) | ("absent", None) | ("wait", None) for an unknown tx.

    The ledger index is read BEFORE the memo scan, and "absent" is decided
    against that pre-scan value alone (#443 review fix). Reading it after the
    scan let a tx that validated in the gap between the two reads look
    "absent" — the scan (started earlier) wouldn't see it yet, but the
    index (read later) would already be past `lls`, so the caller would
    resubmit a tx that already landed and double-send. Using the earlier,
    smaller reading is always safe: if the scan itself has since found the
    tx, "landed" wins below regardless of which index we read."""
    index = await deps.ledger_index_fn()
    try:
        entries = await deps.find_txs_fn(tag, lls)
    except Exception:
        logging.warning(f"closet market memo lookup failed for {tag}: {traceback.format_exc()}")
        return "wait", None
    for entry in entries:
        if tx_entry_result(entry) == "tesSUCCESS":
            return "landed", tx_entry_hash(entry) or cms.HASH_UNKNOWN
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


def _escrow_create_sequence(tx: dict[str, Any], owner: str) -> int | None:
    """The owner-sequence of a validated, successful EscrowCreate from `owner`."""
    body = _tx_body(tx)
    if (
        (tx.get("meta") or {}).get("TransactionResult") != "tesSUCCESS"
        or body.get("TransactionType") != "EscrowCreate"
        or body.get("Account") != owner
    ):
        return None
    seq_raw = body.get("Sequence") or body.get("TicketSequence")
    return seq_raw if isinstance(seq_raw, int) else None


async def _reconcile_escrow(
    conn: sqlite3.Connection, order: dict[str, Any], deps: ClosetMarketDeps
) -> tuple[str, str | None]:
    """(#443 final review C2) Before a pending bid is cancelled "unopened",
    look for its escrow on-ledger by Condition: a lost or lapsed signing
    status must not untrack a live escrow until CancelAfter.

    ("opened", fill_id | None) when a matching escrow was found and the bid
    opened; ("wait", None) when a lookup failed; ("absent", None) otherwise.

    (PR #502 G1) With user_lls set, "absent" additionally needs a validated
    ledger index — read BEFORE the lookup — past user_lls, and a lookup that
    proves it covers user_lls; otherwise ("wait", None)."""
    user_lls = order["user_lls"]
    if deps.find_escrow_by_condition_fn is None:
        return ("absent" if user_lls is None else "wait"), None
    if user_lls is not None:
        index = await deps.ledger_index_fn()
        if index is None or index <= user_lls:
            return "wait", None
    try:
        node = await deps.find_escrow_by_condition_fn(
            order["owner"], order["condition"], require_ledger=user_lls
        )
    except Exception:
        logging.warning(
            f"closet bid {order['id']}: escrow-by-condition lookup failed: {traceback.format_exc()}"
        )
        return "wait", None
    if (
        node is None
        or _escrow_mismatch(node, order, deps) is not None
        or node.get("CancelAfter") != order["cancel_after"]
        or node.get("FinishAfter") is not None
    ):
        return "absent", None
    # The escrow object doesn't carry its creating Sequence (the EscrowFinish /
    # EscrowCancel key); recover it from the EscrowCreate that created it.
    prev = node.get("PreviousTxnID")
    if not isinstance(prev, str) or not prev:
        return "absent", None
    try:
        tx = await deps.get_tx_fn(prev)
    except Exception:
        logging.warning(
            f"closet bid {order['id']}: escrow create lookup failed: {traceback.format_exc()}"
        )
        return "wait", None
    if not tx.get("validated"):
        return "wait", None
    seq = _escrow_create_sequence(tx, order["owner"])
    if seq is None:
        return "absent", None
    logging.warning(f"closet bid {order['id']}: escrow found on-ledger by condition (seq {seq})")
    cms.mark_bid_open(
        conn,
        order["id"],
        escrow_tx_hash=prev,
        escrow_owner_seq=seq,
    )
    _journal(deps, "order", cms.get_order(conn, order["id"]))
    fill = cms.cross_incoming(conn, order["id"], fee_bps=deps.fee_bps)
    return "opened", str(fill["id"]) if fill else None


async def _cancel_unopened_unless_found(
    conn: sqlite3.Connection, order: dict[str, Any], deps: ClosetMarketDeps, reason: str
) -> str | None:
    verdict, fill_id = await _reconcile_escrow(conn, order, deps)
    if verdict == "absent":
        _cancel_unopened(conn, order, deps, reason)
    return fill_id


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
                return await _cancel_unopened_unless_found(
                    conn, order, deps, "signing request expired"
                )
            return None
        if status.get("account") != order["owner"]:
            # An escrow from a different account never matches (Account check),
            # so this still cancels unless the owner's own escrow exists.
            return await _cancel_unopened_unless_found(
                conn, order, deps, "the bid was signed by a different account than yours"
            )
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
            return await _cancel_unopened_unless_found(
                conn, order, deps, "the escrow never validated"
            )
        return None
    result = (tx.get("meta") or {}).get("TransactionResult")
    if result != "tesSUCCESS":
        return await _cancel_unopened_unless_found(
            conn, order, deps, f"escrow create failed: {result}"
        )
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
        reason = f"escrow did not match the bid ({problem})"
        if order["user_lls"] is not None:
            # (PR #502 G1) "no longer on-ledger" may be a lagging node, and the
            # tracked tx is not the only way an escrow can land: gate the
            # cancel on the deadline and a covered by-condition lookup.
            return await _cancel_unopened_unless_found(conn, order, deps, reason)
        _cancel_unopened(conn, order, deps, reason)
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
        # Nothing reached the ledger — the write-ahead intent recorded by
        # _begin_order_phase describes a tx that was never sent, so clear it.
        cms.update_order(
            conn,
            order["id"],
            pending_phase=None,
            pending_lls=None,
            error=f"{phase} failed; will retry",
        )
        return "not_submitted"
    if out.state == "confirmed":
        fields: dict[str, Any] = {
            hash_field: out.tx_hash or cms.HASH_UNKNOWN,
            "pending_phase": None,
            "pending_lls": None,
            "error": None,
        }
        if complete:
            fields["state"] = _final_cancel_state(order)
        cms.update_order(conn, order["id"], **fields)
    elif out.state == "unknown":
        # The write-ahead intent already recorded this phase; keep it, but
        # trust the outcome's own last_ledger_seq (the tx that actually left,
        # bounded by max_last_ledger_seq) over our pre-submit estimate.
        cms.update_order(conn, order["id"], pending_phase=phase, pending_lls=out.last_ledger_seq)
    else:
        cms.update_order(
            conn,
            order["id"],
            pending_phase=None,
            pending_lls=None,
            error=f"{phase} failed; will retry",
        )
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
            lls = await _begin_order_phase(conn, order, "cancel", deps)
            if lls is None:
                return
            out = await _submit(
                deps.escrow_cancel_fn,
                order["owner"],
                seq,
                cms.memo_tag("cancel", oid),
                max_last_ledger_seq=lls,
            )
            _apply_order_outcome(
                conn, order, out, phase="cancel", hash_field="cancel_refund_hash", complete=True
            )
            _journal(deps, "order", cms.get_order(conn, oid))
            return
        fulfillment = deps.unseal_fn(order["fulfillment_enc"])  # before any intent is written
        lls = await _begin_order_phase(conn, order, "cancel_finish", deps)
        if lls is None:
            return
        out = await _submit(
            deps.escrow_finish_fn,
            order["owner"],
            seq,
            order["condition"],
            fulfillment,
            cms.memo_tag("cancel_finish", oid),
            max_last_ledger_seq=lls,
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
        lls = await _begin_order_phase(conn, order, "cancel_refund", deps)
        if lls is None:
            return
        out = await _submit(
            deps.payment_fn,
            order["owner"],
            order["price_brix"],
            cms.memo_tag("cancel_refund", oid),
            memos.ACTION_CLOSET_REFUND,
            max_last_ledger_seq=lls,
        )
        _apply_order_outcome(
            conn, order, out, phase="cancel_refund", hash_field="cancel_refund_hash", complete=True
        )
    _journal(deps, "order", cms.get_order(conn, oid))


# --- fills ----------------------------------------------------------------------


def _brix_value(amount: Any) -> Decimal | None:
    if not isinstance(amount, dict):
        return None
    if str(amount.get("currency", "")).upper() != str(config.BRIX_CURRENCY_HEX).upper():
        return None
    if amount.get("issuer") != config.BRIX_ISSUER:
        return None
    try:
        return Decimal(str(amount.get("value")))
    except (InvalidOperation, ValueError):
        return None


def _fail(conn: sqlite3.Connection, fill: dict[str, Any], reason: str) -> bool:
    cms.update_fill(conn, fill["id"], state=cms.FAILED, error=reason)
    return True


async def _begin_fill_phase(
    conn: sqlite3.Connection, fill: dict[str, Any], phase: str, deps: ClosetMarketDeps
) -> int | None:
    """Write-ahead intent for a fill, mirroring `_begin_order_phase`: persist
    pending_phase/pending_lls BEFORE the backend tx is submitted (without
    touching `state`), so a crash between the tx landing and this process
    recording the outcome still leaves a resolvable trail. Returns None
    (submit nothing) if the ledger index can't be read."""
    idx = await deps.ledger_index_fn()
    if idx is None:
        return None
    lls = idx + deps.ledger_margin
    cms.update_fill(conn, fill["id"], pending_phase=phase, pending_lls=lls)
    return lls


def _apply_fill_outcome(
    conn: sqlite3.Connection,
    fill: dict[str, Any],
    out: TxOutcome | None,
    *,
    phase: str,
    hash_field: str,
    success_state: str | None = None,
) -> str:
    if out is None:
        # Nothing reached the ledger — the write-ahead intent recorded by
        # _begin_fill_phase describes a tx that was never sent, so clear it.
        cms.update_fill(
            conn,
            fill["id"],
            pending_phase=None,
            pending_lls=None,
            error=f"{phase} failed; will retry",
        )
        return "not_submitted"
    if out.state == "confirmed":
        fields: dict[str, Any] = {
            hash_field: out.tx_hash or cms.HASH_UNKNOWN,
            "pending_phase": None,
            "pending_lls": None,
            "error": None,
        }
        if success_state is not None:
            fields["state"] = success_state
        cms.update_fill(conn, fill["id"], **fields)
    elif out.state == "unknown":
        # The write-ahead intent already recorded this phase; keep it, but
        # trust the outcome's own last_ledger_seq (the tx that actually left,
        # bounded by max_last_ledger_seq) over our pre-submit estimate.
        cms.update_fill(
            conn,
            fill["id"],
            state=cms.INDETERMINATE,
            pending_phase=phase,
            pending_lls=out.last_ledger_seq,
        )
    else:
        cms.update_fill(
            conn,
            fill["id"],
            pending_phase=None,
            pending_lls=None,
            attempts=int(fill["attempts"]) + 1,
            error=f"{phase} failed; will retry",
        )
    return out.state


async def _funds(conn: sqlite3.Connection, fill: dict[str, Any], deps: ClosetMarketDeps) -> bool:
    if fill["funds_source"] == cms.FUNDS_PAYMENT:
        return await _take_funds(conn, fill, deps)
    return await _escrow_funds(conn, fill, deps)


def _pays_fill_in_full(entry: dict[str, Any], fill: dict[str, Any]) -> bool:
    """The account_tx entry is the buyer's own payment delivering at least the
    price in BRIX."""
    tx = entry.get("tx") or entry.get("tx_json") or {}
    meta = entry.get("meta") or entry.get("metaData") or {}
    delivered = _brix_value(meta.get("delivered_amount") if isinstance(meta, dict) else None)
    return (
        tx.get("Account") == fill["buyer"]
        and delivered is not None
        and delivered >= Decimal(fill["price_brix"])
    )


async def _reconcile_invoice(
    conn: sqlite3.Connection,
    fill: dict[str, Any],
    deps: ClosetMarketDeps,
    *,
    exclude_hash: str | None = None,
) -> str:
    """(#443 final review C2) Before a take fill is failed with no verified
    funds, look for its Payment on-ledger by InvoiceID — a Joey sign row
    lapses to "expired" even when the client-submitted tx validated.

    "found": a validated tesSUCCESS payment was found under a hash we weren't
    tracking; signed_txid now points at it and the next pass settles (claim or
    refund). "wait": a lookup failed, or it is the tx we already track and the
    tx server has not caught up. "absent": nothing — the failure stands.

    (PR #502 G1) With user_lls set, "absent" needs the validated index (read
    before the lookup) past user_lls AND a lookup covering user_lls; until
    then this waits. `exclude_hash` is a tracked tx already known to be
    validated and unusable, so finding it again proves nothing."""
    user_lls = fill["user_lls"]
    if deps.find_invoice_payments_fn is None:
        return "absent" if user_lls is None else "wait"
    idx = await deps.ledger_index_fn()
    if idx is None:
        return "wait"
    if user_lls is not None and idx <= user_lls:
        return "wait"
    elapsed = max(0.0, deps.now_fn() - fill["created_ts"])
    min_ledger = max(1, idx - int(elapsed / 3) - 1000)
    try:
        entries = await deps.find_invoice_payments_fn(
            cms.invoice_id(fill["id"]), min_ledger, require_ledger=user_lls
        )
    except Exception:
        logging.warning(
            f"closet fill {fill['id']}: invoice payment lookup failed: {traceback.format_exc()}"
        )
        return "wait"
    candidates = [
        e
        for e in entries
        if e.get("validated")
        and tx_entry_result(e) == "tesSUCCESS"
        and (exclude_hash is None or tx_entry_hash(e) != exclude_hash)
    ]
    if not candidates:
        return "absent"
    # (PR #502 G2) InvoiceID is public: a junk Payment (wrong sender, short)
    # listed first must not mask the buyer's real one. Prefer the buyer's
    # full payment; only if there is none adopt the first entry, which then
    # takes the wrong-sender / partial refund path.
    entry = next((e for e in candidates if _pays_fill_in_full(e, fill)), candidates[0])
    tx_hash = tx_entry_hash(entry)
    if not tx_hash:
        return "wait"
    if tx_hash == fill["signed_txid"]:
        return "wait"
    logging.warning(f"closet fill {fill['id']}: payment {tx_hash} found on-ledger by InvoiceID")
    cms.update_fill(conn, fill["id"], signed_txid=tx_hash)
    return "found"


async def _fail_unless_paid(
    conn: sqlite3.Connection,
    fill: dict[str, Any],
    deps: ClosetMarketDeps,
    reason: str,
    *,
    tracked_validated: bool = False,
) -> bool:
    """Fail the fill only once no payment can still arrive. `tracked_validated`
    marks a failure decided from the tracked tx having validated unusable
    (tec, wrong shape): pre-G1 rows (user_lls NULL) fail at once as before;
    with user_lls set the same deadline + covered-lookup gate applies, and
    the tracked hash itself is ignored by the lookup (PR #502 G1)."""
    if tracked_validated and fill["user_lls"] is None:
        return _fail(conn, fill, reason)
    verdict = await _reconcile_invoice(
        conn, fill, deps, exclude_hash=fill["signed_txid"] if tracked_validated else None
    )
    if verdict == "found":
        return True
    if verdict == "wait":
        return False
    return _fail(conn, fill, reason)


PAYMENT_GIVEUP_SECONDS = 86400  # 24h backstop for a take fill with no path to a terminal state:
# Xaman/Joey autofill LastLedgerSequence on every payload, so a signed tx that's still
# unfindable/unvalidated a full day later cannot go on to validate later either.


async def _take_funds(
    conn: sqlite3.Connection, fill: dict[str, Any], deps: ClosetMarketDeps
) -> bool:
    """(#443 review fix, tightened in round 3) A `None` read — a transient
    XUMM API error, a 429 with an empty cache, a lagging/overloaded tx-lookup
    server — is NEVER treated as failure, at any age. The only paths to
    failure before a definitive success/failure tx is found:
      - pre-sign: the status read is NOT None and is either explicitly
        `expired`, or `signed` with no `txid` yet, for longer than
        PAYMENT_GIVEUP_SECONDS;
      - post-sign, LLS-decidable: the validated ledger has passed the signed
        tx's own `LastLedgerSequence` while it's still unvalidated —
        re-checked once against a fresh lookup first, to rule out a
        landing-in-the-gap race (the same hazard `_phase_outcome` had);
      - post-sign, not LLS-decidable: BOTH the initial lookup and one
        re-fetch come back with `error == "txnNotFound"`, for longer than
        PAYMENT_GIVEUP_SECONDS. Any other error body (`tooBusy`, `slowDown`,
        ...), a raised exception, or simply an unvalidated tx with no
        deadline to judge — all of these wait, however long it's been.
    `signing_wait_seconds` plays no role here (a different, much shorter,
    arbitrary wall-clock budget).
    Each of those failures first looks for the Payment on-ledger by
    InvoiceID (`_fail_unless_paid`, final review C2): a found payment is
    adopted and settled; a failed lookup waits."""
    if not fill["signed_txid"]:
        status = (
            await deps.payload_status_fn(fill["payload_uuid"]) if fill["payload_uuid"] else None
        )
        if status is None:
            return False  # never fails on a None read, no matter how old
        if not status.get("signed"):
            if status.get("expired"):
                return await _fail_unless_paid(conn, fill, deps, "payment request expired")
            if deps.now_fn() - fill["created_ts"] > PAYMENT_GIVEUP_SECONDS:
                return await _fail_unless_paid(
                    conn, fill, deps, "no payment arrived within 24 hours"
                )
            return False
        txid = status.get("txid")
        if not txid:
            if deps.now_fn() - fill["created_ts"] > PAYMENT_GIVEUP_SECONDS:
                return await _fail_unless_paid(
                    conn, fill, deps, "no payment arrived within 24 hours"
                )
            return False
        cms.update_fill(conn, fill["id"], signed_txid=txid)
        return True
    # Read the ledger index BEFORE the tx lookup (#443 review fix round 2):
    # reading it after let a tx that validates in the gap between the two
    # reads look "never validated" and fail a fill whose BRIX had already
    # arrived — the same race `_phase_outcome` had.
    index = await deps.ledger_index_fn()
    try:
        tx = await deps.get_tx_fn(fill["signed_txid"])
    except Exception:
        logging.warning(
            f"closet fill {fill['id']}: payment lookup failed: {traceback.format_exc()}"
        )
        return False
    body = _tx_body(tx)
    if not tx.get("validated"):
        last_ledger_seq = body.get("LastLedgerSequence")
        if isinstance(last_ledger_seq, int) and index is not None and index > last_ledger_seq:
            # `index` was read before this lookup, so the tx could have
            # validated in the gap between the two — re-check once before
            # trusting "failed".
            try:
                recheck = await deps.get_tx_fn(fill["signed_txid"])
            except Exception:
                logging.warning(
                    f"closet fill {fill['id']}: payment re-check failed: {traceback.format_exc()}"
                )
                return False
            if not recheck.get("validated"):
                return await _fail_unless_paid(conn, fill, deps, "the payment never validated")
            return False  # validated after all — the next pass processes it normally
        # No LastLedgerSequence to judge absence against (a genuine
        # not-found, or a bare rippled error body like tooBusy/slowDown).
        # The ONLY way this ever fails is a confirmed txnNotFound on BOTH
        # this read and one fresh re-fetch, and only past the giveup — any
        # other error, or simply not knowing, waits indefinitely.
        if (
            tx.get("error") == "txnNotFound"
            and deps.now_fn() - fill["created_ts"] > PAYMENT_GIVEUP_SECONDS
        ):
            try:
                recheck = await deps.get_tx_fn(fill["signed_txid"])
            except Exception:
                logging.warning(
                    f"closet fill {fill['id']}: payment giveup re-check failed: {traceback.format_exc()}"
                )
                return False
            if recheck.get("error") == "txnNotFound":
                return await _fail_unless_paid(
                    conn, fill, deps, "no payment arrived within 24 hours"
                )
        return False
    meta = tx.get("meta") or {}
    if meta.get("TransactionResult") != "tesSUCCESS":
        return await _fail_unless_paid(
            conn,
            fill,
            deps,
            f"payment failed: {meta.get('TransactionResult')}",
            tracked_validated=True,
        )
    delivered = _brix_value(meta.get("delivered_amount"))
    tx_hash = str(tx.get("hash") or body.get("hash") or fill["signed_txid"])
    sender = body.get("Account")
    not_this_payment = (
        body.get("TransactionType") != "Payment"
        or body.get("Destination") != deps.app_account
        or str(body.get("InvoiceID", "")).upper() != cms.invoice_id(fill["id"])
    )
    if not_this_payment:
        logging.error(
            f"closet fill {fill['id']}: signed tx {fill['signed_txid']} is not this fill's payment"
        )
        # (#443 final review I4) BRIX that did arrive at the app wallet is
        # returned to its sender rather than kept with the fill failed.
        if not (
            body.get("Destination") == deps.app_account
            and delivered is not None
            and delivered > 0
            and isinstance(sender, str)
        ):
            return await _fail_unless_paid(
                conn,
                fill,
                deps,
                "the signed transaction is not this purchase's payment",
                tracked_validated=True,
            )
    elif delivered is None or delivered <= 0:
        return await _fail_unless_paid(
            conn, fill, deps, "the payment delivered no BRIX", tracked_validated=True
        )
    assert delivered is not None
    try:
        if not_this_payment or sender != fill["buyer"] or delivered < Decimal(fill["price_brix"]):
            if not_this_payment:
                reason = "the signed transaction is not this purchase's payment"
            elif sender != fill["buyer"]:
                reason = "payment signed by a different account"
            else:
                reason = "partial payment"
            cms.update_fill(
                conn,
                fill["id"],
                state=cms.REFUND_PENDING,
                payment_tx_hash=tx_hash,
                refund_to=sender,
                refund_brix=cms.fmt_brix(delivered),
                error=reason,
            )
            return True
        cms.claim_ask_for_payment(conn, fill["id"], tx_hash)
    except sqlite3.IntegrityError:
        return await _fail_unless_paid(
            conn,
            fill,
            deps,
            "this payment was already used for another purchase",
            tracked_validated=True,
        )
    return True


FINISH_GUARD_SECONDS = 300  # skip a doomed EscrowFinish this close to (or past) CancelAfter


def _ripple_now(deps: ClosetMarketDeps) -> int:
    return int(deps.now_fn()) - RIPPLE_EPOCH_OFFSET


def _expire_unfinishable_bid(conn: sqlite3.Connection, fill: dict[str, Any]) -> None:
    """The bid can no longer be finished (we're inside the guard window, or a
    finish just failed while its CancelAfter has already passed). Route it to
    cancelling — the bid machine's own EscrowCancel/EscrowFinish+refund path
    returns the BRIX and finalizes it EXPIRED — rather than looping forever on
    a finish the ledger will keep rejecting (tec*). One atomic update sets
    both the bid's state and its cancel_reason."""
    cms.abort_unfunded_fill(
        conn,
        fill["id"],
        "the bid expired before it could be filled",
        bid_state=cms.CANCELLING,
        cancel_reason="expired",
    )


async def _escrow_funds(
    conn: sqlite3.Connection, fill: dict[str, Any], deps: ClosetMarketDeps
) -> bool:
    blocker = cms.fill_blocker(conn, fill["id"])
    if blocker is not None:
        cms.abort_unfunded_fill(conn, fill["id"], blocker)
        return True
    bid = cms.get_order(conn, fill["bid_order_id"])
    assert bid is not None and bid["escrow_owner_seq"] is not None
    if (
        bid["cancel_after"] is not None
        and _ripple_now(deps) >= bid["cancel_after"] - FINISH_GUARD_SECONDS
    ):
        # Past CancelAfter (minus a safety margin) the ledger rejects
        # EscrowFinish outright (tec*), yet the escrow node stays on-ledger —
        # left unhandled, this step returns False forever, encumbering the
        # seller's unit and leaving the bid stuck `matched` (a state the
        # expiry sweep's orders_needing_attention never looks at).
        _expire_unfinishable_bid(conn, fill)
        return True
    fulfillment = deps.unseal_fn(bid["fulfillment_enc"])  # before any intent is written
    lls = await _begin_fill_phase(conn, fill, "finish", deps)
    if lls is None:
        return False
    out = await _submit(
        deps.escrow_finish_fn,
        bid["owner"],
        bid["escrow_owner_seq"],
        bid["condition"],
        fulfillment,
        cms.memo_tag("finish", fill["id"]),
        max_last_ledger_seq=lls,
    )
    verdict = _apply_fill_outcome(
        conn, fill, out, phase="finish", hash_field="escrow_finish_hash", success_state=cms.FUNDED
    )
    if verdict in ("confirmed", "unknown"):
        return True
    if verdict == "failed":
        try:
            node = await deps.get_escrow_fn(bid["owner"], bid["escrow_owner_seq"])
        except Exception:
            return False
        if node is None:
            cms.abort_unfunded_fill(
                conn,
                fill["id"],
                "the bid's escrow is gone (expired or cancelled)",
                bid_state=cms.EXPIRED,
            )
            return True
        if bid["cancel_after"] is not None and _ripple_now(deps) >= bid["cancel_after"]:
            _expire_unfinishable_bid(conn, fill)
            return True
    return False


async def _move(conn: sqlite3.Connection, fill: dict[str, Any], deps: ClosetMarketDeps) -> bool:
    if fill["seller"] == fill["buyer"]:
        raise RuntimeError(f"closet fill {fill['id']} has seller == buyer")
    first, second = sorted((fill["seller"], fill["buyer"]))
    async with owner_lock.owner_lock(first), owner_lock.owner_lock(second):
        result = cms.move_asset(conn, fill["id"])
    return result in (cms.ASSET_MOVED, cms.REFUND_PENDING)


async def _pay(conn: sqlite3.Connection, fill: dict[str, Any], deps: ClosetMarketDeps) -> bool:
    fid = fill["id"]
    changed = False
    if fill["forward_tx_hash"] is None:
        net = Decimal(fill["price_brix"]) - Decimal(fill["fee_brix"])
        if net <= 0:
            cms.update_fill(conn, fid, forward_tx_hash=cms.HASH_NOTHING_DUE)
        else:
            lls = await _begin_fill_phase(conn, fill, "forward", deps)
            if lls is None:
                return False
            out = await _submit(
                deps.payment_fn,
                fill["seller"],
                cms.fmt_brix(net),
                cms.memo_tag("forward", fid),
                memos.ACTION_CLOSET_FORWARD,
                max_last_ledger_seq=lls,
            )
            verdict = _apply_fill_outcome(
                conn, fill, out, phase="forward", hash_field="forward_tx_hash"
            )
            if verdict != "confirmed":
                return verdict == "unknown"
        changed = True
        fill = cms.get_fill(conn, fid) or fill
    if Decimal(fill["overshoot_brix"]) > 0 and fill["overshoot_tx_hash"] is None:
        lls = await _begin_fill_phase(conn, fill, "overshoot", deps)
        if lls is None:
            return changed
        out = await _submit(
            deps.payment_fn,
            fill["buyer"],
            fill["overshoot_brix"],
            cms.memo_tag("overshoot", fid),
            memos.ACTION_CLOSET_REFUND,
            max_last_ledger_seq=lls,
        )
        verdict = _apply_fill_outcome(
            conn, fill, out, phase="overshoot", hash_field="overshoot_tx_hash"
        )
        if verdict != "confirmed":
            return changed or verdict == "unknown"
    cms.update_fill(conn, fid, state=cms.PAID, error=None)
    return True


async def _mirror(conn: sqlite3.Connection, fill: dict[str, Any], deps: ClosetMarketDeps) -> bool:
    """Re-sync each side's Closet token from the (authoritative) DB. A full
    overwrite from current DB state is idempotent, so every error — including
    an indeterminate modify — is simply retried."""
    changed = False
    for side in ("seller", "buyer"):
        if fill[f"{side}_mirrored"]:
            continue
        owner = fill[side]
        try:
            async with owner_lock.owner_lock(owner):
                await deps.mirror_fn(conn, owner)
        except closet_token.ClosetMirrorError:
            pass  # the modify committed; only the token-record mirror write failed
        except Exception:
            logging.warning(
                f"closet fill {fill['id']}: mirror for {owner} failed: {traceback.format_exc()}"
            )
            cms.update_fill(
                conn,
                fill["id"],
                attempts=int(fill["attempts"]) + 1,
                error=f"Closet update for {owner} failed; will retry",
            )
            continue
        cms.mark_side_mirrored(conn, fill["id"], side)
        changed = True
    return changed


async def _refund(conn: sqlite3.Connection, fill: dict[str, Any], deps: ClosetMarketDeps) -> bool:
    to = fill["refund_to"] or fill["buyer"]
    value = fill["refund_brix"] or cms.fill_in_amount(fill)
    if Decimal(value) <= 0:
        cms.update_fill(conn, fill["id"], state=cms.REFUNDED, refund_tx_hash=cms.HASH_NOTHING_DUE)
        return True
    lls = await _begin_fill_phase(conn, fill, "refund", deps)
    if lls is None:
        return False
    out = await _submit(
        deps.payment_fn,
        to,
        value,
        cms.memo_tag("refund", fill["id"]),
        memos.ACTION_CLOSET_REFUND,
        max_last_ledger_seq=lls,
    )
    verdict = _apply_fill_outcome(
        conn, fill, out, phase="refund", hash_field="refund_tx_hash", success_state=cms.REFUNDED
    )
    return verdict in ("confirmed", "unknown")


_PHASE_ROLLBACK = {
    "finish": cms.FUNDS_PENDING,
    "forward": cms.ASSET_MOVED,
    "overshoot": cms.ASSET_MOVED,
    "refund": cms.REFUND_PENDING,
}


async def _resolve_fill_phase(
    conn: sqlite3.Connection, fill: dict[str, Any], deps: ClosetMarketDeps
) -> bool:
    phase = fill["pending_phase"]
    if phase not in _PHASE_ROLLBACK:
        logging.error(f"closet fill {fill['id']}: indeterminate with unknown phase {phase!r}")
        return False
    verdict, tx_hash = await _phase_outcome(
        deps, cms.memo_tag(phase, fill["id"]), fill["pending_lls"]
    )
    if verdict == "wait":
        return False
    fields: dict[str, Any] = {
        "state": _PHASE_ROLLBACK[phase],
        "pending_phase": None,
        "pending_lls": None,
    }
    if verdict == "landed":
        if phase == "finish":
            fields.update(state=cms.FUNDED, escrow_finish_hash=tx_hash)
        elif phase == "refund":
            fields.update(state=cms.REFUNDED, refund_tx_hash=tx_hash)
        else:
            fields[f"{phase}_tx_hash"] = tx_hash
    cms.update_fill(conn, fill["id"], **fields)
    return True


_STEPS: dict[
    str, Callable[[sqlite3.Connection, dict[str, Any], ClosetMarketDeps], Awaitable[bool]]
] = {
    cms.FUNDS_PENDING: _funds,
    cms.FUNDED: _move,
    cms.ASSET_MOVED: _pay,
    cms.PAID: _mirror,
    cms.REFUND_PENDING: _refund,
    cms.INDETERMINATE: _resolve_fill_phase,
}


async def settle_fill(fill_id: str, deps: ClosetMarketDeps) -> str | None:
    """Advance one fill as far as it can go right now. Serialized per fill
    (inline kick + sweep may race); returns the state it stopped at."""
    async with owner_lock.owner_lock(f"closet-fill:{fill_id}"):
        state: str | None = None
        for _ in range(MAX_STEPS):
            conn = deps.conn_factory()
            try:
                current = cms.get_fill(conn, fill_id)
                if current is None:
                    return None
                state = str(current["state"])
                if state in cms.TERMINAL_FILL_STATES:
                    return state
                if current["pending_phase"]:
                    # A crash can leave a write-ahead intent recorded for a
                    # phase whose backend tx (or lack thereof) predates the
                    # fill's current `state` transition — that intent must be
                    # resolved BEFORE dispatching on `state`, or a retry would
                    # resubmit a tx that may already be on-ledger.
                    progressed = await _resolve_fill_phase(conn, current, deps)
                else:
                    progressed = await _STEPS[state](conn, current, deps)
                after = cms.get_fill(conn, fill_id)
                assert after is not None
                if progressed:
                    _journal(deps, "fill", after)
                state = str(after["state"])
                if not progressed or state in cms.TERMINAL_FILL_STATES:
                    return state
            finally:
                conn.close()
        return state
