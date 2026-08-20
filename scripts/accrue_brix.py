#!/usr/bin/env python3
"""Daily BRIX drip accrual (#48): 1 BRIX per unlisted live NFT per UTC day.

  python scripts/accrue_brix.py --network testnet
  python scripts/accrue_brix.py --network mainnet --date 2026-08-18

Reads live tokens from `onchain_<net>.db`, checks each one's live sell offers
on-ledger, and writes accrual rows to `history_<net>.db`. Nothing is paid here
— accruals are DB-only until a holder claims (see POST /api/brix/claim).

Idempotent twice over: the accruals table's PK makes a re-run a no-op, and a
`brix_meta` cursor means a missed cron day is caught up automatically on the
next run. Safe to run repeatedly.

pm2 cron registration (mirrors lfg-snapshot; deliberately at 00:20 UTC, after
the 00:10 snapshot):

  pm2 start scripts/accrue_brix.py --name lfg-brix-accrue \
    --cron "20 0 * * *" --no-autorestart --interpreter .venv/bin/python \
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

from lfg_core import brix_drip, config, history_store, nft_index, system_wallets  # noqa: E402


def system_accounts() -> frozenset[str]:
    """Wallets that must never earn a drip — the same set the leaderboards
    exclude, so system-held inventory can't farm the distribution.

    Retired project wallets are unioned in durably (#414) — a wallet dropped
    from a config slot is still ours and still must not earn."""
    return system_wallets.with_durable(
        frozenset(
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
    )


def _utc_date(value: str) -> str:
    """Reject a malformed --date at parse time.

    Otherwise the bad value only blows up inside run_accrual — after both
    databases are open and every sell-offer lookup has already been paid for.
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

    # Fail closed on a split network. The DB paths follow --network, but the
    # offer lookups go through xrpl_ops' single-network JSON_RPC_URL globals.
    # Mismatched, every cross-network NFT id would come back "no offers" — and
    # since unlisted is what PAYS, that silently grants BRIX to listed tokens,
    # inverting the whole fail-closed posture. Same seam the marketplace gates
    # trait on-ledger ops behind (ECONOMY_NETWORK == XRPL_NETWORK).
    if args.network != config.XRPL_NETWORK:
        print(
            f"refusing to accrue: --network {args.network} but XRPL_NETWORK is "
            f"{config.XRPL_NETWORK}; offer lookups would run against the wrong chain "
            f"and pay listed tokens. Re-run with XRPL_NETWORK={args.network}."
        )
        return 2

    oconn = nft_index.init_db(nft_index.index_db_path(args.network))
    hconn = history_store.init_history_db(history_store.history_db_path(args.network))
    brix_drip.ensure_schema(hconn)

    # The name check above cannot see an XRPL_JSON_RPC_URL override, so also
    # confirm the endpoint's actual chain identity before trusting any offer
    # lookup. Fail-closed: on the wrong chain every token looks unlisted, and
    # unlisted is what pays.
    chain_error = await brix_drip.verify_endpoint_chain(hconn, args.network)
    if chain_error:
        print(f"refusing to accrue: {chain_error}")
        return 2

    excluded = system_accounts()
    tokens = nft_index.live_nfts(oconn)
    # Only look up listing state for tokens that could actually earn — a clio
    # round trip per system-held or ownerless token is pure waste.
    holders = {
        t.nft_id: t.owner for t in tokens if t.owner and not t.is_burned and t.owner not in excluded
    }
    print(f"[{args.network}] {len(tokens)} live tokens, {len(holders)} eligible; checking offers…")
    listing_state = await brix_drip.fetch_sell_offer_state(holders)

    reports = brix_drip.run_accrual(
        hconn,
        tokens,
        listed_fn=lambda nft_id: listing_state.get(nft_id),
        system_accounts=excluded,
        today=args.date,
    )
    if not reports:
        print(f"[{args.network}] nothing to accrue — cursor is current")
        return 0

    for r in reports:
        print(
            f"[{args.network}] {r.epoch}: accrued={r.accrued} listed={r.skipped_listed} "
            f"system={r.skipped_system} burned={r.skipped_burned} "
            f"ownerless={r.skipped_ownerless} unknown={r.unknown}"
        )
        if r.unknown:
            # Fail-closed under-accrual: these tokens earned nothing because
            # their offer state could not be read, and the PK means a re-run
            # will NOT retroactively grant them (spec §10).
            print(f"[{args.network}] WARNING: {r.unknown} tokens skipped on unknown offer state")

    outstanding = hconn.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM brix_accruals WHERE claim_id IS NULL"
    ).fetchone()[0]
    print(f"[{args.network}] total unclaimed liability: {outstanding} BRIX")
    return 0


def main() -> int:
    return asyncio.run(_amain())


if __name__ == "__main__":
    raise SystemExit(main())
