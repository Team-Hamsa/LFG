#!/usr/bin/env python3
"""Populate the on-chain NFT index from a Bithomp collection CSV export.

Bithomp serves the whole collection (current + historical, incl. burned) from
its CDN with metadata already parsed — a far more reliable source than scraping
IPFS per token. This imports such a CSV into the per-`nft_id` index, so the
backfill/auditor have a complete offline source. The live clio listener still
runs to keep the index fresh after the import.

  python scripts/import_bithomp_csv.py --network mainnet --csv LFGOdata.csv

Expected columns (extra columns are ignored): NFT ID, Name, Owner, URI, Image,
and one `Attribute <TraitType>` column per trait (TraitType uses the layer
names, e.g. `Attribute Head`). A burned flag is picked up from a `Burned` or
`Status` column when present (current exports omit it → all rows treated live).
"""

from __future__ import annotations

import argparse
import csv
import os
import sqlite3
import sys

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)

from lfg_core import nft_index, swap_meta  # noqa: E402

_ATTR_PREFIX = "Attribute "


def _is_burned(row: dict[str, str]) -> bool:
    """Read a burned flag from a Burned/Status column, tolerant of casing and
    of the column being absent (current exports have no such column)."""
    for key, value in row.items():
        k = (key or "").strip().lower()
        v = (value or "").strip().lower()
        if k == "burned":
            return v in {"1", "true", "yes", "burned"}
        if k == "status":
            return v == "burned"
    return False


def csv_record(row: dict[str, str]) -> nft_index.OnchainNft:
    """Map one Bithomp CSV row to an OnchainNft. Pure — no I/O. Attributes come
    from `Attribute <TraitType>` columns, normalized the same way the swap path
    does; `mutable` is unknown from a CSV (left None, filled later by the
    listener); `uri_hex` is the hex of the CSV's decoded URI."""
    raw_attrs = [
        {"trait_type": key[len(_ATTR_PREFIX) :], "value": (value or "").strip()}
        for key, value in row.items()
        if key and key.startswith(_ATTR_PREFIX)
    ]
    attributes = swap_meta.normalize_attributes(raw_attrs)
    body = swap_meta.detect_body(attributes)
    name = (row.get("Name") or "").strip()
    uri = (row.get("URI") or "").strip()
    return nft_index.OnchainNft(
        nft_id=(row.get("NFT ID") or "").strip(),
        nft_number=swap_meta.extract_nft_number(name),
        owner=(row.get("Owner") or "").strip() or None,
        is_burned=_is_burned(row),
        mutable=None,
        uri_hex=uri.encode("ascii", "ignore").hex() if uri else "",
        body=body,
        attributes=attributes,
        image=(row.get("Image") or "").strip(),
        ledger_index=None,
    )


def import_csv(conn: sqlite3.Connection, path: str) -> dict[str, int]:
    """Import every row of a Bithomp CSV into the index. Idempotent (upsert by
    nft_id). Returns {imported, skipped} (skipped = rows with no NFT ID)."""
    imported = 0
    skipped = 0
    with open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            rec = csv_record(row)
            if not rec.nft_id:
                skipped += 1
                continue
            nft_index.upsert(conn, rec)
            imported += 1
    return {"imported": imported, "skipped": skipped}


def main() -> int:
    from lfg_core import config

    parser = argparse.ArgumentParser(description="Import a Bithomp collection CSV into the index.")
    parser.add_argument("--network", choices=["mainnet", "testnet"], default=config.XRPL_NETWORK)
    parser.add_argument("--csv", required=True, help="path to the Bithomp CSV export")
    args = parser.parse_args()

    db_path = nft_index.index_db_path(args.network)
    conn = nft_index.init_db(db_path)
    counts = import_csv(conn, args.csv)
    print(f"Network: {args.network}  DB: {db_path}")
    print(f"  Imported: {counts['imported']}  Skipped (no NFT ID): {counts['skipped']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
