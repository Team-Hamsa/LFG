"""Fee-cover settlement: resolve a promise's accept from the history archive,
pay the refund, and recover indeterminate payouts. All I/O is injected via
FeeCoverDeps so every path is testable without a ledger.

Spec: docs/superpowers/specs/2026-09-14-marketplace-fee-cover-design.md
(§Durability, §Fill detection and promise release).

sqlite connections are opened inside the worker thread that uses them
(asyncio.to_thread), never shared across threads.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from lfg_core import brokers, config, db_path, fee_cover, fee_cover_store, history_store, xrpl_ops

RIPPLE_EPOCH_OFFSET = 946_684_800
# A promise is written when its bid validates; its accept cannot predate that,
# but clocks differ between the service and ledger close times — allow slack.
ACCEPT_LOOKBACK_SECONDS = 3600
# The listener flushes the archive's validated cursor ahead of committing the
# derived nft_events sale row; allow margin so an accept straddling the expiry
# is not released before it's fully indexed (seconds of listener lag).
EXPIRY_RELEASE_MARGIN_SECONDS = 600


def _unix_now() -> int:
    return int(time.time())


@dataclass
class FeeCoverDeps:
    network: str
    app_db_path: str
    history_db_path: str
    payer: str
    ledger_margin: int
    get_tx: Callable[[str], Awaitable[dict[str, Any]]]
    send_refund: Callable[..., Awaitable[xrpl_ops.ClaimPayment]]
    find_refund_payment: Callable[..., Awaitable[str | None]]
    current_ledger: Callable[[], Awaitable[int | None]]
    broker_rate_for: Callable[[str, str], float | None]
    linked: Callable[[str, str], bool]
    now: Callable[[], int] = _unix_now


@dataclass
class SweepReport:
    settled: int = 0
    released: int = 0
    paid: int = 0
    recovered: int = 0


# --- archive helpers (sync, caller-owned connection) --------------------------


def normalize_tx(result: dict[str, Any]) -> dict[str, Any]:
    """Flatten a `tx` result to the archive shape: API v2 nests the tx fields
    under tx_json; v1 and the history archive keep them top-level."""
    inner = result.get("tx_json")
    if not isinstance(inner, dict):
        return dict(result)
    flat = dict(inner)
    for key, value in result.items():
        if key != "tx_json":
            flat[key] = value
    return flat


def find_accept_tx(
    hconn: sqlite3.Connection, nft_id: str, offer_index: str, since_unix: int
) -> dict[str, Any] | None:
    row = hconn.execute(
        "SELECT x.raw_json FROM nft_events e JOIN xrpl_txs x ON x.tx_hash = e.tx_hash"
        " WHERE e.nft_id = ? AND e.event = 'sale' AND e.ts >= ?"
        " AND json_extract(x.raw_json, '$.NFTokenBuyOffer') = ? LIMIT 1",
        (nft_id, since_unix, offer_index),
    ).fetchone()
    if row is None:
        return None
    tx = json.loads(row[0])
    return tx if isinstance(tx, dict) else None


def cancel_seen(hconn: sqlite3.Connection, nft_id: str, offer_index: str) -> bool:
    """Cancel events group every offer closed for a token into one
    comma-joined offer_index (history_events, #411)."""
    for (joined,) in hconn.execute(
        "SELECT offer_index FROM nft_events WHERE nft_id = ? AND event = 'offer_cancel'"
        " AND offer_index IS NOT NULL",
        (nft_id,),
    ):
        if offer_index in str(joined).split(","):
            return True
    return False


def archive_blocker(hconn: sqlite3.Connection, network: str, unix_ts: int) -> str | None:
    """Why this archive cannot prove what happened up to `unix_ts`, or None.
    Same provenance test as epoch_state.certify_epoch."""
    state = history_store.get_archive_state(hconn, network)
    if state is None:
        return "no archive_state row"
    if not state.baseline_complete:
        return "baseline not complete"
    if (
        state.continuity_gap_at is not None
        or state.continuity_gap_after is not None
        or state.continuity_gap_before is not None
        or state.continuity_gap_reason is not None
    ):
        return "continuity gap recorded"
    if state.validated_close_time is None or state.validated_close_time <= unix_ts:
        return "archive not yet validated past the expiry"
    return None


# --- sync units run in worker threads -------------------------------------------


def _open_promise_and_accept(
    deps: FeeCoverDeps, offer_index: str
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None]:
    conn = fee_cover_store.connect(deps.app_db_path)
    try:
        promise = fee_cover_store.get_promise(conn, offer_index)
        view = fee_cover_store.view_for_offer(conn, offer_index)
    finally:
        conn.close()
    if promise is None or promise["state"] != "open":
        return promise, None, view
    hconn = history_store.init_history_db(deps.history_db_path)
    try:
        accept = find_accept_tx(
            hconn,
            str(promise["nft_id"]),
            offer_index,
            int(promise["created_at"]) - ACCEPT_LOOKBACK_SECONDS,
        )
    finally:
        hconn.close()
    return promise, accept, view


def _decide_and_fill(
    deps: FeeCoverDeps, promise: dict[str, Any], validated: dict[str, Any]
) -> dict[str, Any] | None:
    nft_id = str(promise["nft_id"])
    decision = fee_cover.compute_refund(
        validated,
        promise=promise,
        payer=deps.payer,
        broker_rate_for=lambda account: deps.broker_rate_for(account, nft_id),
        linked=deps.linked,
    )
    conn = fee_cover_store.connect(deps.app_db_path)
    try:
        fee_cover_store.fill_promise(conn, str(promise["offer_index"]), decision, now=deps.now())
        return fee_cover_store.view_for_offer(conn, str(promise["offer_index"]))
    finally:
        conn.close()


def _refund_row(deps: FeeCoverDeps, key: str) -> dict[str, Any] | None:
    conn = fee_cover_store.connect(deps.app_db_path)
    try:
        return fee_cover_store.get_refund(conn, key)
    finally:
        conn.close()


def _rows(deps: FeeCoverDeps, state: str) -> list[dict[str, Any]]:
    conn = fee_cover_store.connect(deps.app_db_path)
    try:
        return fee_cover_store.refunds_in_state(conn, deps.network, state)
    finally:
        conn.close()


def _open_promises(deps: FeeCoverDeps) -> list[dict[str, Any]]:
    conn = fee_cover_store.connect(deps.app_db_path)
    try:
        return fee_cover_store.open_promises(conn, deps.network)
    finally:
        conn.close()


def _claim(deps: FeeCoverDeps, key: str, last_ledger_seq: int) -> bool:
    conn = fee_cover_store.connect(deps.app_db_path)
    try:
        return fee_cover_store.claim_for_payout(conn, key, last_ledger_seq, now=deps.now())
    finally:
        conn.close()


def _record(
    deps: FeeCoverDeps, key: str, state: str, tx_hash: str | None, last_ledger_seq: int | None
) -> None:
    conn = fee_cover_store.connect(deps.app_db_path)
    try:
        fee_cover_store.record_payout(
            conn, key, state=state, tx_hash=tx_hash, last_ledger_seq=last_ledger_seq, now=deps.now()
        )
    finally:
        conn.close()


def _unclaim(deps: FeeCoverDeps, key: str) -> None:
    conn = fee_cover_store.connect(deps.app_db_path)
    try:
        fee_cover_store.return_to_owed(conn, key, now=deps.now())
    finally:
        conn.close()


def _release_reason(deps: FeeCoverDeps, promise: dict[str, Any]) -> str | None:
    """'cancelled' / 'expired' when the archive proves the bid can never fill;
    None (leave open) otherwise — fail closed."""
    nft_id = str(promise["nft_id"])
    offer_index = str(promise["offer_index"])
    hconn = history_store.init_history_db(deps.history_db_path)
    try:
        since = int(promise["created_at"]) - ACCEPT_LOOKBACK_SECONDS
        # Check if the bid was accepted (filled): settle_promise owns it.
        if find_accept_tx(hconn, nft_id, offer_index, since) is not None:
            return None
        # Check if the bid was cancelled.
        if cancel_seen(hconn, nft_id, offer_index):
            return "cancelled"
        # Check expiration: if no expiration set, keep the promise open.
        expiration = promise["bid_expiration"]
        if expiration is None:
            return None
        expiry_unix = int(expiration) + RIPPLE_EPOCH_OFFSET
        # Bid is not expired yet.
        if deps.now() <= expiry_unix:
            return None
        # Bid is expired. Check archive state before releasing: the listener
        # may still be indexing the accept that validates at or near the expiry.
        # Only release if the archive is certified past expiry + margin.
        if (
            archive_blocker(hconn, deps.network, expiry_unix + EXPIRY_RELEASE_MARGIN_SECONDS)
            is not None
        ):
            return None
        # Final check: archive is past expiry+margin; if the accept now appears,
        # settle_promise will own it (e.g. listener just indexed it).
        if find_accept_tx(hconn, nft_id, offer_index, since) is not None:
            return None
        return "expired"
    finally:
        hconn.close()


def _release(deps: FeeCoverDeps, offer_index: str, reason: str) -> bool:
    conn = fee_cover_store.connect(deps.app_db_path)
    try:
        return fee_cover_store.release_promise(conn, offer_index, reason, now=deps.now())
    finally:
        conn.close()


# --- async orchestration ---------------------------------------------------------


async def settle_promise(deps: FeeCoverDeps, offer_index: str) -> dict[str, Any] | None:
    """Resolve one open promise against its archived accept; returns the
    fee-cover view. Leaves the promise open on anything unproven."""
    promise, accept, view = await asyncio.to_thread(_open_promise_and_accept, deps, offer_index)
    if promise is None or promise["state"] != "open" or accept is None:
        return view
    tx_hash = accept.get("hash")
    if not isinstance(tx_hash, str) or not tx_hash:
        return view
    try:
        result = await deps.get_tx(tx_hash)
    except Exception:
        logging.warning(
            "fee cover: get_tx(%s) failed; promise %s stays open",
            tx_hash,
            offer_index,
            exc_info=True,
        )
        return view
    if not isinstance(result, dict) or result.get("validated") is not True:
        return view
    validated = normalize_tx(result)
    validated["hash"] = validated.get("hash") or tx_hash
    return await asyncio.to_thread(_decide_and_fill, deps, promise, validated)


async def pay_refund(deps: FeeCoverDeps, accept_tx_hash: str) -> str:
    row = await asyncio.to_thread(_refund_row, deps, accept_tx_hash)
    if row is None:
        return "missing"
    if row["state"] != "owed":
        return str(row["state"])
    current = await deps.current_ledger()
    if current is None:
        return "owed"
    provisional = current + deps.ledger_margin * 10
    if not await asyncio.to_thread(_claim, deps, accept_tx_hash, provisional):
        latest = await asyncio.to_thread(_refund_row, deps, accept_tx_hash)
        return str(latest["state"]) if latest else "missing"
    try:
        payment = await deps.send_refund(
            str(row["bidder"]),
            int(row["refund_drops"]),
            accept_tx_hash,
            campaign_id=int(row["campaign_id"]),
            max_last_ledger_seq=provisional,
        )
    except xrpl_ops.ClaimNotSubmitted as exc:
        logging.warning("fee cover refund %s not submitted: %s", accept_tx_hash, exc)
        await asyncio.to_thread(_unclaim, deps, accept_tx_hash)
        return "owed"
    except asyncio.CancelledError:
        raise
    except Exception:
        # The provisional deadline is already recorded, so recovery can decide.
        logging.exception(
            "fee cover refund %s raised; leaving it submitted for recovery", accept_tx_hash
        )
        return "submitted"
    state = "submitted" if payment.state == "unknown" else payment.state
    await asyncio.to_thread(
        _record, deps, accept_tx_hash, state, payment.tx_hash, payment.last_ledger_seq
    )
    return state


async def recover_refunds(deps: FeeCoverDeps) -> dict[str, str]:
    """confirmed when the memo-tagged refund is on-ledger; failed only when it
    is absent AND the validated ledger has passed the row's deadline."""
    rows = await asyncio.to_thread(_rows, deps, "submitted")
    if not rows:
        return {}
    validated = await deps.current_ledger()
    if validated is None:
        return {}
    outcomes: dict[str, str] = {}
    for row in rows:
        key = str(row["accept_tx_hash"])
        last_ledger_seq = row["last_ledger_seq"]
        try:
            found = await deps.find_refund_payment(
                key,
                destination=str(row["bidder"]),
                drops=int(row["refund_drops"]),
                min_ledger=last_ledger_seq,
            )
        except Exception:
            logging.warning(
                "fee cover recovery: account_tx lookup failed for %s", key, exc_info=True
            )
            continue
        if found:
            await asyncio.to_thread(_record, deps, key, "confirmed", found, None)
            outcomes[key] = "confirmed"
            continue
        if last_ledger_seq is None or validated <= int(last_ledger_seq):
            continue
        await asyncio.to_thread(_record, deps, key, "failed", None, None)
        outcomes[key] = "failed"
    return outcomes


async def sweep_once(
    deps: FeeCoverDeps,
    *,
    attempts: dict[str, int],
    max_attempts: int,
    on_giveup: Callable[[dict[str, Any]], None],
) -> SweepReport:
    report = SweepReport()
    for promise in await asyncio.to_thread(_open_promises, deps):
        offer_index = str(promise["offer_index"])
        view = await settle_promise(deps, offer_index)
        if view is not None and view["state"] != "open":
            report.settled += 1
            continue
        reason = await asyncio.to_thread(_release_reason, deps, promise)
        if reason is not None and await asyncio.to_thread(_release, deps, offer_index, reason):
            report.released += 1
    for row in await asyncio.to_thread(_rows, deps, "owed"):
        key = str(row["accept_tx_hash"])
        if attempts.get(key, 0) >= max_attempts:
            continue
        state = await pay_refund(deps, key)
        if state == "owed":
            attempts[key] = attempts.get(key, 0) + 1
            if attempts[key] >= max_attempts:
                on_giveup(row)
        else:
            attempts.pop(key, None)
            if state in ("confirmed", "submitted", "failed"):
                report.paid += 1
    report.recovered = len(await recover_refunds(deps))
    return report


def service_deps(network: str, *, linked: Callable[[str, str], bool]) -> FeeCoverDeps:
    """The production wiring: real ledger calls, the network's app DB and
    history archive, refunds signed by config.SIGNING_ACCOUNT."""
    return FeeCoverDeps(
        network=network,
        app_db_path=db_path.app_db_path(network),
        history_db_path=history_store.history_db_path(network),
        payer=config.SIGNING_ACCOUNT,
        ledger_margin=config.FEE_COVER_LEDGER_MARGIN,
        get_tx=xrpl_ops.get_tx,
        send_refund=xrpl_ops.send_fee_cover_refund,
        find_refund_payment=xrpl_ops.find_fee_cover_payment,
        current_ledger=xrpl_ops.current_validated_ledger_index,
        broker_rate_for=lambda account, nft_id: (brokers.resolve(account, nft_id) or {}).get(
            "broker_rate"
        ),
        linked=linked,
    )
