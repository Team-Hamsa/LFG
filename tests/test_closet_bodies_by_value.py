# Task 2: bodies stored by value as ordinary "Body" closet assets (schema v2).

import os
import sqlite3

os.environ.setdefault("BUNNY_PULL_ZONE", "test.b-cdn.net")
os.environ.setdefault("LAYER_SOURCE", "local")

from lfg_core import closet_token as bt
from lfg_core import economy_store as es
from lfg_core import trait_economy as te


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    es.init_economy_schema(c)
    return c


def test_closet_assets_round_trips_body_slot_row():
    conn = _conn()
    es.set_closet_contents(conn, "rUser", [("Body", "Milady", 2)], [])
    assets = {(o, s, v): n for o, s, v, n in es.read_closet_assets(conn)}
    assert assets[("rUser", "Body", "Milady")] == 2


def test_build_closet_metadata_puts_body_rows_in_assets_and_bodies_empty():
    meta = bt.build_closet_metadata("rUser", [("Body", "Milady", 1)], [])
    assert meta["lfg_closet"]["bodies"] == []
    assert {"slot": "Body", "value": "Milady", "count": 1} in meta["lfg_closet"]["assets"]


def test_parse_closet_metadata_converts_legacy_editions_via_genesis():
    # Simulate a pre-migration on-chain Closet token: raw "bodies" editions,
    # no Body rows in "assets" (the shape build_closet_metadata used to write).
    legacy_meta = {
        "lfg_closet": {
            "assets": [{"slot": "Head", "value": "None", "count": 1}],
            "bodies": [3],
        }
    }
    genesis = te.Genesis(trait_counts={}, edition_bodies={3: ("Milady", "milady")})

    assets, legacy_editions = bt.parse_closet_metadata(legacy_meta, genesis=genesis)

    assert ("Body", "Milady", 1) in assets
    assert ("Head", "None", 1) in assets
    assert legacy_editions == []


def test_parse_closet_metadata_without_genesis_returns_legacy_editions_unconverted():
    legacy_meta = {"lfg_closet": {"assets": [], "bodies": [3]}}
    assets, legacy_editions = bt.parse_closet_metadata(legacy_meta)
    assert assets == []
    assert legacy_editions == [3]


def test_parse_closet_metadata_unknown_edition_retained_not_dropped():
    # An edition not in the genesis must survive in legacy_editions — the token
    # is authoritative, so dropping it would lose the body on a listener rebuild.
    legacy_meta = {"lfg_closet": {"assets": [], "bodies": [999]}}
    genesis = te.Genesis(trait_counts={}, edition_bodies={})
    assets, legacy_editions = bt.parse_closet_metadata(legacy_meta, genesis=genesis)
    assert assets == []
    assert legacy_editions == [999]


def test_parse_closet_metadata_mixed_known_and_unknown_editions():
    legacy_meta = {"lfg_closet": {"assets": [], "bodies": [3, 999, 7]}}
    genesis = te.Genesis(
        trait_counts={},
        edition_bodies={3: ("Milady", "milady"), 7: ("Skeleton", "skeleton")},
    )
    assets, legacy_editions = bt.parse_closet_metadata(legacy_meta, genesis=genesis)
    assert ("Body", "Milady", 1) in assets
    assert ("Body", "Skeleton", 1) in assets
    # Only the unknown edition remains unresolved.
    assert legacy_editions == [999]


def test_build_closet_metadata_carries_unresolved_legacy_editions():
    # When unknown editions are passed as bodies, they must be written to the
    # token so they stay on the authoritative on-chain record.
    meta = bt.build_closet_metadata("rUser", [("Head", "None", 1)], [999, 42])
    assert meta["lfg_closet"]["bodies"] == [42, 999]
