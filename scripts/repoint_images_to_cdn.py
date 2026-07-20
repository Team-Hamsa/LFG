#!/usr/bin/env python3
"""Repoint on-chain-index image URLs to the working CDN URL.

`onchain_nfts.image` holds an unpinned `ipfs://` URL for most of the original
collection, so consumers that fetch it raw (the OG/X share card; the `/api/img`
proxy when the local archive lacks the edition) get a broken image. The art
lives on BunnyCDN, but the path variant (`LFGO/<ed>/<ed>_<N>.png`) increments
with each swap, so the correct URL can't be constructed — only probed. This
script probes the CDN per edition and writes the first resolving URL into the
index. Idempotent: only ipfs-shaped/empty rows are targeted (already-CDN rows
are skipped without any HTTP call, unless --force).

Pairs with the nft_index.upsert image clobber-guard, which stops the listener
from overwriting these repointed URLs back to ipfs on a later tx. Deploy the
guard first, then run this.

    .venv/bin/python scripts/repoint_images_to_cdn.py --network mainnet [--dry-run] [--force]

Design: docs/superpowers/specs/2026-07-20-image-cdn-repoint-design.md
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sqlite3
import sys
from collections.abc import Awaitable, Callable
from typing import TypedDict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import aiohttp  # noqa: E402

from lfg_core import nft_index  # noqa: E402

NETWORKS = ("testnet", "mainnet")
CDN_HOST = "https://lfgo.b-cdn.net/LFGO"
MAX_VARIANT = 8
CONCURRENCY = 30
Prober = Callable[[str], Awaitable[bool]]


class RepointSummary(TypedDict):
    scanned: int
    skipped_already_cdn: int
    target_editions: int
    repointed_editions: int
    repointed_rows: int
    nohit_editions: list[int]


def is_ipfs_url(url: str) -> bool:
    """True for the three ipfs shapes the index stores: raw ipfs://, a `/ipfs/`
    path gateway (e.g. dweb.link/ipfs/<cid>), or a `<cid>.ipfs.<host>` subdomain
    gateway. Empty is NOT ipfs (handled separately as a target)."""
    return bool(url) and (url.startswith("ipfs://") or "/ipfs/" in url or ".ipfs." in url)


def needs_repoint(url: str, force: bool) -> bool:
    """A row is a target when its image is empty or ipfs-shaped (or --force)."""
    return force or not url or is_ipfs_url(url)


def candidate_urls(edition: int) -> list[str]:
    """Ordered CDN URLs to probe for one edition's art."""
    urls = [f"{CDN_HOST}/{edition}/{edition}_{v}.png" for v in range(MAX_VARIANT + 1)]
    urls.append(f"{CDN_HOST}/lfg_{edition}.png")
    return urls


async def repoint_images(
    conn: sqlite3.Connection,
    *,
    prober: Prober,
    concurrency: int = CONCURRENCY,
    dry_run: bool = False,
    force: bool = False,
) -> RepointSummary:
    """Probe the CDN for every target edition and write the first resolving URL
    into every live nft_id of that edition. Returns a summary dict. `prober` is
    injected (returns True iff a URL resolves) so this is unit-testable offline."""
    rows = conn.execute(
        "SELECT nft_id, nft_number, image FROM onchain_nfts "
        "WHERE is_burned=0 AND nft_number IS NOT NULL"
    ).fetchall()

    targets: dict[int, list[str]] = {}
    skipped = 0
    for nft_id, edition, image in rows:
        if needs_repoint(image or "", force):
            targets.setdefault(int(edition), []).append(nft_id)
        else:
            skipped += 1

    sem = asyncio.Semaphore(concurrency)

    async def probe(edition: int) -> tuple[int, str | None]:
        async with sem:
            for url in candidate_urls(edition):
                if await prober(url):
                    return edition, url
            return edition, None

    results = await asyncio.gather(*(probe(ed) for ed in targets))

    repointed_rows = 0
    repointed_editions = 0
    nohit: list[int] = []
    for edition, winner in sorted(results):
        if winner is None:
            nohit.append(edition)
            continue
        repointed_editions += 1
        nft_ids = targets[edition]
        repointed_rows += len(nft_ids)
        if not dry_run:
            for nft_id in nft_ids:
                conn.execute("UPDATE onchain_nfts SET image=? WHERE nft_id=?", (winner, nft_id))
    if not dry_run:
        conn.commit()

    return {
        "scanned": len(rows),
        "skipped_already_cdn": skipped,
        "target_editions": len(targets),
        "repointed_editions": repointed_editions,
        "repointed_rows": repointed_rows,
        "nohit_editions": sorted(nohit),
    }


def _make_http_prober(session: aiohttp.ClientSession) -> Prober:
    async def prober(url: str) -> bool:
        try:
            async with session.get(url, allow_redirects=True) as r:
                return r.status == 200
        except Exception:
            return False

    return prober


async def _amain() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    env_net = os.getenv("XRPL_NETWORK", "").strip().lower()
    default = env_net if env_net in NETWORKS else None
    parser.add_argument("--network", choices=NETWORKS, default=default, required=default is None)
    parser.add_argument("--dry-run", action="store_true", help="probe and report; write nothing")
    parser.add_argument(
        "--force", action="store_true", help="re-probe already-CDN rows too (stale swap variants)"
    )
    args = parser.parse_args()

    index_path = nft_index.index_db_path(args.network)
    conn = nft_index.init_db(index_path)
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        connector = aiohttp.TCPConnector(limit=CONCURRENCY)
        async with aiohttp.ClientSession(
            timeout=timeout, connector=connector, headers={"User-Agent": "lfg-repoint"}
        ) as session:
            summary = await repoint_images(
                conn,
                prober=_make_http_prober(session),
                dry_run=args.dry_run,
                force=args.force,
            )
    finally:
        conn.close()

    mode = "DRY-RUN (no writes)" if args.dry_run else "applied"
    print(f"Network: {args.network}  DB: {index_path}  [{mode}]")
    print(f"  Live rows scanned          : {summary['scanned']}")
    print(f"  Skipped (already CDN)      : {summary['skipped_already_cdn']}")
    print(f"  Target editions            : {summary['target_editions']}")
    print(
        f"  Repointed editions / rows  : {summary['repointed_editions']} / {summary['repointed_rows']}"
    )
    print(f"  No CDN image found ({len(summary['nohit_editions'])}): {summary['nohit_editions']}")
    return 0


def main() -> int:
    return asyncio.run(_amain())


if __name__ == "__main__":
    raise SystemExit(main())
