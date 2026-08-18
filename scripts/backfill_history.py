#!/usr/bin/env python3
"""One-time (resumable, idempotent) ledger-history backfill.

  python scripts/backfill_history.py --network mainnet
  python scripts/backfill_history.py --network mainnet --distributor rXXX
  python scripts/backfill_history.py --network mainnet --derive-only
  python scripts/backfill_history.py --network mainnet --catch-up-from-gap \
      --baseline-provenance "..." --distributor rXXX   # bounded gap recovery (#329)

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
from collections.abc import Mapping
from typing import Any

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))

from xrpl.asyncio.clients import AsyncWebsocketClient  # noqa: E402

from lfg_core import history_store, sponsored_mint  # noqa: E402
from lfg_core.archive_reverify import (  # noqa: E402,F401
    PAGE_LIMIT,
    REQUEST_TIMEOUT,
    REQUIRED_BASELINE_SOURCES,
    RETRY_BASE_DELAY,
    RETRY_MAX,
    RETRYABLE_ERRORS,
    THROTTLE_SECONDS,
    _is_validated_entry,
    _warn_if_unvalidated,
    acquire_certification_lock,
    backfill_account_tx,
    backfill_nft_history,
    baseline_account_coverage,
    baseline_coverage_document,
    make_request_fn,
    nft_ids_in_ledger_range,
    store_raw_tx,
    validate_baseline_endpoint,
    validate_baseline_source_coverage,
)

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
assert REQUIRED_BASELINE_SOURCES <= VALID_SOURCES


def validate_catchup_state(
    state: history_store.ArchiveState | None,
    *,
    expected_source_tag: int | None = None,
    expected_sources: set[str] | frozenset[str] | None = None,
    expected_accounts: Mapping[str, str] | None = None,
) -> int:
    """Admit a bounded catch-up (#329) only when it can be provably sound.

    A catch-up records a cumulative [earliest, tip] baseline while paging only
    the gap window, so it stands on three legs: a prior full certification
    proved [earliest, old tip], the live stream (or that certification) proved
    coverage through `continuity_gap_after`, and the bounded page proves
    [gap_after, tip]. Missing any leg means the cumulative claim would be
    false — refuse and direct the operator to full certification instead.

    The first leg is a claim about BREADTH as well as range: this run records
    cumulative [earliest, tip] coverage for every source and account it
    attests, while paging only the gap. So the prior certification must
    already cover each of them — a source it never swept, or the same source
    swept for a DIFFERENT account, has no pre-gap history in the archive, and
    attesting it would launder that hole into a fresh baseline (an
    already-served wallet could then look eligible for another sponsored
    mint).

    Returns the gap's lower bound (the paging start)."""

    if state is None:
        raise ValueError(
            "no archive_state row: this archive was never certified — "
            "run full certification (--complete-audited-baseline)"
        )
    if (
        state.baseline_ledger_min != history_store.EARLIEST_AVAILABLE_LEDGER
        or state.baseline_ledger_max is None
    ):
        raise ValueError(
            "archive has no prior full-range certification to extend — "
            "run full certification (--complete-audited-baseline)"
        )
    # A prior baseline is only trustworthy ground if its breadth was attested
    # under the current rules: an archive migrated from the pre-SourceTag (or
    # pre-#331 version-1) format keeps its bounds but carries no verifiable
    # coverage document, and sponsored_mint would reject it at admission time
    # anyway. Re-certifying on top of it would launder that unattested history
    # into a fresh v2 baseline.
    prior_sources = sponsored_mint.baseline_coverage_sources(state.baseline_coverage)
    prior_accounts = sponsored_mint.baseline_coverage_accounts(state.baseline_coverage)
    if prior_sources is None or prior_accounts is None:
        raise ValueError(
            "archive baseline carries no verifiable coverage document (legacy or "
            "pre-#331 format); its historical breadth was never attested under the "
            "current rules — run full certification (--complete-audited-baseline)"
        )
    if expected_sources is not None:
        unproven = sorted(set(expected_sources) - set(prior_sources))
        if unproven:
            raise ValueError(
                "prior certification never swept source(s) "
                f"{', '.join(unproven)}, so this archive holds no pre-gap history "
                "for them; a bounded catch-up must not attest coverage it does not "
                "have — run full certification (--complete-audited-baseline)"
            )
    if expected_accounts is not None:
        changed = sorted(
            name
            for name, account in expected_accounts.items()
            if prior_accounts.get(name) != account
        )
        if changed:
            raise ValueError(
                "prior certification covered a different (or no) account for "
                f"{', '.join(changed)}; the new account's pre-gap history was never "
                "paged, so a bounded catch-up must not attest it — run full "
                "certification (--complete-audited-baseline)"
            )
    if expected_source_tag is not None and state.source_tag != expected_source_tag:
        raise ValueError(
            f"archive was certified for SourceTag {state.source_tag}, but this run "
            f"would attest SourceTag {expected_source_tag}; a bounded catch-up must "
            "not rebrand an archive — run full certification for the new tag"
        )
    if state.continuity_gap_at is None:
        raise ValueError(
            "archive records no continuity gap — nothing to catch up "
            "(if the baseline is incomplete for another reason, run full certification)"
        )
    if state.continuity_gap_after is None:
        raise ValueError(
            "continuity gap has no lower bound (continuity_gap_after IS NULL); a bounded "
            "page cannot prove coverage of an unbounded gap — run full certification"
        )
    return state.continuity_gap_after


def catchup_bounds(
    state: history_store.ArchiveState | None,
    snapshot: history_store.EndpointSnapshot,
    *,
    claimed_genesis_hash: str | None = None,
    expected_source_tag: int | None = None,
    expected_sources: set[str] | frozenset[str] | None = None,
    expected_accounts: Mapping[str, str] | None = None,
) -> tuple[int, int]:
    """Resolve the bounded paging window [gap_after, validated tip], fail-closed."""

    gap_after = validate_catchup_state(
        state,
        expected_source_tag=expected_source_tag,
        expected_sources=expected_sources,
        expected_accounts=expected_accounts,
    )
    assert state is not None
    if claimed_genesis_hash is not None and claimed_genesis_hash.strip() != snapshot.genesis_hash:
        raise ValueError("claimed genesis does not match the endpoint chain identity")
    if state.genesis_hash != snapshot.genesis_hash:
        raise ValueError("archive genesis does not match the endpoint chain identity")
    page_min = max(gap_after, history_store.EARLIEST_AVAILABLE_LEDGER)
    if snapshot.validated_ledger_index < page_min:
        raise ValueError(
            "endpoint validated tip is below the gap's lower bound; "
            "the gap cannot be provably covered from this endpoint"
        )
    return page_min, snapshot.validated_ledger_index


def record_catchup_baseline(
    conn: Any,
    *,
    network: str,
    genesis_hash: str,
    tip: int,
    paged_min: int,
    provenance: str,
    sources: set[str] | frozenset[str],
    accounts: dict[str, str],
    source_tag: int,
) -> bool:
    """Record the cumulative [earliest, tip] baseline after a bounded page.

    Only the *paging* was bounded: [earliest, gap_after] is already in the
    archive from the prior certification plus the live stream, and store_raw_tx
    is INSERT OR IGNORE, so bounded-run ∪ existing-archive covers the full
    range. The gap-clearing gate stays in record_archive_baseline unchanged —
    this returns whether the gap actually cleared (it does not when the tip
    stopped short of the gap's upper extent)."""

    coverage = baseline_coverage_document(
        accounts,
        sources=sources,
        source_tag=source_tag,
        ledger_min=history_store.EARLIEST_AVAILABLE_LEDGER,
        ledger_max=tip,
    )
    history_store.record_archive_baseline(
        conn,
        network=network,
        genesis_hash=genesis_hash,
        ledger_min=history_store.EARLIEST_AVAILABLE_LEDGER,
        ledger_max=tip,
        provenance=f"bounded catch-up over [{paged_min}, {tip}]: {provenance}",
        source_tag=source_tag,
        coverage=json.dumps(coverage, sort_keys=True, separators=(",", ":")),
    )
    state = history_store.get_archive_state(conn, network)
    return state is not None and state.baseline_complete and state.continuity_gap_at is None


def skip_burned_before_window(
    oconn: Any,
    hconn: Any,
    ids: list[str],
    window_min: int,
) -> tuple[list[str], int]:
    """Drop tokens provably burned strictly before the catch-up window (#381).

    A token whose burn is already indexed (`onchain_nfts.is_burned=1`) AND
    whose burn event's ledger position (`nft_events`, event='burn') is known
    and strictly below `window_min` cannot have any transaction inside
    [window_min, tip] — paging it is a guaranteed "+0 txs" RPC round-trip.

    Fail-closed: a token is only skipped on positive proof. Not marked burned,
    no recorded burn event, a NULL/unparsable ledger_index, or a burn at/after
    the window's lower bound → the token is paged anyway. Only bounded
    catch-up mode calls this; full certification sweeps everything.

    Returns (ids_to_page, skipped_count)."""

    import sqlite3

    try:
        burned = {r[0] for r in oconn.execute("SELECT nft_id FROM onchain_nfts WHERE is_burned=1")}
    except sqlite3.Error:
        return ids, 0
    kept: list[str] = []
    skipped = 0
    for nft_id in ids:
        if nft_id in burned:
            try:
                row = hconn.execute(
                    "SELECT MAX(ledger_index) FROM nft_events WHERE nft_id=? AND event='burn'",
                    (nft_id,),
                ).fetchone()
            except sqlite3.Error:
                # nft_events missing/unreadable: no proof — page everything.
                return ids, 0
            burn_li = row[0] if row else None
            # A non-positive ledger index is malformed burn evidence, not proof
            # the burn predates the window — fail closed and page the token.
            if isinstance(burn_li, int) and 0 < burn_li < window_min:
                skipped += 1
                continue
        kept.append(nft_id)
    return kept, skipped


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
    parser.add_argument(
        "--catch-up-from-gap",
        action="store_true",
        help="bounded re-certification (#329): page only [continuity_gap_after, validated "
        "tip] and clear the recorded continuity gap, instead of re-paging the full range",
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
    if args.complete_audited_baseline and args.catch_up_from_gap:
        parser.error("--catch-up-from-gap and --complete-audited-baseline are mutually exclusive")
    if args.complete_audited_baseline and any(value is None for value in baseline_values):
        parser.error(
            "--complete-audited-baseline requires --genesis-hash, --baseline-ledger-min, "
            "--baseline-ledger-max, and --baseline-provenance"
        )
    if args.catch_up_from_gap:
        # The paging range comes from archive_state and the endpoint tip, never
        # the operator; the provenance attestation stays mandatory (#329 keeps
        # the human in the loop — no auto self-heal). --genesis-hash is an
        # optional cross-check against the archive's recorded chain identity.
        if args.baseline_ledger_min is not None or args.baseline_ledger_max is not None:
            parser.error("--catch-up-from-gap derives its ledger range; do not pass one")
        if args.baseline_provenance is None:
            parser.error("--catch-up-from-gap requires --baseline-provenance")
    if not args.complete_audited_baseline and not args.catch_up_from_gap:
        if any(value is not None for value in baseline_values):
            parser.error("baseline metadata requires --complete-audited-baseline")
    if args.derive_only and (args.complete_audited_baseline or args.catch_up_from_gap):
        parser.error("--derive-only cannot certify a baseline")
    if args.complete_audited_baseline or args.catch_up_from_gap:
        try:
            validate_baseline_source_coverage(wanted, distributor=args.distributor)
        except ValueError as exc:
            parser.error(str(exc))

    from derive_history_events import issuers_for_network

    net = bf.NETWORKS[args.network]
    clio = net["clio"]
    issuer, brix_issuer = issuers_for_network(args.network)
    issuer = net["issuer"] or issuer
    conn = history_store.init_history_db(history_store.history_db_path(args.network))

    certify_lock = None
    if args.complete_audited_baseline or args.catch_up_from_gap:
        # Cross-process single-flight (#402): the listener's auto catch-up,
        # the service's Start-time auto-reverify and manual runs all serialize
        # on the same advisory flock — never two certification sweeps against
        # one archive. Held (via the open handle) until process exit.
        certify_lock = acquire_certification_lock(history_store.history_db_path(args.network))
        if certify_lock is None:
            logging.error(
                "another certification/catch-up run holds the lock for this archive; "
                "refusing to run concurrently — retry after it finishes"
            )
            return 3

    if args.derive_only:
        from derive_history_events import rederive  # Task 5

        rederive(conn, args.network, distributor=args.distributor)
        return 0

    endpoint_snapshot: history_store.EndpointSnapshot | None = None
    async with AsyncWebsocketClient(clio) as client:
        request_fn = make_request_fn(client)

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
        elif args.catch_up_from_gap:
            archive_state = history_store.get_archive_state(conn, args.network)
            endpoint_snapshot = await history_store.fetch_endpoint_snapshot(request_fn)
            try:
                fixed_ledger_min, fixed_ledger_max = catchup_bounds(
                    archive_state,
                    endpoint_snapshot,
                    claimed_genesis_hash=args.genesis_hash,
                    expected_source_tag=config.SOURCE_TAG,
                    # The breadth this run would attest below — checked against
                    # the prior certification before a single page is fetched.
                    expected_sources=wanted,
                    expected_accounts=baseline_account_coverage(
                        wanted,
                        distributor=args.distributor,
                        nft_issuer=issuer,
                        brix_issuer=brix_issuer,
                    ),
                )
            except ValueError as exc:
                logging.error("bounded catch-up refused: %s", exc)
                return 2
            logging.info(
                "bounded catch-up: paging [%d, %d] (gap stamped at %s: %s)",
                fixed_ledger_min,
                fixed_ledger_max,
                archive_state.continuity_gap_at if archive_state else "?",
                archive_state.continuity_gap_reason if archive_state else "?",
            )

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
            if fixed_ledger_min != -1:
                # The account sweeps above already archived this window, so any
                # token minted while the listener (and thus the index) was down
                # is recoverable from the raw rows.
                extra = sorted(
                    nft_ids_in_ledger_range(
                        conn,
                        nft_issuer=issuer,
                        ledger_min=fixed_ledger_min,
                        ledger_max=fixed_ledger_max,
                    )
                    - set(ids)
                )
                if extra:
                    logging.info(
                        "nft_history: %d token(s) absent from the index were discovered "
                        "in the certified window and will be paged too",
                        len(extra),
                    )
                    ids += extra
            if args.catch_up_from_gap:
                # Bounded window only (#381): a token provably burned before
                # the gap opened cannot have txs inside [gap_after, tip].
                # Full certification runs never narrow.
                before = len(ids)
                ids, skipped = skip_burned_before_window(oconn, conn, ids, fixed_ledger_min)
                if skipped:
                    logging.info(
                        "nft_history: skipping %d of %d token(s) whose indexed burn "
                        "predates the gap window (burn ledger < %d); tokens with an "
                        "unknown burn position are paged anyway (fail-closed)",
                        skipped,
                        before,
                        fixed_ledger_min,
                    )
            total = 0
            for i, nft_id in enumerate(ids, 1):
                total += await backfill_nft_history(
                    conn,
                    request_fn,
                    nft_id,
                    network=args.network,
                    ledger_min=fixed_ledger_min,
                    ledger_max=fixed_ledger_max,
                )
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
    elif args.catch_up_from_gap:
        assert args.baseline_provenance is not None
        assert endpoint_snapshot is not None
        accounts = baseline_account_coverage(
            wanted,
            distributor=args.distributor,
            nft_issuer=issuer,
            brix_issuer=brix_issuer,
        )
        cleared = record_catchup_baseline(
            conn,
            network=args.network,
            genesis_hash=endpoint_snapshot.genesis_hash,
            tip=fixed_ledger_max,
            paged_min=fixed_ledger_min,
            provenance=args.baseline_provenance,
            sources=wanted,
            accounts=accounts,
            source_tag=config.SOURCE_TAG,
        )
        if not cleared:
            logging.error(
                "bounded catch-up recorded a baseline through ledger %d but the "
                "continuity gap did NOT clear (its upper extent lies above the tip "
                "this run certified). The archive stays fail-closed; re-run "
                "--catch-up-from-gap against a fresher tip, or run full certification.",
                fixed_ledger_max,
            )
            return 1
        logging.info(
            "bounded catch-up cleared the continuity gap; certified baseline restored "
            "through ledger %d",
            fixed_ledger_max,
        )
    return 0


def main() -> int:
    return asyncio.run(_amain())


if __name__ == "__main__":
    raise SystemExit(main())
