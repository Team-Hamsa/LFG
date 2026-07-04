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

from lfg_core import config, history_store, nft_index  # noqa: E402


def audit_history(hconn: Any, oconn: Any) -> dict[str, int]:
    """Cross-check derived nft_events mint/burn counts against the live
    on-chain index. Counts DISTINCT nft_id per event type to tolerate
    re-derivation overlap (INSERT OR REPLACE can leave duplicate logical
    events across reruns)."""
    mints = hconn.execute(
        "SELECT COUNT(DISTINCT nft_id) FROM nft_events WHERE event='mint'"
    ).fetchone()[0]
    burns = hconn.execute(
        "SELECT COUNT(DISTINCT nft_id) FROM nft_events WHERE event='burn'"
    ).fetchone()[0]
    live_events = mints - burns
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

    result = audit_history(hconn, oconn)
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
