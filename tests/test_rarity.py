# Tests for the variable rarity engine (lfg_core/rarity.py).
import os
import random
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

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
os.environ.setdefault("BUNNY_PULL_ZONE", "nft.pullzone.example")

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
    assert {
        "network",
        "body",
        "category",
        "trait",
        "live_count",
        "floor_weight",
        "boost_initial",
        "boost_step_hours",
        "boost_started_at",
        "enabled",
        "first_seen_at",
    } <= cols


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


def test_weighted_pick_returns_available_trait(conn):
    seed_row(conn, "Red", 50)
    seed_row(conn, "Blue", 50)
    pick = rarity.weighted_pick(
        conn, "*", "Background", ["Red", "Blue"], network="testnet", now=NOW, rng=random.Random(1)
    )
    assert pick in ("Red", "Blue")


def test_weighted_pick_respects_weights(conn):
    # 99:1 split — use body='*' to match the pick's body parameter so
    # recalculate_rarity produces the correct counts for the * path.
    for i in range(99):
        insert_nft(conn, i + 1, background="Common", body="*")
    insert_nft(conn, 100, background="Rare", body="*")
    rng = random.Random(42)
    picks = [
        rarity.weighted_pick(
            conn, "*", "Background", ["Common", "Rare"], network="testnet", now=NOW, rng=rng
        )
        for _ in range(1000)
    ]
    common = picks.count("Common")
    assert 950 <= common <= 1000  # ~99% expected


def test_weighted_pick_autoinserts_unknown_trait(conn):
    seed_row(conn, "Red", 100)
    rarity.weighted_pick(
        conn,
        "*",
        "Background",
        ["Red", "BrandNew"],
        network="testnet",
        now=NOW,
        rng=random.Random(1),
    )
    row = conn.execute(
        """SELECT live_count, floor_weight FROM trait_rarity WHERE
           network='testnet' AND body='*' AND category='Background'
           AND trait='BrandNew'"""
    ).fetchone()
    assert row == (0, 0.005)


def test_weighted_pick_excludes_disabled(conn):
    seed_row(conn, "Red", 100)
    seed_row(conn, "Banned", 100, enabled=0)
    rng = random.Random(7)
    picks = {
        rarity.weighted_pick(
            conn, "*", "Background", ["Red", "Banned"], network="testnet", now=NOW, rng=rng
        )
        for _ in range(50)
    }
    assert picks == {"Red"}


def test_weighted_pick_all_disabled_raises(conn):
    seed_row(conn, "Banned", 100, enabled=0)
    with pytest.raises(ValueError):
        rarity.weighted_pick(conn, "*", "Background", ["Banned"], network="testnet", now=NOW)


def test_weighted_pick_network_isolated(conn):
    # Mainnet rows must not influence a testnet pick: testnet sees only
    # auto-inserted floor rows → effectively uniform, both picked over 100 draws.
    seed_row(conn, "Common", 1000000, network="mainnet")
    seed_row(conn, "Rare", 1, network="mainnet")
    rng = random.Random(3)
    picks = {
        rarity.weighted_pick(
            conn, "*", "Background", ["Common", "Rare"], network="testnet", now=NOW, rng=rng
        )
        for _ in range(100)
    }
    assert picks == {"Common", "Rare"}


def test_weighted_pick_active_boost_dominates(conn):
    # Boosted floor trait (7×) vs one common trait: floor 0.5%×7 = 3.5%
    # vs ~100% share → boosted picked sometimes but minority; verify the
    # boost moved it well above its unboosted expectation.
    seed_row(conn, "Common", 95)
    seed_row(conn, "Fresh", 5, boost_initial=7.0, boost_started_at=iso(NOW - timedelta(hours=1)))
    rng = random.Random(5)
    picks = [
        rarity.weighted_pick(
            conn, "*", "Background", ["Common", "Fresh"], network="testnet", now=NOW, rng=rng
        )
        for _ in range(2000)
    ]
    fresh = picks.count("Fresh")
    # unboosted expectation ≈ 5% → 100; boosted ≈ 35/130 ≈ 27% → ~540
    assert fresh > 350


def test_weighted_pick_denominator_spans_whole_category(conn):
    # Regression (ape Star-eyes bug): 200 legacy apes store Eyes='None' —
    # a value that is never in the mintable candidate list — and a single
    # 'Star' mint exists. The live-share denominator must span the whole
    # (body, category) population, not just the candidates, or Star's share
    # becomes 1/1 = 1.0 and it wins ~80% of every roll, snowballing.
    for i in range(200):
        conn.execute(
            "INSERT INTO LFG (nft_number, Eyes, body_type, network) VALUES (?,?,?,?)",
            (i + 1, "None", "ape", "testnet"),
        )
    conn.execute(
        "INSERT INTO LFG (nft_number, Eyes, body_type, network) VALUES (?,?,?,?)",
        (201, "Star", "ape", "testnet"),
    )
    conn.commit()
    available = ["Star", "3D", "Aviators", "Laser", "Hypno"]
    rng = random.Random(11)
    picks = [
        rarity.weighted_pick(conn, "ape", "Eyes", available, network="testnet", now=NOW, rng=rng)
        for _ in range(1000)
    ]
    star = picks.count("Star")
    # Star's true live share is 1/201 ≈ 0.5% → floor-clamped like the rest,
    # so the roll should be near-uniform (~200 each). The buggy candidate-only
    # denominator gives Star ≈ 98% (~980).
    assert star < 400, f"Star over-picked: {star}/1000"


def test_weighted_pick_new_body_cold_start_does_not_snowball(conn):
    # Regression (milady identical-mints bug): a brand-new body has no legacy
    # rows to widen the denominator — after the first few mints of one value,
    # its live share is count/count = 1.0 vs the 0.005 floor for every other
    # value, so the exact same trait wins ~96% of rolls and snowballs (the
    # first 3 mainnet miladys were identical across every category). Laplace
    # smoothing must keep a cold category near-uniform.
    for i in range(3):
        conn.execute(
            "INSERT INTO LFG (nft_number, Eyes, body_type, network) VALUES (?,?,?,?)",
            (i + 1, "Bright Blue", "milady", "testnet"),
        )
    conn.commit()
    available = [
        "Bright Blue",
        "Curious Blue",
        "Curious Brown",
        "Curious Green",
        "Lively Brown",
        "Lively Green",
        "Tired Blue",
        "Tired Brown",
        "Tired Green",
    ]
    rng = random.Random(21)
    picks = [
        rarity.weighted_pick(conn, "milady", "Eyes", available, network="testnet", now=NOW, rng=rng)
        for _ in range(1000)
    ]
    winner = picks.count("Bright Blue")
    # Smoothed share (3+1)/(3+9) = 1/3 vs 1/12 each for the other eight →
    # ~33% expected. The buggy proportional share (1.0 vs 8×0.005) gives ~96%.
    assert winner < 500, f"Bright Blue over-picked: {winner}/1000"
    assert len(set(picks)) == len(available), "cold-start roll should reach every value"


def test_smoothed_share_negligible_on_mature_category():
    # Smoothing must not distort established rarity: 30/100 with 10 values
    # → (30+1)/(100+10) ≈ 0.28, still ≈ the true 0.30 share.
    w = rarity.effective_weight(30, 100, 0.005, None, 24, None, NOW, population_size=10)
    assert w == pytest.approx(31 / 110)


# Task 4: recalculate_rarity + staleness guard


def insert_nft(
    conn,
    number,
    background="Red",
    body_trait="Straight Dark",
    hat="Cap",
    body="male",
    network="testnet",
):
    conn.execute(
        """INSERT INTO LFG (nft_number, Background, Body, Hat, body_type, network)
           VALUES (?,?,?,?,?,?)""",
        (number, background, body_trait, hat, body, network),
    )
    conn.commit()


def test_recalc_counts_live_nfts(conn):
    insert_nft(conn, 1, background="Red")
    insert_nft(conn, 2, background="Red")
    insert_nft(conn, 3, background="Blue")
    rarity.recalculate_rarity(conn, network="testnet")
    rows = dict(
        conn.execute(
            """SELECT trait, live_count FROM trait_rarity
           WHERE network='testnet' AND category='Background'"""
        )
    )
    assert rows == {"Red": 2, "Blue": 1}


def test_recalc_excludes_burned(conn):
    insert_nft(conn, 1, background="Red")
    insert_nft(conn, 2, background="Red")
    conn.execute("INSERT INTO burned_nfts (nft_number) VALUES (2)")
    rarity.recalculate_rarity(conn, network="testnet")
    (count,) = conn.execute(
        """SELECT live_count FROM trait_rarity WHERE network='testnet'
           AND category='Background' AND trait='Red'"""
    ).fetchone()
    assert count == 1


def test_recalc_maps_hat_column_to_head_category(conn):
    insert_nft(conn, 1, hat="Crown")
    rarity.recalculate_rarity(conn, network="testnet")
    (count,) = conn.execute(
        """SELECT live_count FROM trait_rarity WHERE network='testnet'
           AND category='Head' AND trait='Crown'"""
    ).fetchone()
    assert count == 1


def test_recalc_builds_body_type_category(conn):
    insert_nft(conn, 1, body="male")
    insert_nft(conn, 2, body="male")
    insert_nft(conn, 3, body="ape")
    rarity.recalculate_rarity(conn, network="testnet")
    rows = dict(
        conn.execute(
            """SELECT trait, live_count FROM trait_rarity
           WHERE network='testnet' AND body='*' AND category=?""",
            (rarity.BODY_CATEGORY,),
        )
    )
    assert rows == {"male": 2, "ape": 1}


def test_recalc_network_scoped(conn):
    insert_nft(conn, 1, background="Red", network="mainnet")
    insert_nft(conn, 2, background="Blue", network="testnet")
    rarity.recalculate_rarity(conn, network="testnet")
    rows = list(
        conn.execute(
            """SELECT trait FROM trait_rarity WHERE network='testnet'
           AND category='Background'"""
        )
    )
    assert rows == [("Blue",)]


def test_recalc_preserves_boost_columns(conn):
    # Use body='*' so insert_nft's body_type matches the seed_row's body='*'
    insert_nft(conn, 1, background="Red", body="*")
    seed_row(conn, "Red", 0, boost_initial=7.0)
    rarity.recalculate_rarity(conn, network="testnet")
    boost, count = conn.execute(
        """SELECT boost_initial, live_count FROM trait_rarity
           WHERE network='testnet' AND category='Background'
           AND trait='Red' AND body='*'"""
    ).fetchone()
    assert boost == 7.0 and count == 1


def test_recalc_resets_stale_counts_to_zero(conn):
    seed_row(conn, "Ghost", 99)  # trait no longer present in any live NFT
    rarity.recalculate_rarity(conn, network="testnet")
    (count,) = conn.execute(
        """SELECT live_count FROM trait_rarity WHERE network='testnet'
           AND category='Background' AND trait='Ghost'"""
    ).fetchone()
    assert count == 0


def test_staleness_guard_triggers_recalc(conn):
    # Cached counts disagree with the live collection → pick must recalc first.
    # Use body='*' so body_type matches the seed_row (body='*') after recalc.
    insert_nft(conn, 1, background="Red", body="*")
    seed_row(conn, "Red", 42)  # wrong cache
    rarity.weighted_pick(
        conn, "*", "Background", ["Red"], network="testnet", now=NOW, rng=random.Random(1)
    )
    (count,) = conn.execute(
        """SELECT live_count FROM trait_rarity WHERE network='testnet'
           AND category='Background' AND trait='Red' AND body='*'"""
    ).fetchone()
    assert count == 1


# Task 5: Boost lifecycle


def test_arm_boost_sets_columns(conn):
    seed_row(conn, "Fresh", 0)
    rarity.arm_boost(
        conn, "*", "Background", "Fresh", network="testnet", boost_initial=7.0, boost_step_hours=24
    )
    row = conn.execute(
        """SELECT boost_initial, boost_step_hours, boost_started_at
           FROM trait_rarity WHERE trait='Fresh'"""
    ).fetchone()
    assert row == (7.0, 24, None)  # armed but dormant


def test_arm_boost_rearms_finished_boost(conn):
    seed_row(conn, "Old", 5, boost_initial=7.0, boost_started_at=iso(NOW - timedelta(days=30)))
    rarity.arm_boost(
        conn, "*", "Background", "Old", network="testnet", boost_initial=5.0, boost_step_hours=24
    )
    row = conn.execute(
        """SELECT boost_initial, boost_started_at FROM trait_rarity
           WHERE trait='Old'"""
    ).fetchone()
    assert row == (5.0, None)  # clock reset to dormant


def test_start_boost_clock_only_when_armed_and_dormant(conn):
    seed_row(conn, "Fresh", 0, boost_initial=7.0)
    seed_row(conn, "Plain", 0)
    started_at = iso(NOW - timedelta(hours=2))
    seed_row(conn, "Running", 0, boost_initial=7.0, boost_started_at=started_at)

    rarity.start_boost_clock(conn, "*", "Background", "Fresh", network="testnet", now=NOW)
    rarity.start_boost_clock(conn, "*", "Background", "Plain", network="testnet", now=NOW)
    rarity.start_boost_clock(conn, "*", "Background", "Running", network="testnet", now=NOW)

    rows = dict(conn.execute("SELECT trait, boost_started_at FROM trait_rarity"))
    assert rows["Fresh"] == NOW.isoformat()  # clock started
    assert rows["Plain"] is None  # no boost configured
    assert rows["Running"] == started_at  # already running: untouched


def test_boost_status_strings(conn):
    seed_row(conn, "Dormant", 0, boost_initial=7.0)
    seed_row(conn, "Active", 0, boost_initial=7.0, boost_started_at=iso(NOW - timedelta(hours=25)))
    seed_row(conn, "None", 0)
    assert rarity.boost_status(7.0, 24, None, NOW) == "dormant"
    assert rarity.boost_status(7.0, 24, iso(NOW - timedelta(hours=25)), NOW).startswith("active 6x")
    assert rarity.boost_status(None, 24, None, NOW) == "—"


# Task 7: Webapp integration


class FakeStore:
    """Minimal async layer store for selection tests."""

    def __init__(self, tree):
        self.tree = tree  # {body: {trait_type: [values]}}

    async def list_bodies(self):
        return sorted(self.tree)

    async def list_trait_types(self, body):
        return sorted(self.tree[body])

    async def list_values(self, body, trait_type):
        return self.tree[body].get(trait_type, [])


def test_select_random_attributes_uses_engine(conn):
    import asyncio

    from lfg_core import traits

    store = FakeStore({"male": {"Background": ["Red", "Blue"], "Body": ["Straight Dark"]}})
    body, attrs = asyncio.get_event_loop().run_until_complete(
        traits.select_random_attributes(
            store, conn=conn, network="testnet", now=NOW, rng=random.Random(1)
        )
    )
    assert body == "male"
    types = {a["trait_type"] for a in attrs}
    assert types == {"Background", "Body"}
    # Engine left auto-detected rows behind
    n = conn.execute("""SELECT COUNT(*) FROM trait_rarity
                        WHERE network='testnet'""").fetchone()[0]
    assert n >= 3  # 2 backgrounds + 1 body trait (+ Body Type row)


# Task 8: Legacy bot integration


def test_category_for_folder():
    assert rarity.category_for_folder("1 background") == "Background"
    assert rarity.category_for_folder("8 hat:hair") == "Head"
    assert rarity.category_for_folder("9 accessory") == "Accessory"
    assert rarity.category_for_folder("2 back") == "Back"
    assert rarity.category_for_folder("99 unknown_thing") is None


# Task 9: Network/body stamping in db_helpers.record_nft_mint


def test_record_nft_mint_stamps_network_and_body(tmp_path):
    from lfg_core import db_helpers

    db = str(tmp_path / "t.db")
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE LFG (nft_number INTEGER PRIMARY KEY)")
    c.commit()
    c.close()
    ok = db_helpers.record_nft_mint(
        nft_number=9001,
        nft_id="ABC",
        discord_id="1",
        owner_address="r1",
        metadata_url="m",
        image_url="i",
        traits={"Background": "Red"},
        network="testnet",
        body_type="male",
        db_path=db,
    )
    assert ok
    c = sqlite3.connect(db)
    row = c.execute("""SELECT network, body_type, Background FROM LFG
                       WHERE nft_number=9001""").fetchone()
    c.close()
    assert row == ("testnet", "male", "Red")


def test_select_random_attributes_weights_body_pick(conn):
    import asyncio

    from lfg_core import traits

    # 99 male : 1 ape in the collection → male should dominate body picks
    for i in range(99):
        insert_nft(conn, i + 1, body="male")
    insert_nft(conn, 100, body="ape")
    rarity.recalculate_rarity(conn, network="testnet")
    store = FakeStore({"male": {"Background": ["Red"]}, "ape": {"Background": ["Red"]}})
    rng = random.Random(9)
    bodies = [
        asyncio.get_event_loop().run_until_complete(
            traits.select_random_attributes(store, conn=conn, network="testnet", now=NOW, rng=rng)
        )[0]
        for _ in range(200)
    ]
    assert bodies.count("male") > 150


# Task 10: seed_from_collection, set_floor, set_enabled, get_odds


def test_seed_backfills_body_from_traits(conn):
    # Legacy rows have body_type='*'; seed derives it from Body trait via detect_body
    insert_nft(conn, 1, body_trait="Straight Dark", body="*")  # → male
    insert_nft(conn, 2, body_trait="Curved Light", body="*")  # → female
    insert_nft(conn, 3, body_trait="Ape Body", body="*")  # → ape
    insert_nft(conn, 4, body_trait="Bones", body="*")  # → skeleton
    rarity.seed_from_collection(conn, network="testnet")
    rows = dict(conn.execute("SELECT nft_number, body_type FROM LFG"))
    assert rows == {1: "male", 2: "female", 3: "ape", 4: "skeleton"}
    body_counts = dict(
        conn.execute(
            """SELECT trait, live_count FROM trait_rarity
           WHERE category=? AND network='testnet'""",
            (rarity.BODY_CATEGORY,),
        )
    )
    assert body_counts == {"male": 1, "female": 1, "ape": 1, "skeleton": 1}


def test_seed_marks_testnet_numbers(conn):
    insert_nft(conn, 1, network="mainnet")
    insert_nft(conn, 2, network="mainnet")
    rarity.seed_from_collection(conn, network="mainnet", mark_testnet=[2])
    rows = dict(conn.execute("SELECT nft_number, network FROM LFG"))
    assert rows == {1: "mainnet", 2: "testnet"}


def test_set_floor_global_and_per_trait(conn):
    seed_row(conn, "Red", 10)
    seed_row(conn, "Blue", 10)
    rarity.set_floor(conn, 0.01, network="testnet")
    floors = {
        r[0] for r in conn.execute("SELECT floor_weight FROM trait_rarity WHERE network='testnet'")
    }
    assert floors == {0.01}
    rarity.set_floor(conn, 0.05, network="testnet", body="*", category="Background", trait="Red")
    (red,) = conn.execute("SELECT floor_weight FROM trait_rarity WHERE trait='Red'").fetchone()
    assert red == 0.05


def test_set_enabled(conn):
    seed_row(conn, "Red", 10)
    rarity.set_enabled(conn, "*", "Background", "Red", False, network="testnet")
    (e,) = conn.execute("SELECT enabled FROM trait_rarity WHERE trait='Red'").fetchone()
    assert e == 0


# Task 12: distribution sanity


def test_distribution_matches_weights(conn):
    # 60/30/10 split, floor small enough not to bind. 10k draws → observed
    # frequencies within ±3 percentage points of expected.
    # Use body="*" so recalc puts counts in the "*" body rows that
    # weighted_pick(body="*") will read.
    for i in range(60):
        insert_nft(conn, i + 1, background="A", body="*")
    for i in range(30):
        insert_nft(conn, i + 61, background="B", body="*")
    for i in range(10):
        insert_nft(conn, i + 91, background="C", body="*")
    rarity.recalculate_rarity(conn, network="testnet")
    rng = random.Random(1234)
    picks = [
        rarity.weighted_pick(
            conn, "*", "Background", ["A", "B", "C"], network="testnet", now=NOW, rng=rng
        )
        for _ in range(10000)
    ]
    for trait, expected in (("A", 0.60), ("B", 0.30), ("C", 0.10)):
        observed = picks.count(trait) / 10000
        assert abs(observed - expected) < 0.03, (trait, observed)
