#!/usr/bin/env python3
"""Conservation cross-check: ledger history vs. the on-chain NFT index.

  python scripts/audit_history.py --network mainnet

Compares mint/burn counts derived from `nft_events` (history_<net>.db)
against the live-token count in `onchain_nfts` (onchain_<net>.db). Any
non-zero drift means the two stores have fallen out of sync (missed
listener event, stale backfill, etc.) — investigate before trusting
leaderboard/history numbers.

Exit code is non-zero (1) when drift != 0.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))

from lfg_core import config, history_store, nft_index  # noqa: E402


def nftoken_taxon(nft_id: str) -> int:
    """Decode the (scrambled) taxon embedded in a 64-hex NFTokenID.

    Per XLS-20, the on-ledger taxon field is scrambled with
    `384160001 * sequence + 2459 (mod 2^32)` to spread otherwise-sequential
    ids; XOR with that recovers the minted taxon. Malformed / non-ledger
    ids (e.g. test fixtures) return -1, which never matches a real taxon."""
    try:
        scrambled = int(nft_id[48:56], 16)
        sequence = int(nft_id[56:64], 16)
    except ValueError:
        return -1
    if len(nft_id) != 64:
        return -1
    return scrambled ^ ((384160001 * sequence + 2459) % (1 << 32))


def audit_history(hconn: Any, oconn: Any, taxon: int | None = None) -> dict[str, int]:
    """Cross-check derived nft_events mint/burn counts against the live
    on-chain index. Counts DISTINCT nft_id per event type to tolerate
    re-derivation overlap (INSERT OR REPLACE can leave duplicate logical
    events across reruns). When `taxon` is given, history counts are
    scoped to that collection taxon — the issuer account has minted
    other-taxon tokens (Closets, trait tokens, old taxon-1337 tests) that
    the index deliberately excludes."""

    def _ids(event: str) -> set[str]:
        rows = hconn.execute(
            "SELECT DISTINCT nft_id FROM nft_events WHERE event=?", (event,)
        ).fetchall()
        return {nft_id for (nft_id,) in rows if taxon is None or nftoken_taxon(nft_id) == taxon}

    mint_ids = _ids("mint")
    burn_ids = _ids("burn")
    mints = len(mint_ids)
    burns = len(burn_ids)
    # Live = minted-and-not-burned as a SET, not mints-minus-burns: the
    # archive can hold burn events for tokens whose mint predates the
    # available clio history (burn-only ids), which would skew a count
    # subtraction without representing real drift.
    live_events = len(mint_ids - burn_ids)
    live_index = oconn.execute("SELECT COUNT(*) FROM onchain_nfts WHERE is_burned=0").fetchone()[0]
    drift = live_events - live_index
    return {
        "mints": mints,
        "burns": burns,
        "live_events": live_events,
        "live_index": live_index,
        "drift": drift,
    }


def main(argv: list[str] | None = None, *, hconn: Any = None, oconn: Any = None) -> int:
    parser = argparse.ArgumentParser(description="Conservation audit: history vs. onchain index.")
    parser.add_argument("--network", default=config.XRPL_NETWORK)
    parser.add_argument("--history-db", help="override history_<net>.db path")
    parser.add_argument("--onchain-db", help="override onchain_<net>.db path")
    parser.add_argument(
        "--taxon",
        type=int,
        help="collection taxon to scope history counts to (default: the network's collection taxon)",
    )
    args = parser.parse_args(argv)

    if hconn is None:
        hpath = args.history_db or history_store.history_db_path(args.network)
        if not os.path.isfile(hpath):
            print(f"No history DB at {hpath}. Run scripts/backfill_history.py first.")
            return 2
        hconn = history_store.init_history_db(hpath)
    if oconn is None:
        opath = args.onchain_db or nft_index.index_db_path(args.network)
        if not os.path.isfile(opath):
            print(f"No onchain index DB at {opath}. Run scripts/backfill_onchain.py first.")
            return 2
        oconn = nft_index.init_db(opath)

    import backfill_onchain as bf

    taxon = args.taxon
    if taxon is None:
        net = bf.NETWORKS.get(args.network) or {}
        taxon = net.get("taxon")
    result = audit_history(hconn, oconn, taxon=taxon)
    print(f"Network: {args.network}")
    print(f"  mints (history):       {result['mints']}")
    print(f"  burns (history):       {result['burns']}")
    print(f"  live (history-derived):{result['live_events']}")
    print(f"  live (onchain index):  {result['live_index']}")
    print(f"  drift:                 {result['drift']}")
    if result["drift"] == 0:
        print("PASS: history and onchain index conserve mint/burn count.")
        return 0
    print("FAIL: drift detected between history and onchain index.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
