# Tests for the variable rarity engine (lfg_core/rarity.py).
import os
import sys
import sqlite3
import random
from datetime import datetime, timezone, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# Dummy env so lfg_core.config import doesn't fail (same trick as webapp/test_smoke.py)
os.environ.setdefault("DISCORD_BOT_TOKEN", "x")
os.environ.setdefault("XUMM_API_KEY", "x")
os.environ.setdefault("XUMM_API_SECRET", "x")
os.environ.setdefault("BUNNY_CDN_ACCESS_KEY", "x")
os.environ.setdefault("BUNNY_CDN_STORAGE_ZONE", "x")
os.environ.setdefault("SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2")  # dummy testnet seed
os.environ.setdefault("TOKEN_ISSUER_ADDRESS", "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
os.environ.setdefault("TOKEN_CURRENCY_HEX", "4C46474F00000000000000000000000000000000")
os.environ.setdefault("XRPL_NETWORK", "testnet")

from lfg_core import rarity  # noqa: E402

NOW = datetime(2026, 6, 12, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    # Minimal LFG + burned_nfts shaped like production
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


def test_ensure_schema_creates_trait_rarity(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(trait_rarity)")}
    assert {"network", "body", "category", "trait", "live_count",
            "floor_weight", "boost_initial", "boost_step_hours",
            "boost_started_at", "enabled", "first_seen_at"} <= cols


def test_ensure_schema_adds_lfg_columns(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(LFG)")}
    assert "network" in cols and "body_type" in cols


def test_ensure_schema_idempotent(conn):
    rarity.ensure_schema(conn)  # second call must not raise


def iso(dt):
    return dt.isoformat()


def test_share_is_proportional():
    # 30 of 100 → 0.3 (floor 0.005 doesn't bind)
    assert rarity.effective_weight(30, 100, 0.005, None, 24, None, NOW) == pytest.approx(0.3)


def test_floor_clamps_zero_and_low_counts():
    assert rarity.effective_weight(0, 100, 0.005, None, 24, None, NOW) == pytest.approx(0.005)
    assert rarity.effective_weight(1, 10000, 0.005, None, 24, None, NOW) == pytest.approx(0.005)


def test_empty_category_uses_floor():
    assert rarity.effective_weight(0, 0, 0.005, None, 24, None, NOW) == pytest.approx(0.005)


def test_dormant_boost_is_floor_only():
    # boost configured but clock not started → multiplier 1
    assert rarity.effective_weight(0, 100, 0.005, 7.0, 24, None, NOW) == pytest.approx(0.005)


def test_boost_steps_down_per_window():
    started = iso(NOW - timedelta(hours=1))
    assert rarity.boost_multiplier(7.0, 24, started, NOW) == pytest.approx(7.0)
    started = iso(NOW - timedelta(hours=25))
    assert rarity.boost_multiplier(7.0, 24, started, NOW) == pytest.approx(6.0)
    started = iso(NOW - timedelta(hours=24 * 6 + 1))
    assert rarity.boost_multiplier(7.0, 24, started, NOW) == pytest.approx(1.0)


def test_boost_window_boundary_exact():
    # Exactly 24h elapsed → second window begins → 6x
    started = iso(NOW - timedelta(hours=24))
    assert rarity.boost_multiplier(7.0, 24, started, NOW) == pytest.approx(6.0)


def test_boost_never_below_one():
    started = iso(NOW - timedelta(days=365))
    assert rarity.boost_multiplier(7.0, 24, started, NOW) == pytest.approx(1.0)


def test_active_boost_multiplies_base():
    started = iso(NOW - timedelta(hours=1))
    # base = max(0.0, 0.005) = 0.005; ×7
    assert rarity.effective_weight(0, 100, 0.005, 7.0, 24, started, NOW) == pytest.approx(0.035)


def seed_row(conn, trait, count, category="Background", body="*",
             network="testnet", **kw):
    conn.execute(
        """INSERT INTO trait_rarity (network, body, category, trait,
           live_count, floor_weight, boost_initial, boost_step_hours,
           boost_started_at, enabled)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (network, body, category, trait, count,
         kw.get("floor_weight", 0.005), kw.get("boost_initial"),
         kw.get("boost_step_hours", 24), kw.get("boost_started_at"),
         kw.get("enabled", 1)))
    conn.commit()


def test_weighted_pick_returns_available_trait(conn):
    seed_row(conn, "Red", 50)
    seed_row(conn, "Blue", 50)
    pick = rarity.weighted_pick(conn, "*", "Background", ["Red", "Blue"],
                                network="testnet", now=NOW,
                                rng=random.Random(1))
    assert pick in ("Red", "Blue")


def test_weighted_pick_respects_weights(conn):
    # 99:1 split — with floor 0.005 the rare trait sits at floor.
    seed_row(conn, "Common", 990)
    seed_row(conn, "Rare", 10)
    rng = random.Random(42)
    picks = [rarity.weighted_pick(conn, "*", "Background",
                                  ["Common", "Rare"], network="testnet",
                                  now=NOW, rng=rng) for _ in range(1000)]
    common = picks.count("Common")
    assert 950 <= common <= 1000  # ~99% expected


def test_weighted_pick_autoinserts_unknown_trait(conn):
    seed_row(conn, "Red", 100)
    rarity.weighted_pick(conn, "*", "Background", ["Red", "BrandNew"],
                         network="testnet", now=NOW, rng=random.Random(1))
    row = conn.execute(
        """SELECT live_count, floor_weight FROM trait_rarity WHERE
           network='testnet' AND body='*' AND category='Background'
           AND trait='BrandNew'""").fetchone()
    assert row == (0, 0.005)


def test_weighted_pick_excludes_disabled(conn):
    seed_row(conn, "Red", 100)
    seed_row(conn, "Banned", 100, enabled=0)
    rng = random.Random(7)
    picks = {rarity.weighted_pick(conn, "*", "Background", ["Red", "Banned"],
                                  network="testnet", now=NOW, rng=rng)
             for _ in range(50)}
    assert picks == {"Red"}


def test_weighted_pick_all_disabled_raises(conn):
    seed_row(conn, "Banned", 100, enabled=0)
    with pytest.raises(ValueError):
        rarity.weighted_pick(conn, "*", "Background", ["Banned"],
                             network="testnet", now=NOW)


def test_weighted_pick_network_isolated(conn):
    # Mainnet rows must not influence a testnet pick: testnet sees only
    # auto-inserted floor rows → effectively uniform, both picked over 100 draws.
    seed_row(conn, "Common", 1000000, network="mainnet")
    seed_row(conn, "Rare", 1, network="mainnet")
    rng = random.Random(3)
    picks = {rarity.weighted_pick(conn, "*", "Background", ["Common", "Rare"],
                                  network="testnet", now=NOW, rng=rng)
             for _ in range(100)}
    assert picks == {"Common", "Rare"}


def test_weighted_pick_active_boost_dominates(conn):
    # Boosted floor trait (7×) vs one common trait: floor 0.5%×7 = 3.5%
    # vs ~100% share → boosted picked sometimes but minority; verify the
    # boost moved it well above its unboosted expectation.
    seed_row(conn, "Common", 95)
    seed_row(conn, "Fresh", 5, boost_initial=7.0,
             boost_started_at=iso(NOW - timedelta(hours=1)))
    rng = random.Random(5)
    picks = [rarity.weighted_pick(conn, "*", "Background",
                                  ["Common", "Fresh"], network="testnet",
                                  now=NOW, rng=rng) for _ in range(2000)]
    fresh = picks.count("Fresh")
    # unboosted expectation ≈ 5% → 100; boosted ≈ 35/130 ≈ 27% → ~540
    assert fresh > 350
