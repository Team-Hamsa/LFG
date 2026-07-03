# lfg_core/seasons.py
# Season metadata for trait layers (#114).
#
# Sidecar manifest `layers/seasons.json` maps "body/Category/Value" -> season
# number. A sidecar (not file renames) keeps the layer tree untouched so
# rendering, the CDN mirror, and existing NFT metadata stay valid. Traits
# absent from the manifest have unknown/earlier season (get_season -> None).
#
# Seed the manifest from a season's CSV export with
# scripts/seed_seasons_from_csv.py; exclude a season from minting with
# scripts/disable_season_traits.py (flips trait_rarity.enabled=0 — mint-only:
# swaps and rendering read the layer store directly and are unaffected).

import json
import os
import re
import sqlite3

from lfg_core import config

_DUP_SUFFIX = re.compile(r"#\d+$")


def strip_dup_suffix(value: str) -> str:
    """Drop a trailing "#N" duplicate-export suffix ("Basketball#1" -> "Basketball")."""
    return _DUP_SUFFIX.sub("", value)


def manifest_path() -> str:
    return os.path.join(config.LAYERS_DIR, "seasons.json")


def load_seasons(path: str | None = None) -> dict[str, int]:
    """Manifest as {"body/Category/Value": season}; {} if no manifest yet."""
    path = path or manifest_path()
    if not os.path.isfile(path):
        return {}
    with open(path) as f:
        return {str(k): int(v) for k, v in json.load(f).items()}


def get_season(
    body: str, category: str, value: str, *, manifest: dict[str, int] | None = None
) -> int | None:
    manifest = load_seasons() if manifest is None else manifest
    return manifest.get(f"{body}/{category}/{value}")


def build_manifest(
    csv_paths: list[str],
    layer_tree: dict[str, dict[str, list[str]]],
    *,
    season: int,
) -> dict[str, int]:
    """Tag layer-store traits named by a season's CSV export.

    csv_paths are the export's relative paths ("Male Eyes/Laser.png",
    "Background/Laflame.png"). Rules:
    - the file stem is the trait value; a trailing "#N" duplicate-export
      suffix is stripped;
    - "Background" has no body prefix and applies to every body;
    - body-prefixed categories ("Male Eyes") name the source body, but the
      ape/skeleton stores were built from the same art, so the (category,
      value) pair tags EVERY body whose store carries that file;
    - values not present in any body's store are skipped (renamed or never
      shipped).
    """
    manifest: dict[str, int] = {}
    for rel in csv_paths:
        cat_dir, _, filename = rel.rpartition("/")
        value = strip_dup_suffix(os.path.splitext(filename)[0])
        if value == "None":
            # Absent-trait sentinel, present in every season — never tag it.
            continue
        category = cat_dir if cat_dir == "Background" else cat_dir.split(" ", 1)[-1]
        for body, categories in layer_tree.items():
            if value in categories.get(category, []):
                manifest[f"{body}/{category}/{value}"] = season
    return manifest


def build_premiere_manifest(
    records: list[tuple[str, list[str], int]],
    layer_tree: dict[str, dict[str, list[str]]],
    *,
    aliases: dict[tuple[str, str], str] | None = None,
    overrides: dict[tuple[str, str], int] | None = None,
) -> dict[str, int]:
    """Tag layer-store traits with their premiere season.

    records are (category, candidate_names, season) from the all-seasons
    premiere CSV — candidate_names is the collapsed trait_name plus its
    variant spellings. Matching is case-insensitive per category across every
    body; a stray "z9," layer-ordering prefix and a trailing "#N" duplicate
    suffix are stripped before comparison. "None" (the absent-trait sentinel)
    is never tagged. aliases maps a CSV (category, name) to the store's
    spelling; overrides force (category, store_value) -> season across all
    bodies. Overrides are applied last and take precedence over any
    CSV-derived season — they carry artist-confirmed corrections.
    """
    aliases = aliases or {}
    overrides = overrides or {}

    def norm(name: str) -> str:
        return strip_dup_suffix(name.removeprefix("z9,")).lower()

    # (category, normalized value) -> [(body, exact store value), ...]
    store: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for body, categories in layer_tree.items():
        for category, values in categories.items():
            for value in values:
                store.setdefault((category, norm(value)), []).append((body, value))

    manifest: dict[str, int] = {}
    for category, names, season in records:
        names = [aliases.get((category, n), n) for n in names]
        for name in names:
            key = norm(name)
            if key == "none":
                continue
            for body, value in store.get((category, key), []):
                manifest[f"{body}/{category}/{value}"] = season
    for (category, value), season in overrides.items():
        for body, exact in store.get((category, norm(value)), []):
            manifest[f"{body}/{category}/{exact}"] = season
    return manifest


def disable_season(
    conn: sqlite3.Connection,
    manifest: dict[str, int],
    *,
    season: int,
    network: str,
) -> list[tuple[str, str, str]]:
    """Set trait_rarity.enabled=0 for every manifest trait of `season` on
    `network`. Guarded: refuses (no changes at all) if any (body, category)
    would be left with zero enabled traits — weighted_pick raises on empty
    categories, which would break minting."""
    targets = [
        tuple(key.split("/", 2))
        for key, s in manifest.items()
        if s == season and len(key.split("/", 2)) == 3
    ]
    for body, category in {(b, c) for b, c, _ in targets}:
        disabled_values = {t for b, c, t in targets if (b, c) == (body, category)}
        survivors = conn.execute(
            """SELECT COUNT(*) FROM trait_rarity
               WHERE network=? AND body=? AND category=? AND enabled=1
                 AND trait NOT IN ({})""".format(",".join("?" * len(disabled_values))),
            (network, body, category, *disabled_values),
        ).fetchone()[0]
        if survivors == 0:
            raise ValueError(
                f"refusing: disabling season {season} would leave {body}/{category} "
                "with zero enabled traits"
            )
    changed: list[tuple[str, str, str]] = []
    for body, category, trait in targets:
        cur = conn.execute(
            """UPDATE trait_rarity SET enabled=0
               WHERE network=? AND body=? AND category=? AND trait=? AND enabled=1""",
            (network, body, category, trait),
        )
        if cur.rowcount:
            changed.append((body, category, trait))
    conn.commit()
    return changed
