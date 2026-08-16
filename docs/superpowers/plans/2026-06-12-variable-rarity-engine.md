# Variable Rarity Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace uniform-random trait selection (webapp + legacy bot) with a proportional-with-floor rarity engine backed by a cached `trait_rarity` table, including a dormant-then-stepped boost for new traits, body-type weighting, network scoping, and admin tooling.

**Architecture:** A new pure-logic module `lfg_core/rarity.py` owns schema, weight math, weighted selection, recalculation, and boost lifecycle, all against the existing SQLite `lfg_nfts.db`. Both mint paths call one `weighted_pick()`; collection-mutating paths call `recalculate_rarity()`. A CLI (`rarity_admin.py`) bootstraps/administers the table; Discord `/admin` gains View Odds / Boost / Disable.

**Tech Stack:** Python 3, sqlite3 (stdlib), pytest, discord.py. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-06-12-variable-rarity-engine-design.md`

**Conventions used throughout:**
- `trait_rarity.category` uses the **layer-store trait-type names** from `lfg_core.swap_meta.TRAIT_ORDER` (so headwear is `Head`); the LFG table's column is `Hat` — the mapping lives in one dict in `rarity.py`.
- `body` values: `female|male|skeleton|ape`, sentinel `'*'` for the legacy path, aggregate rows, and the reserved `Body Type` category.
- All times are UTC ISO strings; functions accept an injectable `now` (datetime) for tests. Randomness accepts an injectable `rng`.
- Run tests with: `python -m pytest tests/test_rarity.py webapp/test_smoke.py -v` from the repo root.

**File structure:**
- Create: `lfg_core/rarity.py` (schema + math + pick + recalc + boost), `tests/test_rarity.py`, `tests/__init__.py` (empty), `rarity_admin.py` (CLI)
- Modify: `lfg_core/config.py` (rarity settings + DB path), `lfg_core/layer_store.py` + `lfg_core/traits.py` + `lfg_core/swap_compose.py` + `lfg_core/swap_flow.py` + `lfg_core/mint_flow.py` (gender→body rename, engine integration), `db_helpers.py` (network/body stamp), `main.py` (legacy pick, burn hook, admin UI)

---

### Task 1: Config + schema (`ensure_schema`)

**Files:**
- Modify: `lfg_core/config.py` (append to end)
- Create: `lfg_core/rarity.py`, `tests/__init__.py` (empty file), `tests/test_rarity.py`

- [ ] **Step 1: Add config settings**

Append to `lfg_core/config.py`:

```python
# Variable rarity engine
DB_PATH = os.getenv("DB_PATH", "lfg_nfts.db")
RARITY_FLOOR = float(os.getenv("RARITY_FLOOR", "0.005"))
RARITY_BOOST_INITIAL = float(os.getenv("RARITY_BOOST_INITIAL", "7"))
RARITY_BOOST_STEP_HOURS = int(os.getenv("RARITY_BOOST_STEP_HOURS", "24"))
```

- [ ] **Step 2: Write the failing tests**

Create `tests/__init__.py` (empty) and `tests/test_rarity.py`:

```python
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
os.environ.setdefault("SEED", "sEdTM1uX8pu2do5XvTeSrXpPVPNVrk2")  # dummy testnet-format seed
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
    assert "network" in cols and "body" in cols


def test_ensure_schema_idempotent(conn):
    rarity.ensure_schema(conn)  # second call must not raise
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `python -m pytest tests/test_rarity.py -v`
Expected: FAIL/ERROR with `ModuleNotFoundError: No module named 'lfg_core.rarity'` (or AttributeError).

- [ ] **Step 4: Write the implementation**

Create `lfg_core/rarity.py`:

```python
# lfg_core/rarity.py
# Variable rarity engine: proportional-with-floor trait weights cached in
# the trait_rarity table, with a dormant-then-stepped boost for new traits.
# Pure sqlite3 + stdlib; time and randomness are injectable for tests.
# Spec: docs/superpowers/specs/2026-06-12-variable-rarity-engine-design.md

import logging
import random as _random
import sqlite3
from datetime import datetime, timezone

from lfg_core import config

BODY_SENTINEL = "*"          # legacy/ungendered rows and Body Type rows
BODY_CATEGORY = "Body Type"  # reserved category weighting the body pick

# trait_rarity.category uses layer-store trait-type names (TRAIT_ORDER);
# the LFG table's headwear column is named Hat (layer tree uses Head).
LFG_COLUMN_FOR_CATEGORY = {
    "Background": "Background", "Back": "Back", "Body": "Body",
    "Clothing": "Clothing", "Mouth": "Mouth", "Eyebrows": "Eyebrows",
    "Eyes": "Eyes", "Head": "Hat", "Accessory": "Accessory",
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trait_rarity (
    network          TEXT NOT NULL DEFAULT 'mainnet',
    body             TEXT NOT NULL,
    category         TEXT NOT NULL,
    trait            TEXT NOT NULL,
    live_count       INTEGER NOT NULL DEFAULT 0,
    floor_weight     REAL NOT NULL DEFAULT 0.005,
    boost_initial    REAL,
    boost_step_hours INTEGER DEFAULT 24,
    boost_started_at TIMESTAMP,
    enabled          INTEGER NOT NULL DEFAULT 1,
    first_seen_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (network, body, category, trait)
)
"""


def utcnow():
    return datetime.now(timezone.utc)


def connect(db_path=None):
    return sqlite3.connect(db_path or config.DB_PATH)


def ensure_schema(conn):
    """Create trait_rarity and add network/body columns to LFG. Idempotent."""
    conn.execute(_SCHEMA)
    lfg_cols = {r[1] for r in conn.execute("PRAGMA table_info(LFG)")}
    if lfg_cols:  # LFG may not exist yet on a fresh DB; init_db owns it
        if "network" not in lfg_cols:
            conn.execute(
                "ALTER TABLE LFG ADD COLUMN network TEXT NOT NULL DEFAULT 'mainnet'")
        if "body" not in lfg_cols:
            conn.execute(
                "ALTER TABLE LFG ADD COLUMN body TEXT NOT NULL DEFAULT '*'")
    conn.commit()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_rarity.py -v`
Expected: 3 PASS.

- [ ] **Step 6: Commit**

```bash
git add lfg_core/config.py lfg_core/rarity.py tests/__init__.py tests/test_rarity.py
git commit -m "feat(rarity): trait_rarity schema + config settings"
```

---

### Task 2: Weight math (`boost_multiplier`, `effective_weight`)

**Files:**
- Modify: `lfg_core/rarity.py` (append)
- Test: `tests/test_rarity.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_rarity.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_rarity.py -v`
Expected: new tests FAIL with `AttributeError: ... no attribute 'effective_weight'`.

- [ ] **Step 3: Write the implementation**

Append to `lfg_core/rarity.py`:

```python
def boost_multiplier(boost_initial, boost_step_hours, boost_started_at, now):
    """Stepped decay: boost_initial for the first window, −1 per window,
    never below 1. Dormant (clock unstarted) or unconfigured → 1."""
    if not boost_initial or not boost_started_at:
        return 1.0
    started = datetime.fromisoformat(boost_started_at)
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    hours = (now - started).total_seconds() / 3600.0
    windows = int(hours // (boost_step_hours or 24))
    return max(1.0, boost_initial - windows)


def effective_weight(live_count, category_total, floor_weight,
                     boost_initial, boost_step_hours, boost_started_at, now):
    """weight = max(live_share, floor) × boost multiplier. Relative weight,
    not a normalized probability."""
    share = (live_count / category_total) if category_total else 0.0
    base = max(share, floor_weight)
    return base * boost_multiplier(boost_initial, boost_step_hours,
                                   boost_started_at, now)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_rarity.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add lfg_core/rarity.py tests/test_rarity.py
git commit -m "feat(rarity): proportional-with-floor weight math + stepped boost decay"
```

---

### Task 3: `weighted_pick` with auto-detect

**Files:**
- Modify: `lfg_core/rarity.py` (append)
- Test: `tests/test_rarity.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_rarity.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_rarity.py -v`
Expected: new tests FAIL with `AttributeError: ... no attribute 'weighted_pick'`.

- [ ] **Step 3: Write the implementation**

Append to `lfg_core/rarity.py`:

```python
def _ensure_rows(conn, network, body, category, available, now):
    """Auto-detect: insert floor-weight rows for traits the engine hasn't
    seen (e.g. a PNG just dropped into the layer store). No boost."""
    for trait in available:
        conn.execute(
            """INSERT OR IGNORE INTO trait_rarity
               (network, body, category, trait, live_count, floor_weight,
                first_seen_at)
               VALUES (?, ?, ?, ?, 0, ?, ?)""",
            (network, body, category, trait, config.RARITY_FLOOR,
             now.isoformat()))
    conn.commit()


def weighted_pick(conn, body, category, available, *, network=None,
                  now=None, rng=_random):
    """Pick one trait from `available` (the values that exist in the layer
    store — the store stays the authority on what's mintable) using
    proportional-with-floor × boost weights from trait_rarity."""
    if not available:
        raise ValueError(f"No traits available for {body}/{category}")
    network = network or config.XRPL_NETWORK
    now = now or utcnow()
    ensure_schema(conn)
    _ensure_rows(conn, network, body, category, available, now)

    placeholders = ",".join("?" * len(available))
    rows = conn.execute(
        f"""SELECT trait, live_count, floor_weight, boost_initial,
                   boost_step_hours, boost_started_at
            FROM trait_rarity
            WHERE network=? AND body=? AND category=? AND enabled=1
              AND trait IN ({placeholders})""",
        (network, body, category, *available)).fetchall()
    if not rows:
        raise ValueError(
            f"All traits disabled for {body}/{category} on {network}")

    total = sum(r[1] for r in rows)
    traits = [r[0] for r in rows]
    weights = [effective_weight(r[1], total, r[2], r[3], r[4], r[5], now)
               for r in rows]
    return rng.choices(traits, weights=weights, k=1)[0]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_rarity.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add lfg_core/rarity.py tests/test_rarity.py
git commit -m "feat(rarity): weighted_pick with auto-detect and network isolation"
```

---

### Task 4: `recalculate_rarity` + staleness guard

**Files:**
- Modify: `lfg_core/rarity.py` (append + one edit inside `weighted_pick`)
- Test: `tests/test_rarity.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_rarity.py`:

```python
def insert_nft(conn, number, background="Red", body_trait="Straight Dark",
               hat="Cap", body="male", network="testnet"):
    conn.execute(
        """INSERT INTO LFG (nft_number, Background, Body, Hat, body, network)
           VALUES (?,?,?,?,?,?)""",
        (number, background, body_trait, hat, body, network))
    conn.commit()


def test_recalc_counts_live_nfts(conn):
    insert_nft(conn, 1, background="Red")
    insert_nft(conn, 2, background="Red")
    insert_nft(conn, 3, background="Blue")
    rarity.recalculate_rarity(conn, network="testnet")
    rows = dict(conn.execute(
        """SELECT trait, live_count FROM trait_rarity
           WHERE network='testnet' AND category='Background'"""))
    assert rows == {"Red": 2, "Blue": 1}


def test_recalc_excludes_burned(conn):
    insert_nft(conn, 1, background="Red")
    insert_nft(conn, 2, background="Red")
    conn.execute("INSERT INTO burned_nfts (nft_number) VALUES (2)")
    rarity.recalculate_rarity(conn, network="testnet")
    (count,) = conn.execute(
        """SELECT live_count FROM trait_rarity WHERE network='testnet'
           AND category='Background' AND trait='Red'""").fetchone()
    assert count == 1


def test_recalc_maps_hat_column_to_head_category(conn):
    insert_nft(conn, 1, hat="Crown")
    rarity.recalculate_rarity(conn, network="testnet")
    (count,) = conn.execute(
        """SELECT live_count FROM trait_rarity WHERE network='testnet'
           AND category='Head' AND trait='Crown'""").fetchone()
    assert count == 1


def test_recalc_builds_body_type_category(conn):
    insert_nft(conn, 1, body="male")
    insert_nft(conn, 2, body="male")
    insert_nft(conn, 3, body="ape")
    rarity.recalculate_rarity(conn, network="testnet")
    rows = dict(conn.execute(
        """SELECT trait, live_count FROM trait_rarity
           WHERE network='testnet' AND body='*' AND category=?""",
        (rarity.BODY_CATEGORY,)))
    assert rows == {"male": 2, "ape": 1}


def test_recalc_network_scoped(conn):
    insert_nft(conn, 1, background="Red", network="mainnet")
    insert_nft(conn, 2, background="Blue", network="testnet")
    rarity.recalculate_rarity(conn, network="testnet")
    rows = list(conn.execute(
        """SELECT trait FROM trait_rarity WHERE network='testnet'
           AND category='Background'"""))
    assert rows == [("Blue",)]


def test_recalc_preserves_boost_columns(conn):
    insert_nft(conn, 1, background="Red")
    seed_row(conn, "Red", 0, boost_initial=7.0)
    rarity.recalculate_rarity(conn, network="testnet")
    boost, count = conn.execute(
        """SELECT boost_initial, live_count FROM trait_rarity
           WHERE network='testnet' AND category='Background'
           AND trait='Red'""").fetchone()
    assert boost == 7.0 and count == 1


def test_recalc_resets_stale_counts_to_zero(conn):
    seed_row(conn, "Ghost", 99)  # trait no longer present in any live NFT
    rarity.recalculate_rarity(conn, network="testnet")
    (count,) = conn.execute(
        """SELECT live_count FROM trait_rarity WHERE network='testnet'
           AND category='Background' AND trait='Ghost'""").fetchone()
    assert count == 0


def test_staleness_guard_triggers_recalc(conn):
    # Cached counts disagree with the live collection → pick must recalc first.
    insert_nft(conn, 1, background="Red")
    seed_row(conn, "Red", 42)  # wrong cache
    rarity.weighted_pick(conn, "*", "Background", ["Red"],
                         network="testnet", now=NOW, rng=random.Random(1))
    (count,) = conn.execute(
        """SELECT live_count FROM trait_rarity WHERE network='testnet'
           AND category='Background' AND trait='Red'""").fetchone()
    assert count == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_rarity.py -v`
Expected: new tests FAIL (`recalculate_rarity` undefined; guard test fails on count 42).

- [ ] **Step 3: Write the implementation**

Append to `lfg_core/rarity.py`:

```python
def _live_where(network):
    """WHERE fragment selecting live (unburned) LFG rows for a network."""
    return ("""network=? AND nft_number NOT IN
               (SELECT nft_number FROM burned_nfts)""", (network,))


def recalculate_rarity(conn, network=None):
    """Recount live_count for every (body, category, trait) from the LFG
    table minus burned_nfts, plus the reserved Body Type category. Upserts
    counts; preserves boost/floor/enabled columns; zeroes traits that no
    longer occur. Cheap (GROUP BY over a few thousand rows)."""
    network = network or config.XRPL_NETWORK
    ensure_schema(conn)
    where, params = _live_where(network)

    conn.execute("UPDATE trait_rarity SET live_count=0 WHERE network=?",
                 (network,))
    upsert = """INSERT INTO trait_rarity
                (network, body, category, trait, live_count, floor_weight)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(network, body, category, trait)
                DO UPDATE SET live_count=excluded.live_count"""
    for category, column in LFG_COLUMN_FOR_CATEGORY.items():
        rows = conn.execute(
            f"""SELECT body, "{column}", COUNT(*) FROM LFG
                WHERE {where} AND "{column}" != '' AND "{column}" IS NOT NULL
                GROUP BY body, "{column}\"""", params).fetchall()
        for body, trait, count in rows:
            conn.execute(upsert, (network, body or BODY_SENTINEL, category,
                                  trait, count, config.RARITY_FLOOR))
    body_rows = conn.execute(
        f"SELECT body, COUNT(*) FROM LFG WHERE {where} GROUP BY body",
        params).fetchall()
    for body, count in body_rows:
        if body and body != BODY_SENTINEL:
            conn.execute(upsert, (network, BODY_SENTINEL, BODY_CATEGORY,
                                  body, count, config.RARITY_FLOOR))
    conn.commit()


def _is_stale(conn, network, category):
    """True when cached category counts disagree with the live collection."""
    column = LFG_COLUMN_FOR_CATEGORY.get(category)
    if column is None:
        return False  # Body Type and unknown categories: recalc handles them
    (cached,) = conn.execute(
        """SELECT COALESCE(SUM(live_count), 0) FROM trait_rarity
           WHERE network=? AND category=?""", (network, category)).fetchone()
    where, params = _live_where(network)
    (actual,) = conn.execute(
        f"""SELECT COUNT(*) FROM LFG WHERE {where}
            AND "{column}" != '' AND "{column}" IS NOT NULL""",
        params).fetchone()
    return cached != actual
```

Then edit `weighted_pick`: insert these two lines directly after the `_ensure_rows(...)` call:

```python
    if _is_stale(conn, network, category):
        recalculate_rarity(conn, network=network)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_rarity.py -v`
Expected: all PASS. Note: `test_weighted_pick_*` tests from Task 3 seed rows without LFG rows, making the cache "stale" — the guard recalcs and zeroes their counts, which would break them. **If they fail for this reason**, update those tests to insert matching LFG rows via `insert_nft` instead of `seed_row` for count-bearing traits, keeping `seed_row` only for boost/disabled flags. Do that now if needed and re-run until green.

- [ ] **Step 5: Commit**

```bash
git add lfg_core/rarity.py tests/test_rarity.py
git commit -m "feat(rarity): recalculate_rarity + staleness guard in weighted_pick"
```

---

### Task 5: Boost lifecycle (`arm_boost`, `start_boost_clock`, `boost_status`)

**Files:**
- Modify: `lfg_core/rarity.py` (append)
- Test: `tests/test_rarity.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_rarity.py`:

```python
def test_arm_boost_sets_columns(conn):
    seed_row(conn, "Fresh", 0)
    rarity.arm_boost(conn, "*", "Background", "Fresh", network="testnet",
                     boost_initial=7.0, boost_step_hours=24)
    row = conn.execute(
        """SELECT boost_initial, boost_step_hours, boost_started_at
           FROM trait_rarity WHERE trait='Fresh'""").fetchone()
    assert row == (7.0, 24, None)  # armed but dormant


def test_arm_boost_rearms_finished_boost(conn):
    seed_row(conn, "Old", 5, boost_initial=7.0,
             boost_started_at=iso(NOW - timedelta(days=30)))
    rarity.arm_boost(conn, "*", "Background", "Old", network="testnet",
                     boost_initial=5.0, boost_step_hours=24)
    row = conn.execute(
        """SELECT boost_initial, boost_started_at FROM trait_rarity
           WHERE trait='Old'""").fetchone()
    assert row == (5.0, None)  # clock reset to dormant


def test_start_boost_clock_only_when_armed_and_dormant(conn):
    seed_row(conn, "Fresh", 0, boost_initial=7.0)
    seed_row(conn, "Plain", 0)
    started_at = iso(NOW - timedelta(hours=2))
    seed_row(conn, "Running", 0, boost_initial=7.0,
             boost_started_at=started_at)

    rarity.start_boost_clock(conn, "*", "Background", "Fresh",
                             network="testnet", now=NOW)
    rarity.start_boost_clock(conn, "*", "Background", "Plain",
                             network="testnet", now=NOW)
    rarity.start_boost_clock(conn, "*", "Background", "Running",
                             network="testnet", now=NOW)

    rows = dict(conn.execute(
        "SELECT trait, boost_started_at FROM trait_rarity"))
    assert rows["Fresh"] == NOW.isoformat()   # clock started
    assert rows["Plain"] is None              # no boost configured
    assert rows["Running"] == started_at      # already running: untouched


def test_boost_status_strings(conn):
    seed_row(conn, "Dormant", 0, boost_initial=7.0)
    seed_row(conn, "Active", 0, boost_initial=7.0,
             boost_started_at=iso(NOW - timedelta(hours=25)))
    seed_row(conn, "None", 0)
    assert rarity.boost_status(7.0, 24, None, NOW) == "dormant"
    assert rarity.boost_status(7.0, 24, iso(NOW - timedelta(hours=25)),
                               NOW).startswith("active 6x")
    assert rarity.boost_status(None, 24, None, NOW) == "—"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_rarity.py -v`
Expected: FAIL with `AttributeError: ... no attribute 'arm_boost'`.

- [ ] **Step 3: Write the implementation**

Append to `lfg_core/rarity.py`:

```python
def arm_boost(conn, body, category, trait, *, network=None,
              boost_initial=None, boost_step_hours=None):
    """Admin opt-in: configure a dormant boost. Resets the clock, so it also
    re-arms a finished boost (comeback event)."""
    network = network or config.XRPL_NETWORK
    cur = conn.execute(
        """UPDATE trait_rarity
           SET boost_initial=?, boost_step_hours=?, boost_started_at=NULL
           WHERE network=? AND body=? AND category=? AND trait=?""",
        (boost_initial if boost_initial is not None
         else config.RARITY_BOOST_INITIAL,
         boost_step_hours if boost_step_hours is not None
         else config.RARITY_BOOST_STEP_HOURS,
         network, body, category, trait))
    conn.commit()
    if cur.rowcount == 0:
        raise ValueError(f"No trait_rarity row for "
                         f"{network}/{body}/{category}/{trait}")


def start_boost_clock(conn, body, category, trait, *, network=None,
                      now=None):
    """Called when a mint completes: if the picked trait has an armed,
    dormant boost, start its clock. No-op otherwise."""
    network = network or config.XRPL_NETWORK
    now = now or utcnow()
    conn.execute(
        """UPDATE trait_rarity SET boost_started_at=?
           WHERE network=? AND body=? AND category=? AND trait=?
             AND boost_initial IS NOT NULL AND boost_started_at IS NULL""",
        (now.isoformat(), network, body, category, trait))
    conn.commit()


def boost_status(boost_initial, boost_step_hours, boost_started_at, now):
    """Human-readable boost state for admin views."""
    if not boost_initial:
        return "—"
    if not boost_started_at:
        return "dormant"
    mult = boost_multiplier(boost_initial, boost_step_hours,
                            boost_started_at, now)
    if mult <= 1.0:
        return "finished"
    started = datetime.fromisoformat(boost_started_at)
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    step = boost_step_hours or 24
    total_h = (boost_initial - 1) * step
    left_h = total_h - (now - started).total_seconds() / 3600.0
    return f"active {mult:g}x — {left_h / 24:.1f}d left"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_rarity.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add lfg_core/rarity.py tests/test_rarity.py
git commit -m "feat(rarity): boost lifecycle - arm, trigger on mint, status"
```

---

### Task 6: gender → body rename in `lfg_core`

Mechanical rename; the store API and call sites stop calling the body class "gender". **Persisted data keys are NOT renamed**: `swap_meta.make_record()`'s `"gender"` JSON key and `nft["gender"]` dict keys built from existing swap records stay as-is (backward compatibility with files on disk/CDN); `detect_gender()` is renamed to `detect_body()` with a `detect_gender = detect_body` alias kept so nothing external breaks.

**Files:**
- Modify: `lfg_core/layer_store.py`, `lfg_core/traits.py`, `lfg_core/swap_compose.py`, `lfg_core/swap_meta.py`, `lfg_core/mint_flow.py`, `lfg_core/swap_flow.py`, `lfg_core/config.py:98` (comment)

- [ ] **Step 1: Rename in layer_store.py**

In `lfg_core/layer_store.py`: rename `list_genders` → `list_bodies` (both store classes), every `gender` parameter/variable → `body`, and update the header comment `<gender>/<TraitType>/<Value>` → `<body>/<TraitType>/<Value>`. Same comment fix in `lfg_core/config.py:98`.

- [ ] **Step 2: Rename call sites**

- `lfg_core/traits.py`: `select_random_attributes(store, gender=None)` → `(store, body=None)`; `store.list_genders()` → `store.list_bodies()`; returns `(body, attributes)`.
- `lfg_core/swap_compose.py`: `gender` params → `body` in `missing_layers` and `compose_nft` (and the f-strings using them).
- `lfg_core/swap_meta.py`: `detect_gender` → `detect_body`; add `detect_gender = detect_body` alias line directly below; the `"gender"` key in `make_record`'s returned dict stays.
- `lfg_core/mint_flow.py`: local variable `gender` → `body` around line 228.
- `lfg_core/swap_flow.py`: keep `nft["gender"]` dict reads (persisted-record key) but rename any local `gender` variables passed onward as `body`.

Verify nothing dangles:

```bash
grep -rn "list_genders" lfg_core/ webapp/ main.py
```

Expected: no matches.

- [ ] **Step 3: Run the full suite**

Run: `python -m pytest tests/test_rarity.py webapp/test_smoke.py -v`
Expected: all PASS (fix any smoke-test references to renamed symbols, e.g. imports of `traits`).

- [ ] **Step 4: Commit**

```bash
git add lfg_core/ webapp/
git commit -m "refactor: rename gender to body across lfg_core (skeleton/ape aren't genders)"
```

---

### Task 7: Webapp integration — weighted body + trait selection

**Files:**
- Modify: `lfg_core/traits.py` (replace uniform choices), `lfg_core/mint_flow.py` (post-record hook)
- Test: `tests/test_rarity.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_rarity.py`:

```python
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
    store = FakeStore({"male": {"Background": ["Red", "Blue"],
                                "Body": ["Straight Dark"]}})
    body, attrs = asyncio.get_event_loop().run_until_complete(
        traits.select_random_attributes(store, conn=conn, network="testnet",
                                        now=NOW, rng=random.Random(1)))
    assert body == "male"
    types = {a["trait_type"] for a in attrs}
    assert types == {"Background", "Body"}
    # Engine left auto-detected rows behind
    n = conn.execute("""SELECT COUNT(*) FROM trait_rarity
                        WHERE network='testnet'""").fetchone()[0]
    assert n >= 3  # 2 backgrounds + 1 body trait (+ Body Type row)


def test_select_random_attributes_weights_body_pick(conn):
    import asyncio
    from lfg_core import traits
    # 99 male : 1 ape in the collection → male should dominate body picks
    for i in range(99):
        insert_nft(conn, i + 1, body="male")
    insert_nft(conn, 100, body="ape")
    rarity.recalculate_rarity(conn, network="testnet")
    store = FakeStore({"male": {"Background": ["Red"]},
                       "ape": {"Background": ["Red"]}})
    rng = random.Random(9)
    bodies = [asyncio.get_event_loop().run_until_complete(
        traits.select_random_attributes(store, conn=conn, network="testnet",
                                        now=NOW, rng=rng))[0]
        for _ in range(200)]
    assert bodies.count("male") > 150
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_rarity.py -v`
Expected: FAIL with `TypeError: select_random_attributes() got an unexpected keyword argument 'conn'`.

- [ ] **Step 3: Rewrite `lfg_core/traits.py`**

```python
# lfg_core/traits.py
# Rarity-weighted trait selection from the unified layer store (used by the
# webapp mint flow). The classic bot's directory-based helpers live in
# main.py. Weights come from lfg_core.rarity (proportional-with-floor).

import random

from lfg_core import rarity
from lfg_core.swap_meta import TRAIT_ORDER


async def select_random_attributes(store, body=None, *, conn=None,
                                   network=None, now=None, rng=random):
    """Pick a body (rarity-weighted unless given) and one rarity-weighted
    value per trait type from the unified layer store. Returns
    (body, attributes) where attributes is a metadata-style
    [{trait_type, value}] list in layer order."""
    own_conn = conn is None
    if own_conn:
        conn = rarity.connect()
    try:
        if body is None:
            bodies = await store.list_bodies()
            if not bodies:
                raise ValueError("Layer store has no body directories")
            body = rarity.weighted_pick(
                conn, rarity.BODY_SENTINEL, rarity.BODY_CATEGORY, bodies,
                network=network, now=now, rng=rng)
        attributes = []
        for trait_type in TRAIT_ORDER:
            values = await store.list_values(body, trait_type)
            if values:
                value = rarity.weighted_pick(conn, body, trait_type, values,
                                             network=network, now=now,
                                             rng=rng)
                attributes.append({"trait_type": trait_type, "value": value})
        if not attributes:
            raise ValueError(f"No trait layers found for body '{body}'")
        return body, attributes
    finally:
        if own_conn:
            conn.close()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_rarity.py webapp/test_smoke.py -v`
Expected: all PASS.

- [ ] **Step 5: Hook mint completion in `lfg_core/mint_flow.py`**

Directly after the `if saved:` block that follows `record_nft_mint` (mint_flow.py:279-283), add a call that stamps rarity state. Add `from lfg_core import rarity` to the imports, then:

```python
        if saved:
            def _update_rarity():
                conn = rarity.connect()
                try:
                    conn.execute(
                        "UPDATE LFG SET network=?, body=? WHERE nft_number=?",
                        (config.XRPL_NETWORK, body, session.nft_number))
                    conn.commit()
                    for attr in metadata["attributes"]:
                        rarity.start_boost_clock(conn, body,
                                                 attr["trait_type"],
                                                 attr["value"])
                    rarity.start_boost_clock(conn, rarity.BODY_SENTINEL,
                                             rarity.BODY_CATEGORY, body)
                    rarity.recalculate_rarity(conn)
                finally:
                    conn.close()
            try:
                await asyncio.to_thread(_update_rarity)
            except Exception:
                logging.error(f"rarity update failed: {traceback.format_exc()}")
```

(The existing `if saved: _reserved_numbers.discard(...)` line stays; place this inside the same `if saved:` block. `body` is the variable renamed in Task 6 at mint_flow.py:228.)

- [ ] **Step 6: Run the full suite and commit**

Run: `python -m pytest tests/test_rarity.py webapp/test_smoke.py -v`
Expected: all PASS.

```bash
git add lfg_core/traits.py lfg_core/mint_flow.py tests/test_rarity.py
git commit -m "feat(rarity): webapp mint path uses weighted body + trait selection"
```

---

### Task 8: Legacy bot integration (`main.py get_random_trait` + burn hook)

**Files:**
- Modify: `main.py:256-263` (`get_random_trait`), `main.py` burn handler (~line 1430, after the `INSERT INTO burned_nfts`)
- Test: `tests/test_rarity.py` (append)

- [ ] **Step 1: Write the failing test for folder-name → category mapping**

The legacy path derives the rarity category from the trait folder name (`"8 hat:hair"` → `Head`). Put the mapping in `rarity.py` so it's testable without Discord. Append to `tests/test_rarity.py`:

```python
def test_category_for_folder():
    assert rarity.category_for_folder("1 background") == "Background"
    assert rarity.category_for_folder("8 hat:hair") == "Head"
    assert rarity.category_for_folder("9 accessory") == "Accessory"
    assert rarity.category_for_folder("2 back") == "Back"
    assert rarity.category_for_folder("99 unknown_thing") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_rarity.py::test_category_for_folder -v`
Expected: FAIL with AttributeError.

- [ ] **Step 3: Implement `category_for_folder` in `lfg_core/rarity.py`**

```python
FOLDER_CATEGORY = {
    "background": "Background", "back": "Back", "body": "Body",
    "clothing": "Clothing", "mouth": "Mouth", "eyebrows": "Eyebrows",
    "eyes": "Eyes", "hat:hair": "Head", "accessory": "Accessory",
}


def category_for_folder(folder_name):
    """Map a legacy trait_layers folder name ('8 hat:hair') to a rarity
    category ('Head'). None if unrecognized."""
    import re
    name = re.sub(r"^\d+\s*", "", folder_name).strip().lower()
    return FOLDER_CATEGORY.get(name)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_rarity.py::test_category_for_folder -v`
Expected: PASS.

- [ ] **Step 5: Rewrite `get_random_trait` in `main.py`**

Replace the body of `get_random_trait` (main.py:256-263). Add `from lfg_core import rarity` near main.py's other imports.

```python
def get_random_trait(trait_layer_dir):
    """
    Select an image file from the given trait layer directory, weighted by
    the rarity engine (proportional-with-floor). Falls back to uniform
    random if the engine is unavailable so the bot never bricks.
    """
    files = get_trait_files(trait_layer_dir)
    if not files:
        raise ValueError(f"No valid image files found in directory: {trait_layer_dir}")
    category = rarity.category_for_folder(os.path.basename(trait_layer_dir))
    if category is None:
        return random.choice(files)
    try:
        by_stem = {os.path.splitext(f)[0]: f for f in files}
        pick = rarity_pick_for_legacy(category, list(by_stem))
        return by_stem[pick]
    except Exception as e:
        logging.warning(f"rarity engine unavailable, uniform fallback: {e}")
        return random.choice(files)


def rarity_pick_for_legacy(category, stems):
    """Open a short-lived connection and run a weighted pick for the legacy
    (ungendered) path."""
    conn = rarity.connect()
    try:
        return rarity.weighted_pick(conn, rarity.BODY_SENTINEL, category,
                                    stems)
    finally:
        conn.close()
```

- [ ] **Step 6: Hook the burn path**

In the `/admin` burn handler (main.py ~1430-1460), directly after the `INSERT INTO burned_nfts ...` execute + commit succeeds, add:

```python
                try:
                    rarity.recalculate_rarity(conn)
                except Exception as e:
                    logging.error(f"rarity recalc after burn failed: {e}")
```

(`conn` is the already-open sqlite connection in that handler; recalc uses the network from config.)

- [ ] **Step 7: Verify main.py imports cleanly and tests pass**

Run: `python -c "import ast; ast.parse(open('main.py').read())" && python -m pytest tests/test_rarity.py webapp/test_smoke.py -v`
Expected: no syntax error; all tests PASS.

- [ ] **Step 8: Commit**

```bash
git add main.py lfg_core/rarity.py tests/test_rarity.py
git commit -m "feat(rarity): legacy bot path uses weighted picks; burn triggers recalc"
```

---

### Task 9: Network/body stamping in `db_helpers.record_nft_mint`

**Files:**
- Modify: `db_helpers.py:57` (`record_nft_mint`)
- Test: `tests/test_rarity.py` (append)

- [ ] **Step 1: Write the failing test**

`record_nft_mint` hardcodes `sqlite3.connect('lfg_nfts.db')`; add an optional `db_path` parameter (default `'lfg_nfts.db'`) so it's testable, plus `network`/`body` parameters. Append to `tests/test_rarity.py`:

```python
def test_record_nft_mint_stamps_network_and_body(tmp_path):
    import db_helpers
    db = str(tmp_path / "t.db")
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE LFG (nft_number INTEGER PRIMARY KEY)")
    c.commit()
    c.close()
    ok = db_helpers.record_nft_mint(
        nft_number=9001, nft_id="ABC", discord_id="1", owner_address="r1",
        metadata_url="m", image_url="i", traits={"Background": "Red"},
        network="testnet", body="male", db_path=db)
    assert ok
    c = sqlite3.connect(db)
    row = c.execute("""SELECT network, body, Background FROM LFG
                       WHERE nft_number=9001""").fetchone()
    c.close()
    assert row == ("testnet", "male", "Red")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_rarity.py::test_record_nft_mint_stamps_network_and_body -v`
Expected: FAIL with `TypeError: ... unexpected keyword argument 'network'`.

- [ ] **Step 3: Modify `record_nft_mint`**

In `db_helpers.py`:
- Signature becomes:
  ```python
  def record_nft_mint(nft_number: int, nft_id: str, discord_id: str,
                      owner_address: str, metadata_url: str, image_url: str,
                      traits: dict, network: str = 'mainnet',
                      body: str = '*', db_path: str = 'lfg_nfts.db') -> bool:
  ```
- `sqlite3.connect('lfg_nfts.db')` → `sqlite3.connect(db_path)`
- Add to the `new_columns` dict: `'network': "TEXT NOT NULL DEFAULT 'mainnet'"`, `'body': "TEXT NOT NULL DEFAULT '*'"`
- Extend the INSERT column list with `network, body`, two more `?` placeholders, and append `network, body` to the values tuple.

- [ ] **Step 4: Pass network/body from `mint_flow`**

In `lfg_core/mint_flow.py`, in the `record = dict(...)` construction (~line 267), add:

```python
            network=config.XRPL_NETWORK,
            body=body,
```

Then **simplify the Task 7 hook**: the `UPDATE LFG SET network=?, body=? ...` statement inside `_update_rarity` is now redundant — delete those three lines (the `conn.execute(...)` and `conn.commit()` for that UPDATE), keeping the boost-clock and recalc calls.

- [ ] **Step 5: Run the full suite**

Run: `python -m pytest tests/test_rarity.py webapp/test_smoke.py -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add db_helpers.py lfg_core/mint_flow.py tests/test_rarity.py
git commit -m "feat(rarity): stamp network and body on mint records"
```

---

### Task 10: `rarity_admin.py` CLI (seed, refresh, boost, floor, enable/disable, odds)

**Files:**
- Create: `rarity_admin.py`
- Test: `tests/test_rarity.py` (append — tests target the underlying functions, which live in `rarity.py`; the CLI file is argparse glue)

- [ ] **Step 1: Write the failing tests for `seed_from_collection` and `set_floor`**

Append to `tests/test_rarity.py`:

```python
def test_seed_backfills_body_from_traits(conn):
    # Legacy rows have body='*'; seed derives it from the Body trait value
    # via swap_meta.detect_body and recounts.
    insert_nft(conn, 1, body_trait="Straight Dark", body="*")   # → male
    insert_nft(conn, 2, body_trait="Curved Light", body="*")    # → female
    insert_nft(conn, 3, body_trait="Ape", body="*")             # → ape
    insert_nft(conn, 4, body_trait="Bones", body="*")           # → skeleton
    rarity.seed_from_collection(conn, network="testnet")
    rows = dict(conn.execute("SELECT nft_number, body FROM LFG"))
    assert rows == {1: "male", 2: "female", 3: "ape", 4: "skeleton"}
    body_counts = dict(conn.execute(
        """SELECT trait, live_count FROM trait_rarity
           WHERE category=? AND network='testnet'""",
        (rarity.BODY_CATEGORY,)))
    assert body_counts == {"male": 1, "female": 1, "ape": 1, "skeleton": 1}


def test_seed_marks_testnet_numbers(conn):
    insert_nft(conn, 1, network="mainnet")
    insert_nft(conn, 2, network="mainnet")
    rarity.seed_from_collection(conn, network="mainnet",
                                mark_testnet=[2])
    rows = dict(conn.execute("SELECT nft_number, network FROM LFG"))
    assert rows == {1: "mainnet", 2: "testnet"}


def test_set_floor_global_and_per_trait(conn):
    seed_row(conn, "Red", 10)
    seed_row(conn, "Blue", 10)
    rarity.set_floor(conn, 0.01, network="testnet")
    floors = {r[0] for r in conn.execute(
        "SELECT floor_weight FROM trait_rarity WHERE network='testnet'")}
    assert floors == {0.01}
    rarity.set_floor(conn, 0.05, network="testnet", body="*",
                     category="Background", trait="Red")
    (red,) = conn.execute("""SELECT floor_weight FROM trait_rarity
                             WHERE trait='Red'""").fetchone()
    assert red == 0.05


def test_set_enabled(conn):
    seed_row(conn, "Red", 10)
    rarity.set_enabled(conn, "*", "Background", "Red", False,
                       network="testnet")
    (e,) = conn.execute("""SELECT enabled FROM trait_rarity
                           WHERE trait='Red'""").fetchone()
    assert e == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_rarity.py -v`
Expected: FAIL with `AttributeError: ... 'seed_from_collection'`.

- [ ] **Step 3: Implement the admin functions in `lfg_core/rarity.py`**

```python
def seed_from_collection(conn, network=None, mark_testnet=None,
                         layer_values=None):
    """Bootstrap: optionally mark known test mints as testnet, backfill
    LFG.body from the stored Body trait, register any layer-store values,
    then full recount. layer_values: optional
    {body: {category: [trait, ...]}} from scanning the stores."""
    from lfg_core.swap_meta import detect_body
    network = network or config.XRPL_NETWORK
    ensure_schema(conn)
    if mark_testnet:
        qs = ",".join("?" * len(mark_testnet))
        conn.execute(
            f"UPDATE LFG SET network='testnet' WHERE nft_number IN ({qs})",
            list(mark_testnet))
    rows = conn.execute(
        "SELECT nft_number, Body FROM LFG WHERE body=?",
        (BODY_SENTINEL,)).fetchall()
    for number, body_trait in rows:
        conn.execute("UPDATE LFG SET body=? WHERE nft_number=?",
                     (detect_body([{"trait_type": "Body",
                                    "value": body_trait or ""}]), number))
    now = utcnow()
    for body, categories in (layer_values or {}).items():
        for category, values in categories.items():
            _ensure_rows(conn, network, body, category, values, now)
    conn.commit()
    recalculate_rarity(conn, network=network)


def set_floor(conn, floor, *, network=None, body=None, category=None,
              trait=None):
    """Set floor_weight globally for a network, or for one trait when
    body/category/trait are all given."""
    network = network or config.XRPL_NETWORK
    if trait is not None:
        conn.execute(
            """UPDATE trait_rarity SET floor_weight=? WHERE network=?
               AND body=? AND category=? AND trait=?""",
            (floor, network, body, category, trait))
    else:
        conn.execute(
            "UPDATE trait_rarity SET floor_weight=? WHERE network=?",
            (floor, network))
    conn.commit()


def set_enabled(conn, body, category, trait, enabled, *, network=None):
    network = network or config.XRPL_NETWORK
    conn.execute(
        """UPDATE trait_rarity SET enabled=? WHERE network=? AND body=?
           AND category=? AND trait=?""",
        (1 if enabled else 0, network, body, category, trait))
    conn.commit()


def get_odds(conn, body, category, *, network=None, now=None):
    """Rows for admin display: [(trait, live_count, share%, weight,
    boost status), ...] sorted by weight desc."""
    network = network or config.XRPL_NETWORK
    now = now or utcnow()
    rows = conn.execute(
        """SELECT trait, live_count, floor_weight, boost_initial,
                  boost_step_hours, boost_started_at, enabled
           FROM trait_rarity WHERE network=? AND body=? AND category=?""",
        (network, body, category)).fetchall()
    total = sum(r[1] for r in rows)
    out = []
    for trait, count, floor, bi, bs, bsa, enabled in rows:
        share = (count / total * 100) if total else 0.0
        weight = effective_weight(count, total, floor, bi, bs, bsa, now)
        status = "disabled" if not enabled else boost_status(bi, bs, bsa, now)
        out.append((trait, count, share, weight, status))
    return sorted(out, key=lambda r: -r[3])
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_rarity.py -v`
Expected: all PASS.

- [ ] **Step 5: Write the CLI glue**

Create `rarity_admin.py`:

```python
#!/usr/bin/env python3
# Admin CLI for the variable rarity engine. Operates on the network
# selected by XRPL_NETWORK unless --network overrides it.
#
#   python rarity_admin.py seed [--mark-testnet 9001 9002]
#   python rarity_admin.py refresh
#   python rarity_admin.py odds --body '*' --category Background
#   python rarity_admin.py boost --body '*' --category Head --trait Crown \
#       [--initial 7] [--step-hours 24]
#   python rarity_admin.py set-floor 0.005 [--body B --category C --trait T]
#   python rarity_admin.py disable|enable --body B --category C --trait T

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv()

from lfg_core import config, rarity  # noqa: E402


def scan_layer_values():
    """Scan the unified layer store for {body: {category: [values]}}."""
    from lfg_core import layer_store
    store = layer_store.make_store()

    async def scan():
        out = {}
        for body in await store.list_bodies():
            out[body] = {}
            for trait_type in await store.list_trait_types(body):
                values = await store.list_values(body, trait_type)
                if values:
                    out[body][trait_type] = values
        return out
    return asyncio.run(scan())


def main():
    p = argparse.ArgumentParser(description="Rarity engine admin")
    p.add_argument("--network", default=None)
    p.add_argument("--db", default=None, help="path to sqlite db")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("seed")
    s.add_argument("--mark-testnet", nargs="*", type=int, default=None)
    sub.add_parser("refresh")
    o = sub.add_parser("odds")
    for cmd in ("boost", "disable", "enable"):
        c = sub.add_parser(cmd)
        c.add_argument("--body", required=True)
        c.add_argument("--category", required=True)
        c.add_argument("--trait", required=True)
        if cmd == "boost":
            c.add_argument("--initial", type=float, default=None)
            c.add_argument("--step-hours", type=int, default=None)
    o.add_argument("--body", required=True)
    o.add_argument("--category", required=True)
    f = sub.add_parser("set-floor")
    f.add_argument("floor", type=float)
    f.add_argument("--body")
    f.add_argument("--category")
    f.add_argument("--trait")

    args = p.parse_args()
    net = args.network or config.XRPL_NETWORK
    conn = rarity.connect(args.db)
    try:
        rarity.ensure_schema(conn)
        if args.cmd == "seed":
            try:
                layer_values = scan_layer_values()
            except Exception as e:
                print(f"layer store scan skipped: {e}")
                layer_values = None
            rarity.seed_from_collection(conn, network=net,
                                        mark_testnet=args.mark_testnet,
                                        layer_values=layer_values)
            print(f"seeded ({net})")
        elif args.cmd == "refresh":
            rarity.recalculate_rarity(conn, network=net)
            print(f"recounted ({net})")
        elif args.cmd == "odds":
            for trait, count, share, weight, status in rarity.get_odds(
                    conn, args.body, args.category, network=net):
                print(f"{trait:30s} n={count:5d} share={share:6.2f}% "
                      f"w={weight:.4f} {status}")
        elif args.cmd == "boost":
            rarity.arm_boost(conn, args.body, args.category, args.trait,
                             network=net, boost_initial=args.initial,
                             boost_step_hours=args.step_hours)
            print(f"boost armed: {args.trait} (dormant until first mint)")
        elif args.cmd in ("disable", "enable"):
            rarity.set_enabled(conn, args.body, args.category, args.trait,
                               args.cmd == "enable", network=net)
            print(f"{args.trait}: {args.cmd}d")
        elif args.cmd == "set-floor":
            rarity.set_floor(conn, args.floor, network=net, body=args.body,
                             category=args.category, trait=args.trait)
            print("floor updated")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
```

**Note:** `layer_store.make_store()` — check the actual factory name in `lfg_core/layer_store.py` (it may be a class constructed from config, e.g. `CdnLayerStore()`/`LocalLayerStore()` chosen by `config.LAYER_SOURCE`). Use whatever the webapp's `server.py`/`mint_flow.py` uses to build its store; mirror that exact call here.

- [ ] **Step 6: Smoke the CLI against a scratch DB**

```bash
python rarity_admin.py --db /tmp/rarity_scratch.db --network testnet refresh && \
python rarity_admin.py --db /tmp/rarity_scratch.db --network testnet odds --body '*' --category Background
```

Expected: `recounted (testnet)` then empty odds output, no traceback. (LFG table absent → ensure_schema tolerates it; if recalc fails on missing LFG, guard `recalculate_rarity` with a table-existence check returning early — fix and re-run.)

- [ ] **Step 7: Commit**

```bash
git add rarity_admin.py lfg_core/rarity.py tests/test_rarity.py
git commit -m "feat(rarity): admin CLI - seed, refresh, boost, floor, enable, odds"
```

---

### Task 11: Discord `/admin` panel — View Odds / Boost / Disable

**Files:**
- Modify: `main.py` (the existing admin View class, near the Lookup NFT / Burn NFT buttons ~line 1300-1500)

The existing admin panel uses buttons + modals (see Lookup NFT for the pattern). Follow that pattern exactly.

- [ ] **Step 1: Add a `RarityOddsModal`**

```python
class RarityOddsModal(discord.ui.Modal, title="View Rarity Odds"):
    body = discord.ui.TextInput(label="Body (* for legacy/Body Type)",
                                default="*", max_length=20)
    category = discord.ui.TextInput(
        label="Category (Background, Head, Body Type, ...)", max_length=30)

    async def on_submit(self, interaction: discord.Interaction):
        from lfg_core import rarity
        conn = rarity.connect()
        try:
            rows = rarity.get_odds(conn, self.body.value.strip(),
                                   self.category.value.strip())
        finally:
            conn.close()
        if not rows:
            await interaction.response.send_message(
                "No rarity rows for that body/category.", ephemeral=True)
            return
        lines = [f"`{t:24.24s}` n={c:<5d} {s:5.1f}% w={w:.4f}  {st}"
                 for t, c, s, w, st in rows[:25]]
        embed = discord.Embed(
            title=f"Odds — {self.body.value} / {self.category.value}",
            description="\n".join(lines), color=0x00FF00)
        await interaction.response.send_message(embed=embed, ephemeral=True)
```

- [ ] **Step 2: Add a `RarityBoostModal`**

```python
class RarityBoostModal(discord.ui.Modal, title="Arm Trait Boost"):
    body = discord.ui.TextInput(label="Body (* for legacy)", default="*",
                                max_length=20)
    category = discord.ui.TextInput(label="Category", max_length=30)
    trait = discord.ui.TextInput(label="Trait value", max_length=60)
    initial = discord.ui.TextInput(label="Boost multiplier", default="7",
                                   max_length=5)
    confirm = discord.ui.TextInput(
        label="Type CONFIRM if trait already has mints", required=False,
        max_length=10)

    async def on_submit(self, interaction: discord.Interaction):
        from lfg_core import rarity
        conn = rarity.connect()
        try:
            row = conn.execute(
                """SELECT live_count FROM trait_rarity WHERE network=?
                   AND body=? AND category=? AND trait=?""",
                (config.XRPL_NETWORK, self.body.value.strip(),
                 self.category.value.strip(),
                 self.trait.value.strip())).fetchone()
            if row is None:
                await interaction.response.send_message(
                    "Unknown trait — it must exist in the rarity table "
                    "(mint once or run seed).", ephemeral=True)
                return
            if row[0] > 0 and self.confirm.value.strip() != "CONFIRM":
                await interaction.response.send_message(
                    f"'{self.trait.value}' already has {row[0]} mints. "
                    "Re-submit with CONFIRM to arm a comeback boost.",
                    ephemeral=True)
                return
            rarity.arm_boost(conn, self.body.value.strip(),
                             self.category.value.strip(),
                             self.trait.value.strip(),
                             boost_initial=float(self.initial.value))
        finally:
            conn.close()
        await interaction.response.send_message(
            f"Boost armed for **{self.trait.value}** "
            f"({self.initial.value}×, dormant until first organic mint).",
            ephemeral=True)
        await log_admin_action(
            interaction.client,
            f"🎚️ Boost armed by {interaction.user}: "
            f"{self.body.value}/{self.category.value}/{self.trait.value} "
            f"@ {self.initial.value}x")
```

`log_admin_action` — reuse the existing admin-channel logging helper used by the burn flow (main.py:1239 area sends to `ADMIN_LOG_CHANNEL_ID`); if it's inline rather than a helper, extract it to a `log_admin_action(client, message)` coroutine first and use it from both places. A `RarityDisableModal` follows the same shape (fields body/category/trait + a literal `DISABLE`/`ENABLE` action field; calls `rarity.set_enabled`; logs the action).

- [ ] **Step 3: Add three buttons to the admin view**

In the admin View class, following the existing button pattern:

```python
    @discord.ui.button(label="View Odds", style=discord.ButtonStyle.secondary,
                       emoji="🎲", row=2)
    async def view_odds(self, interaction, button):
        await interaction.response.send_modal(RarityOddsModal())

    @discord.ui.button(label="Boost Trait", style=discord.ButtonStyle.primary,
                       emoji="🚀", row=2)
    async def boost_trait(self, interaction, button):
        await interaction.response.send_modal(RarityBoostModal())

    @discord.ui.button(label="Toggle Trait", style=discord.ButtonStyle.danger,
                       emoji="🚫", row=2)
    async def toggle_trait(self, interaction, button):
        await interaction.response.send_modal(RarityDisableModal())
```

(Adjust `row=` to a free row in the existing view; check current button rows first.)

- [ ] **Step 4: Verify and commit**

Run: `python -c "import ast; ast.parse(open('main.py').read())" && python -m pytest tests/test_rarity.py webapp/test_smoke.py -v`
Expected: parse OK, all tests PASS.

```bash
git add main.py
git commit -m "feat(rarity): admin panel - view odds, arm boost, toggle trait"
```

---

### Task 12: Distribution sanity test + full verification

**Files:**
- Test: `tests/test_rarity.py` (append)

- [ ] **Step 1: Write the distribution test**

```python
def test_distribution_matches_weights(conn):
    # 60/30/10 split, floor small enough not to bind. 10k draws → observed
    # frequencies within ±3 percentage points of expected.
    for i in range(60):
        insert_nft(conn, i + 1, background="A")
    for i in range(30):
        insert_nft(conn, i + 61, background="B")
    for i in range(10):
        insert_nft(conn, i + 91, background="C")
    rarity.recalculate_rarity(conn, network="testnet")
    rng = random.Random(1234)
    picks = [rarity.weighted_pick(conn, "*", "Background", ["A", "B", "C"],
                                  network="testnet", now=NOW, rng=rng)
             for _ in range(10000)]
    for trait, expected in (("A", 0.60), ("B", 0.30), ("C", 0.10)):
        observed = picks.count(trait) / 10000
        assert abs(observed - expected) < 0.03, (trait, observed)
```

- [ ] **Step 2: Run the full suite**

Run: `python -m pytest tests/test_rarity.py webapp/test_smoke.py -v`
Expected: all PASS (rarity tests + 52 existing smoke tests).

- [ ] **Step 3: Commit**

```bash
git add tests/test_rarity.py
git commit -m "test(rarity): distribution sanity - 10k draws match expected frequencies"
```

---

### Task 13: Rollout (manual, per spec)

Not code — operator steps after merge:

- [ ] On the testnet env: `python rarity_admin.py seed`, then `python rarity_admin.py odds --body '*' --category 'Body Type'` and spot-check counts against `SELECT body, COUNT(*) FROM LFG GROUP BY body`.
- [ ] Mint a few testnet NFTs through the webapp; re-run `odds` and watch counts move.
- [ ] Arm a boost with short windows (`--initial 3 --step-hours 1`) on a dummy trait; mint until it triggers; verify `odds` shows `active 3x`, then `2x` an hour later, then finished.
- [ ] On mainnet: `python rarity_admin.py seed` (with `--mark-testnet` for any known test mints), deploy, monitor the first mints' admin logs.
- [ ] Rollback if needed: revert the selection call sites (Tasks 7-8) to uniform random; the table is additive and harmless to leave in place.

---

## Self-review notes

- **Spec coverage:** schema+network (T1, T9), weight math+boost decay (T2), pick+auto-detect+staleness (T3-T4), boost lifecycle incl. trigger-on-completed-mint (T5, T7), gender→body rename (T6), webapp+Body Type weighting (T7), legacy path+fallback+burn hook (T8), CLI seed/refresh/floor (T10 — `refresh` is the XRPL-NFT-Listener's hook), Discord admin+audit logging (T11), distribution sanity (T12), rollout (T13). Trait-swap recalc: swaps don't currently write LFG trait columns; the staleness guard + listener `refresh` cover drift — revisit when swaps write to the DB.
- **Known judgment calls for the implementer:** exact placement of admin buttons (`row=`), the layer-store factory name in Task 10 Step 5, and the Task 4 note about reworking Task 3 tests that the staleness guard invalidates.
