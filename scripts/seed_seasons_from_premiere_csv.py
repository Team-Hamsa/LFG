#!/usr/bin/env python
# Rebuild layers/seasons.json from the all-seasons premiere CSV (#114).
#
#   .venv/bin/python scripts/seed_seasons_from_premiere_csv.py \
#       --csv docs/premiered_traits_by_season.csv [--dry-run]
#
# Unlike seed_seasons_from_csv.py (single-season file export, merges), this
# REPLACES the manifest wholesale: the premiere CSV covers every season, so a
# rebuild is the only way to drop stale/wrong tags. Rows in the "Animated" /
# "One of Ones" categories are not composable mint layers and are skipped;
# rows matching no layer-store trait are reported (Season 4 traits haven't
# shipped yet and are expected here).

import argparse
import asyncio
import csv
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lfg_core import seasons  # noqa: E402
from lfg_core.layer_store import get_layer_store  # noqa: E402

SKIP_CATEGORIES = {"Animated", "One of Ones"}

# CSV spelling -> layer-store spelling (renames the export didn't track).
ALIASES: dict[tuple[str, str], str] = {
    ("Clothing", "Prisoner Jumpsuit"): "Prison Jumpsuit",
    ("Head", "Chucky"): "Chucky Hair",
    ("Body", "Ape X-Ray"): "Ape Xray",
    ("Body", "Ape Melting X-Ray"): "Ape Melting XRay",
    ("Body", "Iridescent Skeleton"): "Irridescent Skeleton",
}

# Store traits the CSV missed entirely; seasons confirmed by the artist
# (2026-07-03): Third Eye premiered in S3, Wood/Banana Dress in S2,
# both Saiyan hairs in S1.
OVERRIDES: dict[tuple[str, str], int] = {
    ("Eyes", "Third Eye"): 3,
    ("Eyes", "Third Eyelashes"): 3,
    ("Body", "Straight Wood"): 2,
    ("Clothing", "Banana Dress"): 2,
    ("Head", "Saiyan"): 1,
    ("Head", "Super Saiyan"): 1,
}


async def _layer_tree():
    store = get_layer_store()
    tree: dict[str, dict[str, list[str]]] = {}
    for body in await store.list_bodies():
        tree[body] = {}
        for trait_type in await store.list_trait_types(body):
            tree[body][trait_type] = await store.list_values(body, trait_type)
    return tree


def main() -> None:
    parser = argparse.ArgumentParser(description="Rebuild seasons.json from the premiere CSV")
    parser.add_argument("--csv", required=True, help="premiered_traits_by_season.csv export")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    records: list[tuple[str, list[str], int]] = []
    with open(args.csv, newline="") as f:
        for row in csv.DictReader(f):
            category = row["category"]
            if category in SKIP_CATEGORIES:
                continue
            season = int(row["premiere_season"].removeprefix("Season "))
            names = [row["trait_name"]] + [
                v.strip() for v in row["collapsed_variant_names"].split("|") if v.strip()
            ]
            records.append((category, names, season))
    if not records:
        sys.exit(f"{args.csv} has no usable rows")

    tree = asyncio.run(_layer_tree())
    csv_manifest = seasons.build_premiere_manifest(records, tree, aliases=ALIASES)
    manifest = seasons.build_premiere_manifest(records, tree, aliases=ALIASES, overrides=OVERRIDES)
    for key in sorted(k for k in manifest if k in csv_manifest and manifest[k] != csv_manifest[k]):
        print(f"OVERRIDE beats CSV: {key} S{csv_manifest[key]} -> S{manifest[key]}")

    # Matching is case-insensitive (build_premiere_manifest lowercases both
    # sides), so the unmatched report must compare lowercased too.
    matched_pairs = {(c, v.lower()) for c, v in (tuple(k.split("/", 2)[1:]) for k in manifest)}
    unmatched = 0
    for category, names, season in records:
        names = [ALIASES.get((category, n), n) for n in names]
        stems = {seasons.strip_dup_suffix(n.removeprefix("z9,")).lower() for n in names}
        if all(s != "none" and (category, s) not in matched_pairs for s in stems):
            unmatched += 1
            print(f"UNMATCHED (S{season}, not in any body's layer store): {category}/{names[0]}")

    per_season: dict[int, int] = {}
    for s in manifest.values():
        per_season[s] = per_season.get(s, 0) + 1
    print(
        f"{len(records)} CSV rows -> {len(manifest)} manifest entries "
        f"{dict(sorted(per_season.items()))}; {unmatched} unmatched"
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
