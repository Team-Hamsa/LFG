# Tests for #494 Option A: read-only mint-odds transparency
# (lfg_core.rarity.mint_odds / stale_categories). The parity requirement is
# exact -- mint_odds's odds_pct for every live candidate must equal
# weighted_pick's own weight, normalized. Captured with the CaptureRng
# technique from tests/test_rarity_cap.py / tests/test_simulate_rarity.py:
# never re-derive the weight math independently in this test, or a second
# implementation could silently drift from the real picker and this test
# would not catch it.
#
# #565 review round 1 added two things this file also covers:
#   - mint_odds must apply trait_config.value_allowed (body affinity) the
#     same way traits.py::select_random_attributes does before ever calling
#     weighted_pick -- a body-restricted trait must not inflate its own odds
#     or deflate everyone else's in the slot.
#   - stale_categories is read-only (never calls recalculate_rarity).
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lfg_core import config, rarity, trait_config  # noqa: E402

NET = "testnet"
BODY = "male"
CATEGORY = "Clothing"
NOW = datetime(2026, 9, 17, 12, 0, 0, tzinfo=timezone.utc)


def _fake_cfg():
    """A minimal TraitConfig whose only rule restricts "Restricted" to
    female bodies -- so value_allowed(BODY="male", ...) is False for it,
    isolating the one predicate mint_odds must now apply, independent of
    whatever the real trait_config.yaml happens to say today."""
    return trait_config.TraitConfig(
        layers=(),
        z_overrides=(),
        affinity={CATEGORY: {"Restricted": ["female"]}},
        universal_layers=frozenset(),
        swap_pairs=(),
        exclusions=(),
    )


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
    """One (body, category) population exercising all five odds archetypes
    in a single slot: a dominant trait clamped by the #198 share cap
    ("Capped"), a zero-mint trait pinned to its own floor ("Floor"), an
    actively-boosted trait ("Boosted"), a parked trait excluded from picks
    entirely ("Parked", enabled=0), and a trait that's admin-enabled but
    body-restricted away from BODY by trait_config affinity ("Restricted") --
    #565 review round 1's addition, since mint_odds must now apply that rule
    too, not just `enabled`."""
    monkeypatch.setattr(config, "RARITY_CAP_MULTIPLE", 1.2)
    monkeypatch.setattr(trait_config, "get_config", lambda path=None: _fake_cfg())
    _seed_lfg(conn, [("Capped", 88), ("Boosted", 2), ("Parked", 10), ("Restricted", 5)])
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
    _seed(conn, "Restricted", 5)
    conn.commit()
    return conn


TOTAL_LIVE = 88 + 0 + 2 + 10 + 5  # Capped + Floor + Boosted + Parked + Restricted


def test_mint_odds_matches_weighted_pick_weights(rarity_fixture):
    conn = rarity_fixture
    cfg = trait_config.get_config()
    raw = ["Capped", "Floor", "Boosted", "Parked", "Restricted"]
    # Mirrors traits.py::select_random_attributes: the CALLER filters through
    # value_allowed before weighted_pick ever sees the candidate list --
    # weighted_pick itself has no idea trait_config exists.
    available = [v for v in raw if cfg.value_allowed(BODY, CATEGORY, v)]
    assert "Restricted" not in available  # sanity: the fake cfg excludes it for BODY="male"
    rng = CaptureRng()
    rarity.weighted_pick(conn, BODY, CATEGORY, available, network=NET, now=NOW, rng=rng)
    captured = dict(zip(rng.traits, rng.weights, strict=True))
    # Sanity: the fixture exercises all three live archetypes, and Parked
    # (disabled) never reaches weighted_pick's own candidate set at all.
    assert captured.keys() == {"Capped", "Floor", "Boosted"}
    total_weight = sum(captured.values())

    odds = rarity.mint_odds(conn, NET, BODY, now=NOW)
    rows = {r["value"]: r for r in odds[CATEGORY]}
    assert rows.keys() == {"Capped", "Floor", "Boosted", "Parked", "Restricted"}

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
    assert rows["Parked"]["share_pct"] == pytest.approx(10 / TOTAL_LIVE * 100)

    # Restricted: enabled=1 (not admin-parked) but value_allowed=False for
    # BODY -- the #565 Greptile P1 fix. It keeps its true historical share
    # (it minted before the rule applied, or on a different body) but must
    # never contribute odds or inflate the candidate_count denominator.
    assert rows["Restricted"]["odds_pct"] == 0.0
    assert rows["Restricted"]["enabled"] is True
    assert rows["Restricted"]["live_count"] == 5
    assert rows["Restricted"]["share_pct"] == pytest.approx(5 / TOTAL_LIVE * 100)


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


# ---------------------------------------------------------------------------
# stale_categories (#565 review round 1): read-only staleness signal for a
# public endpoint that must never trigger weighted_pick's write-on-stale
# behavior (recalculate_rarity) itself.
# ---------------------------------------------------------------------------


def test_stale_categories_flags_mismatched_category(conn):
    # A trait_rarity row whose cached live_count disagrees with the LFG
    # table's actual count for that (network, category) -- no matching LFG
    # rows were inserted, so the cache is wrong on its face.
    _seed(conn, "Ghost", 999)
    conn.commit()
    assert rarity.stale_categories(conn, NET, [CATEGORY]) == [CATEGORY]


def test_stale_categories_empty_when_fresh(rarity_fixture):
    conn = rarity_fixture
    # rarity_fixture's _seed_lfg calls kept trait_rarity in lockstep with LFG.
    assert rarity.stale_categories(conn, NET, [CATEGORY]) == []


def test_stale_categories_ignores_categories_with_no_rows(rarity_fixture):
    conn = rarity_fixture
    # A category nothing was ever seeded for is trivially not stale (0 == 0),
    # and must never be reported just because mint_odds also skips it.
    assert rarity.stale_categories(conn, NET, ["Background"]) == []


def test_stale_categories_only_reports_the_requested_ones(conn):
    _seed(conn, "Ghost", 999)  # Clothing: stale
    conn.commit()
    assert rarity.stale_categories(conn, NET, ["Background", CATEGORY]) == [CATEGORY]


def test_stale_categories_never_writes(conn):
    _seed(conn, "Ghost", 999)
    conn.commit()
    before = conn.execute("SELECT live_count FROM trait_rarity WHERE trait='Ghost'").fetchone()[0]
    result = rarity.stale_categories(conn, NET, [CATEGORY])
    after = conn.execute("SELECT live_count FROM trait_rarity WHERE trait='Ghost'").fetchone()[0]
    assert result == [CATEGORY]  # confirms the check actually ran and saw the mismatch
    assert after == before == 999  # and never "fixed" it via recalculate_rarity
