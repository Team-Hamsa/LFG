#!/usr/bin/env python3
"""Move byte-identical universal layer values into layers/shared/<type>/.

Verify-then-move: a trait value is only migrated when it is present in ALL
four body directories (ape/female/male/skeleton) with byte-identical file
contents. Anything not present everywhere, or present but divergent, is
skipped and reported — never guessed or force-merged.

Values not migrated stay served exactly as before: LocalLayerStore/
CdnLayerStore already union in shared/ (Task 17), so mint/swap/rendering see
no difference before or after this script runs.

seasons.json bookkeeping (folded into the --execute path, not a separate
script): when a value moves, its four per-body season-manifest keys
(<body>/<type>/<value>) collapse into one shared/<type>/<value> entry. If the
four per-body entries disagree on season, the MINIMUM season (earliest
premiere) is kept and the disagreement is reported. A missing manifest file,
or a moved value with no matching per-body key at all, is a no-op for the
manifest — nothing is invented.

Usage:
  python scripts/migrate_shared_layers.py                      # dry-run (default)
  python scripts/migrate_shared_layers.py --execute             # move files for real
  python scripts/migrate_shared_layers.py --trait-types Background Back Accessory --execute
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BODIES = ["ape", "female", "male", "skeleton"]
EXTS = (".png", ".gif", ".mp4")


def _digest(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _rewrite_seasons(
    manifest_path: str,
    moved: list[tuple[str, str]],
    dry_run: bool,
) -> list[tuple[str, str, dict[str, int]]]:
    """Collapse the four per-body season-manifest keys of each moved value into
    one shared/<type>/<value> key (keeping the MINIMUM season on disagreement).
    Returns the list of (trait_type, value, {body: season}) disagreements found.
    Missing manifest file, or a moved value absent from the manifest entirely,
    is a no-op. Never writes when dry_run is True."""
    if not moved or not os.path.isfile(manifest_path):
        return []
    with open(manifest_path) as f:
        manifest: dict[str, int] = json.load(f)

    conflicts: list[tuple[str, str, dict[str, int]]] = []
    changed = False
    for trait_type, value in moved:
        per_body: dict[str, int] = {}
        for body in BODIES:
            key = f"{body}/{trait_type}/{value}"
            if key in manifest:
                per_body[body] = manifest[key]
        if not per_body:
            continue  # value not in manifest at all = no-op
        seasons = set(per_body.values())
        if len(seasons) > 1:
            conflicts.append((trait_type, value, dict(per_body)))
        min_season = min(seasons)
        for body in per_body:
            del manifest[f"{body}/{trait_type}/{value}"]
        manifest[f"shared/{trait_type}/{value}"] = min_season
        changed = True

    if changed and not dry_run:
        tmp_path = manifest_path + ".part"
        with open(tmp_path, "w") as f:
            json.dump(manifest, f, indent=1, sort_keys=True)
            f.write("\n")
        os.replace(tmp_path, manifest_path)

    return conflicts


def migrate(
    layers_dir: str,
    trait_types: list[str],
    dry_run: bool = True,
    seasons_manifest: str | None = None,
) -> dict[str, Any]:
    if seasons_manifest is None:
        seasons_manifest = os.path.join(layers_dir, "seasons.json")

    moved: list[tuple[str, str]] = []
    skipped: list[tuple[str, str, str]] = []
    for trait_type in trait_types:
        values: set[str] = set()
        for body in BODIES:
            d = os.path.join(layers_dir, body, trait_type)
            if os.path.isdir(d):
                values |= {
                    os.path.splitext(f)[0]
                    for f in os.listdir(d)
                    if os.path.splitext(f)[1].lower() in EXTS and not f.startswith(".")
                }
        for value in sorted(values):
            paths = []
            for body in BODIES:
                for ext in EXTS:
                    p = os.path.join(layers_dir, body, trait_type, value + ext)
                    if os.path.isfile(p):
                        paths.append(p)
                        break
            if len(paths) < len(BODIES):
                skipped.append((trait_type, value, "not-in-all-bodies"))
                continue
            if len({_digest(p) for p in paths}) != 1:
                skipped.append((trait_type, value, "divergent"))
                continue
            dest_dir = os.path.join(layers_dir, "shared", trait_type)
            dest = os.path.join(dest_dir, os.path.basename(paths[0]))
            moved.append((trait_type, value))
            if not dry_run:
                os.makedirs(dest_dir, exist_ok=True)
                shutil.copy2(paths[0], dest)
                for p in paths:
                    os.remove(p)

    season_conflicts = _rewrite_seasons(seasons_manifest, moved, dry_run)

    return {"moved": moved, "skipped": skipped, "season_conflicts": season_conflicts}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--layers-dir", default="layers")
    p.add_argument("--trait-types", nargs="+", default=["Background", "Back"])
    p.add_argument(
        "--seasons-manifest",
        default=None,
        help="season manifest to rewrite moved-value keys in (default: <layers-dir>/seasons.json)",
    )
    p.add_argument("--execute", action="store_true", help="default is dry-run")
    args = p.parse_args()
    seasons_manifest = args.seasons_manifest or os.path.join(args.layers_dir, "seasons.json")
    result = migrate(
        args.layers_dir,
        args.trait_types,
        dry_run=not args.execute,
        seasons_manifest=seasons_manifest,
    )
    for t, v in result["moved"]:
        print(f"{'would move' if not args.execute else 'moved'}: {t}/{v}")
    for t, v, why in result["skipped"]:
        print(f"skipped ({why}): {t}/{v}")
    for t, v, per_body in result["season_conflicts"]:
        print(f"season conflict ({t}/{v}): {per_body} -> kept min={min(per_body.values())}")


if __name__ == "__main__":
    main()
