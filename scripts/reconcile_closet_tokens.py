#!/usr/bin/env python3
"""Repair Closet ownership anomalies in the economy tables (#383).

A Closet is soulbound user inventory, so exactly one owner holds each Closet
NFToken and that owner is never the project's own signing account. Two ways
that could break:

  * a row keyed to a PROJECT signing account. A freshly minted Closet sits in
    the issuer's wallet until the user accepts the offer, so the listener's
    owner-of-record at NFTokenMint time was the issuer; it wrote a second row
    shadowing the real user's. Unambiguously bogus — deleted here, along with
    that "owner"'s loose closet_assets/closet_bodies (which otherwise inflate
    the conservation audit's census).
  * one nft_id under TWO USER owners. Not repairable from local state: Closet
    tokens never enter onchain_nfts, so only clio can say who holds one. These
    are REPORTED and the run exits non-zero — investigate by hand.

The write path that produced the first kind is fixed (nft_listener._apply_closet
now skips issuer-held Closets and scrubs stale rows, matching
backfill_economy._reconcile_closet). This script is for databases that already
carry the damage; a clean run finding nothing is the expected outcome.

DRY-RUN BY DEFAULT: prints what it WOULD delete and exits. Pass --apply to write.

  # inspect (no writes):
  python scripts/reconcile_closet_tokens.py --network testnet
  # actually repair:
  python scripts/reconcile_closet_tokens.py --network testnet --apply
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)

from lfg_core import closet_reconcile, config, economy_store, nft_index  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--network", choices=["testnet", "mainnet"], required=True)
    ap.add_argument("--apply", action="store_true", help="delete rows (default: dry-run)")
    args = ap.parse_args()

    # The project-account set is derived from ambient config (SWAP_ISSUER_ADDRESS /
    # SIGNING_ACCOUNT, frozen from XRPL_NETWORK at import). Pointing --network at a
    # different chain's DB would classify against the wrong issuer — either missing
    # every bogus row or, worse, matching a legitimate user. Refuse the mismatch.
    if args.network != config.XRPL_NETWORK:
        print(
            f"refusing: --network {args.network} != ambient XRPL_NETWORK "
            f"{config.XRPL_NETWORK}; run with matching env so the project-account "
            f"classifier targets the right chain",
            file=sys.stderr,
        )
        return 2

    accounts = economy_store.project_accounts()
    if not accounts:
        print("refusing: no project signing account resolved from config", file=sys.stderr)
        return 2

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
        report = closet_reconcile.audit_closet_ownership(conn)

        print(f"Network: {args.network}  DB: {db_path}")
        print(f"Project accounts: {', '.join(sorted(accounts))}")
        mode = "deleted" if args.apply else "would delete (dry-run; pass --apply)"
        if report.project_rows:
            print(f"\nClosets keyed to a project account — {mode}:")
            for row in report.project_rows:
                print(f"  {row.owner}  {row.nft_id}  ({row.status})")
        else:
            print("\nClosets keyed to a project account: none")

        unresolved = report.unresolved_duplicates
        if unresolved:
            print("\nDuplicate Closet NFTokens between USER owners — NOT repaired here:")
            for nft_id, owners in sorted(unresolved.items()):
                print(f"  {nft_id}  claimed by {', '.join(owners)}")
            print(
                "  Resolve on-ledger (clio nft_info) and correct by hand; a Closet is\n"
                "  soulbound, so exactly one of these owners is real."
            )

        if not args.apply:
            if report.project_rows:
                print("\nDry run — nothing written. Re-run with --apply to repair.")
            return 0 if report.ok else 1

        if report.project_rows:
            scrubbed = closet_reconcile.repair_closet_ownership(conn, report)
            print(f"\nScrubbed {scrubbed} project-account Closet record(s).")
        # The unique index cannot be created while duplicates exist, so a DB that
        # carried the damage has been running without it. Now that the bogus rows
        # are gone, take it — that is what stops the anomaly recurring.
        economy_store.ensure_closet_token_uniqueness(conn)
        after = closet_reconcile.audit_closet_ownership(conn)
        print(f"Post-repair state: {'OK' if after.ok else 'STILL ANOMALOUS'}")
        return 0 if after.ok else 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
