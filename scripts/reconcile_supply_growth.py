#!/usr/bin/env python3
"""Reconcile listener-missed new-edition mints into the supply_changes ledger.

The only live writer of new-edition growth rows is
`nft_listener._apply_possible_growth`; a character minted while the listener is
down (deploy, restart, network blip) never gets one, so its edition stays
outside the effective genesis and Harvest refuses it forever with
"character has no known genesis edition". `backfill_onchain.py` repairs the
token index but not the supply ledger — this sweep closes that gap.

For every LIVE `onchain_nfts` character whose edition is missing from the
effective genesis (frozen genesis + supply_changes), it writes the same growth
row the listener would have (actor "reconciler", reason
"growth reconcile <nft_id>"), built from the index's stored metadata. Tokens
with unreadable metadata (no attributes / no Body) are skipped and REPORTED,
never guessed at. Idempotent; safe to re-run. Also a pre-flight check for the
economy mainnet flip: a clean run writes nothing.

DRY-RUN BY DEFAULT: prints what it WOULD write and exits. Pass --apply to write.

  # inspect (no writes):
  python scripts/reconcile_supply_growth.py --network testnet
  # actually write:
  python scripts/reconcile_supply_growth.py --network testnet --apply
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
    ap.add_argument("--apply", action="store_true", help="write rows (default: dry-run)")
    args = ap.parse_args()

    db_path = nft_index.index_db_path(args.network)
    if not os.path.exists(db_path):
        print(f"index DB not found: {db_path}", file=sys.stderr)
        return 2
    conn = nft_index.init_db(db_path)
    economy_store.init_economy_schema(conn)
    if not economy_store.genesis_exists(conn):
        print(f"no complete genesis in {db_path}; freeze one first", file=sys.stderr)
        return 2

    report = supply_reconcile.reconcile_growth(conn, dry_run=not args.apply)
    if args.apply:
        conn.commit()

    mode = "wrote" if args.apply else "would write (dry-run; pass --apply)"
    print(f"{args.network}: {mode} {len(report['written'])} growth row(s)")
    for edition in report["written"]:
        print(f"  mint edition #{edition}")
    for edition in report["skipped_unreadable"]:
        print(f"  SKIPPED #{edition}: unreadable metadata — repair the index row first")
    return 1 if report["skipped_unreadable"] else 0


if __name__ == "__main__":
    sys.exit(main())
