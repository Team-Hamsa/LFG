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

from xrpl.core import addresscodec  # noqa: E402

from lfg_core import nft_index, swap_meta  # noqa: E402

_ATTR_PREFIX = "Attribute "


def _row_issuer(row: dict[str, str], nft_id: str) -> str | None:
    """The row's issuer: the Issuer column when present, else decoded from the
    NFT ID (bytes 4..24 of the 32-byte token ID). None when neither works."""
    for key, value in row.items():
        if (key or "").strip().lower() == "issuer" and (value or "").strip():
            return value.strip()
    if len(nft_id) == 64:
        try:
            return addresscodec.encode_classic_address(bytes.fromhex(nft_id)[4:24])
        except ValueError:
            return None
    return None


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


def csv_record(row: dict[str, str], force_burned: bool = False) -> nft_index.OnchainNft:
    """Map one Bithomp CSV row to an OnchainNft. Pure — no I/O. Attributes come
    from `Attribute <TraitType>` columns, normalized the same way the swap path
    does; `mutable` is unknown from a CSV (left None, filled later by the
    listener); `uri_hex` is the hex of the CSV's decoded URI. `force_burned`
    marks every row burned (for a separate burned-only export with no flag col)."""
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
        is_burned=force_burned or _is_burned(row),
        mutable=None,
        uri_hex=uri.encode("ascii", "ignore").hex() if uri else "",
        body=body,
        attributes=attributes,
        image=(row.get("Image") or "").strip(),
        ledger_index=None,
    )


def import_csv(
    conn: sqlite3.Connection,
    path: str,
    force_burned: bool = False,
    issuer: str | None = None,
) -> dict[str, int]:
    """Import every row of a Bithomp CSV into the index. Idempotent (upsert by
    nft_id). `force_burned` marks all rows burned (separate burned export).
    `issuer` scopes the import to one collection: rows whose issuer (Issuer
    column, else decoded from the NFT ID) differs are skipped — Bithomp exports
    can contain foreign tokens. Returns {imported, skipped, skipped_foreign}."""
    imported = 0
    skipped = 0
    skipped_foreign = 0
    with open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            rec = csv_record(row, force_burned=force_burned)
            if not rec.nft_id:
                skipped += 1
                continue
            if issuer is not None and _row_issuer(row, rec.nft_id) != issuer:
                skipped_foreign += 1
                continue
            nft_index.upsert(conn, rec)
            imported += 1
    return {"imported": imported, "skipped": skipped, "skipped_foreign": skipped_foreign}


def main() -> int:
    from lfg_core import config

    parser = argparse.ArgumentParser(description="Import a Bithomp collection CSV into the index.")
    parser.add_argument("--network", choices=["mainnet", "testnet"], default=config.XRPL_NETWORK)
    parser.add_argument("--csv", required=True, help="path to the Bithomp CSV export")
    parser.add_argument(
        "--burned",
        action="store_true",
        help="mark every row burned (for a separate burned-only export)",
    )
    parser.add_argument(
        "--issuer",
        default=config.SWAP_ISSUER_ADDRESS,
        help="collection issuer; rows from other issuers are skipped "
        "(default: SWAP_ISSUER_ADDRESS)",
    )
    args = parser.parse_args()

    db_path = nft_index.index_db_path(args.network)
    conn = nft_index.init_db(db_path)
    counts = import_csv(conn, args.csv, force_burned=args.burned, issuer=args.issuer)
    print(f"Network: {args.network}  DB: {db_path}")
    print(
        f"  Imported: {counts['imported']}  Skipped (no NFT ID): {counts['skipped']}"
        f"  Skipped (foreign issuer): {counts['skipped_foreign']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
