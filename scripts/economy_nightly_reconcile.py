#!/usr/bin/env python3
"""Nightly supply-ledger reconcile (#322): growth + shrinkage in one process.

Runs `supply_reconcile.reconcile_growth` (listener-missed mints, #289) and
`supply_reconcile.reconcile_shrinkage` (out-of-band burns) against one index
connection, with writes, so the nightly `audit_trait_economy.py` run that
follows sees a ledger already caught up — drift becomes a real signal instead
of a re-freeze trigger. Both sweeps are idempotent; a clean night writes 0
rows. Intended for a pm2 cron entry (lfg-economy-reconcile / stg-economy-
reconcile) scheduled just before the audit entry.

  python scripts/economy_nightly_reconcile.py --network mainnet
"""

from __future__ import annotations

import argparse
import os
import sys

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)

from lfg_core import economy_store, nft_index, supply_reconcile  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--network", choices=["testnet", "mainnet"], required=True)
    args = ap.parse_args()

    db_path = nft_index.index_db_path(args.network)
    if not os.path.exists(db_path):
        print(f"index DB not found: {db_path}", file=sys.stderr)
        return 2
    conn = nft_index.init_db(db_path)
    economy_store.init_economy_schema(conn)
    if not economy_store.genesis_exists(conn):
        # No genesis frozen (fresh network) — nothing to reconcile, not a failure.
        print(f"{args.network}: no frozen genesis; skipping reconcile")
        return 0

    growth = supply_reconcile.reconcile_growth(conn)
    shrinkage = supply_reconcile.reconcile_shrinkage(conn)
    conn.commit()

    print(
        f"{args.network}: wrote {len(growth['written'])} growth row(s), "
        f"{len(shrinkage['written'])} shrinkage row(s)"
    )
    for edition in growth["written"]:
        print(f"  mint edition #{edition}")
    for edition, nft_id in shrinkage["written"]:
        print(f"  burn edition #{edition} ({nft_id})")
    unreadable = [(e, "") for e in growth["skipped_unreadable"]] + list(
        shrinkage["skipped_unreadable"]
    )
    for edition, nft_id in unreadable:
        print(f"  SKIPPED #{edition} {nft_id}: unreadable metadata — repair the index row")
    return 1 if unreadable else 0


if __name__ == "__main__":
    sys.exit(main())
