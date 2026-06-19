#!/usr/bin/env python3
"""Collection-integrity report over the on-chain NFT index.

Surfaces the artifacts that imperfect (non-atomic) trait-swaps leave behind,
reconciling the live collection against its expected edition range:
  - missing editions (burned, never re-minted) — the collection is short
  - editions with >1 live token (original burn failed → duplicate)
  - live tokens whose edition name is out of range (bad re-mint naming)
  - live tokens whose name yields no edition number (metadata naming bug)

  python scripts/audit_collection_integrity.py --network mainnet

Reads the per-network index DB (run the backfill / Bithomp import first).
Exit code is non-zero when any anomaly is found.
"""

from __future__ import annotations

import argparse
import os
import sys

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)

from lfg_core import config, nft_index  # noqa: E402

# The LFG collection tops out at edition 3535 (SWAP_MAX_NFT_NUMBER is a broader
# validity cap, not the minted size, so it would flood "missing" with numbers
# that were never minted).
COLLECTION_SIZE = 3535


def main() -> int:
    parser = argparse.ArgumentParser(description="Collection-integrity report over the index.")
    parser.add_argument("--network", choices=["mainnet", "testnet"], default=config.XRPL_NETWORK)
    parser.add_argument(
        "--max-edition",
        type=int,
        default=COLLECTION_SIZE,
        help="highest expected edition number (default: the 3535 collection size)",
    )
    args = parser.parse_args()

    db_path = nft_index.index_db_path(args.network)
    if not os.path.isfile(db_path):
        print(f"No index DB at {db_path}. Run the backfill / Bithomp import first.")
        return 2

    conn = nft_index.init_db(db_path)
    live = nft_index.live_nfts(conn)
    a = nft_index.collection_anomalies(live, args.max_edition)

    # nft_id -> edition for friendly labels in the out-of-range / unparsed lists.
    by_id = {n.nft_id: n for n in live}

    print(f"Network: {args.network}  expected editions: 1..{args.max_edition}")
    print(f"Live tokens: {len(live)}")
    print()
    print(f"Missing editions (no live token): {len(a['missing'])}")
    print(f"  {a['missing']}")
    print(f"Editions with >1 live token: {len(a['multi_live'])}")
    for ed, count in a["multi_live"].items():
        print(f"  #{ed}: {count} live")
    print(f"Out-of-range edition names: {len(a['out_of_range'])}")
    for nid in a["out_of_range"]:
        print(f"  #{by_id[nid].nft_number}  {nid}")
    print(f"Live tokens with no parsed edition (naming bug): {len(a['unparsed'])}")
    for nid in a["unparsed"]:
        uri = (
            bytes.fromhex(by_id[nid].uri_hex).decode("ascii", "ignore")
            if by_id[nid].uri_hex
            else ""
        )
        print(f"  {nid}  uri={uri}")

    total = len(a["missing"]) + len(a["multi_live"]) + len(a["out_of_range"]) + len(a["unparsed"])
    return 1 if total else 0


if __name__ == "__main__":
    raise SystemExit(main())
