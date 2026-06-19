#!/usr/bin/env python3
"""Backfill the per-nft_id on-chain NFT index from clio.

Pages nfts_by_issuer for the collection, fetches each token's metadata, and
upserts every token (live + burned, incl. duplicate editions) into the
per-network index DB (onchain_<network>.db). Idempotent — re-runs refresh rows.

  python scripts/backfill_onchain.py --network testnet
  python scripts/backfill_onchain.py --network mainnet
  python scripts/backfill_onchain.py --issuer r... --taxon 1760 --clio wss://...

Mainnet metadata is on IPFS (slow/flaky); tokens whose metadata can't be fetched
are still recorded (with empty attributes), never dropped.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sqlite3
import sys
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)

from lfg_core import nft_index, swap_meta  # noqa: E402

# Per-network enumeration defaults (mirrors the auditor).
NETWORKS: dict[str, dict[str, Any]] = {
    "mainnet": {
        "issuer": "rLfgoMintj3KBcs4s2XKtquvDwEte2kYfJ",
        "taxon": 1760,
        "clio": "wss://s2-clio.ripple.com",
    },
    "testnet": {
        "issuer": None,  # from config.SWAP_ISSUER_ADDRESS
        "taxon": 1760,
        "clio": "wss://clio.altnet.rippletest.net:51233",
    },
}

FETCH_CONCURRENCY = 16


async def run_backfill(
    conn: sqlite3.Connection,
    enumerate_fn: Callable[[], Awaitable[list[dict[str, Any]]]],
    fetch_meta_fn: Callable[[str], Awaitable[dict[str, Any] | None]],
    concurrency: int = FETCH_CONCURRENCY,
) -> dict[str, int]:
    """Enumerate all tokens, fetch metadata, upsert each. Returns counts.
    enumerate_fn/fetch_meta_fn are injected to keep this unit-testable."""
    tokens = await enumerate_fn()
    sem = asyncio.Semaphore(concurrency)

    async def record(token: dict[str, Any]) -> bool:
        uri_hex = token.get("uri_hex") or ""
        metadata = None
        if uri_hex:
            async with sem:
                metadata = await fetch_meta_fn(uri_hex)
        rec = nft_index.token_record(token, metadata)
        nft_index.upsert(conn, rec)
        return metadata is not None

    results = await asyncio.gather(*(record(t) for t in tokens))
    with_meta = sum(1 for ok in results if ok)
    return {"total": len(tokens), "with_metadata": with_meta, "unreadable": len(tokens) - with_meta}


async def _amain() -> int:
    from lfg_core import config

    parser = argparse.ArgumentParser(description="Backfill the on-chain NFT index.")
    parser.add_argument("--network", choices=sorted(NETWORKS), default=config.XRPL_NETWORK)
    parser.add_argument("--issuer")
    parser.add_argument("--taxon", type=int)
    parser.add_argument("--clio")
    args = parser.parse_args()

    net = NETWORKS[args.network]
    issuer = args.issuer or net["issuer"] or config.SWAP_ISSUER_ADDRESS
    taxon = args.taxon if args.taxon is not None else net["taxon"]
    clio = args.clio or net["clio"]

    conn = nft_index.init_db(nft_index.index_db_path(args.network))

    async def enum() -> list[dict[str, Any]]:
        return await nft_index.enumerate_tokens(clio, issuer, taxon)

    async with aiohttp.ClientSession() as http:

        async def fetch(uri_hex: str) -> dict[str, Any] | None:
            return await swap_meta.fetch_metadata(uri_hex, http)

        counts = await run_backfill(conn, enum, fetch)

    print(f"Network: {args.network}  issuer: {issuer}  taxon: {taxon}")
    print(f"  DB: {nft_index.index_db_path(args.network)}")
    print(f"  Tokens indexed: {counts['total']}")
    print(f"  With metadata: {counts['with_metadata']}")
    print(f"  Unreadable metadata: {counts['unreadable']}")
    return 0


def main() -> int:
    return asyncio.run(_amain())


if __name__ == "__main__":
    raise SystemExit(main())
