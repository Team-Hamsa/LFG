# tests/test_seasons.py
# Env-guard preamble: importing lfg_core.config freezes its constants (e.g.
# IMG_PROXY_ALLOWED_BASES, LAYER_SOURCE) at import time; set the same defaults
# test_smoke.py uses so collection order can't strand them. (Copy the block
# verbatim from tests/test_server_identity_wiring.py — same keys/values.)
import os

os.environ.setdefault("XUMM_API_KEY", "test")
os.environ.setdefault("XUMM_API_SECRET", "test")
os.environ.setdefault("SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2")
os.environ.setdefault("TOKEN_ISSUER_ADDRESS", "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
os.environ.setdefault("TOKEN_CURRENCY_HEX", "4C46474F00000000000000000000000000000000")
os.environ.setdefault("BUNNY_CDN_ACCESS_KEY", "test")
os.environ.setdefault("BUNNY_CDN_STORAGE_ZONE", "test")
os.environ.setdefault("LAYER_SOURCE", "local")
os.environ.setdefault("BUNNY_PULL_ZONE", "nft.pullzone.example")

import json  # noqa: E402
import sqlite3  # noqa: E402

import pytest  # noqa: E402

from lfg_core import rarity, seasons  # noqa: E402

# ---------------------------------------------------------------------------
# Manifest load / lookup
# ---------------------------------------------------------------------------


def test_load_seasons_missing_file_returns_empty(tmp_path):
    assert seasons.load_seasons(str(tmp_path / "nope.json")) == {}


def test_get_season_lookup(tmp_path):
    path = tmp_path / "seasons.json"
    path.write_text(json.dumps({"male/Eyes/Laser": 3}))
    manifest = seasons.load_seasons(str(path))
    assert seasons.get_season("male", "Eyes", "Laser", manifest=manifest) == 3
    assert seasons.get_season("male", "Eyes", "Classic", manifest=manifest) is None


# ---------------------------------------------------------------------------
# build_manifest — seed from the S3 CSV export
# ---------------------------------------------------------------------------

LAYER_TREE = {
    "male": {
        "Background": ["Laflame", "Sky"],
        "Eyes": ["Laser", "Classic"],
        "Accessory": ["Basketball", "Cane"],
    },
    "female": {
        "Background": ["Laflame", "Sky"],
        "Eyes": ["Classic"],
        "Accessory": ["Basketball"],
    },
    "ape": {
        "Background": ["Laflame", "Sky"],
        "Eyes": ["Laser", "Wavy"],
    },
}


def test_build_manifest_maps_body_prefixed_categories():
    manifest = seasons.build_manifest(["Male Eyes/Laser.png"], LAYER_TREE, season=3)
    assert manifest["male/Eyes/Laser"] == 3


def test_build_manifest_background_applies_to_all_bodies():
    manifest = seasons.build_manifest(["Background/Laflame.png"], LAYER_TREE, season=3)
    assert manifest["male/Background/Laflame"] == 3
    assert manifest["female/Background/Laflame"] == 3
    assert manifest["ape/Background/Laflame"] == 3
    assert "male/Background/Sky" not in manifest


def test_build_manifest_strips_duplicate_suffix():
    manifest = seasons.build_manifest(["Male Accessory/Basketball#1.png"], LAYER_TREE, season=3)
    assert manifest["male/Accessory/Basketball"] == 3


def test_build_manifest_propagates_across_bodies_with_same_art():
    # The ape/skeleton stores were built from the same art: a season-3 trait
    # name tags every body that carries it, even bodies absent from the CSV.
    manifest = seasons.build_manifest(["Male Eyes/Laser.png"], LAYER_TREE, season=3)
    assert manifest["ape/Eyes/Laser"] == 3


def test_build_manifest_skips_values_absent_from_store():
    # Female store never received "Wavy" eyes; only bodies that actually
    # carry the file get an entry.
    manifest = seasons.build_manifest(["Female Eyes/Wavy.png"], LAYER_TREE, season=3)
    assert manifest == {"ape/Eyes/Wavy": 3}


def test_build_manifest_skips_none_sentinel():
    # Exports include "None.png" placeholders; "None" is the absent-trait
    # option present in every season and must never be tagged (disabling it
    # would force the trait on every mint).
    tree = {"male": {"Accessory": ["None", "Cane"]}}
    assert seasons.build_manifest(["Male Accessory/None.png"], tree, season=3) == {}


# ---------------------------------------------------------------------------
# build_premiere_manifest — seed from the all-seasons premiere CSV
# ---------------------------------------------------------------------------


def test_premiere_manifest_matches_across_bodies_case_insensitive():
    tree = {"male": {"Eyes": ["Laser"]}, "ape": {"Eyes": ["Laser"]}}
    records = [("Eyes", ["laser"], 3)]
    manifest = seasons.build_premiere_manifest(records, tree)
    assert manifest == {"male/Eyes/Laser": 3, "ape/Eyes/Laser": 3}


def test_premiere_manifest_strips_z9_prefix_and_dup_suffix():
    # Codex export left the "z9," layer-ordering prefix in a few names.
    tree = {"male": {"Eyes": ["Laser", "Wavy#1"]}}
    records = [("Eyes", ["z9,Laser"], 3), ("Eyes", ["z9,Wavy"], 3)]
    manifest = seasons.build_premiere_manifest(records, tree)
    assert manifest == {"male/Eyes/Laser": 3, "male/Eyes/Wavy#1": 3}


def test_premiere_manifest_falls_back_to_variant_names():
    # trait_name "Blackhole" collapsed from variants "Blackhole STATIC" etc.;
    # the store may only carry a variant spelling.
    tree = {"male": {"Background": ["Blackhole STATIC"]}}
    records = [("Background", ["Blackhole", "Blackhole STATIC"], 1)]
    manifest = seasons.build_premiere_manifest(records, tree)
    assert manifest == {"male/Background/Blackhole STATIC": 1}


def test_premiere_manifest_applies_aliases():
    tree = {"male": {"Clothing": ["Prison Jumpsuit"]}}
    records = [("Clothing", ["Prisoner Jumpsuit"], 3)]
    aliases = {("Clothing", "Prisoner Jumpsuit"): "Prison Jumpsuit"}
    manifest = seasons.build_premiere_manifest(records, tree, aliases=aliases)
    assert manifest == {"male/Clothing/Prison Jumpsuit": 3}


def test_premiere_manifest_applies_overrides_for_traits_absent_from_csv():
    tree = {"male": {"Eyes": ["Third Eye"]}, "female": {"Eyes": ["Third Eye"]}}
    manifest = seasons.build_premiere_manifest([], tree, overrides={("Eyes", "Third Eye"): 3})
    assert manifest == {"male/Eyes/Third Eye": 3, "female/Eyes/Third Eye": 3}


def test_premiere_manifest_skips_none_sentinel():
    tree = {"male": {"Back": ["None", "Angel Wings"]}}
    records = [("Back", ["None"], 3)]
    assert seasons.build_premiere_manifest(records, tree) == {}


# ---------------------------------------------------------------------------
# disable_season — flip trait_rarity.enabled=0 for a season, guarded
# ---------------------------------------------------------------------------


def _seeded_conn(layer_values):
    conn = sqlite3.connect(":memory:")
    conn.execute("""CREATE TABLE LFG (
        nft_number INTEGER PRIMARY KEY, nft_id TEXT, discord_id TEXT,
        owner_address TEXT, metadata_url TEXT, image_url TEXT,
        Background TEXT, Back TEXT, Body TEXT, Clothing TEXT, Eyes TEXT,
        Eyebrows TEXT, Mouth TEXT, Hat TEXT, Accessory TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    conn.execute("""CREATE TABLE burned_nfts (
        nft_number INTEGER PRIMARY KEY, nft_id TEXT, discord_id TEXT,
        burned_by TEXT, reason TEXT,
        burned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        original_mint_time TIMESTAMP)""")
    rarity.ensure_schema(conn)
    rarity.seed_from_collection(conn, network="mainnet", layer_values=layer_values)
    return conn


def _enabled(conn, body, category, trait):
    return conn.execute(
        "SELECT enabled FROM trait_rarity WHERE network='mainnet' AND body=? AND category=? AND trait=?",
        (body, category, trait),
    ).fetchone()[0]


def test_disable_season_flips_only_matching_rows():
    conn = _seeded_conn({"male": {"Eyes": ["Laser", "Classic"]}})
    manifest = {"male/Eyes/Laser": 3}
    changed = seasons.disable_season(conn, manifest, season=3, network="mainnet")
    assert ("male", "Eyes", "Laser") in changed
    assert _enabled(conn, "male", "Eyes", "Laser") == 0
    assert _enabled(conn, "male", "Eyes", "Classic") == 1


def test_disable_season_guard_refuses_to_empty_a_category():
    conn = _seeded_conn({"male": {"Eyes": ["Laser", "Wavy"]}})
    manifest = {"male/Eyes/Laser": 3, "male/Eyes/Wavy": 3}
    with pytest.raises(ValueError, match="male/Eyes"):
        seasons.disable_season(conn, manifest, season=3, network="mainnet")
    # Guard aborts before any change.
    assert _enabled(conn, "male", "Eyes", "Laser") == 1


def test_disable_season_ignores_other_seasons_and_networks():
    conn = _seeded_conn({"male": {"Eyes": ["Laser", "Classic"]}})
    manifest = {"male/Eyes/Laser": 2}
    changed = seasons.disable_season(conn, manifest, season=3, network="mainnet")
    assert changed == []
    assert _enabled(conn, "male", "Eyes", "Laser") == 1
