"""Durable worker for project-funded LFGO burns owed by sponsored mints."""

from __future__ import annotations

import asyncio
import inspect
import logging
import sqlite3
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Any, TypeGuard, TypeVar
from uuid import uuid4

from lfg_core import config, sponsored_mint, xrpl_ops

_T = TypeVar("_T")


LEASE_SECONDS = 60
BASE_BACKOFF_SECONDS = 5
MAX_BACKOFF_SECONDS = 3600
POLL_SECONDS = 1.0


@dataclass(frozen=True)
class _LeasedBurn:
    id: str
    previous_status: str
    memo_id: str
    amount: str
    source_account: str
    network: str
    claim_network: str
    issuer: str
    currency: str
    source_tag: int
    attempt_count: int
    lease_token: str
    signed_tx_hash: str | None
    signed_tx_blob: str | None
    signed_ledger_floor: int | None


Submit = Callable[..., Awaitable[xrpl_ops.BurnSubmission]]
Prepare = Callable[..., Awaitable[xrpl_ops.BurnPreparation]]
Reconcile = Callable[..., Awaitable[xrpl_ops.BurnReconciliation]]
IdentityExpired = Callable[[str], Awaitable[int | None]]


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _timestamp(now: int | None) -> int:
    return int(time.time()) if now is None else now


def _backoff(attempt_count: int) -> int:
    exponent = max(0, min(attempt_count - 1, 20))
    return min(MAX_BACKOFF_SECONDS, BASE_BACKOFF_SECONDS * (1 << exponent))


def _valid_ledger_index(value: object) -> TypeGuard[int]:
    return not isinstance(value, bool) and isinstance(value, int) and value > 0


def _complete_signed_identity(burn: _LeasedBurn) -> bool:
    return (
        isinstance(burn.signed_tx_hash, str)
        and bool(burn.signed_tx_hash)
        and isinstance(burn.signed_tx_blob, str)
        and bool(burn.signed_tx_blob)
        and _valid_ledger_index(burn.signed_ledger_floor)
    )


def _accepts_required_range(callback: Reconcile) -> bool:
    try:
        parameters = tuple(inspect.signature(callback).parameters.values())
    except (TypeError, ValueError):
        return False
    if any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters):
        return True
    names = {parameter.name for parameter in parameters}
    return {"required_ledger_min", "required_ledger_max"} <= names


def _acquire(db_path: str, now: int, network: str | None = None) -> _LeasedBurn | None:
    network = network or config.XRPL_NETWORK
    sponsored_mint.ensure_schema(db_path)
    token = uuid4().hex
    with _connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT b.id, b.status, b.memo_id, b.amount, b.source_account,
                   b.network, c.network AS claim_network, b.issuer, b.currency,
                   b.source_tag, b.attempt_count, b.signed_tx_hash,
                   b.signed_tx_blob, b.signed_ledger_floor
            FROM free_mint_burns AS b
            JOIN free_mint_claims AS c ON c.id = b.claim_id
            WHERE b.network = ? AND (
                (b.status = 'indeterminate' AND COALESCE(b.next_attempt_at, 0) <= ?)
                OR (b.status = 'pending' AND COALESCE(b.next_attempt_at, 0) <= ?)
                OR (
                    b.status = 'submitting'
                    AND b.lease_until IS NOT NULL
                    AND b.lease_until <= ?
                ))
            ORDER BY
                CASE b.status
                    WHEN 'indeterminate' THEN 0
                    WHEN 'submitting' THEN 1
                    ELSE 2
                END,
                b.created_at,
                b.id
            LIMIT 1
            """,
            (network, now, now, now),
        ).fetchone()
        if row is None:
            return None
        previous_status = row["status"]
        # Count EVERY lease, not just those from `pending`. Counting only
        # pending meant a burn that keeps reconciling as `indeterminate`
        # re-entered at the same attempt_count forever, so _backoff stayed
        # pinned at its floor (5s) — and each of those passes runs
        # find_sponsored_burn, which pages the signing account's whole
        # validated history. One permanently-indeterminate obligation would
        # hammer the RPC endpoint every 5 seconds indefinitely, with the
        # backoff curve that exists to prevent exactly that never engaging.
        attempt_count = row["attempt_count"] + 1
        last_attempt_at = now
        conn.execute(
            """
            UPDATE free_mint_burns
            SET status = 'submitting',
                attempt_count = ?,
                last_attempt_at = COALESCE(?, last_attempt_at),
                lease_until = ?,
                lease_token = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                attempt_count,
                last_attempt_at,
                now + LEASE_SECONDS,
                token,
                now,
                row["id"],
            ),
        )
        return _LeasedBurn(
            id=row["id"],
            previous_status=previous_status,
            memo_id=row["memo_id"],
            amount=row["amount"],
            source_account=row["source_account"],
            attempt_count=attempt_count,
            network=row["network"],
            claim_network=row["claim_network"],
            issuer=row["issuer"],
            currency=row["currency"],
            source_tag=row["source_tag"],
            lease_token=token,
            signed_tx_hash=row["signed_tx_hash"],
            signed_tx_blob=row["signed_tx_blob"],
            signed_ledger_floor=row["signed_ledger_floor"],
        )


async def _heartbeat(db_path: str, burn: _LeasedBurn, stopped: asyncio.Event) -> None:
    while not stopped.is_set():
        try:
            await asyncio.wait_for(stopped.wait(), timeout=LEASE_SECONDS / 3)
            return
        except TimeoutError:
            pass
        now = int(time.time())
        try:
            with _connect(db_path) as conn:
                cursor = conn.execute(
                    """
                    UPDATE free_mint_burns
                    SET lease_until = ?, updated_at = ?
                    WHERE id = ? AND status = 'submitting' AND lease_token = ?
                    """,
                    (now + LEASE_SECONDS, now, burn.id, burn.lease_token),
                )
            if cursor.rowcount != 1:
                return
        except Exception:
            logging.exception("sponsored burn lease heartbeat failed for %s", burn.id)


async def _with_heartbeat(db_path: str, burn: _LeasedBurn, awaitable: Awaitable[_T]) -> _T:
    stopped = asyncio.Event()
    heartbeat = asyncio.create_task(_heartbeat(db_path, burn, stopped))
    try:
        return await awaitable
    finally:
        stopped.set()
        await heartbeat


def _reconciliation(value: object) -> xrpl_ops.BurnReconciliation:
    if isinstance(value, xrpl_ops.BurnReconciliation):
        return value
    if isinstance(value, str):
        return xrpl_ops.BurnReconciliation(True, value, None)
    if value is None:
        return xrpl_ops.BurnReconciliation(True, None, None)
    return xrpl_ops.BurnReconciliation(False, None, "invalid reconciliation result")


def _call(
    callback: Callable[..., Awaitable[Any]],
    burn: _LeasedBurn,
    **extra: object,
) -> Awaitable[Any]:
    kwargs: dict[str, object] = {
        "amount": burn.amount,
        "source_account": burn.source_account,
        "network": burn.network,
        "issuer": burn.issuer,
        "currency": burn.currency,
        "source_tag": burn.source_tag,
        **extra,
    }
    try:
        parameters = tuple(inspect.signature(callback).parameters.values())
    except (TypeError, ValueError):
        selected = kwargs
    else:
        if any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters):
            selected = kwargs
        else:
            names = {parameter.name for parameter in parameters}
            selected = {key: value for key, value in kwargs.items() if key in names}
    return callback(burn.memo_id, **selected)


def _save_preparation(
    db_path: str,
    burn: _LeasedBurn,
    preparation: xrpl_ops.BurnPreparation,
    now: int,
) -> bool:
    if (
        not preparation.tx_hash
        or not preparation.tx_blob
        or not _valid_ledger_index(preparation.signed_ledger_floor)
    ):
        return False
    with _connect(db_path) as conn:
        cursor = conn.execute(
            """
            UPDATE free_mint_burns
            SET signed_tx_hash = ?, signed_tx_blob = ?, signed_ledger_floor = ?, updated_at = ?
            WHERE id = ? AND status = 'submitting' AND lease_token = ?
              AND signed_tx_hash IS NULL AND signed_tx_blob IS NULL
              AND signed_ledger_floor IS NULL
            """,
            (
                preparation.tx_hash,
                preparation.tx_blob,
                preparation.signed_ledger_floor,
                now,
                burn.id,
                burn.lease_token,
            ),
        )
    return cursor.rowcount == 1


def _finish(
    db_path: str,
    burn: _LeasedBurn,
    *,
    status: str,
    now: int,
    tx_hash: str | None = None,
    error: str | None = None,
    next_attempt_at: int | None = None,
    burned_at: int | None = None,
    fulfillment: str | None = None,
    retire_signed_identity: bool = False,
) -> bool:
    with _connect(db_path) as conn:
        cursor = conn.execute(
            """
            UPDATE free_mint_burns
            SET status = ?,
                tx_hash = COALESCE(?, tx_hash),
                next_attempt_at = ?,
                last_error = ?,
                burned_at = ?,
                fulfillment = ?,
                signed_tx_hash = CASE WHEN ? THEN NULL ELSE signed_tx_hash END,
                signed_tx_blob = CASE WHEN ? THEN NULL ELSE signed_tx_blob END,
                signed_ledger_floor =
                    CASE WHEN ? THEN NULL ELSE signed_ledger_floor END,
                lease_until = NULL,
                lease_token = NULL,
                updated_at = ?
            WHERE id = ? AND status = 'submitting' AND lease_token = ?
            """,
            (
                status,
                tx_hash,
                next_attempt_at,
                error,
                burned_at,
                fulfillment,
                retire_signed_identity,
                retire_signed_identity,
                retire_signed_identity,
                now,
                burn.id,
                burn.lease_token,
            ),
        )
    return cursor.rowcount == 1


def _scope_mismatch(burn: _LeasedBurn) -> str | None:
    if burn.claim_network != burn.network:
        return (
            f"burn scope mismatch: claim network {burn.claim_network!r} does not match "
            f"obligation network {burn.network!r}"
        )
    expected: tuple[tuple[str, object, object], ...] = (
        ("source account", burn.source_account, config.SIGNING_ACCOUNT),
        ("issuer", burn.issuer, config.TOKEN_ISSUER_ADDRESS),
        ("currency", burn.currency, config.TOKEN_CURRENCY_HEX),
        ("SourceTag", burn.source_tag, config.SOURCE_TAG),
    )
    changed = [
        f"{label} persisted={persisted!r} active={active!r}"
        for label, persisted, active in expected
        if persisted != active
    ]
    return "burn scope mismatch: " + "; ".join(changed) if changed else None


async def process_one(
    db_path: str,
    *,
    submit: Submit | None,
    reconcile: Reconcile | None,
    prepare: Prepare | None = None,
    network: str | None = None,
    now: int | None = None,
    clock: Callable[[], int] | None = None,
    identity_expired: IdentityExpired | None = None,
) -> bool:
    """Prepare durably, then submit or reconcile exactly one immutable identity."""
    acquired_at = _timestamp(now)
    selected_network = config.XRPL_NETWORK if network is None else network
    burn = _acquire(db_path, acquired_at, selected_network)
    if burn is None:
        return False

    mismatch = _scope_mismatch(burn)
    if mismatch is not None:
        _finish(
            db_path,
            burn,
            status="failed_terminal",
            now=acquired_at,
            error=mismatch,
        )
        return True

    def completed_at() -> int:
        if clock is not None:
            return int(clock())
        return acquired_at if now is not None else int(time.time())

    if burn.previous_status in ("indeterminate", "submitting"):
        if reconcile is None:
            result = xrpl_ops.BurnReconciliation(False, None, "reconciliation callback unavailable")
        else:
            try:
                raw = await _with_heartbeat(
                    db_path,
                    burn,
                    _call(
                        reconcile,
                        burn,
                        signed_tx_hash=burn.signed_tx_hash,
                    ),
                )
                result = _reconciliation(raw)
            except Exception as exc:
                result = xrpl_ops.BurnReconciliation(False, None, f"reconciliation failed: {exc}")

        expired_last_ledger_sequence: int | None = None
        expiry_error: str | None = None
        signed_ledger_floor = burn.signed_ledger_floor
        if result.complete and result.tx_hash is None and burn.signed_tx_hash is not None:
            if burn.signed_tx_blob is None:
                expiry_error = "signed burn expiry cannot be proven without its transaction blob"
            elif not _valid_ledger_index(signed_ledger_floor):
                expiry_error = (
                    "signed burn expiry cannot be proven without its preparation ledger floor"
                )
            elif identity_expired is None:
                expiry_error = "signed burn expiry callback unavailable"
            else:
                try:
                    expiry = await _with_heartbeat(
                        db_path,
                        burn,
                        identity_expired(burn.signed_tx_blob),
                    )
                    if _valid_ledger_index(expiry) and expiry >= signed_ledger_floor:
                        expired_last_ledger_sequence = expiry
                    elif expiry is not None:
                        expiry_error = "signed burn expiry returned an invalid ledger range"
                except Exception as exc:
                    expiry_error = f"signed burn expiry check failed: {exc}"

        if expired_last_ledger_sequence is not None and reconcile is not None:
            if not _accepts_required_range(reconcile):
                result = xrpl_ops.BurnReconciliation(
                    False,
                    None,
                    "post-expiry reconciliation callback cannot prove a ledger range",
                )
            else:
                try:
                    raw = await _with_heartbeat(
                        db_path,
                        burn,
                        _call(
                            reconcile,
                            burn,
                            signed_tx_hash=burn.signed_tx_hash,
                            required_ledger_min=signed_ledger_floor,
                            required_ledger_max=expired_last_ledger_sequence,
                        ),
                    )
                    result = _reconciliation(raw)
                except Exception as exc:
                    result = xrpl_ops.BurnReconciliation(
                        False,
                        None,
                        f"post-expiry reconciliation failed: {exc}",
                    )

        finished = completed_at()
        if (
            result.complete
            and result.tx_hash
            and (burn.signed_tx_hash is None or result.tx_hash == burn.signed_tx_hash)
        ):
            _finish(
                db_path,
                burn,
                status="burned",
                now=finished,
                tx_hash=result.tx_hash,
                burned_at=finished,
                fulfillment="ledger_burn",
            )
        elif result.complete and result.tx_hash is None and burn.signed_tx_hash is None:
            _finish(
                db_path,
                burn,
                status="pending",
                now=finished,
                next_attempt_at=finished + 1,
            )
        elif (
            result.complete and result.tx_hash is None and expired_last_ledger_sequence is not None
        ):
            _finish(
                db_path,
                burn,
                status="pending",
                now=finished,
                next_attempt_at=finished + 1,
                retire_signed_identity=True,
            )
        else:
            error = result.error or "account history scan incomplete"
            if result.complete and result.tx_hash:
                error = "reconciled hash differed from persisted signed transaction"
            elif result.complete:
                error = expiry_error or "signed burn has not expired on a validated ledger"
            _finish(
                db_path,
                burn,
                status="indeterminate",
                now=finished,
                error=error,
                next_attempt_at=finished + _backoff(max(1, burn.attempt_count)),
            )
        return True

    async with xrpl_ops.submission_coordinator(burn.source_account):
        identity_is_empty = (
            burn.signed_tx_hash is None
            and burn.signed_tx_blob is None
            and burn.signed_ledger_floor is None
        )
        if not identity_is_empty and not _complete_signed_identity(burn):
            finished = completed_at()
            _finish(
                db_path,
                burn,
                status="indeterminate",
                now=finished,
                error="burn obligation has an incomplete persisted signed identity",
                next_attempt_at=finished + _backoff(max(1, burn.attempt_count)),
            )
            return True

        if identity_is_empty:
            if prepare is None:
                preparation = xrpl_ops.BurnPreparation(
                    "failed", None, None, "preparation callback unavailable"
                )
            else:
                try:
                    preparation = await _with_heartbeat(
                        db_path,
                        burn,
                        _call(prepare, burn, coordinator_held=True),
                    )
                except Exception as exc:
                    preparation = xrpl_ops.BurnPreparation(
                        "failed", None, None, f"preparation failed: {exc}"
                    )
            finished = completed_at()
            if preparation.state == "noop":
                _finish(
                    db_path,
                    burn,
                    status="burned",
                    now=finished,
                    burned_at=finished,
                    fulfillment="self_issuer_noop",
                )
                return True
            if (
                preparation.state != "prepared"
                or not preparation.tx_hash
                or not preparation.tx_blob
                or not _valid_ledger_index(preparation.signed_ledger_floor)
            ):
                _finish(
                    db_path,
                    burn,
                    status="pending",
                    now=finished,
                    error=preparation.error or "burn preparation failed",
                    next_attempt_at=finished + _backoff(burn.attempt_count),
                )
                return True
            if not _save_preparation(db_path, burn, preparation, finished):
                return True
            burn = replace(
                burn,
                signed_tx_hash=preparation.tx_hash,
                signed_tx_blob=preparation.tx_blob,
                signed_ledger_floor=preparation.signed_ledger_floor,
            )

        if submit is None:
            submission = xrpl_ops.BurnSubmission(
                "failed", burn.signed_tx_hash, "submission callback unavailable"
            )
        else:
            try:
                submission = await _with_heartbeat(
                    db_path,
                    burn,
                    _call(
                        submit,
                        burn,
                        signed_tx_hash=burn.signed_tx_hash,
                        signed_tx_blob=burn.signed_tx_blob,
                        coordinator_held=True,
                    ),
                )
            except Exception as exc:
                submission = xrpl_ops.BurnSubmission(
                    "indeterminate",
                    burn.signed_tx_hash,
                    f"submission raised unexpectedly: {exc}",
                )

        finished = completed_at()
        if submission.state == "validated" and submission.tx_hash == burn.signed_tx_hash:
            _finish(
                db_path,
                burn,
                status="burned",
                now=finished,
                tx_hash=submission.tx_hash,
                burned_at=finished,
                fulfillment="ledger_burn",
            )
        elif submission.state == "failed":
            _finish(
                db_path,
                burn,
                status="pending",
                now=finished,
                error=submission.error or "validated burn failure",
                next_attempt_at=finished + _backoff(burn.attempt_count),
                retire_signed_identity=True,
            )
        else:
            error = submission.error or "burn submission outcome indeterminate"
            if submission.state == "validated":
                error = "validated response did not match persisted signed hash"
            _finish(
                db_path,
                burn,
                status="indeterminate",
                now=finished,
                error=error,
                next_attempt_at=finished,
            )
        return True


async def run_worker(db_path: str, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            worked = await process_one(
                db_path,
                prepare=xrpl_ops.prepare_sponsored_burn,
                submit=xrpl_ops.submit_sponsored_burn,
                reconcile=xrpl_ops.find_sponsored_burn,
                network=config.XRPL_NETWORK,
                identity_expired=xrpl_ops.sponsored_burn_identity_expired,
            )
        except Exception:
            logging.exception("sponsored burn worker pass failed")
            worked = False
        if worked:
            await asyncio.sleep(0)
            continue
        try:
            await asyncio.wait_for(stop.wait(), timeout=POLL_SECONDS)
        except TimeoutError:
            pass
