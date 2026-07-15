#!/usr/bin/env python3
"""Rebuild the trait-economy tables (closet_tokens / closet_assets /
closet_bodies / trait_tokens) from on-ledger NFToken state.

Enumerates the collection issuer's Closet taxons (config.CLOSET_TAXON and the
legacy config.LEGACY_BUCKET_TAXON) and the tradeable-trait taxon
(config.TRAIT_TAXON) via `nfts_by_issuer`, fetches each token's metadata, and
reconciles the per-network economy tables in onchain_<network>.db:

  * a live Closet NFToken rebuilds its owner's closet_assets/closet_bodies from
    the token metadata and (re)records its lifecycle status (a token held by
    anyone other than the issuer is `active`; one still issuer-held is
    `pending_accept`) -- exactly what the listener's _apply_closet does, done
    here from the authoritative on-ledger snapshot.
  * a live trait NFToken upserts its trait_tokens row (owner + slot/value from
    metadata); a trait_tokens row NOT backed by a live on-ledger token (burned,
    deposited, or transferred out of the taxon) is deleted as stale.

ISSUER-GATED BY CONSTRUCTION: enumeration is `nfts_by_issuer` for
config.SWAP_ISSUER_ADDRESS, so no foreign taxon-1762/176 token a third party
minted can ever enter the reconcile (the forged-Closet/trait attack surface #178
closes for the live listener). Consistent with #178's gating intent; this script
deliberately does NOT reuse nft_listener._apply_closet/_apply_trait_token (which
#178 owns) so the two changes don't collide.

Idempotent: safe to re-run. Same posture/conventions as scripts/backfill_market.py
and scripts/backfill_onchain.py (per-network onchain_<network>.db,
--network testnet|mainnet).

  python scripts/backfill_economy.py --network testnet
"""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

REPO_ROOT = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, REPO_ROOT)

import aiohttp  # noqa: E402

from lfg_core import closet_token, config, economy_store, nft_index, trait_token  # noqa: E402

FETCH_CONCURRENCY = 16

# Per-network clio endpoints (mirrors scripts/backfill_onchain.py). Kept explicit
# rather than config.CLIO_WS_URL so an explicit --network that differs from the
# configured network still enumerates from the right chain.
CLIO: dict[str, str] = {
    "mainnet": "wss://s2-clio.ripple.com",
    "testnet": "wss://clio.altnet.rippletest.net:51233",
}

EnumerateFn = Callable[[int], Awaitable[list[dict[str, Any]]]]
FetchMetaFn = Callable[[str], Awaitable[dict[str, Any] | None]]


def _reconcile_closet(
    conn: sqlite3.Connection, token: dict[str, Any], metadata: Any, issuer: str
) -> bool:
    """Rebuild one owner's Closet contents + token record from an on-ledger
    Closet NFToken snapshot. Returns True if applied, False if skipped.

    Skips (leaves the DB untouched) in two cases the naive rebuild would corrupt:
      * still issuer-held (pending_accept): the on-ledger `owner` is the issuer,
        NOT the user the offer targets — which we can't derive here. The DB
        already holds the pending record from ensure_closet; rebuilding under the
        issuer address would strand the real user's Closet. The listener promotes
        it on accept (#190).
      * unreadable metadata (transient fetch failure / missing uri): forward-only,
        never treat a failed read as an empty closet — set_closet_contents([], [])
        would wipe the owner's real rows and clear mirror_pending (#190)."""
    owner = token.get("owner")
    if not owner:
        return False
    if owner == issuer:
        # pending_accept: owner-of-record is the issuer, not the real user. Don't
        # rebuild under the issuer, and scrub any bogus issuer-keyed Closet row a
        # prior buggy run may have left, so a rerun repairs rather than strands
        # the real user's pending Closet (#190).
        economy_store.delete_closet(conn, issuer)
        return False
    if not isinstance(metadata, dict):
        return False  # unreadable read must not masquerade as an empty closet
    assets, bodies = closet_token.parse_closet_metadata(metadata)
    economy_store.set_closet_contents(conn, owner, assets, bodies)
    status = closet_token.ACTIVE if owner != issuer else closet_token.PENDING_ACCEPT
    existing = economy_store.get_closet_record(conn, owner)
    existing_offer_id = existing[3] if existing is not None else None
    economy_store.set_closet_token(
        conn,
        owner,
        token["nft_id"],
        token.get("uri_hex") or "",
        status=status,
        offer_id=existing_offer_id,
    )
    return True


def _reconcile_trait(conn: sqlite3.Connection, token: dict[str, Any], metadata: Any) -> bool:
    """Upsert one trait_tokens row from a live on-ledger trait NFToken. Returns
    True if applied (owner present + parseable slot/value metadata)."""
    owner = token.get("owner")
    parsed = trait_token.parse_trait_metadata(metadata if isinstance(metadata, dict) else {})
    if owner and parsed:
        economy_store.upsert_trait_token(conn, token["nft_id"], owner, parsed[0], parsed[1])
        return True
    return False


async def backfill_economy(
    conn: sqlite3.Connection,
    enumerate_fn: EnumerateFn,
    fetch_meta_fn: FetchMetaFn,
    *,
    issuer: str,
    concurrency: int = FETCH_CONCURRENCY,
) -> dict[str, int]:
    """Reconcile the economy tables from on-ledger state. `enumerate_fn(taxon)`
    returns every token (live + burned) for that taxon under our issuer;
    `fetch_meta_fn(uri_hex)` resolves metadata. Both injected so this is
    unit-testable without a network. Returns summary counts."""
    economy_store.init_economy_schema(conn)
    sem = asyncio.Semaphore(concurrency)

    async def _meta(uri_hex: str) -> dict[str, Any] | None:
        if not uri_hex:
            return None
        async with sem:
            return await fetch_meta_fn(uri_hex)

    # --- Closets: taxon 1762 (+ legacy 1761). Forward-only reconcile: a Closet
    #     is soulbound, so we rebuild what is on-ledger rather than risk deleting
    #     a valid owner's row on an incomplete enumeration. ---
    closet_taxons: list[int] = []
    for taxon in (config.CLOSET_TAXON, config.LEGACY_BUCKET_TAXON):
        if taxon not in closet_taxons:
            closet_taxons.append(taxon)

    closets_seen = 0
    closets_applied = 0
    for taxon in closet_taxons:
        tokens = await enumerate_fn(taxon)
        live = [t for t in tokens if not t.get("is_burned")]
        closets_seen += len(live)
        metas = await asyncio.gather(*(_meta(t.get("uri_hex") or "") for t in live))
        for token, meta in zip(live, metas, strict=True):
            if _reconcile_closet(conn, token, meta, issuer):
                closets_applied += 1

    # --- Trait tokens: taxon 176 (config.TRAIT_TAXON, flipped from 1763 per
    #     #217). Upsert live tokens, then stale-delete any DB
    #     row NOT backed by a live on-ledger token (burned/deposited/gone). A
    #     live-but-unreadable-metadata token stays in live_ids (membership is by
    #     the enumeration's is_burned flag, not metadata) so it is never
    #     wrongly dropped. ---
    trait_enum = await enumerate_fn(config.TRAIT_TAXON)
    live_traits = [t for t in trait_enum if not t.get("is_burned")]
    live_trait_ids = {t["nft_id"] for t in live_traits if t.get("nft_id")}

    metas = await asyncio.gather(*(_meta(t.get("uri_hex") or "") for t in live_traits))
    traits_upserted = 0
    for token, meta in zip(live_traits, metas, strict=True):
        if _reconcile_trait(conn, token, meta):
            traits_upserted += 1

    traits_deleted_stale = 0
    db_trait_ids = [r[0] for r in conn.execute("SELECT nft_id FROM trait_tokens").fetchall()]
    for nft_id in db_trait_ids:
        if nft_id not in live_trait_ids:
            economy_store.delete_trait_token(conn, nft_id)
            traits_deleted_stale += 1

    return {
        "closets_seen": closets_seen,
        "closets_applied": closets_applied,
        "closets_skipped": closets_seen - closets_applied,
        "traits_seen": len(trait_enum),
        "traits_live": len(live_traits),
        "traits_upserted": traits_upserted,
        "traits_deleted_stale": traits_deleted_stale,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Reconcile the trait-economy tables from chain.")
    # Default parity with scripts/backfill_market.py: omitting --network runs
    # against the ECONOMY network (economy tables live in onchain_<net>.db and
    # the economy resolves reads via ECONOMY_NETWORK). argparse never validates a
    # default against choices, so an ECONOMY_NETWORK outside the known networks
    # must make the flag required rather than flow through to the wrong DB.
    choices = ("testnet", "mainnet")
    default = config.ECONOMY_NETWORK if config.ECONOMY_NETWORK in choices else None
    parser.add_argument("--network", choices=choices, default=default, required=default is None)
    return parser


async def _amain() -> int:
    args = _build_parser().parse_args()

    issuer = config.SWAP_ISSUER_ADDRESS
    clio = CLIO[args.network]
    conn = nft_index.init_db(nft_index.index_db_path(args.network))

    async def enum(taxon: int) -> list[dict[str, Any]]:
        return await nft_index.enumerate_tokens(clio, issuer, taxon)

    async with aiohttp.ClientSession() as http:

        async def fetch(uri_hex: str) -> dict[str, Any] | None:
            return await nft_index.fetch_metadata_multi(http, uri_hex)

        counts = await backfill_economy(conn, enum, fetch, issuer=issuer)

    print(f"Network: {args.network}  issuer: {issuer}")
    print(f"  DB: {nft_index.index_db_path(args.network)}")
    print(f"  Closets seen (live): {counts['closets_seen']}")
    print(f"  Closets applied: {counts['closets_applied']}")
    print(f"  Closets skipped (pending/unreadable): {counts['closets_skipped']}")
    print(f"  Trait tokens seen (all): {counts['traits_seen']}")
    print(f"  Trait tokens live: {counts['traits_live']}")
    print(f"  Trait tokens upserted: {counts['traits_upserted']}")
    print(f"  Trait rows deleted stale: {counts['traits_deleted_stale']}")
    return 0


def main() -> int:
    return asyncio.run(_amain())


if __name__ == "__main__":
    raise SystemExit(main())
