#!/usr/bin/env python3
"""Rebuild the `market_listings` index from on-ledger NFTokenOffer state.

Sweeps BOTH populations known to the per-network on-chain index --
live `onchain_nfts` characters (is_burned=0) and every `trait_tokens` row --
fetching each token's current sell offers (`xrpl_ops.get_nft_sell_offers`)
and upserting a live `market_listings` row for every sell-flagged,
XRP-denominated offer whose Owner matches the token's CURRENT owner-of-record.
A previously-live row whose offer_index doesn't turn up as a currently-valid
offer anywhere in this sweep (cancelled, accepted, or left dangling by a prior
owner) is closed with reason 'stale'. Rows already closed (is_live=0) --
including a sold-but-unsettled trait row -- are never revisited, so `settled`
survives re-runs untouched.

A per-token fetch FAILURE (RPC/network blip) is not "no offers": failed
tokens are counted in the summary and their rows are excluded from the
stale-close pass, so a transient error can never close a real live listing.

Same posture/conventions as scripts/backfill_onchain.py: per-network
onchain_<network>.db, idempotent re-run, --network testnet|mainnet.

  python scripts/backfill_market.py --network testnet
  python scripts/backfill_market.py --network mainnet
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sqlite3
import sys
from typing import Any

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)

from lfg_core import config, market_ops, market_store, nft_index, xrpl_ops  # noqa: E402
from lfg_core.market_ops import FetchOffers  # noqa: E402

FETCH_CONCURRENCY = 16


async def _fetch_offers_strict(nft_id: str) -> list[dict[str, Any]]:
    """Default fetcher for the sweep: raise_on_error=True so a transient RPC
    failure is distinguishable from "no offers" -- the stale-close pass must
    never close a live listing over a lookup blip."""
    return await xrpl_ops.get_nft_sell_offers(nft_id, raise_on_error=True)


def _matching_sell_offers(offers: list[dict[str, Any]], owner: str) -> list[dict[str, Any]]:
    """Sell-flagged, XRP-denominated offers from `get_nft_sell_offers` whose
    Owner equals the token's CURRENT owner-of-record. Excludes buy offers,
    IOU-denominated offers, and offers left on-ledger by a PREVIOUS owner
    (stale sellers) -- same filters the listener applies at offer_create
    time. Destination-locked offers are NOT excluded here: they are stored
    (browse hides them via `destination IS NULL`), matching how
    nft_listener._apply_offer_create handles them."""
    matches: list[dict[str, Any]] = []
    for offer in offers:
        if not isinstance(offer, dict):
            continue
        flags = offer.get("flags")
        if not isinstance(flags, int) or not (flags & market_ops.LSF_SELL_NFTOKEN):
            continue
        amount = offer.get("amount")
        if not isinstance(amount, str) or not amount.isdigit():
            continue
        if offer.get("owner") != owner:
            continue
        offer_index = offer.get("offer_index")
        if not isinstance(offer_index, str) or not offer_index:
            continue
        matches.append(offer)
    return matches


async def backfill_market(
    conn: sqlite3.Connection,
    fetch_offers: FetchOffers = _fetch_offers_strict,
    concurrency: int = FETCH_CONCURRENCY,
) -> dict[str, int]:
    """Rebuild market_listings from on-ledger sell-offer state. Returns
    summary counts: characters_swept, traits_swept, live_listings (distinct
    offer_indexes confirmed live this sweep), closed_stale, fetch_failures
    (tokens whose offer lookup raised -- their rows are exempt from the
    stale-close pass this run)."""
    market_store.init_db(conn)
    conn.row_factory = sqlite3.Row

    characters = conn.execute(
        "SELECT nft_id, owner FROM onchain_nfts WHERE is_burned = 0"
    ).fetchall()
    traits = conn.execute("SELECT nft_id, owner, slot, value FROM trait_tokens").fetchall()

    sem = asyncio.Semaphore(concurrency)
    failed_nft_ids: set[str] = set()

    async def sweep(
        nft_id: str, owner: str, kind: str, slot: str | None, value: str | None
    ) -> list[str]:
        try:
            async with sem:
                offers = await fetch_offers(nft_id)
        except Exception as e:
            # Failure != "no offers": remember the token so the stale-close
            # pass leaves its rows alone -- closing a live listing over a
            # transient lookup error would be a false stale.
            logging.warning(f"backfill_market: offer fetch failed for {nft_id}: {e}")
            failed_nft_ids.add(nft_id)
            return []
        matches = _matching_sell_offers(offers, owner)
        for offer in matches:
            market_store.upsert_listing(
                conn,
                market_store.MarketListing(
                    offer_index=offer["offer_index"],
                    nft_id=nft_id,
                    kind=kind,
                    seller=owner,
                    amount_drops=int(offer["amount"]),
                    destination=offer.get("destination"),
                    slot=slot,
                    value=value,
                    is_live=1,
                ),
            )
        return [str(offer["offer_index"]) for offer in matches]

    tasks = [sweep(row["nft_id"], row["owner"], "character", None, None) for row in characters]
    tasks += [
        sweep(row["nft_id"], row["owner"], "trait", row["slot"], row["value"]) for row in traits
    ]
    results = await asyncio.gather(*tasks)
    valid_offer_indexes = {idx for group in results for idx in group}

    previously_live = conn.execute(
        "SELECT offer_index, nft_id FROM market_listings WHERE is_live = 1"
    ).fetchall()
    closed = 0
    for row in previously_live:
        if row["nft_id"] in failed_nft_ids:
            continue  # token not successfully swept; can't judge its offers
        if row["offer_index"] not in valid_offer_indexes:
            market_store.close_listing(conn, row["offer_index"], "stale")
            closed += 1

    return {
        "characters_swept": len(characters),
        "traits_swept": len(traits),
        "live_listings": len(valid_offer_indexes),
        "closed_stale": closed,
        "fetch_failures": len(failed_nft_ids),
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Rebuild the market_listings index.")
    # Default parity with scripts/backfill_onchain.py (#130): omitting
    # --network runs against the configured network instead of erroring.
    # argparse never validates a default against choices, so an unexpected
    # XRPL_NETWORK (e.g. "devnet") must make the flag required, not flow
    # through to index_db_path and create the wrong DB.
    choices = ("testnet", "mainnet")
    default = config.XRPL_NETWORK if config.XRPL_NETWORK in choices else None
    parser.add_argument("--network", choices=choices, default=default, required=default is None)
    return parser


async def _amain() -> int:
    args = _build_parser().parse_args()

    conn = nft_index.init_db(nft_index.index_db_path(args.network))
    counts = await backfill_market(conn)

    print(f"Network: {args.network}  DB: {nft_index.index_db_path(args.network)}")
    print(f"  Characters swept: {counts['characters_swept']}")
    print(f"  Traits swept: {counts['traits_swept']}")
    print(f"  Live listings: {counts['live_listings']}")
    print(f"  Closed stale: {counts['closed_stale']}")
    print(f"  Fetch failures (stale-close exempt): {counts['fetch_failures']}")
    return 0


def main() -> int:
    return asyncio.run(_amain())


if __name__ == "__main__":
    raise SystemExit(main())
