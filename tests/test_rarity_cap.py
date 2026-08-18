# Tests for the share-ceiling cap in the variable rarity engine (#198):
# effective_weight clamps the share term at RARITY_CAP_MULTIPLE × fair share
# (1 / enabled-candidate-count), never below the floor, with boosts applied
# after the clamp. 0/unset disables the cap (behavior identical to before).
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lfg_core import config, rarity  # noqa: E402

NOW = datetime(2026, 8, 18, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn():
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


def weight(count, total, *, floor=0.005, candidates=0, cap=None, boost=None, started=None):
    return rarity.effective_weight(
        count,
        total,
        floor,
        boost,
        24,
        started,
        NOW,
        candidate_count=candidates,
        cap_multiple=cap,
    )


# ---------------------------------------------------------------------------
# effective_weight unit behavior
# ---------------------------------------------------------------------------


def test_runaway_share_clamped_to_ceiling():
    # 60% share in a 10-candidate category, cap 3× fair (fair=0.1) → 0.3.
    assert weight(60, 100, candidates=10, cap=3.0) == pytest.approx(0.3)


def test_below_ceiling_unchanged():
    # 5% share, ceiling 0.3 → untouched proportional weight.
    assert weight(5, 100, candidates=10, cap=3.0) == pytest.approx(0.05)


def test_flat_category_unaffected():
    # Perfectly flat: every trait at exactly fair share, well under 3× fair.
    uncapped = weight(10, 100, candidates=10, cap=0)
    assert weight(10, 100, candidates=10, cap=3.0) == pytest.approx(uncapped)


def test_floor_beats_cap():
    # Ceiling (3/10 = 0.3) below a 0.5 floor → ceiling raised to the floor;
    # a floor-clamped trait keeps its full floor weight.
    assert weight(0, 100, floor=0.5, candidates=10, cap=3.0) == pytest.approx(0.5)
    # And a runaway is clamped at the floor-raised ceiling, not below it.
    assert weight(90, 100, floor=0.5, candidates=10, cap=3.0) == pytest.approx(0.5)


def test_boost_applies_after_cap():
    started = (NOW - timedelta(hours=1)).isoformat()
    w = weight(60, 100, candidates=10, cap=3.0, boost=7.0, started=started)
    assert w == pytest.approx(0.3 * 7.0)


def test_cap_multiple_zero_disables():
    assert weight(60, 100, candidates=10, cap=0) == pytest.approx(0.6)


def test_zero_candidate_count_disables():
    assert weight(60, 100, candidates=0, cap=3.0) == pytest.approx(0.6)


def test_cap_none_reads_live_config(monkeypatch):
    monkeypatch.setattr(config, "RARITY_CAP_MULTIPLE", 2.0)
    # cap=None → live config value 2.0: ceiling 2/10 = 0.2.
    assert weight(60, 100, candidates=10, cap=None) == pytest.approx(0.2)
    monkeypatch.setattr(config, "RARITY_CAP_MULTIPLE", 0.0)
    assert weight(60, 100, candidates=10, cap=None) == pytest.approx(0.6)


def test_shipped_default_is_off():
    # Never read the frozen config.RARITY_CAP_MULTIPLE constant (ambient env
    # could have set it); assert the shipped default itself: unset ⇒ 0 ⇒ off.
    assert config.RARITY_CAP_MULTIPLE_DEFAULT == 0.0
    assert float(os.getenv("_RARITY_CAP_UNSET_", str(config.RARITY_CAP_MULTIPLE_DEFAULT))) == 0.0


def test_laplace_smoothed_share_still_capped():
    # Smoothing widens the denominator but a runaway stays above the ceiling
    # and is clamped identically.
    w = rarity.effective_weight(
        60, 100, 0.005, None, 24, None, NOW,
        population_size=10, candidate_count=10, cap_multiple=3.0,
    )
    assert w == pytest.approx(0.3)


# ---------------------------------------------------------------------------
# weighted_pick integration (live config read — no restart needed)
# ---------------------------------------------------------------------------


class CaptureRng:
    def __init__(self):
        self.traits: list[str] = []
        self.weights: list[float] = []

    def choices(self, traits, weights, k=1):
        self.traits, self.weights = list(traits), list(weights)
        return [traits[0]]


def seed_row(conn, trait, count, category="Background", body="*", network="testnet", **kw):
    conn.execute(
        """INSERT INTO trait_rarity (network, body, category, trait,
           live_count, floor_weight, boost_initial, boost_step_hours,
           boost_started_at, enabled)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (
            network,
            body,
            category,
            trait,
            count,
            kw.get("floor_weight", 0.005),
            kw.get("boost_initial"),
            kw.get("boost_step_hours", 24),
            kw.get("boost_started_at"),
            kw.get("enabled", 1),
        ),
    )
    conn.commit()


def _seed_lfg_background(conn, values):
    """Insert LFG rows so _is_stale sees matching counts (no recalc wipe)."""
    n = 0
    for value, count in values:
        for _ in range(count):
            n += 1
            conn.execute(
                "INSERT INTO LFG (nft_number, Background, body_type, network) VALUES (?,?,?,?)",
                (n, value, "*", "testnet"),
            )
    conn.commit()


def test_weighted_pick_caps_runaway(conn, monkeypatch):
    monkeypatch.setattr(config, "RARITY_CAP_MULTIPLE", 3.0)
    others = [f"T{i}" for i in range(9)]
    _seed_lfg_background(conn, [("Runaway", 60)] + [(t, 4) for t in others][:0])
    seed_row(conn, "Runaway", 60)
    for t in others:
        seed_row(conn, t, 0)
    # Match LFG count to cached sum so weighted_pick doesn't recalc: 60 rows.
    rng = CaptureRng()
    available = ["Runaway", *others]
    rarity.weighted_pick(conn, "*", "Background", available, network="testnet", now=NOW, rng=rng)
    w = dict(zip(rng.traits, rng.weights))
    # candidate_count = 10 enabled candidates → ceiling 0.3. Smoothed share
    # of Runaway = 61/70 ≈ 0.87 → clamped to 0.3.
    assert w["Runaway"] == pytest.approx(0.3)
    # A floor trait is untouched by the cap.
    assert w["T0"] == pytest.approx(rarity.effective_weight(0, 60, 0.005, None, 24, None, NOW, population_size=10))


def test_weighted_pick_candidate_count_excludes_disabled(conn, monkeypatch):
    monkeypatch.setattr(config, "RARITY_CAP_MULTIPLE", 3.0)
    _seed_lfg_background(conn, [("Runaway", 60)])
    seed_row(conn, "Runaway", 60)
    for i in range(5):
        seed_row(conn, f"E{i}", 0)
    seed_row(conn, "Off", 0, enabled=0)
    rng = CaptureRng()
    available = ["Runaway", "Off", *[f"E{i}" for i in range(5)]]
    rarity.weighted_pick(conn, "*", "Background", available, network="testnet", now=NOW, rng=rng)
    # 6 enabled candidates (Off excluded) → fair 1/6, ceiling 3/6 = 0.5.
    w = dict(zip(rng.traits, rng.weights))
    assert "Off" not in w
    assert w["Runaway"] == pytest.approx(0.5)


def test_weighted_pick_uncapped_when_config_off(conn, monkeypatch):
    monkeypatch.setattr(config, "RARITY_CAP_MULTIPLE", 0.0)
    _seed_lfg_background(conn, [("Runaway", 60)])
    seed_row(conn, "Runaway", 60)
    for i in range(9):
        seed_row(conn, f"T{i}", 0)
    rng = CaptureRng()
    available = ["Runaway", *[f"T{i}" for i in range(9)]]
    rarity.weighted_pick(conn, "*", "Background", available, network="testnet", now=NOW, rng=rng)
    w = dict(zip(rng.traits, rng.weights))
    # Uncapped smoothed share 61/70 — identical to the pre-cap engine.
    assert w["Runaway"] == pytest.approx(61 / 70)
