# Tests for #494 Option A: read-only mint-odds transparency
# (lfg_core.rarity.mint_odds). The parity requirement is exact -- mint_odds's
# odds_pct for every live candidate must equal weighted_pick's own weight,
# normalized. Captured with the CaptureRng technique from
# tests/test_rarity_cap.py / tests/test_simulate_rarity.py: never re-derive
# the weight math independently in this test, or a second implementation
# could silently drift from the real picker and this test would not catch it.
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lfg_core import config, rarity  # noqa: E402

NET = "testnet"
BODY = "male"
CATEGORY = "Clothing"
NOW = datetime(2026, 9, 17, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn(monkeypatch):
    # available_values() would otherwise do a real layer-store scan (the repo
    # default env sets LAYER_SOURCE=local); pin it off so the candidate set
    # this test controls (via `available` passed straight to weighted_pick)
    # is exactly what mint_odds falls back to as well -- deterministic
    # regardless of whatever happens to be on disk in this checkout.
    monkeypatch.setattr(config, "LAYER_SOURCE", "cdn")
    c = sqlite3.connect(":memory:")
    c.execute("""CREATE TABLE LFG (
        nft_number INTEGER PRIMARY KEY, nft_id TEXT, discord_id TEXT,
        owner_address TEXT, metadata_url TEXT, image_url TEXT,
        Background TEXT, Back TEXT, Body TEXT, Clothing TEXT, Eyes TEXT,
        Eyebrows TEXT, Mouth TEXT, Hat TEXT, Accessory TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    c.execute("""CREATE TABLE burned_nfts (
        nft_number INTEGER PRIMARY KEY, nft_id TEXT, discord_id TEXT,
        burned_by TEXT, reason TEXT,
        burned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        original_mint_time TIMESTAMP)""")
    rarity.ensure_schema(c)
    yield c
    c.close()


class CaptureRng:
    """Stand-in rng that records exactly what weighted_pick hands to
    rng.choices -- the real traits/weights a live mint would draw from."""

    def __init__(self):
        self.traits: list[str] = []
        self.weights: list[float] = []

    def choices(self, traits, weights, k=1):
        self.traits, self.weights = list(traits), list(weights)
        return [traits[0]]


def _seed(conn, trait, count, **kw):
    conn.execute(
        """INSERT INTO trait_rarity (network, body, category, trait,
           live_count, floor_weight, boost_initial, boost_step_hours,
           boost_started_at, enabled)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (
            NET,
            BODY,
            CATEGORY,
            trait,
            count,
            kw.get("floor_weight", 0.005),
            kw.get("boost_initial"),
            kw.get("boost_step_hours", 24),
            kw.get("boost_started_at"),
            kw.get("enabled", 1),
        ),
    )


def _seed_lfg(conn, values):
    """Insert matching LFG rows so weighted_pick's staleness check sees the
    same sums as the trait_rarity rows above -- a mismatch would trigger
    recalculate_rarity, which is harmless here (it would reproduce the same
    counts) but this keeps the fixture's intent legible: what's seeded in
    trait_rarity IS the live collection, deliberately, not a coincidence."""
    n = 0
    for value, count in values:
        for _ in range(count):
            n += 1
            conn.execute(
                'INSERT INTO LFG (nft_number, "Clothing", body_type, network) VALUES (?, ?, ?, ?)',
                (n, value, BODY, NET),
            )


@pytest.fixture
def rarity_fixture(conn, monkeypatch):
    """One (body, category) population exercising all four odds archetypes
    in a single slot: a dominant trait clamped by the #198 share cap
    ("Capped"), a zero-mint trait pinned to its own floor ("Floor"), an
    actively-boosted trait ("Boosted"), and a parked trait excluded from
    picks entirely ("Parked", enabled=0)."""
    monkeypatch.setattr(config, "RARITY_CAP_MULTIPLE", 1.2)
    _seed_lfg(conn, [("Capped", 88), ("Boosted", 2), ("Parked", 10)])
    _seed(conn, "Capped", 88)
    _seed(conn, "Floor", 0, floor_weight=0.02)
    _seed(
        conn,
        "Boosted",
        2,
        boost_initial=7.0,
        boost_started_at=(NOW - timedelta(hours=1)).isoformat(),
    )
    _seed(conn, "Parked", 10, enabled=0)
    conn.commit()
    return conn


def test_mint_odds_matches_weighted_pick_weights(rarity_fixture):
    conn = rarity_fixture
    available = ["Capped", "Floor", "Boosted", "Parked"]
    rng = CaptureRng()
    rarity.weighted_pick(conn, BODY, CATEGORY, available, network=NET, now=NOW, rng=rng)
    captured = dict(zip(rng.traits, rng.weights, strict=True))
    # Sanity: the fixture exercises all three live archetypes, and Parked
    # (disabled) never reaches weighted_pick's own candidate set at all.
    assert captured.keys() == {"Capped", "Floor", "Boosted"}
    total_weight = sum(captured.values())

    odds = rarity.mint_odds(conn, NET, BODY, now=NOW)
    rows = {r["value"]: r for r in odds[CATEGORY]}
    assert rows.keys() == {"Capped", "Floor", "Boosted", "Parked"}

    for trait, weight in captured.items():
        expected_pct = weight / total_weight * 100
        assert rows[trait]["odds_pct"] == pytest.approx(expected_pct, abs=1e-9)
        assert rows[trait]["enabled"] is True

    # Parked is excluded from the pick entirely: 0 odds, but still reported
    # (with its live count/share) so the client can show a "parked" badge
    # instead of the trait silently disappearing from the table.
    assert rows["Parked"]["odds_pct"] == 0.0
    assert rows["Parked"]["enabled"] is False
    assert rows["Parked"]["live_count"] == 10
    assert rows["Parked"]["share_pct"] == pytest.approx(10.0)  # 10 of 100 live NFTs


def test_mint_odds_capped_row_clamped(rarity_fixture):
    # Regression guard for the "capped" archetype specifically: without the
    # #198 ceiling, Capped's raw smoothed share would run away (~86%); the
    # cap must visibly clamp it, and the ranking must still make sense.
    conn = rarity_fixture
    odds = rarity.mint_odds(conn, NET, BODY, now=NOW)
    rows = {r["value"]: r for r in odds[CATEGORY]}
    assert rows["Capped"]["odds_pct"] < 80.0
    assert rows["Capped"]["odds_pct"] > rows["Boosted"]["odds_pct"] > rows["Floor"]["odds_pct"] > 0


def test_mint_odds_slot_sums_to_100(rarity_fixture):
    conn = rarity_fixture
    odds = rarity.mint_odds(conn, NET, BODY, now=NOW)
    assert sum(r["odds_pct"] for r in odds[CATEGORY]) == pytest.approx(100.0)


def test_mint_odds_omits_slots_with_no_rows(rarity_fixture):
    conn = rarity_fixture
    odds = rarity.mint_odds(conn, NET, BODY, now=NOW)
    # Only Clothing has any trait_rarity rows for this body in the fixture.
    assert list(odds.keys()) == ["Clothing"]


def test_mint_odds_is_pure_no_writes(rarity_fixture):
    conn = rarity_fixture
    before = conn.execute("SELECT COUNT(*) FROM trait_rarity").fetchone()[0]
    rarity.mint_odds(conn, NET, BODY, now=NOW)
    after = conn.execute("SELECT COUNT(*) FROM trait_rarity").fetchone()[0]
    assert after == before


def test_mint_odds_network_isolated(conn):
    _seed(conn, "Common", 100)
    conn.commit()
    assert rarity.mint_odds(conn, "mainnet", BODY, now=NOW) == {}
