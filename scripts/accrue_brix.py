#!/usr/bin/env python3
"""Daily BRIX drip accrual (#48): 1 BRIX per unlisted live NFT per UTC day.

  python scripts/accrue_brix.py --network testnet
  python scripts/accrue_brix.py --network mainnet --date 2026-08-18

Replays the history archive (`epoch_state`) for owner-of-record and
listed-state as of each epoch's close — zero per-token RPCs (#411) — and
writes accrual rows to `history_<net>.db`. Nothing is paid here — accruals
are DB-only until a holder claims (see POST /api/brix/claim).

Idempotent twice over: the accruals table's PK makes a re-run a no-op, and a
`brix_meta` cursor means a missed cron day is caught up automatically on the
next run. Safe to run repeatedly.

Registered in ecosystem.prod.config.js / ecosystem.staging.config.js as
lfg-brix-accrue / stg-brix-accrue. The slot is 00:40 UTC, not the 00:20 this
docstring once suggested: 00:20 already holds lfg-economy-reconcile and
lfg-sourcetag. Since #411 this job is a DB-only archive replay (no per-token
RPC sweep), so the slot no longer needs to absorb real per-token lookup time,
but the offset is left as-is. Manual equivalent:

  pm2 start scripts/accrue_brix.py --name lfg-brix-accrue \
    --cron "40 0 * * *" --no-autorestart --interpreter .venv/bin/python \
    -- --network mainnet

Like every cron entry it parks "stopped" between runs — normal, not a failure.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)

from lfg_core import brix_drip, config, history_store, nft_index  # noqa: E402


def system_accounts() -> frozenset[str]:
    """Wallets that must never earn a drip — the same set the leaderboards
    exclude, so system-held inventory can't farm the distribution."""
    return frozenset(
        a
        for a in (
            config.SWAP_ISSUER_ADDRESS,
            config.SWAP_OFFER_ISSUER,
            config.BRIX_ISSUER,
            config.BRIX_DISTRIBUTOR_ADDRESS,
            config.BRIX_AMM_ACCOUNT,
            config.SIGNING_ACCOUNT,
        )
        if a
    )


def _utc_date(value: str) -> str:
    """Reject a malformed --date at parse time.

    Otherwise the bad value only blows up inside run_archive_accrual — after
    both databases are open and the archive replay has already been walked.
    """
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"expected a UTC date as YYYY-MM-DD, got {value!r}"
        ) from None
    # Return the RE-FORMATTED value, not the input. strptime happily accepts
    # "2026-8-1", which would then become the epoch_date key verbatim — and an
    # unpadded key neither sorts against padded ones nor collides with them on
    # the primary key, so the same day could accrue twice.
    return parsed.strftime("%Y-%m-%d")


async def _amain() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--network", default=config.XRPL_NETWORK, choices=["testnet", "mainnet"])
    ap.add_argument(
        "--date",
        type=_utc_date,
        metavar="YYYY-MM-DD",
        help="Treat this UTC date as 'today'; epochs are accrued up to the day before it.",
    )
    args = ap.parse_args()

    # Fail closed on a split network. The DB paths follow --network, and the
    # archive replay reads owner-of-record / listed-state purely from
    # history_<net>.db — but the chain-identity check below still guards that
    # this archive is actually the one for --network, not a stale/foreign one.
    # Same seam the marketplace gates trait on-ledger ops behind
    # (ECONOMY_NETWORK == XRPL_NETWORK).
    if args.network != config.XRPL_NETWORK:
        print(
            f"refusing to accrue: --network {args.network} but XRPL_NETWORK is "
            f"{config.XRPL_NETWORK}; the archive would be read against the wrong "
            f"chain identity. Re-run with XRPL_NETWORK={args.network}."
        )
        return 2

    hconn = history_store.init_history_db(history_store.history_db_path(args.network))
    brix_drip.ensure_schema(hconn)

    # The name check above cannot see an XRPL_JSON_RPC_URL override, so also
    # confirm the endpoint's actual chain identity before trusting the
    # archive. Fail-closed: on the wrong chain the archive's replayed history
    # doesn't correspond to this network at all.
    chain_error = await brix_drip.verify_endpoint_chain(hconn, args.network)
    if chain_error:
        print(f"refusing to accrue: {chain_error}")
        return 2

    excluded = system_accounts()
    # No per-token sweep (#411 option 2): owner-of-record and listed-state
    # come from the history archive as of each epoch's close. Zero RPCs;
    # DB-bound.
    # Eligibility is the COLLECTION index, not the archive (#411 C1):
    # nft_events also carries soulbound Closet and tradeable trait tokens,
    # which were never drip-eligible.
    oconn = nft_index.init_db(nft_index.index_db_path(args.network))
    eligible = nft_index.collection_owners(oconn)
    reports = brix_drip.run_archive_accrual(
        hconn, args.network, excluded, today=args.date, eligible=eligible
    )
    if not reports:
        print(f"[{args.network}] nothing to accrue — cursor is current")
        return 0

    for r in reports:
        if r.deferred:
            print(
                f"[{args.network}] {r.epoch}: DEFERRED — {r.deferred}. Nothing written; the "
                f"cursor stays at the last certified epoch and this run will complete it later."
            )
            continue
        print(
            f"[{args.network}] {r.epoch}: accrued={r.accrued} listed={r.skipped_listed} "
            f"system={r.skipped_system} burned={r.skipped_burned} "
            f"ownerless={r.skipped_ownerless} unknown={r.unknown} "
            f"ineligible={r.skipped_ineligible} owner_drift={r.owner_drift}"
        )
        if r.owner_drift:
            print(
                f"[{args.network}] WARNING: {r.owner_drift} token(s) replay a different owner "
                f"than onchain_nfts — the derived table is missing events, so they were NOT "
                f"paid. Run scripts/derive_history_events.py --network {args.network}, then "
                f"scripts/backfill_brix_gap.py to reimburse."
            )
        if r.unknown:
            # Fail-closed under-accrual: listing state could not be
            # reconstructed for these tokens (legacy nft_events rows without
            # offer_index / offer_flags). The PK means a later run will NOT
            # retroactively grant them — rebuild the derived table, then the
            # gap backfill can.
            print(
                f"[{args.network}] WARNING: {r.unknown} tokens skipped on unknown listing state — "
                f"run scripts/derive_history_events.py --network {args.network} to populate "
                f"offer_index/offer_flags, then scripts/backfill_brix_gap.py for the missed epochs"
            )

    outstanding = hconn.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM brix_accruals WHERE claim_id IS NULL"
    ).fetchone()[0]
    print(f"[{args.network}] total unclaimed liability: {outstanding} BRIX")
    return 0


def main() -> int:
    return asyncio.run(_amain())


if __name__ == "__main__":
    raise SystemExit(main())
