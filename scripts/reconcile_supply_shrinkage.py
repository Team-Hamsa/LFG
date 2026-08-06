#!/usr/bin/env python3
"""Reconcile historical out-of-band character burns into the supply ledger (#322).

The listener now records an out-of-band character burn as a `-1` shrinkage row
(`nft_listener._record_burn_shrinkage`), but burns that happened BEFORE that
recorder existed — or while the listener was down — left mints logged `+1` with
no compensating `-1`, drifting the conservation audit until someone re-froze
genesis. This sweep writes the missing burn rows back from the on-chain index's
preserved attributes (`mark_burned` keeps `attributes_json`).

Rules (see `supply_reconcile.reconcile_shrinkage`):
  - only OUR character editions present in the effective genesis;
  - editions with a still-LIVE token are skipped (burned duplicates from the
    legacy burn+remint swap path are replacements, not shrinkage);
  - BLANK characters write nothing (their assets survive in the owner's Closet);
  - unreadable rows are skipped and REPORTED, never guessed at;
  - idempotent per burned nft_id (`supply_changes.nft_id` stamp).

DRY-RUN BY DEFAULT: prints what it WOULD write and exits. Pass --apply to write.

  # inspect (no writes):
  python scripts/reconcile_supply_shrinkage.py --network testnet
  # actually write:
  python scripts/reconcile_supply_shrinkage.py --network testnet --apply
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)

from lfg_core import economy_store, nft_index, supply_reconcile  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--network", choices=["testnet", "mainnet"], required=True)
    ap.add_argument("--apply", action="store_true", help="write rows (default: dry-run)")
    args = ap.parse_args()

    db_path = nft_index.index_db_path(args.network)
    if not os.path.exists(db_path):
        print(f"index DB not found: {db_path}", file=sys.stderr)
        return 2
    if args.apply:
        conn = nft_index.init_db(db_path)
        economy_store.init_economy_schema(conn)
    else:
        # Dry-run must not write AT ALL — init_db/init_economy_schema commit
        # DDL/migrations, so open the file read-only instead.
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        genesis_ok = economy_store.genesis_exists(conn)
    except sqlite3.OperationalError:
        genesis_ok = False
    if not genesis_ok:
        print(f"no complete genesis in {db_path}; freeze one first", file=sys.stderr)
        return 2

    report = supply_reconcile.reconcile_shrinkage(conn, dry_run=not args.apply)
    if args.apply:
        conn.commit()

    mode = "wrote" if args.apply else "would write (dry-run; pass --apply)"
    print(f"{args.network}: {mode} {len(report['written'])} shrinkage row(s)")
    for edition, nft_id in report["written"]:
        print(f"  burn edition #{edition} ({nft_id})")
    for edition, nft_id in report["skipped_unreadable"]:
        print(f"  SKIPPED #{edition} ({nft_id}): unreadable metadata — repair the index row first")
    return 1 if report["skipped_unreadable"] else 0


if __name__ == "__main__":
    sys.exit(main())
