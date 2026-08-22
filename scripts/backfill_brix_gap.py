#!/usr/bin/env python3
"""Reimburse the BRIX drip gap (#412) from historical ownership.

  python scripts/backfill_brix_gap.py --network mainnet               # dry run (default)
  python scripts/backfill_brix_gap.py --network mainnet --apply       # write accruals
  python scripts/backfill_brix_gap.py --network testnet --from 2026-08-01 --to 2026-08-10

Window defaults: --from 2025-09-15 (the day after the last real payout run,
see the #412 spec), --to yesterday (UTC). Strict historical: each NFT earns
1 BRIX for each epoch it was live, unlisted and held by a non-system wallet,
credited to the holder at that epoch's close. Rows land in `brix_accruals`
and are claimed through the existing POST /api/brix/claim flow — nothing is
paid here, unclaimed backpay never leaves the treasury.

The nightly cursor (`brix_meta.last_accrued_epoch`) is never touched.
Idempotent: re-running is a no-op; a partial run resumes.

PREREQUISITES (spec §Ops): confirm the derived table is complete (see
docs/ops/brix-gap-backfill.md — the tesSUCCESS-without-nft_events query must
return 0), re-derive the archive first so nft_events carries
offer_index/offer_flags (`scripts/derive_history_events.py --network <net>`),
and make sure the archive is certified for the window (otherwise every epoch
reports DEFERRED and nothing is written).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))

from accrue_brix import _utc_date, system_accounts  # noqa: E402

from lfg_core import brix_backfill, brix_drip, config, history_store, nft_index  # noqa: E402

DEFAULT_FROM = "2025-09-15"


def _yesterday() -> str:
    return (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")


async def _treasury_balance(address: str) -> float | None:
    """Distributor's BRIX balance, or None when unreadable (report-only)."""
    try:
        from lfg_core import xrpl_ops

        balance = await xrpl_ops.get_trustline_balance(
            address, config.BRIX_CURRENCY_HEX, config.BRIX_ISSUER
        )
        return float(balance) if balance is not None else None
    except Exception:  # noqa: BLE001 — the report must not fail on a balance read
        return None


def _print_report(
    network: str,
    plan: brix_backfill.GapPlan,
    *,
    applied: bool,
    liability: int,
    treasury: float | None,
) -> None:
    verb = "WRITTEN" if applied else "WOULD WRITE"
    print(
        f"[{network}] {verb}: {plan.total_brix} BRIX to {len(plan.wallets)} wallets over {plan.nfts} NFTs"
    )
    if applied:
        print(f"[{network}] rows inserted this run: {plan.written}")
    print(f"[{network}] per-epoch (epoch brix listed unknown ineligible):")
    for line in plan.epochs:
        tag = f"  DEFERRED — {line.deferred}" if line.deferred else ""
        print(
            f"  {line.epoch} {line.brix:6d} {line.listed:6d} {line.unknown:6d} "
            f"{line.ineligible:6d}{tag}"
        )
    if plan.top:
        print(f"[{network}] top wallets:")
        for wallet, amount in plan.top:
            print(f"  {wallet} {amount}")
    if plan.deferred:
        print(
            f"[{network}] {len(plan.deferred)} epoch(s) failed certification and were NOT written:"
        )
        for epoch, reason in plan.deferred:
            print(f"  {epoch}: {reason}")
    if plan.owner_drift:
        print(
            f"[{network}] WARNING: {len(plan.owner_drift)} token(s) replay a different owner "
            f"than onchain_nfts — the derived table is incomplete. First 10: "
            f"{', '.join(plan.owner_drift[:10])}"
        )
        print(f"[{network}] fix with: scripts/derive_history_events.py --network {network}")
    unknown_total = sum(line.unknown for line in plan.epochs)
    if unknown_total:
        print(
            f"[{network}] WARNING: {unknown_total} token-epochs skipped on unknown listing state — "
            f"re-run scripts/derive_history_events.py --network {network} first"
        )
    print(
        f"[{network}] outstanding unclaimed liability (incl. this run if applied): {liability} BRIX"
    )
    if treasury is None:
        print(f"[{network}] treasury balance: unavailable")
    else:
        headroom = treasury - liability - (0 if applied else plan.total_brix)
        print(
            f"[{network}] treasury balance: {treasury:.0f} BRIX; headroom after backfill: {headroom:.0f}"
        )


async def _amain() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--network", default=config.XRPL_NETWORK, choices=["testnet", "mainnet"])
    ap.add_argument(
        "--from", dest="start", type=_utc_date, default=DEFAULT_FROM, metavar="YYYY-MM-DD"
    )
    ap.add_argument("--to", dest="end", type=_utc_date, default=None, metavar="YYYY-MM-DD")
    ap.add_argument("--apply", action="store_true", help="write accrual rows (default: dry run)")
    ap.add_argument("--top", type=int, default=20, help="top-N wallets to print")
    ap.add_argument(
        "--treasury",
        default=config.BRIX_DISTRIBUTOR_ADDRESS,
        help="wallet whose BRIX balance to compare against (default: distributor)",
    )
    args = ap.parse_args()
    end = args.end or _yesterday()
    if args.network != config.XRPL_NETWORK:
        print(f"refusing: --network {args.network} but XRPL_NETWORK is {config.XRPL_NETWORK}")
        return 2

    hconn = history_store.init_history_db(history_store.history_db_path(args.network))
    brix_drip.ensure_schema(hconn)
    chain_error = await brix_drip.verify_endpoint_chain(hconn, args.network)
    if chain_error:
        print(f"refusing: {chain_error}")
        return 2

    # Drip eligibility = the collection index, never the raw archive (#411 C1).
    index_path = nft_index.index_db_path(args.network)
    oconn = nft_index.init_db(index_path)
    eligible = nft_index.collection_owners(oconn)
    if not eligible:
        # Same trap as the nightly: init_db creates an empty file. With an
        # empty map every token is ineligible and the plan is a confident
        # "nothing owed" — refuse before planning anything.
        print(
            f"refusing: the collection index at {index_path} is empty. "
            f"Check ONCHAIN_DB_PATH / --network, and that the listener has run."
        )
        return 2

    plan = brix_backfill.plan_gap_backfill(
        hconn,
        args.network,
        system_accounts(),
        start=args.start,
        end=end,
        apply=args.apply,
        eligible=eligible,
        top_n=args.top,
    )
    if plan.refused:
        # --apply refused BEFORE any write (#411 C2/I1).
        print(f"[{args.network}] REFUSED: {plan.refused}")
        if plan.owner_drift:
            print(
                f"[{args.network}] {len(plan.owner_drift)} drifting token(s); first 10: "
                f"{', '.join(plan.owner_drift[:10])}"
            )
        print(f"[{args.network}] nothing was written.")
        return 2
    liability = hconn.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM brix_accruals WHERE claim_id IS NULL"
    ).fetchone()[0]
    treasury = await _treasury_balance(args.treasury) if args.treasury else None
    _print_report(
        args.network, plan, applied=args.apply, liability=int(liability), treasury=treasury
    )
    if not args.apply:
        print(f"[{args.network}] dry run — re-run with --apply to write")
    return 0


def main() -> int:
    return asyncio.run(_amain())


if __name__ == "__main__":
    raise SystemExit(main())
