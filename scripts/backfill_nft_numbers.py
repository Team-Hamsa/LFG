#!/usr/bin/env python3
"""Fill NULL nft_number rows in the on-chain index from the authoritative app
LFG table.

Some tokens' on-chain metadata `name` carries no parseable edition, so the
index stores nft_number=NULL (nft_index.token_record). That NULL makes the
marketplace show the raw hex nft_id instead of "#<edition>" and blocks
image_archive.edition_for_url from serving the local-archive image. The app LFG
table minted every edition and is the source of truth for nft_id -> nft_number,
so it heals what the chain metadata can't describe.

Idempotent. Run after a fresh backfill, or whenever a marketplace card shows a
hex id instead of an edition number.

    .venv/bin/python scripts/backfill_nft_numbers.py --network mainnet
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lfg_core import db_path, nft_index  # noqa: E402

NETWORKS = ("testnet", "mainnet")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    # Derive the default from the env directly (like db_path) rather than
    # lfg_core.config, so this ops script runs without the full XUMM/Bunny
    # secret set — it only touches sqlite files.
    env_net = os.getenv("XRPL_NETWORK", "").strip().lower()
    default = env_net if env_net in NETWORKS else None
    parser.add_argument("--network", choices=NETWORKS, default=default, required=default is None)
    args = parser.parse_args()

    index_path = nft_index.index_db_path(args.network)
    app_path = db_path.app_db_path(args.network)
    conn = nft_index.init_db(index_path)
    try:
        before = conn.execute(
            "SELECT COUNT(*) FROM onchain_nfts WHERE is_burned=0 AND nft_number IS NULL"
        ).fetchone()[0]
        healed = nft_index.reconcile_numbers_from_app_db(conn, app_path)
        after = conn.execute(
            "SELECT COUNT(*) FROM onchain_nfts WHERE is_burned=0 AND nft_number IS NULL"
        ).fetchone()[0]
    finally:
        conn.close()

    print(f"Network: {args.network}")
    print(f"  Index DB: {index_path}")
    print(f"  App DB:   {app_path}")
    print(f"  NULL nft_number (live) before: {before}  after: {after}")
    print(f"  Reconciled from app DB: {healed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
