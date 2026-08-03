"""Deterministic archive sweep + certification core.

Shared by the one-time manual baseline certification CLI
(scripts/backfill_history.py --complete-audited-baseline) and the automated
re-verification job the sponsored-mint campaign start kicks (#340). The
functions here perform read-only XRPL RPCs and history-DB writes; they never
sign or submit transactions.

`reverify_archive` is the deterministic re-certification entry point. It
never raises on expected failures, returning a `ReverifyResult` with one of
these closed-set machine-readable reasons instead: `"baseline_never_certified"`,
`"genesis_mismatch"`, `"coverage_unbound"`, `"missing_required_sources"`,
`"sweep_failed: <exc>"`, `"gap_not_covered"`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from websockets.exceptions import WebSocketException
from xrpl.asyncio.clients import AsyncWebsocketClient
from xrpl.asyncio.clients.exceptions import XRPLWebsocketException
from xrpl.models.requests import Request

from lfg_core import history_events, history_store, sponsored_mint

PAGE_LIMIT = 200
REQUEST_TIMEOUT = 60
# clio rate-limits public endpoints ('slowDown'); pace requests and back off.
THROTTLE_SECONDS = 0.25
RETRYABLE_ERRORS = {"slowDown", "tooBusy"}
RETRY_MAX = 8
RETRY_BASE_DELAY = 5.0
# A certification run must sweep every source — narrowing `--sources` would
# make the coverage document attest less than the eligibility baseline is
# trusted to prove (#331). The canonical set lives in sponsored_mint so the
# runtime gate and this writer can never disagree.
REQUIRED_BASELINE_SOURCES = sponsored_mint.BASELINE_REQUIRED_SOURCES


def validate_baseline_source_coverage(
    sources: set[str] | frozenset[str],
    *,
    distributor: str | None = None,
) -> None:
    """Require the full source set for a certification run (#331).

    The distributor source is only actually swept when an address is supplied,
    so certifying without `--distributor` would attest a sweep that never
    happened — refuse up front rather than record a false attestation."""

    missing = sorted(REQUIRED_BASELINE_SOURCES - sources)
    if missing:
        raise ValueError("baseline certification requires sources: " + ", ".join(missing))
    if "distributor" in sources and not distributor:
        raise ValueError(
            "baseline certification requires --distributor: the distributor source "
            "cannot be swept (and must not be attested) without an address"
        )


def baseline_account_coverage(
    sources: set[str] | frozenset[str],
    *,
    distributor: str | None,
    nft_issuer: str | None = None,
    brix_issuer: str | None = None,
) -> dict[str, str]:
    """Snapshot the concrete accounts covered by this certification run."""

    from lfg_core import config

    candidates = {
        "issuer": nft_issuer,
        "brix": brix_issuer,
        "token_issuer": config.TOKEN_ISSUER_ADDRESS,
        "signing": config.SIGNING_ACCOUNT,
        "distributor": distributor,
    }
    return {
        source: str(account)
        for source, account in candidates.items()
        if source in sources and account
    }


def baseline_coverage_document(
    accounts: dict[str, str],
    *,
    sources: set[str] | frozenset[str],
    source_tag: int,
    ledger_min: int,
    ledger_max: int,
) -> dict[str, Any]:
    """Persist the exact range, source set, and concrete accounts swept.

    `sources` records source *names* (including `nfts`, which has no account
    address to record) so the runtime gate can verify the sweep was not
    narrowed (#331). Version 2 added that field; the gate rejects version 1."""

    return {
        "version": sponsored_mint.BASELINE_COVERAGE_VERSION,
        "source_tag": source_tag,
        "ledger_min": ledger_min,
        "ledger_max": ledger_max,
        "sources": sorted(sources),
        "accounts": dict(sorted(accounts.items())),
    }


def validate_baseline_endpoint(
    snapshot: history_store.EndpointSnapshot,
    *,
    claimed_genesis_hash: str,
    baseline_ledger_min: int,
    baseline_ledger_max: int,
) -> None:
    if claimed_genesis_hash.strip() != snapshot.genesis_hash:
        raise ValueError("claimed genesis does not match the endpoint chain identity")
    # Not 1: ledgers 1-32569 were lost in 2012 and no node serves them, so a
    # "complete" sweep starts at the earliest ledger that exists. account_tx
    # rejects a lower ledger_index_min outright (lgrIdxMalformed).
    if baseline_ledger_min != history_store.EARLIEST_AVAILABLE_LEDGER:
        raise ValueError(
            "complete SourceTag baseline must start at ledger "
            f"{history_store.EARLIEST_AVAILABLE_LEDGER} (ledgers 1-32569 do not exist)"
        )
    if baseline_ledger_max != snapshot.validated_ledger_index:
        raise ValueError("baseline maximum must equal the validated endpoint tip")


def _is_validated_entry(page: dict[str, Any], entry: dict[str, Any]) -> bool:
    """Accept only explicit non-conflicting validation from page or entry.

    Deliberately strict: callers stamp `tx["validated"] = True` on whatever this
    admits, and that stamp is the evidence `sponsored_mint` treats as proof a
    wallet has transacted under our SourceTag. Inferring validation from a
    missing key would forge that proof.

    The cost of strictness is that an endpoint which reports validation in
    neither place archives NOTHING from that source — silently, since an empty
    archive is indistinguishable from a genuinely empty account. `_warn_if_
    unvalidated` makes that loud instead; do not "fix" a zero-row backfill by
    loosening this predicate."""
    page_validated = page.get("validated")
    entry_validated = entry.get("validated")
    if page_validated is False or entry_validated is False:
        return False
    return page_validated is True or entry_validated is True


def _warn_if_unvalidated(source: str, page: dict[str, Any], skipped: int) -> None:
    """Surface a page whose entries were dropped for want of a validation flag.

    A response shape that carries validation in neither the page nor its entries
    yields a silently empty archive — which then reads as "this account has no
    tagged transactions", i.e. every wallet looks eligible for a sponsored mint.
    Fail loudly at ingest instead of quietly at admission."""
    if not skipped:
        return
    logging.warning(
        "%s: skipped %d entr%s carrying no explicit `validated` flag at page or entry level. "
        "If this is every entry, the endpoint reports validation differently and this source is "
        "archiving nothing — verify the response shape before certifying a baseline.",
        source,
        skipped,
        "y" if skipped == 1 else "ies",
    )


def store_raw_tx(conn: Any, tx: dict[str, Any], *, network: str | None = None) -> bool:
    inserted = history_store.insert_tx(
        conn,
        tx_hash=str(tx.get("hash")),
        ledger_index=tx.get("ledger_index"),
        close_time=history_events.tx_unix_time(tx),
        tx_type=str(tx.get("TransactionType", "")),
        account=tx.get("Account"),
        source_tag=tx.get("SourceTag"),
        raw_json=json.dumps(tx, sort_keys=True),
    )
    # Raw validated history is authoritative and survives projection errors.
    # Observe duplicates too, so a resumable rerun retries the idempotent projection.
    conn.commit()
    if network is not None:
        from lfg_core import sponsored_mint

        sponsored_mint.observe_sponsored_acceptance(tx, tx.get("meta") or {}, network=network)
    return inserted


async def backfill_account_tx(
    conn: Any,
    request_fn: Any,
    account: str,
    source: str,
    *,
    network: str | None = None,
    ledger_min: int = -1,
    ledger_max: int = -1,
) -> int:
    """Page account_tx forward, persisting the marker after every page."""
    certified_range = ledger_min != -1 or ledger_max != -1
    if certified_range and (ledger_min < 0 or ledger_max < ledger_min):
        raise ValueError("certified account_tx range is invalid")
    cursor_source = (
        f"{source}:certified:{ledger_min}:{ledger_max}:{account}" if certified_range else source
    )
    stored = history_store.get_cursor(conn, cursor_source)
    marker: Any = json.loads(stored) if stored else None
    new = 0
    while True:
        req: dict[str, Any] = {
            "method": "account_tx",
            "account": account,
            "ledger_index_min": ledger_min,
            "ledger_index_max": ledger_max,
            "limit": PAGE_LIMIT,
            "forward": True,
        }
        if marker:
            req["marker"] = marker
        result = await request_fn(req)
        if certified_range and (
            result.get("account") != account
            or result.get("ledger_index_min") != ledger_min
            or result.get("ledger_index_max") != ledger_max
            or result.get("validated") is not True
        ):
            raise ValueError("certified account_tx page did not prove its account and range")
        entries = result.get("transactions")
        if certified_range and not isinstance(entries, list):
            # Defaulting a missing key to [] would read "the endpoint did not
            # answer" as "this window is empty" and complete the cursor.
            raise ValueError("certified account_tx page carried no transaction list")
        skipped_unvalidated = 0
        for entry in entries or []:
            if not _is_validated_entry(result, entry):
                skipped_unvalidated += 1
                continue
            tx = history_events.normalize_entry(entry)
            entry_ledger = tx.get("ledger_index")
            if certified_range and (
                isinstance(entry_ledger, bool)
                or not isinstance(entry_ledger, int)
                or not ledger_min <= entry_ledger <= ledger_max
            ):
                raise ValueError("certified account_tx entry fell outside its proven range")
            tx["validated"] = True
            if store_raw_tx(conn, tx, network=network):
                new += 1
        _warn_if_unvalidated(cursor_source, result, skipped_unvalidated)
        if certified_range and skipped_unvalidated:
            # A dropped entry means this window was only partly archived; the
            # cursor must not advance over evidence that was never stored.
            raise ValueError("certified account_tx page contained unvalidated entries")
        marker = result.get("marker")
        history_store.set_cursor(conn, cursor_source, json.dumps(marker) if marker else None)
        if not marker:
            return new


def nft_ids_in_ledger_range(
    conn: Any, *, nft_issuer: str, ledger_min: int, ledger_max: int
) -> set[str]:
    """Our collection's token IDs appearing in archived txs within a range.

    The `nfts` source pages per-token history for every token in the on-chain
    index, but that index is maintained by the listener — so tokens minted
    while it was down are missing from it, which is exactly the window a
    catch-up covers. Paging the index alone would skip those tokens while the
    run still attests cumulative `nfts` coverage. Mining the raw archive
    (already populated for this window by the account sweeps, which run first)
    recovers them."""

    issuer_hex = history_events.issuer_account_hex(nft_issuer)
    found: set[str] = set()
    for row in conn.execute(
        "SELECT raw_json FROM xrpl_txs WHERE ledger_index BETWEEN ? AND ?",
        (ledger_min, ledger_max),
    ):
        try:
            tx = json.loads(row["raw_json"])
        except (TypeError, ValueError) as exc:
            # An unreadable archived tx could be the only record of a token
            # minted during the gap; skipping it would let the run attest
            # `nfts` coverage it never obtained.
            raise ValueError(
                "certified NFT discovery found an unverifiable archived transaction"
            ) from exc
        for ev in history_events.derive_nft_events(tx, nft_issuer=nft_issuer):
            nft_id = ev.get("nft_id")
            if isinstance(nft_id, str) and history_events.nft_id_issuer_matches(nft_id, issuer_hex):
                found.add(nft_id)
    return found


async def backfill_nft_history(
    conn: Any,
    request_fn: Any,
    nft_id: str,
    *,
    network: str | None = None,
    ledger_min: int = -1,
    ledger_max: int = -1,
) -> int:
    """Full nft_history (clio) for one token; cursor keyed per nft_id.

    The pagination marker is persisted after every page (like
    backfill_account_tx) so an interrupted long token history resumes from
    where it left off instead of restarting from page 1.

    A certified range namespaces the cursor, exactly as backfill_account_tx
    does. Without that, a bounded catch-up would short-circuit on the plain
    cursor's terminal "done" — set by the prior full certification — and skip
    the token entirely, leaving its gap-period transactions out of an archive
    that nonetheless attests cumulative `nfts` coverage."""
    source = f"nft_history:{nft_id}"
    certified_range = ledger_min != -1 or ledger_max != -1
    if certified_range and (ledger_min < 0 or ledger_max < ledger_min):
        raise ValueError("certified nft_history range is invalid")
    cursor_source = f"{source}:certified:{ledger_min}:{ledger_max}" if certified_range else source
    stored = history_store.get_cursor(conn, cursor_source)
    if stored == "done":
        return 0
    marker: Any = json.loads(stored) if stored else None
    new = 0
    while True:
        req: dict[str, Any] = {"method": "nft_history", "nft_id": nft_id, "limit": 100}
        if certified_range:
            req["ledger_index_min"] = ledger_min
            req["ledger_index_max"] = ledger_max
        if marker:
            req["marker"] = marker
        result = await request_fn(req)
        if certified_range and (
            result.get("nft_id") != nft_id
            or result.get("ledger_index_min") != ledger_min
            or result.get("ledger_index_max") != ledger_max
            or result.get("validated") is not True
        ):
            raise ValueError("certified nft_history page did not prove its token and range")
        entries = result.get("transactions")
        if certified_range and not isinstance(entries, list):
            raise ValueError("certified nft_history page carried no transaction list")
        skipped_unvalidated = 0
        for entry in entries or []:
            if not _is_validated_entry(result, entry):
                skipped_unvalidated += 1
                continue
            tx = history_events.normalize_entry(entry)
            entry_ledger = tx.get("ledger_index")
            if certified_range and (
                isinstance(entry_ledger, bool)
                or not isinstance(entry_ledger, int)
                or not ledger_min <= entry_ledger <= ledger_max
            ):
                raise ValueError("certified nft_history entry fell outside its proven range")
            tx["validated"] = True
            if store_raw_tx(conn, tx, network=network):
                new += 1
        _warn_if_unvalidated(cursor_source, result, skipped_unvalidated)
        if certified_range and skipped_unvalidated:
            raise ValueError("certified nft_history page contained unvalidated entries")
        marker = result.get("marker")
        history_store.set_cursor(conn, cursor_source, json.dumps(marker) if marker else "done")
        if not marker:
            return new


def make_request_fn(
    client: AsyncWebsocketClient,
) -> Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]:
    """Bounded-retry, throttled request wrapper over one websocket client."""

    async def request_fn(req: dict[str, Any]) -> dict[str, Any]:
        delay = RETRY_BASE_DELAY
        for attempt in range(RETRY_MAX):
            await asyncio.sleep(THROTTLE_SECONDS)
            try:
                r = await asyncio.wait_for(
                    client.request(Request.from_dict(req)), timeout=REQUEST_TIMEOUT
                )
            except (
                TimeoutError,
                asyncio.TimeoutError,
                ConnectionError,
                OSError,
                WebSocketException,
                XRPLWebsocketException,
            ) as e:
                if attempt < RETRY_MAX - 1:
                    logging.warning(f"{req['method']}: {e!r}; backing off {delay:.0f}s")
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 120.0)
                    continue
                raise
            if r.is_successful():
                return r.result
            error = r.result.get("error") if isinstance(r.result, dict) else None
            if error in RETRYABLE_ERRORS and attempt < RETRY_MAX - 1:
                logging.warning(f"{req['method']}: {error}; backing off {delay:.0f}s")
                await asyncio.sleep(delay)
                delay = min(delay * 2, 120.0)
                continue
            raise RuntimeError(f"{req['method']} failed: {r.result}")
        raise RuntimeError(f"{req['method']} failed after {RETRY_MAX} attempts")

    return request_fn


_ATTESTATION_RE = re.compile(r"^auto-reverify at \S+ \(baseline: (?P<orig>.*)\)$", re.DOTALL)


@dataclass(frozen=True)
class ReverifyResult:
    """Outcome of one automated re-certification attempt."""

    ok: bool
    reason: str | None
    ledger_max: int | None
    provenance: str | None


def inherit_attestation(provenance: str) -> str:
    """Return the original human attestation, unwrapping one auto-reverify layer.

    Repeated re-verifies must carry the SAME baseline attestation forever, not
    a nested chain of wrappers."""
    match = _ATTESTATION_RE.match(provenance.strip())
    return match.group("orig") if match else provenance.strip()


async def reverify_archive(
    conn: sqlite3.Connection,
    request_fn: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
    *,
    network: str,
    now: int | None = None,
) -> ReverifyResult:
    """Deterministically re-certify a previously human-certified archive.

    Preconditions are read from archive_state: a prior baseline with a
    non-empty provenance and a bound coverage document. The sweep re-pages
    account_tx over exactly the accounts the original certification covered,
    from EARLIEST_AVAILABLE_LEDGER to the live validated tip, then certifies
    through record_archive_baseline (whose gap-clearing CASE logic is the
    single authority on whether a continuity gap is healed). Never raises on
    expected failures — returns a ReverifyResult with a machine-readable
    reason instead."""

    state = history_store.get_archive_state(conn, network)
    if state is None or not (state.baseline_provenance or "").strip():
        return ReverifyResult(False, "baseline_never_certified", None, None)
    try:
        coverage_doc = json.loads(state.baseline_coverage or "")
        accounts = dict(coverage_doc["accounts"])
    except (ValueError, TypeError, KeyError):
        return ReverifyResult(False, "coverage_unbound", None, None)
    if not accounts:
        return ReverifyResult(False, "coverage_unbound", None, None)
    try:
        validate_baseline_source_coverage(set(accounts))
    except ValueError:
        return ReverifyResult(False, "missing_required_sources", None, None)

    try:
        snapshot = await history_store.fetch_endpoint_snapshot(request_fn)
    except Exception as exc:  # endpoint identity unreadable — nothing to certify against
        return ReverifyResult(False, f"sweep_failed: {exc}", None, None)
    if snapshot.genesis_hash != state.genesis_hash:
        return ReverifyResult(False, "genesis_mismatch", None, None)

    ledger_min = history_store.EARLIEST_AVAILABLE_LEDGER
    ledger_max = snapshot.validated_ledger_index
    try:
        for source, account in sorted(accounts.items()):
            await backfill_account_tx(
                conn,
                request_fn,
                account,
                f"{source}_tx",
                network=network,
                ledger_min=ledger_min,
                ledger_max=ledger_max,
            )
    except Exception as exc:
        # Cursors persisted per page; a retry resumes, exactly like a Ctrl-C'd
        # manual backfill. Nothing was certified, so fail-closed is preserved.
        return ReverifyResult(False, f"sweep_failed: {exc}", None, None)

    from lfg_core import config

    timestamp = int(time.time()) if now is None else int(now)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(timestamp))
    provenance = f"auto-reverify at {stamp} (baseline: {inherit_attestation(state.baseline_provenance or '')})"
    doc = baseline_coverage_document(
        accounts, source_tag=config.SOURCE_TAG, ledger_min=ledger_min, ledger_max=ledger_max
    )
    history_store.record_archive_baseline(
        conn,
        network=network,
        genesis_hash=snapshot.genesis_hash,
        ledger_min=ledger_min,
        ledger_max=ledger_max,
        provenance=provenance,
        source_tag=config.SOURCE_TAG,
        coverage=json.dumps(doc, sort_keys=True, separators=(",", ":")),
        completed_at=timestamp,
    )
    refreshed = history_store.get_archive_state(conn, network)
    if refreshed is None or not refreshed.baseline_complete:
        # A gap whose bound lies past the swept tip survives certification by
        # design (record_archive_baseline's CASE). Report it, don't mask it.
        return ReverifyResult(False, "gap_not_covered", ledger_max, provenance)
    return ReverifyResult(True, None, ledger_max, provenance)
