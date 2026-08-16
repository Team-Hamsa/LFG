#!/usr/bin/env python
# Seed layers/seasons.json from a season's trait CSV export (#114).
#
#   .venv/bin/python scripts/seed_seasons_from_csv.py --csv docs/S3-trait-list.csv --season 3
#
# Merges into the existing manifest (re-runnable; a later export for another
# season just adds its keys). Reports CSV rows that matched no layer-store
# file so renames/never-shipped traits are visible.

import argparse
import asyncio
import csv
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lfg_core import seasons  # noqa: E402
from lfg_core.layer_store import get_layer_store  # noqa: E402


async def _layer_tree():
    store = get_layer_store()
    tree: dict[str, dict[str, list[str]]] = {}
    for body in await store.list_bodies():
        tree[body] = {}
        for trait_type in await store.list_trait_types(body):
            tree[body][trait_type] = await store.list_values(body, trait_type)
    return tree


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed seasons.json from a trait CSV export")
    parser.add_argument("--csv", required=True, help="export with a relative_path column")
    parser.add_argument("--season", type=int, required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    with open(args.csv, newline="") as f:
        rel_paths = [row["relative_path"] for row in csv.DictReader(f)]
    if not rel_paths:
        sys.exit(f"{args.csv} has no rows")

    tree = asyncio.run(_layer_tree())
    new_entries = seasons.build_manifest(rel_paths, tree, season=args.season)

    # Compare (category, value) pairs — a name matched in one category must
    # not suppress the warning for the same name unmatched in another.
    matched_pairs = {tuple(k.split("/", 2)[1:]) for k in new_entries}
    for rel in rel_paths:
        cat_dir, _, filename = rel.rpartition("/")
        category = cat_dir if cat_dir == "Background" else cat_dir.split(" ", 1)[-1]
        stem = seasons.strip_dup_suffix(os.path.splitext(filename)[0])
        if stem != "None" and (category, stem) not in matched_pairs:
            print(f"UNMATCHED (not in any body's layer store): {rel}")

    manifest = seasons.load_seasons()
    manifest.update(new_entries)
    print(
        f"{len(rel_paths)} CSV rows -> {len(new_entries)} manifest entries "
        f"(manifest total {len(manifest)})"
    )
    if args.dry_run:
        print("dry-run: not writing", seasons.manifest_path())
        return
    with open(seasons.manifest_path(), "w") as f:
        json.dump(dict(sorted(manifest.items())), f, indent=1)
        f.write("\n")
    print("wrote", seasons.manifest_path())


if __name__ == "__main__":
    main()
