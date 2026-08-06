#!/usr/bin/env python3
"""One-time (resumable, idempotent) ledger-history backfill.

  python scripts/backfill_history.py --network mainnet
  python scripts/backfill_history.py --network mainnet --distributor rXXX
  python scripts/backfill_history.py --network mainnet --derive-only

Sources: account_tx over the NFT issuer, the BRIX issuer, and (if given) the
airdrop distributor; clio nft_history per nft_id known to onchain_<net>.db.
Pagination markers persist to backfill_state after every page, so Ctrl-C and
re-run is always safe. Derivation (Task 5) rebuilds nft_events/brix_events
from the raw rows."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from typing import Any

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))

from websockets.exceptions import WebSocketException  # noqa: E402
from xrpl.asyncio.clients import AsyncWebsocketClient  # noqa: E402
from xrpl.asyncio.clients.exceptions import XRPLWebsocketException  # noqa: E402
from xrpl.models.requests import Request  # noqa: E402

from lfg_core import history_events, history_store, sponsored_mint  # noqa: E402

PAGE_LIMIT = 200
REQUEST_TIMEOUT = 60
# clio rate-limits public endpoints ('slowDown'); pace requests and back off.
THROTTLE_SECONDS = 0.25
RETRYABLE_ERRORS = {"slowDown", "tooBusy"}
RETRY_MAX = 8
RETRY_BASE_DELAY = 5.0
DEFAULT_SOURCE_ORDER = (
    "issuer",
    "brix",
    "token_issuer",
    "signing",
    "distributor",
    "nfts",
)
DEFAULT_SOURCES = frozenset(DEFAULT_SOURCE_ORDER)
VALID_SOURCES = DEFAULT_SOURCES
# A certification run must sweep every source — narrowing `--sources` would
# make the coverage document attest less than the eligibility baseline is
# trusted to prove (#331). The canonical set lives in sponsored_mint so the
# runtime gate and this writer can never disagree.
REQUIRED_BASELINE_SOURCES = sponsored_mint.BASELINE_REQUIRED_SOURCES
assert REQUIRED_BASELINE_SOURCES <= VALID_SOURCES


def validate_baseline_source_coverage(sources: set[str] | frozenset[str]) -> None:
    """Require the full source set for a certification run (#331)."""

    missing = sorted(REQUIRED_BASELINE_SOURCES - sources)
    if missing:
        raise ValueError("baseline certification requires sources: " + ", ".join(missing))


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
        skipped_unvalidated = 0
        for entry in result.get("transactions", []):
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
        marker = result.get("marker")
        history_store.set_cursor(conn, cursor_source, json.dumps(marker) if marker else None)
        if not marker:
            return new


async def backfill_nft_history(
    conn: Any, request_fn: Any, nft_id: str, *, network: str | None = None
) -> int:
    """Full nft_history (clio) for one token; cursor keyed per nft_id.

    The pagination marker is persisted after every page (like
    backfill_account_tx) so an interrupted long token history resumes from
    where it left off instead of restarting from page 1."""
    source = f"nft_history:{nft_id}"
    stored = history_store.get_cursor(conn, source)
    if stored == "done":
        return 0
    marker: Any = json.loads(stored) if stored else None
    new = 0
    while True:
        req: dict[str, Any] = {"method": "nft_history", "nft_id": nft_id, "limit": 100}
        if marker:
            req["marker"] = marker
        result = await request_fn(req)
        skipped_unvalidated = 0
        for entry in result.get("transactions", []):
            if not _is_validated_entry(result, entry):
                skipped_unvalidated += 1
                continue
            tx = history_events.normalize_entry(entry)
            tx["validated"] = True
            if store_raw_tx(conn, tx, network=network):
                new += 1
        _warn_if_unvalidated(source, result, skipped_unvalidated)
        marker = result.get("marker")
        history_store.set_cursor(conn, source, json.dumps(marker) if marker else "done")
        if not marker:
            return new


async def _amain() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    import backfill_onchain as bf

    from lfg_core import config, nft_index

    parser = argparse.ArgumentParser(description="Ledger history backfill.")
    parser.add_argument("--network", choices=sorted(bf.NETWORKS), default=config.XRPL_NETWORK)
    parser.add_argument("--distributor", help="airdrop distributor wallet to scrape")
    parser.add_argument("--sources", default=",".join(DEFAULT_SOURCE_ORDER))
    parser.add_argument("--derive-only", action="store_true")

    parser.add_argument(
        "--complete-audited-baseline",
        action="store_true",
        help="record an externally audited complete SourceTag baseline after this run",
    )
    parser.add_argument("--genesis-hash", help="chain genesis hash for baseline provenance")
    parser.add_argument("--baseline-ledger-min", type=int)
    parser.add_argument("--baseline-ledger-max", type=int)
    parser.add_argument(
        "--baseline-provenance",
        help="audit/export identifier proving all SourceTag transactions were covered",
    )
    args = parser.parse_args()
    wanted = set(args.sources.split(","))
    unknown = wanted - VALID_SOURCES
    if unknown:
        parser.error(f"unknown --sources value(s): {', '.join(sorted(unknown))}")
    baseline_values = (
        args.genesis_hash,
        args.baseline_ledger_min,
        args.baseline_ledger_max,
        args.baseline_provenance,
    )
    if args.complete_audited_baseline and any(value is None for value in baseline_values):
        parser.error(
            "--complete-audited-baseline requires --genesis-hash, --baseline-ledger-min, "
            "--baseline-ledger-max, and --baseline-provenance"
        )
    if not args.complete_audited_baseline and any(value is not None for value in baseline_values):
        parser.error("baseline metadata requires --complete-audited-baseline")
    if args.derive_only and args.complete_audited_baseline:
        parser.error("--derive-only cannot certify a baseline")
    if args.complete_audited_baseline:
        try:
            validate_baseline_source_coverage(wanted)
        except ValueError as exc:
            parser.error(str(exc))

    from derive_history_events import issuers_for_network

    net = bf.NETWORKS[args.network]
    clio = net["clio"]
    issuer, brix_issuer = issuers_for_network(args.network)
    issuer = net["issuer"] or issuer
    conn = history_store.init_history_db(history_store.history_db_path(args.network))

    if args.derive_only:
        from derive_history_events import rederive  # Task 5

        rederive(conn, args.network, distributor=args.distributor)
        return 0

    endpoint_snapshot: history_store.EndpointSnapshot | None = None
    async with AsyncWebsocketClient(clio) as client:

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
                    # Transient transport trouble gets the same bounded backoff as
                    # slowDown. A torn-down websocket will keep failing and exhaust
                    # the attempts — the run is cursor-resumable, so that is safe.
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

        fixed_ledger_min = -1
        fixed_ledger_max = -1
        if args.complete_audited_baseline:
            assert args.genesis_hash is not None
            assert args.baseline_ledger_min is not None
            assert args.baseline_ledger_max is not None
            endpoint_snapshot = await history_store.fetch_endpoint_snapshot(request_fn)
            validate_baseline_endpoint(
                endpoint_snapshot,
                claimed_genesis_hash=args.genesis_hash,
                baseline_ledger_min=args.baseline_ledger_min,
                baseline_ledger_max=args.baseline_ledger_max,
            )
            fixed_ledger_min = args.baseline_ledger_min
            fixed_ledger_max = args.baseline_ledger_max

        if "issuer" in wanted:
            n = await backfill_account_tx(
                conn,
                request_fn,
                issuer,
                "issuer_tx",
                network=args.network,
                ledger_min=fixed_ledger_min,
                ledger_max=fixed_ledger_max,
            )
            logging.info(f"issuer_tx: +{n}")
        if "brix" in wanted:
            n = await backfill_account_tx(
                conn,
                request_fn,
                brix_issuer,
                "brix_tx",
                network=args.network,
                ledger_min=fixed_ledger_min,
                ledger_max=fixed_ledger_max,
            )
            logging.info(f"brix_tx: +{n}")
        if "token_issuer" in wanted:
            n = await backfill_account_tx(
                conn,
                request_fn,
                config.TOKEN_ISSUER_ADDRESS,
                "token_issuer_tx",
                network=args.network,
                ledger_min=fixed_ledger_min,
                ledger_max=fixed_ledger_max,
            )
            logging.info(f"token_issuer_tx: +{n}")
        if "signing" in wanted:
            n = await backfill_account_tx(
                conn,
                request_fn,
                config.SIGNING_ACCOUNT,
                "signing_tx",
                network=args.network,
                ledger_min=fixed_ledger_min,
                ledger_max=fixed_ledger_max,
            )
            logging.info(f"signing_tx: +{n}")
        if "distributor" in wanted and args.distributor:
            n = await backfill_account_tx(
                conn,
                request_fn,
                args.distributor,
                "distributor_tx",
                network=args.network,
                ledger_min=fixed_ledger_min,
                ledger_max=fixed_ledger_max,
            )
            logging.info(f"distributor_tx: +{n}")
        if "nfts" in wanted:
            oconn = nft_index.init_db(nft_index.index_db_path(args.network))
            ids = [r[0] for r in oconn.execute("SELECT nft_id FROM onchain_nfts")]
            total = 0
            for i, nft_id in enumerate(ids, 1):
                total += await backfill_nft_history(conn, request_fn, nft_id, network=args.network)
                if i % 100 == 0:
                    logging.info(f"nft_history: {i}/{len(ids)} tokens, +{total} txs")
            logging.info(f"nft_history: done, +{total}")
    if args.complete_audited_baseline:
        assert args.genesis_hash is not None
        assert args.baseline_ledger_min is not None
        assert args.baseline_ledger_max is not None
        assert args.baseline_provenance is not None
        assert endpoint_snapshot is not None
        accounts = baseline_account_coverage(
            wanted,
            distributor=args.distributor,
            nft_issuer=issuer,
            brix_issuer=brix_issuer,
        )
        coverage = baseline_coverage_document(
            accounts,
            sources=wanted,
            source_tag=config.SOURCE_TAG,
            ledger_min=args.baseline_ledger_min,
            ledger_max=args.baseline_ledger_max,
        )
        history_store.record_archive_baseline(
            conn,
            network=args.network,
            genesis_hash=endpoint_snapshot.genesis_hash,
            ledger_min=args.baseline_ledger_min,
            ledger_max=args.baseline_ledger_max,
            provenance=args.baseline_provenance,
            source_tag=config.SOURCE_TAG,
            coverage=json.dumps(coverage, sort_keys=True, separators=(",", ":")),
        )
        logging.info("recorded externally audited SourceTag baseline provenance")
    return 0


def main() -> int:
    return asyncio.run(_amain())


if __name__ == "__main__":
    raise SystemExit(main())
