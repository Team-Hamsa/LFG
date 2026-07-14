# Trait Shop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Project-run trait shop: any rarity-enabled trait purchasable for BRIX (burned by construction), minted on demand and settled into the buyer's Closet; plus taxon realignment (traits → 176, Assemble-mints → 1760).

**Architecture:** New `lfg_core/shop.py` (pricing + overrides + catalog derivation, app DB) and `lfg_core/shop_flow.py` (`ShopBuySession` state machine + `shop_orders` store, economy-network `onchain_<net>.db`), wired into `lfg_service/app.py` endpoints and the existing 2-minute sweep. Reuses `xrpl_ops.mint_nft`/`create_nft_offer` (issued-currency amount), XUMM accept payloads, and `run_deposit` settlement.

**Tech Stack:** Python 3, aiohttp, sqlite3, xrpl-py, XUMM SDK, pytest.

**Spec:** `docs/superpowers/specs/2026-07-14-trait-shop-design.md` · **Issue:** #217

## Global Constraints

- `SourceTag = 2606160021` on every XRPL tx / XUMM payload (automatic via existing builders — never hand-set).
- Provenance memos on every tx: new action `shop-buy`; thread real surface via `memos.platform_for_surface`.
- Everything shop-related is `ECONOMY_ENABLED`-gated; trait DB reads resolve via `config.ECONOMY_NETWORK`.
- User↔user marketplace stays XRP-only — do not touch the listener's XRP-denominated public-listing filter.
- New test files that import `lfg_core` at module top MUST copy the env-guard preamble (`BUNNY_PULL_ZONE`/`LAYER_SOURCE`) used by existing tests (see `tests/test_market_flow.py` top-of-file).
- Pre-push gate (ruff/mypy/gitleaks/pytest/validate-trait-config) must stay green; never `--no-verify`.
- All new code type-annotated (mypy strict-ish gate).
- Shop price formula (spec §Catalog & pricing): `smoothed_share = (Σ_bodies live_count + shop_count + 1) / (Σ_bodies category_total + population_size)`; `price = clamp(round(SHOP_BASE_BRIX / smoothed_share), SHOP_MIN_BRIX, SHOP_MAX_BRIX)`; `price_override` wins; `excluded` or rarity-disabled → not purchasable.
- `shop_count` affects shop pricing ONLY — `weighted_pick`/`effective_weight` mint odds unchanged.

---

### Task 1: Config + memo action

**Files:**
- Modify: `lfg_core/config.py` (near `TRAIT_TAXON`, line ~287)
- Modify: `lfg_core/memos.py` (action constants block, lines 62–79)
- Test: `tests/test_shop_config.py`

**Interfaces:**
- Produces: `config.SHOP_BASE_BRIX: float` (default 1.0), `config.SHOP_MIN_BRIX: int` (default 5), `config.SHOP_MAX_BRIX: int` (default 5000), `config.SHOP_OFFER_TTL_SECONDS: int` (default 900), `config.ASSEMBLE_TAXON: int` (default 1760), `config.TRAIT_TAXON` default changed `1763 → 176`; `memos.ACTION_SHOP_BUY = "shop-buy"` (member of `_ACTIONS`).

- [ ] **Step 1: Write the failing test**

```python
"""tests/test_shop_config.py — copy the env-guard preamble from tests/test_market_flow.py first."""
import os

os.environ.setdefault("BUNNY_PULL_ZONE", "test.example")
os.environ.setdefault("LAYER_SOURCE", "local")

from lfg_core import config, memos


def test_shop_config_defaults():
    assert config.SHOP_BASE_BRIX == 1.0
    assert config.SHOP_MIN_BRIX == 5
    assert config.SHOP_MAX_BRIX == 5000
    assert config.SHOP_OFFER_TTL_SECONDS == 900
    assert config.ASSEMBLE_TAXON == 1760
    assert config.TRAIT_TAXON == 176  # default flipped from 1763


def test_shop_buy_memo_action():
    assert memos.ACTION_SHOP_BUY == "shop-buy"
    # closed enum accepts it (raises on unknown actions)
    m = memos.build_memos_json(memos.INITIATOR_BACKEND, memos.PLATFORM_BACKEND, memos.ACTION_SHOP_BUY)
    assert m
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_shop_config.py -v`
Expected: FAIL (`AttributeError: SHOP_BASE_BRIX` / `TRAIT_TAXON == 1763`)

- [ ] **Step 3: Implement**

In `lfg_core/config.py`, change the `TRAIT_TAXON` default and add below it:

```python
TRAIT_TAXON = int(os.getenv("TRAIT_TAXON", "176"))  # flipped from 1763 (#217)
# Assemble-minted rebirth characters get their own taxon; regular /letsgo
# mints stay NFT_TAXON (0) so the main collection is never split (#217).
ASSEMBLE_TAXON = int(os.getenv("ASSEMBLE_TAXON", "1760"))

# Trait Shop (#217): price = clamp(SHOP_BASE_BRIX / smoothed_share, MIN, MAX)
SHOP_BASE_BRIX = float(os.getenv("SHOP_BASE_BRIX", "1.0"))
SHOP_MIN_BRIX = int(os.getenv("SHOP_MIN_BRIX", "5"))
SHOP_MAX_BRIX = int(os.getenv("SHOP_MAX_BRIX", "5000"))
SHOP_OFFER_TTL_SECONDS = int(os.getenv("SHOP_OFFER_TTL_SECONDS", "900"))
```

In `lfg_core/memos.py`, add after `ACTION_DEPOSIT`:

```python
ACTION_SHOP_BUY = "shop-buy"
```

and add `ACTION_SHOP_BUY` to the `_ACTIONS` frozenset.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_shop_config.py -v` — Expected: PASS.
Also run: `.venv/bin/pytest tests/ -k "taxon or memo" -q` and fix any test that asserted the old `1763` default (update the expectation, it's a deliberate flip).

- [ ] **Step 5: Commit**

```bash
git add lfg_core/config.py lfg_core/memos.py tests/test_shop_config.py
git commit -m "feat(shop): config values, taxon defaults (176/1760), shop-buy memo action (#217)"
```

---

### Task 2: `trait_rarity.shop_count` column

**Files:**
- Modify: `lfg_core/rarity.py` (`_SCHEMA` + `ensure_schema` at line ~57; `recalculate_rarity` at line ~218)
- Test: `tests/test_shop_rarity_count.py`

**Interfaces:**
- Produces: column `trait_rarity.shop_count INTEGER NOT NULL DEFAULT 0`, self-migrated by `rarity.ensure_schema`; `rarity.increment_shop_count(conn, network: str, slot: str, value: str) -> None` (increments across ALL body rows for that (category, trait); inserts a `BODY_SENTINEL` row if none exist). `recalculate_rarity` preserves `shop_count`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_shop_rarity_count.py — env-guard preamble as in Task 1, then:
import sqlite3
from lfg_core import rarity


def _conn():
    conn = sqlite3.connect(":memory:")
    rarity.ensure_schema(conn)
    return conn


def test_shop_count_column_migrates():
    conn = _conn()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(trait_rarity)")}
    assert "shop_count" in cols


def test_increment_and_recalc_preserves():
    conn = _conn()
    conn.execute(
        "INSERT INTO trait_rarity (network, body, category, trait, live_count, floor_weight)"
        " VALUES ('testnet', 'male', 'Head', 'Wizard Hat', 3, 0.005)"
    )
    rarity.increment_shop_count(conn, "testnet", "Head", "Wizard Hat")
    (n,) = conn.execute(
        "SELECT shop_count FROM trait_rarity WHERE trait='Wizard Hat'"
    ).fetchone()
    assert n == 1
    rarity.recalculate_rarity(conn, "testnet")  # zeroes+recounts live_count only
    (n,) = conn.execute(
        "SELECT shop_count FROM trait_rarity WHERE trait='Wizard Hat'"
    ).fetchone()
    assert n == 1


def test_increment_inserts_sentinel_row_when_absent():
    conn = _conn()
    rarity.increment_shop_count(conn, "testnet", "Eyes", "Laser")
    row = conn.execute(
        "SELECT body, shop_count FROM trait_rarity WHERE category='Eyes' AND trait='Laser'"
    ).fetchone()
    assert row == (rarity.BODY_SENTINEL, 1)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_shop_rarity_count.py -v` — Expected: FAIL (no column / no function).

- [ ] **Step 3: Implement**

In `ensure_schema`, after the existing LFG column migrations, add the same self-migration pattern:

```python
    rarity_cols = {r[1] for r in conn.execute("PRAGMA table_info(trait_rarity)")}
    if "shop_count" not in rarity_cols:
        conn.execute("ALTER TABLE trait_rarity ADD COLUMN shop_count INTEGER NOT NULL DEFAULT 0")
```

(also add `shop_count INTEGER NOT NULL DEFAULT 0` to `_SCHEMA` for fresh DBs). Then add:

```python
def increment_shop_count(conn: sqlite3.Connection, network: str, slot: str, value: str) -> None:
    """Count one settled shop purchase for (slot, value). Trait tokens are
    body-agnostic, so bump every body row for the trait; if the trait has no
    rows yet, insert one under BODY_SENTINEL so the count is never lost.
    Feeds shop pricing only — mint odds never read shop_count."""
    ensure_schema(conn)
    cur = conn.execute(
        "UPDATE trait_rarity SET shop_count = shop_count + 1"
        " WHERE network=? AND category=? AND trait=?",
        (network, slot, value),
    )
    if cur.rowcount == 0:
        conn.execute(
            "INSERT INTO trait_rarity (network, body, category, trait, live_count,"
            " floor_weight, shop_count) VALUES (?, ?, ?, ?, 0, ?, 1)",
            (network, BODY_SENTINEL, slot, value, config.RARITY_FLOOR),
        )
    conn.commit()
```

`recalculate_rarity` already only writes `live_count` on conflict — verify its `DO UPDATE SET live_count=excluded.live_count` clause is unchanged (that is the preservation contract; the test proves it).

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest tests/test_shop_rarity_count.py tests/test_rarity*.py -v` — Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add lfg_core/rarity.py tests/test_shop_rarity_count.py
git commit -m "feat(shop): trait_rarity.shop_count column + increment helper (#217)"
```

---

### Task 3: `lfg_core/shop.py` — pricing, overrides, catalog

**Files:**
- Create: `lfg_core/shop.py`
- Test: `tests/test_shop_pricing.py`

**Interfaces:**
- Consumes: `rarity.connect`, `rarity.ensure_schema`, `config.SHOP_*` (Task 1), `trait_rarity.shop_count` (Task 2).
- Produces (all sync, app-DB `sqlite3.Connection` first arg, network explicit):
  - `ensure_schema(conn) -> None` — creates `shop_overrides` (spec DDL: PK `(network, slot, value)`, cols `excluded INTEGER NOT NULL DEFAULT 0`, `price_override INTEGER`, `updated_at`).
  - `derived_price(live_total: int, category_total: int, shop_count: int, population_size: int) -> int` — pure formula.
  - `quote(conn, network: str, slot: str, value: str) -> int | None` — live price for one trait; `None` if rarity-disabled everywhere, unknown, or excluded; `price_override` wins.
  - `set_override(conn, network, slot, value, *, excluded: bool | None = None, price_override: int | None = ..., ) -> None` and `get_overrides(conn, network) -> dict[tuple[str, str], dict]`.
  - `catalog(conn, network: str) -> list[dict]` — every enabled, non-excluded (slot, value) with `{"slot", "value", "price_brix"}`, aggregated across bodies.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_shop_pricing.py — env-guard preamble, then:
import sqlite3
from lfg_core import config, rarity, shop


def _conn():
    conn = sqlite3.connect(":memory:")
    rarity.ensure_schema(conn)
    shop.ensure_schema(conn)
    return conn


def _seed(conn, body, cat, trait, live, enabled=1, shop_count=0):
    conn.execute(
        "INSERT INTO trait_rarity (network, body, category, trait, live_count,"
        " floor_weight, enabled, shop_count) VALUES ('testnet',?,?,?,?,0.005,?,?)",
        (body, cat, trait, live, enabled, shop_count),
    )


def test_derived_price_formula():
    # share = (10+0+1)/(100+20) = 11/120; price = round(1.0/share) = 11
    assert shop.derived_price(10, 100, 0, 20) == 11


def test_derived_price_clamps():
    assert shop.derived_price(0, 10_000, 0, 2) == config.SHOP_MAX_BRIX  # ultra-rare capped
    assert shop.derived_price(99, 100, 0, 1) == config.SHOP_MIN_BRIX   # ultra-common floored


def test_quote_aggregates_bodies_and_counts_shop():
    conn = _conn()
    _seed(conn, "male", "Head", "Wizard Hat", 4)
    _seed(conn, "female", "Head", "Wizard Hat", 6, shop_count=2)
    _seed(conn, "male", "Head", "Cap", 90)
    # live_total=10 (+shop 2), category_total=100, population=3 rows
    # share=(10+2+1)/(100+3)=13/103 → price=round(1.0*103/13)=8
    assert shop.quote(conn, "testnet", "Head", "Wizard Hat") == 8


def test_quote_none_when_disabled_or_unknown():
    conn = _conn()
    _seed(conn, "male", "Head", "Halo", 1, enabled=0)
    assert shop.quote(conn, "testnet", "Head", "Halo") is None
    assert shop.quote(conn, "testnet", "Head", "Nope") is None


def test_override_precedence_and_exclusion():
    conn = _conn()
    _seed(conn, "male", "Head", "Wizard Hat", 4)
    shop.set_override(conn, "testnet", "Head", "Wizard Hat", price_override=777)
    assert shop.quote(conn, "testnet", "Head", "Wizard Hat") == 777
    shop.set_override(conn, "testnet", "Head", "Wizard Hat", excluded=True)
    assert shop.quote(conn, "testnet", "Head", "Wizard Hat") is None


def test_catalog_lists_enabled_non_excluded():
    conn = _conn()
    _seed(conn, "male", "Head", "Wizard Hat", 4)
    _seed(conn, "male", "Head", "Halo", 1, enabled=0)
    rows = shop.catalog(conn, "testnet")
    assert [r["value"] for r in rows] == ["Wizard Hat"]
    assert rows[0]["slot"] == "Head" and rows[0]["price_brix"] > 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_shop_pricing.py -v` — Expected: FAIL (`ModuleNotFoundError: lfg_core.shop`).

- [ ] **Step 3: Implement `lfg_core/shop.py`**

```python
"""Trait Shop pricing + overrides + derived catalog (#217).

The catalog is DERIVED: every rarity-enabled (slot, value), aggregated across
bodies (trait tokens are body-agnostic), minus shop_overrides exclusions.
Price uses the same Laplace smoothing as rarity.effective_weight:
    share = (Σ live_count + shop_count + 1) / (Σ category_total + population)
    price = clamp(round(SHOP_BASE_BRIX / share), SHOP_MIN_BRIX, SHOP_MAX_BRIX)
Lives in the app DB next to trait_rarity (same network-column pattern).
Body Type is not sellable: rows under rarity.BODY_CATEGORY are never cataloged.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from . import config
from .rarity import BODY_CATEGORY

_UNSET: Any = object()

_SCHEMA = """CREATE TABLE IF NOT EXISTS shop_overrides (
    network        TEXT NOT NULL,
    slot           TEXT NOT NULL,
    value          TEXT NOT NULL,
    excluded       INTEGER NOT NULL DEFAULT 0,
    price_override INTEGER,
    updated_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (network, slot, value)
)"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(_SCHEMA)
    conn.commit()


def derived_price(live_total: int, category_total: int, shop_count: int, population_size: int) -> int:
    share = (live_total + shop_count + 1) / (category_total + population_size)
    price = round(config.SHOP_BASE_BRIX / share)
    return max(config.SHOP_MIN_BRIX, min(config.SHOP_MAX_BRIX, price))


def _rarity_aggregate(
    conn: sqlite3.Connection, network: str, slot: str, value: str
) -> tuple[int, int, int, int, bool] | None:
    """(live_total, category_total, shop_count, population, any_enabled) for a
    trait aggregated across bodies; None if the trait has no rows."""
    row = conn.execute(
        "SELECT SUM(live_count), MAX(shop_count), MAX(enabled) FROM trait_rarity"
        " WHERE network=? AND category=? AND trait=?",
        (network, slot, value),
    ).fetchone()
    if row is None or row[2] is None:
        return None
    cat = conn.execute(
        "SELECT SUM(live_count), COUNT(*) FROM trait_rarity WHERE network=? AND category=?",
        (network, slot),
    ).fetchone()
    return (row[0] or 0, cat[0] or 0, row[1] or 0, cat[1] or 0, bool(row[2]))


def quote(conn: sqlite3.Connection, network: str, slot: str, value: str) -> int | None:
    ensure_schema(conn)
    ov = conn.execute(
        "SELECT excluded, price_override FROM shop_overrides"
        " WHERE network=? AND slot=? AND value=?",
        (network, slot, value),
    ).fetchone()
    if ov and ov[0]:
        return None
    agg = _rarity_aggregate(conn, network, slot, value)
    if agg is None or not agg[4] or slot == BODY_CATEGORY:
        return None
    if ov and ov[1] is not None:
        return int(ov[1])
    live_total, category_total, shop_count, population, _ = agg
    return derived_price(live_total, category_total, shop_count, population)


def set_override(
    conn: sqlite3.Connection,
    network: str,
    slot: str,
    value: str,
    *,
    excluded: bool | None = None,
    price_override: int | None = _UNSET,
) -> None:
    """Upsert one override; unspecified fields keep their stored value."""
    ensure_schema(conn)
    conn.execute(
        "INSERT INTO shop_overrides (network, slot, value) VALUES (?,?,?)"
        " ON CONFLICT(network, slot, value) DO NOTHING",
        (network, slot, value),
    )
    if excluded is not None:
        conn.execute(
            "UPDATE shop_overrides SET excluded=?, updated_at=CURRENT_TIMESTAMP"
            " WHERE network=? AND slot=? AND value=?",
            (1 if excluded else 0, network, slot, value),
        )
    if price_override is not _UNSET:
        conn.execute(
            "UPDATE shop_overrides SET price_override=?, updated_at=CURRENT_TIMESTAMP"
            " WHERE network=? AND slot=? AND value=?",
            (price_override, network, slot, value),
        )
    conn.commit()


def get_overrides(conn: sqlite3.Connection, network: str) -> dict[tuple[str, str], dict[str, Any]]:
    ensure_schema(conn)
    return {
        (r[0], r[1]): {"excluded": bool(r[2]), "price_override": r[3]}
        for r in conn.execute(
            "SELECT slot, value, excluded, price_override FROM shop_overrides WHERE network=?",
            (network,),
        )
    }


def catalog(conn: sqlite3.Connection, network: str) -> list[dict[str, Any]]:
    ensure_schema(conn)
    out: list[dict[str, Any]] = []
    pairs = conn.execute(
        "SELECT DISTINCT category, trait FROM trait_rarity"
        " WHERE network=? AND enabled=1 AND category != ?"
        " ORDER BY category, trait",
        (network, BODY_CATEGORY),
    ).fetchall()
    for slot, value in pairs:
        price = quote(conn, network, slot, value)
        if price is not None:
            out.append({"slot": slot, "value": value, "price_brix": price})
    return out
```

Note: check `trait_rarity`'s enabled column name in `rarity._SCHEMA` before coding (it may be `enabled` or `status`-flavored) and match it; adjust the test seeds identically.

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest tests/test_shop_pricing.py -v` — Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add lfg_core/shop.py tests/test_shop_pricing.py
git commit -m "feat(shop): pricing formula, shop_overrides store, derived catalog (#217)"
```

---

### Task 4: `shop_orders` store

**Files:**
- Create: `lfg_core/shop_store.py`
- Test: `tests/test_shop_store.py`

**Interfaces:**
- Consumes: nothing new (plain sqlite over the economy-network `onchain_<net>.db` connection the caller passes).
- Produces:
  - `ensure_schema(conn) -> None` — spec DDL (`shop_orders`, PK `session_id`, status ∈ `pending_mint|pending_accept|accepted|settled|expired|failed`).
  - `create_order(conn, session_id, buyer, slot, value, price_brix, now_ts: int) -> None` (status `pending_mint`).
  - `update_order(conn, session_id, *, status=None, nft_id=None, offer_index=None, now_ts: int) -> None`.
  - `get_order(conn, session_id) -> dict | None`.
  - `orders_pending_expiry(conn, older_than_ts: int) -> list[dict]` — `pending_accept` rows created before the cutoff.
  - `orders_unsettled(conn) -> list[dict]` — status `accepted`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_shop_store.py — env-guard preamble, then:
import sqlite3
from lfg_core import shop_store


def _conn():
    conn = sqlite3.connect(":memory:")
    shop_store.ensure_schema(conn)
    return conn


def test_order_lifecycle():
    conn = _conn()
    shop_store.create_order(conn, "s1", "rBuyer", "Head", "Wizard Hat", 42, now_ts=1000)
    o = shop_store.get_order(conn, "s1")
    assert o["status"] == "pending_mint" and o["price_brix"] == 42
    shop_store.update_order(conn, "s1", status="pending_accept", nft_id="ABC",
                            offer_index="OFF1", now_ts=1001)
    o = shop_store.get_order(conn, "s1")
    assert (o["status"], o["nft_id"], o["offer_index"]) == ("pending_accept", "ABC", "OFF1")


def test_expiry_and_unsettled_queries():
    conn = _conn()
    shop_store.create_order(conn, "old", "rA", "Eyes", "Laser", 10, now_ts=100)
    shop_store.update_order(conn, "old", status="pending_accept", now_ts=100)
    shop_store.create_order(conn, "new", "rB", "Eyes", "Laser", 10, now_ts=5000)
    shop_store.update_order(conn, "new", status="pending_accept", now_ts=5000)
    shop_store.create_order(conn, "done", "rC", "Eyes", "Laser", 10, now_ts=100)
    shop_store.update_order(conn, "done", status="accepted", now_ts=200)
    assert [o["session_id"] for o in shop_store.orders_pending_expiry(conn, older_than_ts=1000)] == ["old"]
    assert [o["session_id"] for o in shop_store.orders_unsettled(conn)] == ["done"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_shop_store.py -v` — Expected: FAIL (no module).

- [ ] **Step 3: Implement `lfg_core/shop_store.py`**

```python
"""shop_orders store (#217) — one row per Trait Shop purchase session.

Lives in the economy-network onchain_<net>.db beside trait_tokens (the caller
resolves the connection via config.ECONOMY_NETWORK). Derived-but-authoritative
for order lifecycle; the ledger is authoritative for token/offer existence.
"""

from __future__ import annotations

import sqlite3
from typing import Any

_SCHEMA = """CREATE TABLE IF NOT EXISTS shop_orders (
    session_id   TEXT PRIMARY KEY,
    buyer        TEXT NOT NULL,
    slot         TEXT NOT NULL,
    value        TEXT NOT NULL,
    price_brix   INTEGER NOT NULL,
    nft_id       TEXT,
    offer_index  TEXT,
    status       TEXT NOT NULL,
    created_ts   INTEGER NOT NULL,
    updated_ts   INTEGER NOT NULL
)"""

_COLS = (
    "session_id", "buyer", "slot", "value", "price_brix",
    "nft_id", "offer_index", "status", "created_ts", "updated_ts",
)

VALID_STATUSES = frozenset(
    {"pending_mint", "pending_accept", "accepted", "settled", "expired", "failed"}
)


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(_SCHEMA)
    conn.commit()


def create_order(
    conn: sqlite3.Connection, session_id: str, buyer: str, slot: str,
    value: str, price_brix: int, now_ts: int,
) -> None:
    ensure_schema(conn)
    conn.execute(
        "INSERT INTO shop_orders (session_id, buyer, slot, value, price_brix,"
        " status, created_ts, updated_ts) VALUES (?,?,?,?,?,'pending_mint',?,?)",
        (session_id, buyer, slot, value, price_brix, now_ts, now_ts),
    )
    conn.commit()


def update_order(
    conn: sqlite3.Connection, session_id: str, *, now_ts: int,
    status: str | None = None, nft_id: str | None = None, offer_index: str | None = None,
) -> None:
    if status is not None and status not in VALID_STATUSES:
        raise ValueError(f"unknown shop order status: {status}")
    sets, params = ["updated_ts=?"], [now_ts]
    for col, val in (("status", status), ("nft_id", nft_id), ("offer_index", offer_index)):
        if val is not None:
            sets.append(f"{col}=?")
            params.append(val)
    conn.execute(f"UPDATE shop_orders SET {', '.join(sets)} WHERE session_id=?",
                 (*params, session_id))
    conn.commit()


def _rows(cur: sqlite3.Cursor) -> list[dict[str, Any]]:
    return [dict(zip(_COLS, r)) for r in cur.fetchall()]


def get_order(conn: sqlite3.Connection, session_id: str) -> dict[str, Any] | None:
    ensure_schema(conn)
    rows = _rows(conn.execute(
        f"SELECT {', '.join(_COLS)} FROM shop_orders WHERE session_id=?", (session_id,)))
    return rows[0] if rows else None


def orders_pending_expiry(conn: sqlite3.Connection, older_than_ts: int) -> list[dict[str, Any]]:
    ensure_schema(conn)
    return _rows(conn.execute(
        f"SELECT {', '.join(_COLS)} FROM shop_orders"
        " WHERE status='pending_accept' AND created_ts < ? ORDER BY created_ts",
        (older_than_ts,)))


def orders_unsettled(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    ensure_schema(conn)
    return _rows(conn.execute(
        f"SELECT {', '.join(_COLS)} FROM shop_orders"
        " WHERE status='accepted' ORDER BY created_ts"))
```

- [ ] **Step 4: Run tests** — `.venv/bin/pytest tests/test_shop_store.py -v` — PASS.

- [ ] **Step 5: Commit**

```bash
git add lfg_core/shop_store.py tests/test_shop_store.py
git commit -m "feat(shop): shop_orders store (#217)"
```

---

### Task 5: BRIX offer support in `xrpl_ops.create_nft_offer` (expiration)

**Files:**
- Modify: `lfg_core/xrpl_ops.py:209` (`create_nft_offer`)
- Test: `tests/test_shop_offer_builder.py`

**Interfaces:**
- Produces: `create_nft_offer(nft_id, destination, amount="0", platform=..., campaign=None, expiration: int | None = None, action: str = memos.ACTION_CREATE_OFFER) -> str | None` — `expiration` is a **ripple-epoch** timestamp set on the `NFTokenCreateOffer`; `amount` already accepts an `IssuedCurrencyAmount` (unchanged). `action` lets the shop stamp `shop-buy` provenance.
- Also produces helper: `brix_amount(value: int) -> IssuedCurrencyAmount` in `lfg_core/shop_flow.py` (Task 6 consumes; defined there, listed here for context only).

- [ ] **Step 1: Write the failing test**

Test by monkeypatching the submit path (follow the existing pattern in `tests/` for `xrpl_ops` tests — grep `submit_and_wait` in tests for the fixture style):

```python
# tests/test_shop_offer_builder.py — env-guard preamble, then:
import pytest
from lfg_core import xrpl_ops, memos


@pytest.mark.asyncio
async def test_create_nft_offer_carries_expiration_and_issued_amount(monkeypatch):
    captured = {}

    async def fake_submit(tx, client, wallet):  # match the real call signature used in create_nft_offer
        captured["tx"] = tx
        class R:  # minimal result shim: is_successful + offer index in meta
            result = {"meta": {"offer_id": "OFF"}, "validated": True}
            def is_successful(self):
                return True
        return R()

    monkeypatch.setattr(xrpl_ops, "submit_and_wait", fake_submit)
    amount = {"currency": "BRX", "issuer": "rIssuer", "value": "42"}
    await xrpl_ops.create_nft_offer(
        "F" * 64, "rBuyer", amount=amount, expiration=772000000,
        action=memos.ACTION_SHOP_BUY,
    )
    tx = captured["tx"]
    assert tx.expiration == 772000000
    assert tx.amount == amount or tx.amount.value == "42"
```

Before writing this, read `lfg_core/xrpl_ops.py:209-260` and the existing offer tests to mirror the actual submit call and result parsing — adjust the fake accordingly. The assertions that matter: `expiration` lands on the tx, issued-currency `amount` passes through, memo action is `shop-buy`.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_shop_offer_builder.py -v` — Expected: FAIL (`unexpected keyword argument 'expiration'`).

- [ ] **Step 3: Implement**

Add the two keyword params to `create_nft_offer` and thread them:

```python
async def create_nft_offer(
    nft_id: str,
    destination: str,
    amount: Any = "0",
    platform: str = memos.PLATFORM_BACKEND,
    campaign: str | None = None,
    expiration: int | None = None,
    action: str = memos.ACTION_CREATE_OFFER,
) -> str | None:
    ...
        offer = NFTokenCreateOffer(
            account=config.SIGNING_ACCOUNT,
            destination=destination,
            amount=amount,
            nftoken_id=nft_id,
            flags=NFTokenCreateOfferFlag.TF_SELL_NFTOKEN,
            expiration=expiration,
            source_tag=config.SOURCE_TAG,
            memos=memos.build_memo_models(
                memos.INITIATOR_BACKEND, platform, action, campaign
            ),
        )
```

(`NFTokenCreateOffer.expiration=None` is simply omitted by xrpl-py serialization — no behavior change for existing callers.)

- [ ] **Step 4: Run tests** — `.venv/bin/pytest tests/test_shop_offer_builder.py tests/ -k xrpl_ops -q` — PASS (existing offer tests unaffected).

- [ ] **Step 5: Commit**

```bash
git add lfg_core/xrpl_ops.py tests/test_shop_offer_builder.py
git commit -m "feat(shop): create_nft_offer expiration + action params (#217)"
```

---

### Task 6: `ShopBuySession` flow (`lfg_core/shop_flow.py`)

**Files:**
- Create: `lfg_core/shop_flow.py`
- Test: `tests/test_shop_flow.py`

**Interfaces:**
- Consumes: `shop.quote` (T3), `shop_store` (T4), `xrpl_ops.create_nft_offer(expiration=…, action=…)` (T5), `rarity.increment_shop_count` (T2), `economy_store.record_supply_change(conn, kind, edition, body_value, body_class, trait_deltas, actor, reason)`, `economy_flow.DepositSession`/`run_deposit`, `EconomyDeps` (trait mint fns), `memos.ACTION_SHOP_BUY`, `config.SHOP_OFFER_TTL_SECONDS`, `config.TRAIT_TAXON`.
- Produces:
  - `@dataclass class ShopBuySession` — fields `buyer: str`, `slot: str`, `value: str`, `price_brix: int`, `state: str` (`running|awaiting_accept|done|failed`), `error: str | None`, `nft_id: str | None`, `offer_index: str | None`, `accept: dict | None` (XUMM payload: `qr_url`/`deep_link`/`uuid`/`pushed`), `push_user_token: str | None`, `platform: str`, `id: str` (uuid hex), `to_dict() -> dict`.
  - `async def start_shop_buy(session, deps: ShopDeps) -> None` — quote-frozen price is already on the session; mints (taxon `config.TRAIT_TAXON`, flags `config.TRAIT_NFT_FLAGS`, action `shop-buy`), writes the `supply_changes` growth row (`kind="mint"`, `edition=None`, `body_value=""`, `body_class=""`, `trait_deltas={f"{slot}|{value}": 1}`, `actor="shop"`, `reason=f"shop purchase {session.id}"`), creates the BRIX destination-locked offer with `expiration = now_ripple + SHOP_OFFER_TTL_SECONDS`, builds the XUMM accept payload, records the order `pending_accept`. On offer failure after a successful mint: issuer-burn the token + `record_supply_change(kind="burn", …, trait_deltas={f"{slot}|{value}": -1}, reason=f"shop revert {session.id}")`, order → `failed`.
  - `async def advance_shop_buy(session, deps) -> None` — polls XUMM payload status; on signed: verify signer == session.buyer (else fail `signer_mismatch`, order stays `pending_accept` for the expiry sweep), order → `accepted`, then settle: `run_deposit` into buyer's Closet; on settle success order → `settled` + `rarity.increment_shop_count`; on settle failure leave `accepted` (sweep retries).
  - `@dataclass class ShopDeps` — `conn` (economy onchain DB), `app_conn_factory: Callable[[], sqlite3.Connection]` (app DB for rarity/shop), `economy_deps: EconomyDeps`, `mint_fn`, `offer_fn`, `burn_fn`, `payload_status_fn`, `accept_payload_fn`, `now_ts_fn: Callable[[], int]`, `network: str`.
  - `RIPPLE_EPOCH_OFFSET = 946684800` and `def ripple_expiration(now_unix: int, ttl: int) -> int`.

- [ ] **Step 1: Write the failing tests** — all-fake deps, no network. Cover:

```python
# tests/test_shop_flow.py — env-guard preamble; build a _deps() helper with
# in-memory conns (shop_store.ensure_schema / rarity+shop ensure_schema),
# fake async mint/offer/burn/payload fns recording calls, and a fake
# economy run_deposit hook. Tests:

# 1. happy path: start → pending_accept order with nft_id/offer_index and
#    accept payload; advance(signed by buyer) → deposit called with
#    (buyer, nft_id) → order settled → shop_count incremented → session done.
# 2. mint fails → session failed, order failed, NO supply_changes row.
# 3. offer fails after mint → burn_fn called with the minted nft_id, TWO
#    supply_changes rows (mint then burn revert, net zero), order failed.
# 4. signer mismatch: advance with a different signer account → session
#    failed "signer_mismatch", order still pending_accept, deposit NOT called.
# 5. settle failure: deposit raises → order stays "accepted", session state
#    "failed" is NOT set (session reports awaiting settlement; sweep owns retry)
#    and shop_count NOT incremented.
# 6. ripple_expiration(1_752_000_000, 900) == 1_752_000_900 - 946_684_800.
```

Write these as real test functions with real asserts (follow `tests/test_market_flow.py` fake-deps style — it fakes XUMM status and tx fetch the same way).

- [ ] **Step 2: Run to verify failure** — `.venv/bin/pytest tests/test_shop_flow.py -v` — FAIL (no module).

- [ ] **Step 3: Implement `lfg_core/shop_flow.py`**

Structure (write complete, following `market_flow.py` conventions — `to_dict`, in-memory session registry owned by the service layer, not this module):

```python
"""Trait Shop buy flow (#217): quote-frozen BRIX purchase of an on-demand
minted trait token, settled into the buyer's Closet.

Order of operations (fail-safe, mirrors economy_flow conventions):
  precheck (service layer: economy enabled, active Closet, quote) ->
  mint trait token (reversible: revert = issuer burn + supply reversal) ->
  supply_changes growth row (shop mints are NOT supply-neutral) ->
  BRIX destination-locked sell offer with on-ledger Expiration ->
  XUMM accept (signer must match buyer) ->
  settle via run_deposit (burn back into Closet) ->
  shop_count increment (pricing feedback).
The expiry/settlement sweep in lfg_service owns retry of anything that
stalls after "accepted"; this module never blind-retries on-chain writes.
"""
```

Key code points (implement fully):

```python
RIPPLE_EPOCH_OFFSET = 946_684_800


def ripple_expiration(now_unix: int, ttl: int) -> int:
    return now_unix + ttl - RIPPLE_EPOCH_OFFSET


def brix_amount(value: int) -> dict[str, str]:
    return {
        "currency": config.TOKEN_CURRENCY_HEX,
        "issuer": config.TOKEN_ISSUER_ADDRESS,
        "value": str(value),
    }
```

`start_shop_buy`: compose+upload trait art via `economy_deps.trait_compose_fn`/`trait_upload_fn` (exactly as `run_extract` does — read `economy_flow.run_extract` lines 751+ and reuse its metadata shape), then `deps.mint_fn(url, config.TRAIT_TAXON, flags=config.TRAIT_NFT_FLAGS, action=memos.ACTION_SHOP_BUY, platform=memos.platform_for_surface(session.platform))`, `record_supply_change(...)`, `deps.offer_fn(nft_id, session.buyer, amount=brix_amount(session.price_brix), expiration=ripple_expiration(deps.now_ts_fn(), config.SHOP_OFFER_TTL_SECONDS), action=memos.ACTION_SHOP_BUY)`, then `deps.accept_payload_fn(offer_index, user_token=...)` → store payload dict on `session.accept`; `shop_store.update_order(..., status="pending_accept", nft_id=..., offer_index=...)`; `session.state = "awaiting_accept"`.

`advance_shop_buy`: `status = await deps.payload_status_fn(session.accept["uuid"])`; if signed → extract signer account; mismatch → `session.fail("signer_mismatch")` (order untouched); match → order `accepted`; then `dep = DepositSession(owner=session.buyer, nft_id=session.nft_id)` + `await run_deposit(dep, deps.economy_deps)`; success → order `settled`, `rarity.increment_shop_count(app_conn, deps.network, session.slot, session.value)`, `session.state = "done"`; deposit failure → leave order `accepted`, set `session.state = "settling"` (poll again / sweep).

- [ ] **Step 4: Run tests** — `.venv/bin/pytest tests/test_shop_flow.py -v` — PASS.

- [ ] **Step 5: Commit**

```bash
git add lfg_core/shop_flow.py tests/test_shop_flow.py
git commit -m "feat(shop): ShopBuySession flow — mint, BRIX offer, settle, feedback (#217)"
```

---

### Task 7: Sweep — expiry burn/reversal + settlement retry

**Files:**
- Modify: `lfg_service/app.py` (sweep loop at ~line 1673–1730, next to `settle_pending_trait_sales`)
- Test: `tests/test_shop_sweep.py`

**Interfaces:**
- Consumes: `shop_store.orders_pending_expiry` / `orders_unsettled` (T4), `shop_flow` settle pieces (T6), `xrpl_ops.burn_nft`, `xrpl_ops.cancel_nft_offer`, `record_supply_change`.
- Produces: `async def sweep_shop_orders() -> None` in `lfg_service/app.py`, called from the existing sweep loop right after `settle_pending_trait_sales()`; module counters `_SHOP_SWEEP_MAX_ATTEMPTS = 5` and an in-memory `_shop_settle_attempts: dict[str, int]`.

Behavior to implement and test (write real tests with faked xrpl/deposit fns, in-memory DBs):
1. **Expiry pass:** for each `pending_accept` order older than `SHOP_OFFER_TTL_SECONDS`: cancel the offer (ignore "already gone" errors — expiration made it unacceptable anyway; a cancel of an expired offer still purges the ledger object), issuer-burn the orphaned token, write the `supply_changes` reversal row (`kind="burn"`, `trait_deltas={f"{slot}|{value}": -1}`, `actor="shop"`, `reason=f"shop expiry {session_id}"`), close the order `expired`. If the accept actually landed just before the sweep (burn fails with owner-mismatch / token-not-ours), do NOT burn-revert — mark the order `accepted` so the settlement pass picks it up (fail-closed: never burn a token the buyer paid for).
2. **Settlement pass:** for each `accepted` order, retry the `run_deposit` settle + `increment_shop_count`; on success → `settled`. After `_SHOP_SWEEP_MAX_ATTEMPTS` failures, journal `shop-settlement-giveup-<session_id>.json` to `config.ECONOMY_RECORDS_DIR` (same shape as the trait-sale giveup records) and close the order `failed` — the token sits in the buyer's wallet for a manual Deposit; it is never lost.

Steps: failing tests (expiry closes+burns+reverses; landed-accept is rescued not burned; settle retry increments and settles; giveup journals after max attempts) → run FAIL → implement → run PASS → commit `feat(shop): sweep — offer expiry burn/reversal + settlement retry (#217)`.

---

### Task 8: Service endpoints

**Files:**
- Modify: `lfg_service/app.py` (routes near the market handlers, ~line 845+; session registries near the market session dicts)
- Test: `tests/test_shop_endpoints.py` (follow the existing `lfg_service` endpoint-test harness — grep `aiohttp_client` fixtures in tests for market endpoints)

**Interfaces:**
- Consumes: `shop.catalog`/`shop.quote` (T3), `shop_flow` (T6), existing `require_wallet` auth decorator, `_push_token` resolver, `ensure_closet` active check (grep `closet_required` in `app.py` for the exact helper the market buy path uses), `config.ECONOMY_ENABLED`, `config.ECONOMY_NETWORK`.
- Produces:
  - `GET /api/shop/catalog` — public. 200 `{"items": [{slot, value, price_brix, image_url}]}`; empty list when `ECONOMY_ENABLED` is off. Cached 60s per network with the `_MARKET_CACHE`-style generation pattern (add key kind `"shop"` or a parallel `_SHOP_CACHE` with the same lock/TTL discipline — reuse, don't reinvent). `image_url` uses the same trait-art URL helper the Activity dressing-room uses (grep how `closet_assets` art URLs are served).
  - `POST /api/shop/buy` — authed. Body `{"slot": str, "value": str}`. Fail-closed order: 403 `economy_disabled` → 400 malformed → 404 `unknown_trait` / 403 `not_purchasable` (excluded/disabled) → 403 `closet_required` → create `ShopBuySession` (price frozen from `shop.quote` NOW), `shop_store.create_order`, launch `start_shop_buy`, 200 `{"session_id", "price_brix", "accept": {qr/deep-link/pushed}}`.
  - `GET /api/shop/buy/{session_id}` — authed, session must belong to caller (404 otherwise, matching market session-status semantics); drives `advance_shop_buy`; returns `session.to_dict()`.

Steps: failing endpoint tests (catalog shape + gating; buy happy-path 200 with session; each 4xx; status poll advances; foreign session 404) → FAIL → implement (register routes where the market routes are registered; keep handlers thin — all logic in `shop_flow`) → PASS → commit `feat(shop): /api/shop catalog + buy endpoints (#217)`.

---

### Task 9: Taxon 1760 for Assemble + listener/backfill recognition of 176/1760

**Files:**
- Modify: `scripts/_economy_deps.py:167` (`char_mint_fn` — Assemble's mint site; verify whether Assemble and other char mints share this fn: read `economy_flow.run_assemble` line 421+ first. If shared, add a dedicated `assemble_mint_fn` or pass taxon through, so ONLY Assemble gets 1760)
- Modify: `lfg_core/nft_listener.py` (~line 239 taxon dispatch — confirm `TRAIT_TAXON` matching picks up the flipped default; ensure taxon-1760 tokens flow into the character (`onchain_nfts`) path, not ignored)
- Modify: `scripts/backfill_economy.py` and `scripts/backfill_onchain.py` (taxon enumeration must include `ASSEMBLE_TAXON` for characters)
- Test: `tests/test_taxon_realignment.py`

**Interfaces:**
- Consumes: `config.ASSEMBLE_TAXON`, `config.TRAIT_TAXON` (T1).
- Produces: Assemble mints at taxon 1760; listener classifies taxon-1760 mints as collection characters and taxon-176 mints as trait tokens; backfills enumerate both.

Tests (real, with the listener's existing test harness — grep `apply_tx` tests):
1. A fake `NFTokenMint` tx with taxon 176 from our issuer → `trait_tokens` row upserted.
2. Taxon 1760 mint from our issuer → treated as a character (`onchain_nfts` row), not a trait/closet.
3. `run_assemble` with faked deps mints with `taxon == config.ASSEMBLE_TAXON` (assert on the captured mint call).
4. Character-membership audit: grep the codebase for any `taxon == 0` / `NFT_TAXON` equality check on the read path (`grep -rn "NFT_TAXON" lfg_core/ scripts/ lfg_service/`) — for each hit, decide include-1760 or leave (membership is issuer+membership-based per spec; the plan step is: list hits in the commit message and fix any that would exclude 1760 characters).

Steps: failing tests → FAIL → implement → PASS → commit `feat(shop): taxon realignment — assemble mints 1760, listener/backfill recognize 176/1760 (#217)`.

---

### Task 10: Rarity dashboard shop panel

**Files:**
- Modify: `scripts/trait_dashboard.py` (grid/list cells + two endpoints, following its existing endpoint/audit-log pattern)
- Test: `tests/test_trait_dashboard.py` (extend the existing dashboard test file)

**Interfaces:**
- Consumes: `shop.quote`/`set_override`/`get_overrides` (T3).
- Produces: each trait card/row shows derived shop price + override state; `POST /api/shop/override {slot, value, excluded?, price_override?}` (price_override `null` clears) writing through `shop.set_override`; `GET /api/shop/overrides`. Every mutation appends to `reports/trait_dashboard_audit.log` with actor/field/old→new, exactly like rarity mutations. Input validation mirrors the boost endpoints (reject negative/absurd prices, unknown traits 404).

Steps: failing tests (override roundtrip via HTTP, audit line appended, validation 400s, price shown in the traits payload) → FAIL → implement → PASS → commit `feat(shop): dashboard shop panel — exclude + price override (#217)`.

---

### Task 11: Activity UI — Shop section

**Files:**
- Modify: `webapp/client/` (vanilla-JS no-build — follow the marketplace tab's structure; grep `market` in `webapp/client/app.js` for the tab/section pattern)
- Test: `webapp/` smoke tests (extend the existing smoke-test file that exercises marketplace endpoints/DOM)

**Interfaces:**
- Consumes: `GET /api/shop/catalog`, `POST /api/shop/buy`, `GET /api/shop/buy/{id}` (T8).
- Produces: Shop section in the marketplace UI: catalog grid (trait art, name, slot, `N BRIX`), Buy button → existing signing overlay (QR + deep link + "pushed to your Xaman" state — reuse the market-buy overlay component/flow verbatim), `closet_required` → the existing "you need a Closet" prompt, poll session status → success/failure toast. No native `window.confirm` (Discord iframe swallows it — use the in-app overlay, established convention).

Steps: extend smoke test (catalog renders items; buy click posts and shows overlay; closet_required path shows prompt) → FAIL → implement → PASS → commit `feat(shop): Activity shop UI (#217)`.

---

### Task 12: Conservation audit + docs + wrap-up

**Files:**
- Modify: `scripts/audit_trait_economy.py` (only if its census doesn't already tolerate shop rows — verify first: run it against a DB with a settled + an expired shop order fixture; the spec's invariant is `census == genesis + Σ supply_changes`, which Tasks 6/7 maintain, so likely no change)
- Modify: `CLAUDE.md` (env vars: `SHOP_*`, `ASSEMBLE_TAXON`; `TRAIT_TAXON` default 1763→176 everywhere it's mentioned; short Trait Shop section pointing at the spec)
- Modify: `.env.example` if present (`ls -a` to check)
- Test: `tests/test_shop_conservation.py`

**Steps:**

- [ ] **Step 1:** Write `tests/test_shop_conservation.py`: drive a full fake purchase (T6 happy path) and a full expiry (T7) against in-memory stores, then assert the audit's conservation function reports zero drift for both (import and call the checker function from `scripts/audit_trait_economy.py` directly — grep its name).
- [ ] **Step 2:** Run — fix audit or flows if drift appears.
- [ ] **Step 3:** Update `CLAUDE.md` + `.env` docs (all `1763` mentions → `176`, add new vars).
- [ ] **Step 4:** Full gate: `.venv/bin/pytest -q` then `pre-commit run --all-files --hook-stage pre-push` — green.
- [ ] **Step 5:** Commit `docs(shop): env vars + taxon docs; conservation test (#217)`.
- [ ] **Step 6:** Open the PR (ready, not draft) against `Team-Hamsa/LFG` main; wait for Greptile + CodeRabbit and resolve every actionable finding before merge (repo rule).
- [ ] **Step 7:** After the spec/plan commits exist on main, post permalinks (blob URLs at the current commit SHA) for the spec and this plan as a comment on #217 (`gh issue comment 217 --repo Team-Hamsa/LFG`). *(Skip if already done at planning time.)*

---

## Deployment notes (post-merge, ops — not code tasks)

- `pm2 restart lfg-activity lfg-bot lfg-telegram lfg-index-testnet` after merge (post-merge hook covers lfg-activity).
- Testnet only (`ECONOMY_ENABLED` stays off on mainnet); existing taxon-1763 testnet trait tokens are abandoned by design — holders' tokens go invisible to the economy (approved trade-off).
- Tune `SHOP_BASE_BRIX`/`SHOP_MIN_BRIX`/`SHOP_MAX_BRIX` against real testnet share numbers before announcing.
