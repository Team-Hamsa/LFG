# Phase 2 economy_store additions: closet_tokens + supply_changes + the
# replace-all bucket-contents helper used by both the flows and the listener.

import sqlite3

from lfg_core import economy_store as es


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    es.init_economy_schema(c)
    return c


def test_supply_change_roundtrip():
    c = _conn()
    es.record_supply_change(
        c,
        "mint",
        3536,
        "Straight Blue",
        "male",
        {"Head|None": 1, "Background|Blue": 1},
        "script",
        "test mint",
    )
    rows = es.read_supply_changes(c)
    assert len(rows) == 1
    assert rows[0]["kind"] == "mint"
    assert rows[0]["edition"] == 3536
    assert rows[0]["body_value"] == "Straight Blue"
    assert rows[0]["trait_deltas"]["Head|None"] == 1


def test_supply_changes_ordered():
    c = _conn()
    es.record_supply_change(c, "mint", 1, "B1", "male", {}, "a", "first")
    es.record_supply_change(c, "burn", 2, "B2", "ape", {}, "a", "second")
    rows = es.read_supply_changes(c)
    assert [r["reason"] for r in rows] == ["first", "second"]


def test_set_closet_contents_replaces():
    c = _conn()
    es.set_closet_contents(c, "rUser", [("Head", "None", 2)], [3536])
    es.set_closet_contents(c, "rUser", [("Eyes", "Blue", 1)], [])
    assert es.read_closet_assets(c) == [("rUser", "Eyes", "Blue", 1)]
    assert es.read_closet_bodies(c) == []


def test_set_closet_contents_drops_nonpositive():
    c = _conn()
    es.set_closet_contents(c, "rUser", [("Head", "None", 0), ("Eyes", "Blue", 3)], [])
    assert es.read_closet_assets(c) == [("rUser", "Eyes", "Blue", 3)]


def test_set_closet_contents_is_per_owner():
    c = _conn()
    es.set_closet_contents(c, "rA", [("Head", "None", 1)], [1])
    es.set_closet_contents(c, "rB", [("Eyes", "Red", 1)], [2])
    es.set_closet_contents(c, "rA", [("Head", "None", 5)], [1])  # only rA replaced
    assets = {(o, s, v): n for o, s, v, n in es.read_closet_assets(c)}
    assert assets[("rA", "Head", "None")] == 5
    assert assets[("rB", "Eyes", "Red")] == 1


def test_closet_token_roundtrip():
    c = _conn()
    es.set_closet_token(c, "rUser", "NFTID", "ABCD")
    assert es.get_closet_token(c, "rUser") == ("NFTID", "ABCD")
    assert es.get_closet_token(c, "rNope") is None
    es.set_closet_token(c, "rUser", "NFTID", "EF01")  # uri update in place
    assert es.get_closet_token(c, "rUser") == ("NFTID", "EF01")
