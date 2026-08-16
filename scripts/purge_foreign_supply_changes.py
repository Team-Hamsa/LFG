#!/usr/bin/env python3
"""Purge foreign-issuer `supply_changes` rows from a per-network on-chain DB.

Before the #178 issuer gate, `nft_listener._apply_possible_growth` recorded a
`supply_changes` growth row for ANY mainnet mint whose name parsed as `#N` --
including tokens minted by FOREIGN issuers reusing our naming. Those rows
poison the conservation invariant (`census == genesis + Σ supply_changes`) and
make `audit_trait_economy.py` print DRIFT on a clean collection.

Every production `supply_changes` row is listener-recorded with
`reason = "new-edition mint <nft_id>"` (economy_store.record_supply_change is
called ONLY from nft_listener). This script parses the 64-hex `nft_id` out of
that reason and checks the (unscrambled) issuer AccountID bytes embedded at hex
chars 8..48 -- reusing the SAME membership helpers the market path already
trusts (`nft_listener._nft_id_is_ours`). A row whose nft_id embeds a foreign
issuer is a purge candidate; a row whose nft_id is ours is kept; a row whose
reason carries no parseable nft_id is left ALONE and reported (never guessed).

DRY-RUN BY DEFAULT: prints what it WOULD delete and exits without writing.
Pass --apply to actually delete. Idempotent; safe to re-run.

  # inspect (no writes):
  python scripts/purge_foreign_supply_changes.py --network testnet
  # actually delete:
  python scripts/purge_foreign_supply_changes.py --network testnet --apply

Do NOT run against mainnet without the go-live owner's sign-off -- this is the
data step behind blocker B1 in the trait-economy go-live review.
"""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)

from lfg_core import config, nft_index, nft_listener  # noqa: E402

# The listener writes exactly `new-edition mint <nft_id>` (64-hex NFTokenID).
# Match that FULL shape only — a bare-hex search would classify any manual or
# future reason that merely contains a 64-hex run as an NFTokenID, and --apply
# could then delete that row as foreign, corrupting the very ledger we repair.
# A differently-shaped reason simply doesn't match and the row is left untouched.
_NFT_ID_RE = re.compile(r"^new-edition mint ([0-9A-Fa-f]{64})$")


def _classify_rows(
    conn: sqlite3.Connection, issuer_hex: str
) -> tuple[list[tuple[int, str, str]], list[tuple[int, str, str]], list[tuple[int, str]]]:
    """Split every supply_changes row into (foreign, ours, unparseable).

    Returns:
      foreign      -> [(id, nft_id, reason)]  purge candidates (foreign issuer)
      ours         -> [(id, nft_id, reason)]  kept (our issuer)
      unparseable  -> [(id, reason)]          kept (no nft_id in reason)
    """
    foreign: list[tuple[int, str, str]] = []
    ours: list[tuple[int, str, str]] = []
    unparseable: list[tuple[int, str]] = []
    for row_id, reason in conn.execute("SELECT id, reason FROM supply_changes ORDER BY id"):
        reason_str = str(reason or "")
        match = _NFT_ID_RE.match(reason_str)
        if match is None:
            unparseable.append((int(row_id), reason_str))
            continue
        nft_id = match.group(1)
        if nft_listener._nft_id_is_ours(nft_id, issuer_hex):
            ours.append((int(row_id), nft_id, reason_str))
        else:
            foreign.append((int(row_id), nft_id, reason_str))
    return foreign, ours, unparseable


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--network", required=True, choices=["testnet", "mainnet"])
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually delete the foreign rows (default is a dry run that only prints).",
    )
    args = parser.parse_args()

    # The classification issuer is derived from ambient config (SWAP_ISSUER_ADDRESS,
    # frozen from XRPL_NETWORK at import). If --network selects a different DB than
    # the ambient network, EVERY row would be classified foreign and --apply would
    # wipe the whole ledger, including genuine growth rows. Refuse the mismatch.
    if args.network != config.XRPL_NETWORK:
        print(
            f"refusing: --network {args.network} != ambient XRPL_NETWORK "
            f"{config.XRPL_NETWORK}; run with matching env so the issuer classifier "
            f"targets the right chain",
            file=sys.stderr,
        )
        return 2

    db_path = nft_index.index_db_path(args.network)
    if not os.path.exists(db_path):
        print(f"index DB not found: {db_path}", file=sys.stderr)
        return 2

    issuer_hex = nft_listener._issuer_account_hex()
    print(f"DB: {db_path}")
    print(f"Our issuer: {config.SWAP_ISSUER_ADDRESS} ({issuer_hex})")

    conn = sqlite3.connect(db_path)
    try:
        foreign, ours, unparseable = _classify_rows(conn, issuer_hex)
        total = len(foreign) + len(ours) + len(unparseable)
        print(
            f"supply_changes rows: {total} total | "
            f"{len(ours)} ours (keep) | {len(foreign)} foreign (purge) | "
            f"{len(unparseable)} no-nft_id (keep)"
        )
        for row_id, nft_id, reason in foreign:
            print(f"  FOREIGN id={row_id} nft_id={nft_id} reason={reason!r}")
        if unparseable:
            print(f"  ({len(unparseable)} row(s) had no nft_id in reason and were left untouched)")

        if not foreign:
            print("Nothing to purge.")
            return 0

        if not args.apply:
            print(f"\nDRY RUN: would delete {len(foreign)} foreign row(s). Re-run with --apply.")
            return 0

        conn.executemany(
            "DELETE FROM supply_changes WHERE id=?", [(row_id,) for row_id, _, _ in foreign]
        )
        conn.commit()
        print(f"\nDeleted {len(foreign)} foreign supply_changes row(s).")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
