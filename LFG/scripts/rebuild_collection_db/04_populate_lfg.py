#!/usr/bin/env python3
"""Step 4: Write resolved real traits into the bot's LFG table.

Dry-run by default (prints the plan + sanity stats). Pass --apply to write and
--prune to also delete mainnet rows whose edition is NOT a live on-chain NFT
(burned-and-never-re-minted editions that would otherwise pollute rarity counts).

Metadata trait_type -> LFG column mapping:
  Head     -> Hat        (LFG headwear column)
  Accesory -> Accessory  (source misspells it; LFG column is correct)
  others map 1:1 (Background, Back, Body, Clothing, Mouth, Eyebrows, Eyes)

  python 04_populate_lfg.py --traits work/traits.json --db ../../lfg_nfts.db
  python 04_populate_lfg.py --traits work/traits.json --db ../../lfg_nfts.db --apply --prune
"""
import argparse
import json
import os
import sqlite3
import sys
from collections import Counter

# repo root is two levels up from scripts/rebuild_collection_db/
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, REPO_ROOT)
from lfg_core.swap_meta import detect_body  # noqa: E402

LFG_COLS = ["Background", "Back", "Body", "Clothing", "Eyes",
            "Eyebrows", "Mouth", "Hat", "Accessory"]

TRAIT_TO_COL = {
    "Background": "Background", "Back": "Back", "Body": "Body",
    "Clothing": "Clothing", "Eyes": "Eyes", "Eyebrows": "Eyebrows",
    "Mouth": "Mouth", "Head": "Hat", "Hat": "Hat",
    "Accesory": "Accessory", "Accessory": "Accessory",
}


def row_for(rec):
    """Map a resolved record's traits to LFG columns; return (cols, body_type)."""
    cols = {c: "" for c in LFG_COLS}
    for tt, val in rec.get("attrs", {}).items():
        col = TRAIT_TO_COL.get(tt)
        if col:
            cols[col] = val if val is not None else ""
    body_type = detect_body([{"trait_type": "Body", "value": cols.get("Body", "")}])
    return cols, body_type


def main():
    """Report the rebuild plan; with --apply, write traits into the LFG table."""
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--traits", default="work/traits.json")
    p.add_argument("--db", default=os.path.join(REPO_ROOT, "lfg_nfts.db"))
    p.add_argument("--network", default="mainnet")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--prune", action="store_true")
    args = p.parse_args()

    data = json.load(open(args.traits))
    results = {int(k): v for k, v in data["results"].items()}
    errors = data.get("errors", [])
    conflicts = data.get("conflicts", [])

    conn = sqlite3.connect(args.db)
    cur = conn.cursor()
    existing = {r[0]: r[1] for r in cur.execute("SELECT nft_number, network FROM LFG")}
    net_nums = {n for n, net in existing.items() if net == args.network}

    live_eds = set(results.keys())
    to_update = live_eds & net_nums
    to_insert = live_eds - set(existing.keys())
    non_live = net_nums - live_eds

    print("=== RESOLUTION ===")
    print(f"resolved live editions : {len(live_eds)}  range {min(live_eds)}..{max(live_eds)}")
    print(f"errors (unresolved)    : {len(errors)}")
    print(f"conflicts (dup edition): {len(conflicts)} {conflicts[:10]}")
    print(f"sources                : {dict(Counter(r.get('source') for r in results.values()))}")
    print("=== LFG TABLE PLAN ===")
    print(f"current {args.network} rows : {len(net_nums)}")
    print(f"  UPDATE w/ traits     : {len(to_update)}")
    print(f"  INSERT new rows      : {len(to_insert)}")
    print(f"  non-live rows        : {len(non_live)}  (prune candidates: {sorted(non_live)[:20]})")
    bt = Counter(detect_body([{"trait_type": "Body", "value": r["attrs"].get("Body", "")}])
                 for r in results.values())
    print(f"body_type distribution : {dict(bt)}")

    if not args.apply:
        print("\nDRY RUN — re-run with --apply [--prune] to commit.")
        conn.close()
        return

    upd = ins = pruned = 0
    for ed, rec in results.items():
        cols, body_type = row_for(rec)
        nft_id = rec.get("nft_id")
        if ed in existing:
            setclause = ", ".join(f'"{c}"=?' for c in LFG_COLS)
            cur.execute(
                f'UPDATE LFG SET {setclause}, body_type=?, network=?, '
                f'nft_id=COALESCE(nft_id, ?) WHERE nft_number=?',
                [cols[c] for c in LFG_COLS] + [body_type, args.network, nft_id, ed])
            upd += 1
        else:
            collist = ", ".join(f'"{c}"' for c in LFG_COLS)
            ph = ", ".join("?" for _ in LFG_COLS)
            cur.execute(
                f'INSERT INTO LFG (nft_number, {collist}, body_type, network, nft_id) '
                f'VALUES (?, {ph}, ?, ?, ?)',
                [ed] + [cols[c] for c in LFG_COLS] + [body_type, args.network, nft_id])
            ins += 1
    if args.prune:
        for ed in non_live:
            cur.execute("DELETE FROM LFG WHERE nft_number=? AND network=?",
                        (ed, args.network))
            pruned += 1
    conn.commit()
    conn.close()
    print(f"\nAPPLIED: updated {upd}, inserted {ins}, pruned {pruned}")


if __name__ == "__main__":
    main()
