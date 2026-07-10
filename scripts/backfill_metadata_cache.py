#!/usr/bin/env python3
"""Backfill the uri_metadata_cache for every live token in the on-chain index.

The swapper roster (/api/nfts) is served from local data: tokens from the
on-chain index, metadata from the uri_metadata_cache. A token whose metadata
is missing from the cache costs a public-IPFS-gateway fetch on the hot path —
and drops out of the roster entirely when the gateway flakes. This script
fills the cache up front so the hot path never needs the network:

  python scripts/backfill_metadata_cache.py --network mainnet --csv LFGOdata.csv

Sources, in order:
  1. Case migration: pre-normalization cache rows were keyed with the
     ledger's UPPERCASE hex URIs while the index stores lowercase; fold them
     into the canonical lowercase form (nft_index.migrate_meta_cache_case).
  2. Bithomp CSV (offline): rows whose URI is ipfs:// — legacy mints, whose
     metadata is immutable and carries no burnCount — are reconstructed from
     the CSV's pre-parsed Name/Image/Video/Attribute columns. CSV rows with
     http(s) URIs are deliberately NOT trusted: swap outputs carry a
     burnCount the CSV lacks, and a wrong burnCount would collide upload
     basenames on the next swap.
  3. Live fetch (optional, --no-fetch to skip): whatever is still missing is
     fetched via nft_index.fetch_metadata_multi (multi-gateway for ipfs://,
     direct for CDN URLs).

Idempotent and resumable: already-cached URIs are never re-fetched; each
fetched batch commits as it lands, so Ctrl-C and re-run is safe.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import sqlite3
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, REPO_ROOT)

import aiohttp  # noqa: E402

from lfg_core import nft_index, swap_meta  # noqa: E402

_ATTR_PREFIX = "Attribute "
# Commit granularity for the live-fetch pass (resumability beats batching).
_FETCH_COMMIT_EVERY = 25


def csv_metadata(row: dict[str, str]) -> tuple[str, dict[str, Any]] | None:
    """(lowercase uri_hex, metadata dict) reconstructed from a Bithomp CSV
    row, or None for rows this source can't be trusted for: no URI, no Name,
    or a non-ipfs:// URI (swap outputs carry a burnCount the CSV lacks).
    The dict carries exactly the fields swap_meta.normalize_nft consumes;
    burnCount is legitimately absent for legacy ipfs mints (defaults to 0)."""
    uri = (row.get("URI") or "").strip()
    name = (row.get("Name") or "").strip()
    if not uri or not name or not uri.startswith("ipfs://"):
        return None
    attributes = swap_meta.normalize_attributes(
        [
            {"trait_type": key[len(_ATTR_PREFIX) :], "value": (value or "").strip()}
            for key, value in row.items()
            if key and key.startswith(_ATTR_PREFIX)
        ]
    )
    meta: dict[str, Any] = {
        "name": name,
        "image": (row.get("Image") or "").strip(),
        "video": (row.get("Video") or "").strip() or None,
        "attributes": attributes,
    }
    return uri.encode("ascii", "ignore").hex().lower(), meta


def live_uri_hexes(conn: sqlite3.Connection) -> list[str]:
    """Every distinct uri_hex carried by a live token, lowercased."""
    cur = conn.execute(
        "SELECT DISTINCT LOWER(uri_hex) FROM onchain_nfts"
        " WHERE is_burned = 0 AND uri_hex IS NOT NULL AND uri_hex != ''"
    )
    return [row[0] for row in cur.fetchall()]


def missing_uri_hexes(conn: sqlite3.Connection, uri_hexes: list[str]) -> list[str]:
    cached = nft_index.meta_cache_get_many(conn, uri_hexes)
    return [u for u in uri_hexes if u not in cached]


def apply_csv(conn: sqlite3.Connection, path: str, missing: set[str]) -> int:
    """Cache every CSV-reconstructable metadata dict for URIs in `missing`.
    Returns the number cached."""
    metas: dict[str, dict[str, Any]] = {}
    with open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            got = csv_metadata(row)
            if got and got[0] in missing:
                metas[got[0]] = got[1]
    nft_index.meta_cache_put_many(conn, metas)
    return len(metas)


async def fetch_missing(conn: sqlite3.Connection, missing: list[str]) -> int:
    """Fetch metadata for the leftover URIs (multi-gateway ipfs / direct CDN),
    committing as batches land. Returns the number cached."""
    fetched = 0
    async with aiohttp.ClientSession() as http:
        for i in range(0, len(missing), _FETCH_COMMIT_EVERY):
            batch = missing[i : i + _FETCH_COMMIT_EVERY]
            results = await asyncio.gather(
                *[nft_index.fetch_metadata_multi(http, u) for u in batch]
            )
            good = {u: m for u, m in zip(batch, results, strict=False) if isinstance(m, dict)}
            nft_index.meta_cache_put_many(conn, good)
            fetched += len(good)
            done = min(i + _FETCH_COMMIT_EVERY, len(missing))
            print(f"  fetch: {done}/{len(missing)} tried, {fetched} cached", flush=True)
    return fetched


def run(conn: sqlite3.Connection, csv_paths: list[str], fetch: bool = True) -> dict[str, int]:
    migrated = nft_index.migrate_meta_cache_case(conn)
    uris = live_uri_hexes(conn)
    missing = missing_uri_hexes(conn, uris)
    stats = {
        "live_uris": len(uris),
        "already_cached": len(uris) - len(missing),
        "case_migrated": migrated,
        "from_csv": 0,
        "fetched": 0,
    }
    for path in csv_paths:
        stats["from_csv"] += apply_csv(conn, path, set(missing))
        missing = missing_uri_hexes(conn, uris)
    if fetch and missing:
        stats["fetched"] = asyncio.get_event_loop().run_until_complete(fetch_missing(conn, missing))
        missing = missing_uri_hexes(conn, uris)
    stats["still_missing"] = len(missing)
    return stats


def main() -> int:
    from lfg_core import config

    parser = argparse.ArgumentParser(description="Backfill the uri_metadata_cache.")
    parser.add_argument("--network", choices=["mainnet", "testnet"], default=config.XRPL_NETWORK)
    parser.add_argument(
        "--csv",
        action="append",
        default=[],
        help="Bithomp CSV export (repeatable); offline source for legacy ipfs:// rows",
    )
    parser.add_argument(
        "--no-fetch",
        action="store_true",
        help="skip the live-fetch pass (offline sources only)",
    )
    args = parser.parse_args()

    db_path = nft_index.index_db_path(args.network)
    conn = nft_index.init_db(db_path)
    try:
        stats = run(conn, args.csv, fetch=not args.no_fetch)
    finally:
        conn.close()
    print(f"Network: {args.network}  DB: {db_path}")
    print(
        f"  Live URIs: {stats['live_uris']}  Already cached: {stats['already_cached']}"
        f"  Case-migrated: {stats['case_migrated']}"
    )
    print(
        f"  From CSV: {stats['from_csv']}  Fetched: {stats['fetched']}"
        f"  Still missing: {stats['still_missing']}"
    )
    return 0 if stats["still_missing"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
