# Marketplace Fee Cover Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a buyer places a bid through LFG on a character listed on an
allowlisted external marketplace (xrp.cafe), and the marketplace's bot settles
it, LFG automatically refunds that marketplace's broker fee to the buyer in XRP.
The campaign is controlled from Discord `/admin`.

**Architecture:**

- **Durable money state** lives in the per-network app DB, in a new store:
  `lfg_core/fee_cover_store.py`.
- **Pure rules** decide promises and refund amounts from the validated accept
  transaction: `lfg_core/fee_cover.py`.
- **I/O orchestration** (archive lookup, payout, recovery, sweep) sits behind
  injectable deps: `lfg_core/fee_cover_settle.py`.
- **`lfg_service/app.py` is thin wiring.** It quotes at bid start, promises at
  bid finalize, settles on the bid status poll, sweeps in
  `_settlement_sweep_loop`, and exposes the admin endpoints.
- **A Discord sub-panel** drives the admin endpoints through the SDK client.
- **The vanilla-JS client** renders the refund state.

**Tech Stack:** Python 3.10, aiohttp, sqlite3, xrpl-py, discord.py 2.x, vanilla
ES modules (Node-executed pure tests), pytest.

**Spec:** `docs/superpowers/specs/2026-09-14-marketplace-fee-cover-design.md`.
Read it first; this plan implements it section by section.

## Global Constraints

- Every XRPL transaction carries `SourceTag = 2606160021` plus provenance memos
  from `lfg_core/memos.py`. Refund Payments use the new memo action
  `fee-cover`, campaign memo `fee-cover-<campaign_id>`, and an extra recovery
  memo `lfg:fee_cover:<accept_tx_hash>`.
- Money state lives in `db_path.app_db_path(network)`, never in
  `onchain_<net>.db` / `history_<net>.db`.
- `fee_cover_refunds.accept_tx_hash` is the PRIMARY KEY and
  `fee_cover_refunds.offer_index` is UNIQUE. One refund per sale is enforced by
  sqlite.
- Refund amount = `min(promised_drops, observed_fee × coverage_bps // 10000,
  observed_royalty × ROYALTY_CEILING_BPS // 10000)`, with
  `ROYALTY_CEILING_BPS = 5000`. That ceiling is a code constant and is never an
  admin knob.
- Refunds are paid from `config.SIGNING_ACCOUNT` (mainnet: the NFT issuer via
  regular key), with `Account` set explicitly and a `LastLedgerSequence`
  pinned before submit.
- An indeterminate payout is never retried blind. `owed → submitted` is one
  conditional UPDATE. Only recovery decides `submitted → confirmed|failed`.
- The fee cover must never break the bid flow. Every fee-cover call in a market
  handler is wrapped in `try/except` and logs a warning.
- Campaign defaults (Discord modal): coverage 100%, budget 50 XRP, per-wallet
  cap 5 XRP per 30 days, min bid 1 XRP, no end. Ships with no campaign active.
- New env var: `FEE_COVER_LEDGER_MARGIN` (default 40).
- Tests never read the deployed `.env` (root `conftest.py` sets
  `LFG_SKIP_DOTENV=1`). New test files need no env-guard preamble, except
  Discord-surface tests, which set the bot env the way
  `tests/test_discord_admin_x_toggle.py` does.
- **Test event loops:** new tests drive coroutines with a private-loop `_run`
  helper (`asyncio.new_event_loop()` … `loop.close()`), never `asyncio.run()` or
  `asyncio.get_event_loop()`. On Python 3.10, `asyncio.run()` leaves the global
  loop unset, and older modules later in the alphabetical run (e.g.
  `test_image_archive.py`, `test_market_api.py`) call `get_event_loop()` and
  fail. Verified while testing this plan. When a test must call production
  code that uses `asyncio.run()`, monkeypatch `asyncio.run` for that test.
- No Claude/AI attribution in commits or PR bodies.
- Pre-push gate is blocking: ruff, ruff-format, mypy, gitleaks, pytest. Never
  `--no-verify`. In a worktree, symlink the venv first:
  `ln -s /home/hamsa/LFG/.venv .venv`.

## File Structure

| File | Status | Responsibility |
|---|---|---|
| `lfg_core/fee_cover_store.py` | create | Schema, campaigns + audit, promises, refunds, budget/wallet accounting, payout state transitions, status summary |
| `lfg_core/fee_cover.py` | create | Pure rules: external listing pick, promise amount/decline, refund computation from a validated accept, audit invariants |
| `lfg_core/fee_cover_settle.py` | create | `FeeCoverDeps`, archive lookups, `settle_promise`, `pay_refund`, `recover_refunds`, `sweep_once` |
| `lfg_core/memos.py` | modify | `ACTION_FEE_COVER` |
| `lfg_core/xrpl_ops.py` | modify | `send_fee_cover_refund`, `find_fee_cover_payment`, shared memo scan, `get_xrp_balance_drops` |
| `lfg_core/market_store.py` | modify | `live_listings_for_nft` |
| `lfg_core/market_flow.py` | modify | `BidSession.fee_cover` |
| `lfg_core/config.py` | modify | `FEE_COVER_LEDGER_MARGIN` |
| `lfg_service/app.py` | modify | Quote/promise/settle wiring, sweep + startup recovery, admin endpoints, browse estimate, my-bids view |
| `surfaces/_client/client.py` | modify | `fee_cover_status/start/update/stop` |
| `surfaces/discord_bot/fee_cover_admin.py` | create | Modal, sub-panel view, status embed, form parsing |
| `surfaces/discord_bot/admin.py` | modify | "💸 Fee Cover" button opening the sub-panel |
| `webapp/client/market_pure.js` | modify | `feeCoverNote`, `feeCoverLine`, `feeCoverPending`, `externalFillCopy` third arg, `mapListingRow.feeCoverXrp` |
| `webapp/client/app.js`, `webapp/client/index.html` | modify | Render refund copy, keep polling for the refund, cache-busters |
| `scripts/recover_fee_cover_refunds.py` | create | Recovery + `--requeue` |
| `scripts/fee_cover_report.py` | create | Status report + `--audit` |
| `ecosystem.prod.config.js`, `ecosystem.staging.config.js` | modify | Optional audit cron entries |
| `CLAUDE.md` | modify | Env var + ops section |
| `tests/fixtures/fee_cover/cafe_brokered_accept.json` | create | Real validated cafe-brokered accept (`FED6256D…`) |
| `tests/test_fee_cover_store.py`, `tests/test_fee_cover_rules.py`, `tests/test_fee_cover_settle.py`, `tests/test_fee_cover_xrpl.py`, `tests/test_fee_cover_api.py`, `tests/test_fee_cover_admin_api.py`, `tests/test_discord_fee_cover_admin.py`, `tests/test_fee_cover_client.py`, `tests/test_fee_cover_scripts.py` | create | Tests per task |

Work on a feature branch (`feat/marketplace-fee-cover`) off `main`. It lands as
a normal reviewed PR (Greptile + CodeRabbit).

---

### Task 1: Fee-cover store — schema, campaigns, audit

**Files:**
- Create: `lfg_core/fee_cover_store.py`
- Test: `tests/test_fee_cover_store.py`

**Interfaces:**
- Consumes: nothing
- Produces:
  - `BPS = 10_000`, `DEFAULT_WALLET_WINDOW_SECONDS = 2_592_000`
  - `connect(db_path: str) -> sqlite3.Connection`, which creates the schema.
  - `Knobs(coverage_bps: int, budget_drops: int, wallet_cap_drops: int,
    min_bid_drops: int)` with `.validate()`.
  - `Campaign`, a frozen dataclass with fields `id, network, status,
    coverage_bps, budget_drops, wallet_cap_drops, wallet_window_seconds,
    min_bid_drops, started_at, started_by, ends_at, stopped_at, stopped_by`
    and `.is_live(now: int) -> bool`.
  - Campaign queries:
    - `current_campaign(conn, network) -> Campaign | None`: status active,
      possibly past its end.
    - `live_campaign(conn, network, now=None) -> Campaign | None`
    - `get_campaign(conn, campaign_id) -> Campaign | None`
    - `latest_campaign(conn, network) -> Campaign | None`
  - Campaign mutations:
    - `start_campaign(conn, *, network, actor, knobs, duration_seconds,
      now=None) -> tuple[Campaign, str]`, where the result is `"started"` or
      `"already_active"`.
    - `update_campaign(conn, *, network, actor, knobs, now=None) ->
      tuple[Campaign | None, str]`, where the result is `"updated"` or
      `"no_live_campaign"`.
    - `stop_campaign(conn, *, network, actor, now=None) ->
      tuple[Campaign | None, str]`, where the result is `"stopped"` or
      `"already_inactive"`.
  - `audit(conn, *, network, actor, action, at, campaign_id, result,
    details=None) -> None`
  - Helpers `_now(now)` and `_immediate(conn)` (a context manager), used by
    Task 2 in the same module.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_fee_cover_store.py
"""Fee-cover store (spec: docs/superpowers/specs/2026-09-14-marketplace-fee-cover-design.md)."""

from __future__ import annotations

import sqlite3

import pytest

from lfg_core import fee_cover_store as store

NET = "testnet"
KNOBS = store.Knobs(coverage_bps=10_000, budget_drops=50_000_000, wallet_cap_drops=5_000_000, min_bid_drops=1_000_000)


@pytest.fixture()
def conn(tmp_path):
    c = store.connect(str(tmp_path / "app.db"))
    yield c
    c.close()


def _audit_rows(conn):
    return [dict(r) for r in conn.execute("SELECT action, result, actor FROM fee_cover_audit ORDER BY id")]


def test_start_creates_a_live_campaign_with_the_knobs(conn):
    campaign, result = store.start_campaign(conn, network=NET, actor="discord:1", knobs=KNOBS, duration_seconds=None, now=1000)
    assert result == "started"
    assert campaign.status == "active"
    assert campaign.coverage_bps == 10_000
    assert campaign.budget_drops == 50_000_000
    assert campaign.wallet_cap_drops == 5_000_000
    assert campaign.min_bid_drops == 1_000_000
    assert campaign.wallet_window_seconds == store.DEFAULT_WALLET_WINDOW_SECONDS
    assert campaign.ends_at is None
    assert store.live_campaign(conn, NET, now=2000) == campaign
    assert _audit_rows(conn) == [{"action": "start", "result": "started", "actor": "discord:1"}]


def test_start_twice_is_idempotent_and_audited(conn):
    first, _ = store.start_campaign(conn, network=NET, actor="discord:1", knobs=KNOBS, duration_seconds=None, now=1000)
    second, result = store.start_campaign(conn, network=NET, actor="discord:2", knobs=KNOBS, duration_seconds=None, now=1001)
    assert result == "already_active"
    assert second.id == first.id
    assert [r["result"] for r in _audit_rows(conn)] == ["started", "already_active"]


def test_expired_campaign_is_not_live_and_start_replaces_it(conn):
    old, _ = store.start_campaign(conn, network=NET, actor="discord:1", knobs=KNOBS, duration_seconds=100, now=1000)
    assert old.ends_at == 1100
    assert store.live_campaign(conn, NET, now=1099) is not None
    assert store.live_campaign(conn, NET, now=1100) is None
    new, result = store.start_campaign(conn, network=NET, actor="discord:1", knobs=KNOBS, duration_seconds=None, now=1200)
    assert result == "started"
    assert new.id != old.id
    retired = store.get_campaign(conn, old.id)
    assert retired is not None and retired.status == "stopped" and retired.stopped_by == "system"


def test_update_changes_knobs_only_on_a_live_campaign(conn):
    missing, result = store.update_campaign(conn, network=NET, actor="discord:1", knobs=KNOBS, now=1000)
    assert (missing, result) == (None, "no_live_campaign")
    store.start_campaign(conn, network=NET, actor="discord:1", knobs=KNOBS, duration_seconds=None, now=1000)
    new_knobs = store.Knobs(coverage_bps=5_000, budget_drops=10_000_000, wallet_cap_drops=2_000_000, min_bid_drops=0)
    updated, result = store.update_campaign(conn, network=NET, actor="discord:1", knobs=new_knobs, now=1001)
    assert result == "updated"
    assert updated is not None
    assert (updated.coverage_bps, updated.budget_drops, updated.wallet_cap_drops, updated.min_bid_drops) == (5_000, 10_000_000, 2_000_000, 0)


def test_stop_then_stop_again(conn):
    started, _ = store.start_campaign(conn, network=NET, actor="discord:1", knobs=KNOBS, duration_seconds=None, now=1000)
    stopped, result = store.stop_campaign(conn, network=NET, actor="discord:2", now=1500)
    assert result == "stopped"
    assert stopped is not None and stopped.id == started.id
    assert stopped.status == "stopped" and stopped.stopped_by == "discord:2" and stopped.stopped_at == 1500
    again, result = store.stop_campaign(conn, network=NET, actor="discord:2", now=1600)
    assert result == "already_inactive"
    assert again is not None and again.id == started.id
    assert store.live_campaign(conn, NET, now=1700) is None


@pytest.mark.parametrize(
    "knobs",
    [
        store.Knobs(coverage_bps=10_001, budget_drops=1, wallet_cap_drops=1, min_bid_drops=0),
        store.Knobs(coverage_bps=-1, budget_drops=1, wallet_cap_drops=1, min_bid_drops=0),
        store.Knobs(coverage_bps=100, budget_drops=0, wallet_cap_drops=1, min_bid_drops=0),
        store.Knobs(coverage_bps=100, budget_drops=1, wallet_cap_drops=0, min_bid_drops=0),
        store.Knobs(coverage_bps=100, budget_drops=1, wallet_cap_drops=1, min_bid_drops=-1),
    ],
)
def test_invalid_knobs_are_rejected(conn, knobs):
    with pytest.raises(ValueError):
        store.start_campaign(conn, network=NET, actor="discord:1", knobs=knobs, duration_seconds=None, now=1000)


def test_blank_actor_and_bad_duration_are_rejected(conn):
    with pytest.raises(ValueError):
        store.start_campaign(conn, network=NET, actor="  ", knobs=KNOBS, duration_seconds=None, now=1000)
    with pytest.raises(ValueError):
        store.start_campaign(conn, network=NET, actor="discord:1", knobs=KNOBS, duration_seconds=0, now=1000)


def test_sqlite_allows_one_active_campaign_per_network(conn):
    store.start_campaign(conn, network=NET, actor="discord:1", knobs=KNOBS, duration_seconds=None, now=1000)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO fee_cover_campaigns (network, status, coverage_bps, budget_drops, wallet_cap_drops,"
            " min_bid_drops, started_at, started_by) VALUES (?, 'active', 1, 1, 1, 0, 1, 'x')",
            (NET,),
        )
    conn.rollback()  # the failed INSERT left Python's implicit transaction open
    # a different network is independent
    store.start_campaign(conn, network="mainnet", actor="discord:1", knobs=KNOBS, duration_seconds=None, now=1000)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_fee_cover_store.py -v`
Expected: FAIL with `ImportError: cannot import name 'fee_cover_store'`

- [ ] **Step 3: Write the implementation**

```python
# lfg_core/fee_cover_store.py
"""Durable state for the marketplace fee-cover campaign.

Spec: docs/superpowers/specs/2026-09-14-marketplace-fee-cover-design.md.

Lives in the per-network APP DB (db_path.app_db_path), never the droppable
onchain_<net>.db / history_<net>.db: these rows are money owed. Two invariants
are enforced by sqlite rather than application logic — one active campaign per
network (partial unique index) and one refund per sale (accept_tx_hash PRIMARY
KEY + offer_index UNIQUE).
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from typing import Any

BPS = 10_000
DEFAULT_WALLET_WINDOW_SECONDS = 30 * 86_400

_SCHEMA = """
CREATE TABLE IF NOT EXISTS fee_cover_campaigns (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  network TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('active','stopped')),
  coverage_bps INTEGER NOT NULL CHECK (coverage_bps BETWEEN 0 AND 10000),
  budget_drops INTEGER NOT NULL CHECK (budget_drops > 0),
  wallet_cap_drops INTEGER NOT NULL,
  wallet_window_seconds INTEGER NOT NULL DEFAULT 2592000,
  min_bid_drops INTEGER NOT NULL DEFAULT 1000000,
  started_at INTEGER NOT NULL, started_by TEXT NOT NULL,
  ends_at INTEGER, stopped_at INTEGER, stopped_by TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_fee_cover_one_active
  ON fee_cover_campaigns(network) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS fee_cover_promises (
  offer_index TEXT PRIMARY KEY,
  campaign_id INTEGER NOT NULL REFERENCES fee_cover_campaigns(id),
  network TEXT NOT NULL,
  nft_id TEXT NOT NULL, bidder TEXT NOT NULL,
  bid_drops INTEGER NOT NULL, ask_drops INTEGER NOT NULL,
  broker TEXT NOT NULL, broker_rate REAL NOT NULL,
  coverage_bps INTEGER NOT NULL, promised_drops INTEGER NOT NULL,
  bid_expiration INTEGER,
  state TEXT NOT NULL CHECK (state IN ('open','filled','released','declined')),
  reason TEXT,
  accept_tx_hash TEXT,
  created_at INTEGER NOT NULL, closed_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_fee_cover_promises_open
  ON fee_cover_promises(network, state);
CREATE INDEX IF NOT EXISTS idx_fee_cover_promises_bidder
  ON fee_cover_promises(network, bidder, created_at);

CREATE TABLE IF NOT EXISTS fee_cover_refunds (
  accept_tx_hash TEXT PRIMARY KEY,
  offer_index TEXT NOT NULL UNIQUE REFERENCES fee_cover_promises(offer_index),
  campaign_id INTEGER NOT NULL,
  network TEXT NOT NULL,
  bidder TEXT NOT NULL,
  seller TEXT, broker TEXT,
  observed_fee_drops INTEGER, observed_royalty_drops INTEGER,
  refund_drops INTEGER NOT NULL DEFAULT 0,
  state TEXT NOT NULL CHECK (state IN ('owed','submitted','confirmed','failed','declined')),
  reason TEXT,
  payout_tx_hash TEXT, last_ledger_seq INTEGER,
  created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fee_cover_refunds_state
  ON fee_cover_refunds(network, state);

CREATE TABLE IF NOT EXISTS fee_cover_audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  network TEXT NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL,
  at INTEGER NOT NULL, campaign_id INTEGER, result TEXT NOT NULL, details TEXT
);
"""


@dataclass(frozen=True)
class Knobs:
    coverage_bps: int
    budget_drops: int
    wallet_cap_drops: int
    min_bid_drops: int

    def validate(self) -> None:
        if not 0 <= self.coverage_bps <= BPS:
            raise ValueError("coverage must be between 0% and 100%")
        if self.budget_drops <= 0:
            raise ValueError("budget must be greater than zero")
        if self.wallet_cap_drops <= 0:
            raise ValueError("per-wallet cap must be greater than zero")
        if self.min_bid_drops < 0:
            raise ValueError("minimum bid cannot be negative")


@dataclass(frozen=True)
class Campaign:
    id: int
    network: str
    status: str
    coverage_bps: int
    budget_drops: int
    wallet_cap_drops: int
    wallet_window_seconds: int
    min_bid_drops: int
    started_at: int
    started_by: str
    ends_at: int | None
    stopped_at: int | None
    stopped_by: str | None

    def is_live(self, now: int) -> bool:
        return self.status == "active" and (self.ends_at is None or now < self.ends_at)


def connect(db_path: str) -> sqlite3.Connection:
    """Open the app DB for fee-cover work (WAL, busy timeout, FKs) and create
    the schema. Open on the thread that uses it — sqlite3 connections are
    never shared across threads here."""
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_SCHEMA)
    return conn


def _now(now: int | None) -> int:
    return int(time.time()) if now is None else int(now)


@contextmanager
def _immediate(conn: sqlite3.Connection) -> Iterator[None]:
    """BEGIN IMMEDIATE ... COMMIT: the write lock is taken before the reads
    that decide the write, so two concurrent writers cannot both pass a
    headroom check."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()


def _actor(actor: str) -> str:
    value = actor.strip() if isinstance(actor, str) else ""
    if not value:
        raise ValueError("actor is required")
    return value


def _campaign(row: sqlite3.Row | None) -> Campaign | None:
    if row is None:
        return None
    return Campaign(
        id=int(row["id"]),
        network=str(row["network"]),
        status=str(row["status"]),
        coverage_bps=int(row["coverage_bps"]),
        budget_drops=int(row["budget_drops"]),
        wallet_cap_drops=int(row["wallet_cap_drops"]),
        wallet_window_seconds=int(row["wallet_window_seconds"]),
        min_bid_drops=int(row["min_bid_drops"]),
        started_at=int(row["started_at"]),
        started_by=str(row["started_by"]),
        ends_at=None if row["ends_at"] is None else int(row["ends_at"]),
        stopped_at=None if row["stopped_at"] is None else int(row["stopped_at"]),
        stopped_by=None if row["stopped_by"] is None else str(row["stopped_by"]),
    )


def audit(
    conn: sqlite3.Connection,
    *,
    network: str,
    actor: str,
    action: str,
    at: int,
    campaign_id: int | None,
    result: str,
    details: Mapping[str, Any] | None = None,
) -> None:
    conn.execute(
        "INSERT INTO fee_cover_audit (network, actor, action, at, campaign_id, result, details)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            network,
            actor,
            action,
            at,
            campaign_id,
            result,
            json.dumps(dict(details), sort_keys=True) if details is not None else None,
        ),
    )


def get_campaign(conn: sqlite3.Connection, campaign_id: int) -> Campaign | None:
    return _campaign(
        conn.execute("SELECT * FROM fee_cover_campaigns WHERE id = ?", (campaign_id,)).fetchone()
    )


def current_campaign(conn: sqlite3.Connection, network: str) -> Campaign | None:
    """The row still marked active — possibly already past ends_at."""
    return _campaign(
        conn.execute(
            "SELECT * FROM fee_cover_campaigns WHERE network = ? AND status = 'active'", (network,)
        ).fetchone()
    )


def latest_campaign(conn: sqlite3.Connection, network: str) -> Campaign | None:
    return _campaign(
        conn.execute(
            "SELECT * FROM fee_cover_campaigns WHERE network = ? ORDER BY id DESC LIMIT 1",
            (network,),
        ).fetchone()
    )


def live_campaign(conn: sqlite3.Connection, network: str, now: int | None = None) -> Campaign | None:
    campaign = current_campaign(conn, network)
    return campaign if campaign is not None and campaign.is_live(_now(now)) else None


def start_campaign(
    conn: sqlite3.Connection,
    *,
    network: str,
    actor: str,
    knobs: Knobs,
    duration_seconds: int | None,
    now: int | None = None,
) -> tuple[Campaign, str]:
    knobs.validate()
    if duration_seconds is not None and duration_seconds <= 0:
        raise ValueError("duration must be greater than zero")
    who = _actor(actor)
    ts = _now(now)
    with _immediate(conn):
        current = current_campaign(conn, network)
        if current is not None and current.is_live(ts):
            audit(conn, network=network, actor=who, action="start", at=ts,
                  campaign_id=current.id, result="already_active")
            return current, "already_active"
        if current is not None:
            # An active row past its end: retire it so the one-active index
            # admits the new campaign.
            conn.execute(
                "UPDATE fee_cover_campaigns SET status = 'stopped', stopped_at = ?, stopped_by = 'system'"
                " WHERE id = ?",
                (current.ends_at, current.id),
            )
            audit(conn, network=network, actor="system", action="expire", at=ts,
                  campaign_id=current.id, result="expired")
        cursor = conn.execute(
            "INSERT INTO fee_cover_campaigns (network, status, coverage_bps, budget_drops,"
            " wallet_cap_drops, wallet_window_seconds, min_bid_drops, started_at, started_by, ends_at)"
            " VALUES (?, 'active', ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                network,
                knobs.coverage_bps,
                knobs.budget_drops,
                knobs.wallet_cap_drops,
                DEFAULT_WALLET_WINDOW_SECONDS,
                knobs.min_bid_drops,
                ts,
                who,
                ts + duration_seconds if duration_seconds else None,
            ),
        )
        campaign_id = int(cursor.lastrowid or 0)
        audit(conn, network=network, actor=who, action="start", at=ts, campaign_id=campaign_id,
              result="started", details={**asdict(knobs), "duration_seconds": duration_seconds})
    started = get_campaign(conn, campaign_id)
    assert started is not None
    return started, "started"


def update_campaign(
    conn: sqlite3.Connection,
    *,
    network: str,
    actor: str,
    knobs: Knobs,
    now: int | None = None,
) -> tuple[Campaign | None, str]:
    """Change the knobs of the live campaign. Applies to NEW promises only —
    every promise snapshots its coverage and rate when it is made."""
    knobs.validate()
    who = _actor(actor)
    ts = _now(now)
    with _immediate(conn):
        current = current_campaign(conn, network)
        if current is None or not current.is_live(ts):
            audit(conn, network=network, actor=who, action="update", at=ts,
                  campaign_id=current.id if current else None, result="no_live_campaign",
                  details=asdict(knobs))
            return None, "no_live_campaign"
        conn.execute(
            "UPDATE fee_cover_campaigns SET coverage_bps = ?, budget_drops = ?, wallet_cap_drops = ?,"
            " min_bid_drops = ? WHERE id = ?",
            (knobs.coverage_bps, knobs.budget_drops, knobs.wallet_cap_drops, knobs.min_bid_drops, current.id),
        )
        audit(conn, network=network, actor=who, action="update", at=ts, campaign_id=current.id,
              result="updated", details=asdict(knobs))
    return get_campaign(conn, current.id), "updated"


def stop_campaign(
    conn: sqlite3.Connection, *, network: str, actor: str, now: int | None = None
) -> tuple[Campaign | None, str]:
    """Stop new promises. Open promises are still honored (spec: Stop semantics)."""
    who = _actor(actor)
    ts = _now(now)
    with _immediate(conn):
        current = current_campaign(conn, network)
        if current is None:
            latest = latest_campaign(conn, network)
            audit(conn, network=network, actor=who, action="stop", at=ts,
                  campaign_id=latest.id if latest else None, result="already_inactive")
            return latest, "already_inactive"
        conn.execute(
            "UPDATE fee_cover_campaigns SET status = 'stopped', stopped_at = ?, stopped_by = ? WHERE id = ?",
            (ts, who, current.id),
        )
        audit(conn, network=network, actor=who, action="stop", at=ts, campaign_id=current.id,
              result="stopped")
    return get_campaign(conn, current.id), "stopped"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_fee_cover_store.py -v`
Expected: 12 passed. The parametrized knob test counts 5; the other tests
count 7.

- [ ] **Step 5: Format, type-check, commit**

```bash
.venv/bin/ruff format lfg_core/fee_cover_store.py tests/test_fee_cover_store.py
.venv/bin/ruff check --fix lfg_core/fee_cover_store.py tests/test_fee_cover_store.py
.venv/bin/mypy lfg_core/fee_cover_store.py
git add lfg_core/fee_cover_store.py tests/test_fee_cover_store.py
git commit -m "feat(fee-cover): campaign store with audit and one-active invariant"
```

---
### Task 2: Fee-cover store — promises, refunds, payout transitions, status

**Files:**
- Modify: `lfg_core/fee_cover_store.py` (append)
- Test: `tests/test_fee_cover_store.py` (append)

**Interfaces:**
- Consumes (Task 1): `connect`, `Campaign`, `Knobs`, `start_campaign`,
  `stop_campaign`, `get_campaign`, `current_campaign`, `latest_campaign`,
  `_immediate`, `_now`, `BPS`.
- Produces:
  - Promise records:
    - `PromiseInput(offer_index, network, nft_id, bidder, bid_drops,
      ask_drops, broker, broker_rate, bid_expiration)`, a frozen dataclass.
    - `RefundDecision(accept_tx_hash, seller, broker, observed_fee_drops,
      observed_royalty_drops, refund_drops, reason)`, a frozen dataclass.
      `reason is None` means owed.
  - Accounting:
    - `committed_drops(conn, campaign_id) -> int`
    - `paid_drops(conn, campaign_id) -> int`
    - `wallet_usage_drops(conn, network, wallet, since) -> int`
    - `headroom_reason(conn, campaign, network, bidder, drops, now) ->
      str | None`, returning `"budget_exhausted"`, `"wallet_cap"` or `None`.
  - Promise lifecycle:
    - `record_promise(conn, campaign, inp, *, promised_drops, decline_reason,
      now=None) -> dict | None`. `None` means the campaign stopped before the
      write lock was taken.
    - `get_promise(conn, offer_index) -> dict | None`
    - `open_promises(conn, network) -> list[dict]`
    - `release_promise(conn, offer_index, reason, now=None) -> bool`
    - `fill_promise(conn, offer_index, decision, now=None) -> dict | None`,
      which returns the refund row.
  - Refund lookups:
    - `get_refund(conn, accept_tx_hash) -> dict | None`
    - `get_refund_by_offer(conn, offer_index) -> dict | None`
    - `refunds_in_state(conn, network, state) -> list[dict]`
  - Payout transitions:
    - `claim_for_payout(conn, accept_tx_hash, last_ledger_seq, now=None) ->
      bool`
    - `record_payout(conn, accept_tx_hash, *, state, tx_hash,
      last_ledger_seq, now=None) -> None`
    - `return_to_owed(conn, accept_tx_hash, now=None) -> bool`
    - `requeue_failed(conn, accept_tx_hash, now=None) -> str`, returning
      `"requeued"`, `"not_failed"` or `"budget_exhausted"`.
  - Views:
    - `view_for_offer(conn, offer_index) -> dict | None` with keys
      `state, drops, xrp, reason, payout_tx_hash`.
    - `status_summary(conn, network, now=None) -> dict`

- [ ] **Step 1: Write the failing tests** (append to `tests/test_fee_cover_store.py`)

```python
# --- Task 2: promises, refunds, payout transitions ------------------------

BIDDER = "rBidder1111111111111111111111111"
SELLER = "rSeller1111111111111111111111111"
CAFE = "rpx9JThQ2y37FaGeeJP7PXDUVEXY3PHZSC"


def _live(conn, *, budget=1_000_000, cap=500_000, now=1000):
    knobs = store.Knobs(coverage_bps=10_000, budget_drops=budget, wallet_cap_drops=cap, min_bid_drops=0)
    campaign, _ = store.start_campaign(conn, network=NET, actor="discord:1", knobs=knobs, duration_seconds=None, now=now)
    return campaign


def _inp(offer_index="BID1", bidder=BIDDER, bid=5_075_000):
    return store.PromiseInput(
        offer_index=offer_index, network=NET, nft_id="NFT1", bidder=bidder, bid_drops=bid,
        ask_drops=4_990_000, broker=CAFE, broker_rate=0.01589, bid_expiration=900_000_000,
    )


def _owed(accept="ACCEPT1", refund=80_642):
    return store.RefundDecision(accept, SELLER, CAFE, 80_642, 349_605, refund, None)


def test_record_promise_opens_and_reserves_budget(conn):
    c = _live(conn)
    row = store.record_promise(conn, c, _inp(), promised_drops=80_642, decline_reason=None, now=1001)
    assert row is not None and row["state"] == "open" and row["reason"] is None
    assert row["coverage_bps"] == 10_000 and row["promised_drops"] == 80_642
    assert store.committed_drops(conn, c.id) == 80_642


def test_record_promise_is_idempotent_on_offer_index(conn):
    c = _live(conn)
    first = store.record_promise(conn, c, _inp(), promised_drops=80_642, decline_reason=None, now=1001)
    second = store.record_promise(conn, c, _inp(), promised_drops=1, decline_reason="below_clearing", now=1002)
    assert second == first
    assert store.committed_drops(conn, c.id) == 80_642


def test_record_promise_budget_boundary(conn):
    c = _live(conn, budget=100_000, cap=1_000_000)
    exact = store.record_promise(conn, c, _inp("A"), promised_drops=100_000, decline_reason=None, now=1001)
    assert exact is not None and exact["state"] == "open"
    over = store.record_promise(conn, c, _inp("B", bidder="rOther"), promised_drops=1, decline_reason=None, now=1002)
    assert over is not None and over["state"] == "declined" and over["reason"] == "budget_exhausted"
    assert store.committed_drops(conn, c.id) == 100_000


def test_record_promise_wallet_cap_counts_only_the_window(conn):
    c = _live(conn, budget=10_000_000, cap=100_000, now=1000)
    store.record_promise(conn, c, _inp("OLD"), promised_drops=100_000, decline_reason=None, now=1000)
    capped = store.record_promise(conn, c, _inp("NOW"), promised_drops=1, decline_reason=None, now=1001)
    assert capped is not None and capped["reason"] == "wallet_cap"
    later = 1000 + c.wallet_window_seconds + 1
    fresh = store.record_promise(conn, c, _inp("LATER"), promised_drops=100_000, decline_reason=None, now=later)
    assert fresh is not None and fresh["state"] == "open"


def test_record_promise_pure_decline_is_recorded_without_reserving(conn):
    c = _live(conn)
    row = store.record_promise(conn, c, _inp(), promised_drops=80_000, decline_reason="below_clearing", now=1001)
    assert row is not None and row["state"] == "declined" and row["reason"] == "below_clearing"
    assert store.committed_drops(conn, c.id) == 0


def test_record_promise_rejects_unrecorded_reasons(conn):
    c = _live(conn)
    for reason in ("campaign_inactive", "not_external_listing"):
        with pytest.raises(ValueError):
            store.record_promise(conn, c, _inp(), promised_drops=1, decline_reason=reason, now=1001)


def test_record_promise_after_stop_writes_nothing(conn):
    c = _live(conn)
    store.stop_campaign(conn, network=NET, actor="discord:1", now=1001)
    assert store.record_promise(conn, c, _inp(), promised_drops=1, decline_reason=None, now=1002) is None
    assert store.get_promise(conn, "BID1") is None


def test_release_frees_budget_and_only_releases_open(conn):
    c = _live(conn)
    store.record_promise(conn, c, _inp(), promised_drops=80_642, decline_reason=None, now=1001)
    assert store.release_promise(conn, "BID1", "expired", now=2000) is True
    assert store.release_promise(conn, "BID1", "expired", now=2001) is False
    row = store.get_promise(conn, "BID1")
    assert row["state"] == "released" and row["reason"] == "expired" and row["closed_at"] == 2000
    assert store.committed_drops(conn, c.id) == 0


def test_fill_promise_writes_one_owed_refund(conn):
    c = _live(conn)
    store.record_promise(conn, c, _inp(), promised_drops=80_642, decline_reason=None, now=1001)
    refund = store.fill_promise(conn, "BID1", _owed(), now=1100)
    assert refund["state"] == "owed" and refund["refund_drops"] == 80_642
    assert refund["bidder"] == BIDDER and refund["seller"] == SELLER and refund["campaign_id"] == c.id
    assert store.get_promise(conn, "BID1")["state"] == "filled"
    # poll + sweep race: the second fill returns the same row, no second refund
    again = store.fill_promise(conn, "BID1", _owed(accept="ACCEPT1"), now=1101)
    assert again == refund
    assert conn.execute("SELECT COUNT(*) FROM fee_cover_refunds").fetchone()[0] == 1
    assert store.committed_drops(conn, c.id) == 80_642


def test_fill_promise_declined_refund_frees_budget(conn):
    c = _live(conn)
    store.record_promise(conn, c, _inp(), promised_drops=80_642, decline_reason=None, now=1001)
    decision = store.RefundDecision("ACCEPT1", SELLER, CAFE, 80_642, 349_605, 0, "linked_counterparty")
    refund = store.fill_promise(conn, "BID1", decision, now=1100)
    assert refund["state"] == "declined" and refund["refund_drops"] == 0 and refund["reason"] == "linked_counterparty"
    assert store.committed_drops(conn, c.id) == 0


def test_fill_promise_on_a_released_promise_returns_none(conn):
    c = _live(conn)
    store.record_promise(conn, c, _inp(), promised_drops=80_642, decline_reason=None, now=1001)
    store.release_promise(conn, "BID1", "cancelled", now=1002)
    assert store.fill_promise(conn, "BID1", _owed(), now=1100) is None


def test_payout_claim_happens_exactly_once(conn):
    c = _live(conn)
    store.record_promise(conn, c, _inp(), promised_drops=80_642, decline_reason=None, now=1001)
    store.fill_promise(conn, "BID1", _owed(), now=1100)
    assert store.claim_for_payout(conn, "ACCEPT1", 5_000, now=1101) is True
    assert store.claim_for_payout(conn, "ACCEPT1", 5_000, now=1102) is False
    row = store.get_refund(conn, "ACCEPT1")
    assert row["state"] == "submitted" and row["last_ledger_seq"] == 5_000


def test_record_payout_and_return_to_owed(conn):
    c = _live(conn)
    store.record_promise(conn, c, _inp(), promised_drops=80_642, decline_reason=None, now=1001)
    store.fill_promise(conn, "BID1", _owed(), now=1100)
    store.claim_for_payout(conn, "ACCEPT1", 5_000, now=1101)
    assert store.return_to_owed(conn, "ACCEPT1", now=1102) is True
    row = store.get_refund(conn, "ACCEPT1")
    assert row["state"] == "owed" and row["last_ledger_seq"] is None
    store.claim_for_payout(conn, "ACCEPT1", 6_000, now=1103)
    store.record_payout(conn, "ACCEPT1", state="confirmed", tx_hash="PAYOUT1", last_ledger_seq=5_990, now=1104)
    row = store.get_refund(conn, "ACCEPT1")
    assert (row["state"], row["payout_tx_hash"], row["last_ledger_seq"]) == ("confirmed", "PAYOUT1", 5_990)
    assert store.paid_drops(conn, c.id) == 80_642
    with pytest.raises(ValueError):
        store.record_payout(conn, "ACCEPT1", state="owed", tx_hash=None, last_ledger_seq=None, now=1105)


def test_requeue_failed_requires_budget_headroom(conn):
    c = _live(conn, budget=100_000, cap=1_000_000)
    store.record_promise(conn, c, _inp("BID1"), promised_drops=80_642, decline_reason=None, now=1001)
    store.fill_promise(conn, "BID1", _owed("ACCEPT1"), now=1100)
    store.claim_for_payout(conn, "ACCEPT1", 5_000, now=1101)
    store.record_payout(conn, "ACCEPT1", state="failed", tx_hash=None, last_ledger_seq=5_000, now=1102)
    assert store.get_refund(conn, "ACCEPT1")["reason"] == "payout_failed"
    assert store.committed_drops(conn, c.id) == 0
    # someone else takes most of the budget meanwhile
    store.record_promise(conn, c, _inp("BID2", bidder="rOther"), promised_drops=50_000, decline_reason=None, now=1103)
    assert store.requeue_failed(conn, "ACCEPT1", now=1104) == "budget_exhausted"
    store.release_promise(conn, "BID2", "cancelled", now=1105)
    assert store.requeue_failed(conn, "ACCEPT1", now=1106) == "requeued"
    row = store.get_refund(conn, "ACCEPT1")
    assert row["state"] == "owed" and row["reason"] is None and row["payout_tx_hash"] is None
    assert store.requeue_failed(conn, "ACCEPT1", now=1107) == "not_failed"


def test_view_folds_refund_over_promise(conn):
    c = _live(conn)
    assert store.view_for_offer(conn, "BID1") is None
    store.record_promise(conn, c, _inp(), promised_drops=80_642, decline_reason=None, now=1001)
    assert store.view_for_offer(conn, "BID1") == {
        "state": "open", "drops": 80_642, "xrp": "0.080642", "reason": None, "payout_tx_hash": None,
    }
    store.fill_promise(conn, "BID1", _owed(), now=1100)
    store.claim_for_payout(conn, "ACCEPT1", 5_000, now=1101)
    store.record_payout(conn, "ACCEPT1", state="confirmed", tx_hash="PAYOUT1", last_ledger_seq=4_990, now=1102)
    assert store.view_for_offer(conn, "BID1") == {
        "state": "confirmed", "drops": 80_642, "xrp": "0.080642", "reason": None, "payout_tx_hash": "PAYOUT1",
    }


def test_status_summary(conn):
    assert store.status_summary(conn, NET, now=1000)["state"] == "never_started"
    c = _live(conn, budget=1_000_000, cap=1_000_000)
    store.record_promise(conn, c, _inp("A"), promised_drops=80_000, decline_reason=None, now=1001)
    store.record_promise(conn, c, _inp("B"), promised_drops=70_000, decline_reason=None, now=1002)
    store.record_promise(conn, c, _inp("C"), promised_drops=1, decline_reason="below_clearing", now=1003)
    store.fill_promise(conn, "B", _owed("ACC_B", refund=70_000), now=1100)
    summary = store.status_summary(conn, NET, now=1200)
    assert summary["state"] == "active"
    assert summary["campaign"]["id"] == c.id
    assert summary["committed_drops"] == 150_000
    assert summary["remaining_drops"] == 850_000
    assert summary["paid_drops"] == 0
    assert summary["open_promises"] == 1
    assert summary["refunds_by_state"] == {"owed": 1}
    assert summary["declines_by_reason"] == {"below_clearing": 1}
    assert summary["top_wallets"] == [{"wallet": BIDDER, "drops": 70_000}]
    store.stop_campaign(conn, network=NET, actor="discord:1", now=1300)
    assert store.status_summary(conn, NET, now=1400)["state"] == "stopped"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_fee_cover_store.py -v`
Expected: FAIL with `AttributeError: module 'lfg_core.fee_cover_store' has no attribute 'PromiseInput'`.
Task 1's tests still pass.

- [ ] **Step 3: Write the implementation** (append to `lfg_core/fee_cover_store.py`)

Also add `from lfg_core import market_ops` to the imports at the top of the
file.

```python
# --- promises and refunds -------------------------------------------------

_COMMITTED_REFUND_STATES = ("owed", "submitted", "confirmed")
# Pure-rule verdicts that mean "an ordinary bid": nothing is recorded for them.
_UNRECORDED_DECLINES = frozenset({"campaign_inactive", "not_external_listing"})


@dataclass(frozen=True)
class PromiseInput:
    offer_index: str
    network: str
    nft_id: str
    bidder: str
    bid_drops: int
    ask_drops: int
    broker: str
    broker_rate: float
    bid_expiration: int | None


@dataclass(frozen=True)
class RefundDecision:
    accept_tx_hash: str
    seller: str | None
    broker: str | None
    observed_fee_drops: int | None
    observed_royalty_drops: int | None
    refund_drops: int
    reason: str | None  # None => owed; otherwise the decline reason


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def committed_drops(conn: sqlite3.Connection, campaign_id: int) -> int:
    """Budget in use: open promises plus refunds that are owed, in flight or paid."""
    row = conn.execute(
        "SELECT COALESCE((SELECT SUM(promised_drops) FROM fee_cover_promises"
        "                 WHERE campaign_id = ? AND state = 'open'), 0)"
        "     + COALESCE((SELECT SUM(refund_drops) FROM fee_cover_refunds"
        "                 WHERE campaign_id = ? AND state IN ('owed','submitted','confirmed')), 0)",
        (campaign_id, campaign_id),
    ).fetchone()
    return int(row[0])


def paid_drops(conn: sqlite3.Connection, campaign_id: int) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(refund_drops), 0) FROM fee_cover_refunds"
        " WHERE campaign_id = ? AND state = 'confirmed'",
        (campaign_id,),
    ).fetchone()
    return int(row[0])


def wallet_usage_drops(conn: sqlite3.Connection, network: str, wallet: str, since: int) -> int:
    """One wallet's open promises + committed refunds created since `since`, across campaigns."""
    row = conn.execute(
        "SELECT COALESCE((SELECT SUM(promised_drops) FROM fee_cover_promises"
        "                 WHERE network = ? AND bidder = ? AND state = 'open' AND created_at >= ?), 0)"
        "     + COALESCE((SELECT SUM(refund_drops) FROM fee_cover_refunds"
        "                 WHERE network = ? AND bidder = ? AND state IN ('owed','submitted','confirmed')"
        "                   AND created_at >= ?), 0)",
        (network, wallet, since, network, wallet, since),
    ).fetchone()
    return int(row[0])


def headroom_reason(
    conn: sqlite3.Connection, campaign: Campaign, network: str, bidder: str, drops: int, now: int
) -> str | None:
    if committed_drops(conn, campaign.id) + drops > campaign.budget_drops:
        return "budget_exhausted"
    since = now - campaign.wallet_window_seconds
    if wallet_usage_drops(conn, network, bidder, since) + drops > campaign.wallet_cap_drops:
        return "wallet_cap"
    return None


def get_promise(conn: sqlite3.Connection, offer_index: str) -> dict[str, Any] | None:
    return _row(
        conn.execute("SELECT * FROM fee_cover_promises WHERE offer_index = ?", (offer_index,)).fetchone()
    )


def open_promises(conn: sqlite3.Connection, network: str) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM fee_cover_promises WHERE network = ? AND state = 'open' ORDER BY created_at",
            (network,),
        )
    ]


def record_promise(
    conn: sqlite3.Connection,
    campaign: Campaign,
    inp: PromiseInput,
    *,
    promised_drops: int,
    decline_reason: str | None,
    now: int | None = None,
) -> dict[str, Any] | None:
    """Write the promise for one on-ledger bid (idempotent on offer_index).

    `decline_reason` is the pure-rule verdict (fee_cover.promise_decline_reason).
    Budget and wallet-cap headroom are decided HERE, inside the write lock, so
    two bids can never both take the last of the budget. Coverage is
    snapshotted from `campaign` (the read the caller priced against). Returns
    None — writing nothing — when the campaign stopped before the lock.
    """
    if decline_reason in _UNRECORDED_DECLINES:
        raise ValueError(f"{decline_reason} is never recorded")
    ts = _now(now)
    with _immediate(conn):
        existing = conn.execute(
            "SELECT * FROM fee_cover_promises WHERE offer_index = ?", (inp.offer_index,)
        ).fetchone()
        if existing is not None:
            return dict(existing)
        fresh = get_campaign(conn, campaign.id)
        if fresh is None or not fresh.is_live(ts):
            return None
        reason = decline_reason
        if reason is None:
            reason = headroom_reason(conn, fresh, inp.network, inp.bidder, promised_drops, ts)
        state = "open" if reason is None else "declined"
        conn.execute(
            "INSERT INTO fee_cover_promises (offer_index, campaign_id, network, nft_id, bidder, bid_drops,"
            " ask_drops, broker, broker_rate, coverage_bps, promised_drops, bid_expiration, state, reason,"
            " created_at, closed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                inp.offer_index, fresh.id, inp.network, inp.nft_id, inp.bidder, inp.bid_drops,
                inp.ask_drops, inp.broker, inp.broker_rate, campaign.coverage_bps, promised_drops,
                inp.bid_expiration, state, reason, ts, None if state == "open" else ts,
            ),
        )
    return get_promise(conn, inp.offer_index)


def release_promise(conn: sqlite3.Connection, offer_index: str, reason: str, now: int | None = None) -> bool:
    cursor = conn.execute(
        "UPDATE fee_cover_promises SET state = 'released', reason = ?, closed_at = ?"
        " WHERE offer_index = ? AND state = 'open'",
        (reason, _now(now), offer_index),
    )
    conn.commit()
    return cursor.rowcount == 1


def get_refund(conn: sqlite3.Connection, accept_tx_hash: str) -> dict[str, Any] | None:
    return _row(
        conn.execute("SELECT * FROM fee_cover_refunds WHERE accept_tx_hash = ?", (accept_tx_hash,)).fetchone()
    )


def get_refund_by_offer(conn: sqlite3.Connection, offer_index: str) -> dict[str, Any] | None:
    return _row(
        conn.execute("SELECT * FROM fee_cover_refunds WHERE offer_index = ?", (offer_index,)).fetchone()
    )


def refunds_in_state(conn: sqlite3.Connection, network: str, state: str) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM fee_cover_refunds WHERE network = ? AND state = ? ORDER BY created_at",
            (network, state),
        )
    ]


def fill_promise(
    conn: sqlite3.Connection, offer_index: str, decision: RefundDecision, now: int | None = None
) -> dict[str, Any] | None:
    """Close an open promise against its validated accept and write the refund
    row, atomically. Idempotent: a repeat (poll + sweep race) returns the
    existing refund. None when the promise is not open and has no refund."""
    ts = _now(now)
    with _immediate(conn):
        existing = conn.execute(
            "SELECT * FROM fee_cover_refunds WHERE offer_index = ? OR accept_tx_hash = ?",
            (offer_index, decision.accept_tx_hash),
        ).fetchone()
        if existing is not None:
            return dict(existing)
        promise = conn.execute(
            "SELECT * FROM fee_cover_promises WHERE offer_index = ? AND state = 'open'", (offer_index,)
        ).fetchone()
        if promise is None:
            return None
        conn.execute(
            "UPDATE fee_cover_promises SET state = 'filled', accept_tx_hash = ?, closed_at = ?"
            " WHERE offer_index = ?",
            (decision.accept_tx_hash, ts, offer_index),
        )
        owed = decision.reason is None
        conn.execute(
            "INSERT INTO fee_cover_refunds (accept_tx_hash, offer_index, campaign_id, network, bidder, seller,"
            " broker, observed_fee_drops, observed_royalty_drops, refund_drops, state, reason, created_at,"
            " updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                decision.accept_tx_hash, offer_index, promise["campaign_id"], promise["network"],
                promise["bidder"], decision.seller, decision.broker, decision.observed_fee_drops,
                decision.observed_royalty_drops, decision.refund_drops if owed else 0,
                "owed" if owed else "declined", decision.reason, ts, ts,
            ),
        )
    return get_refund(conn, decision.accept_tx_hash)


def claim_for_payout(
    conn: sqlite3.Connection, accept_tx_hash: str, last_ledger_seq: int, now: int | None = None
) -> bool:
    """owed -> submitted in ONE conditional write that also records the
    provisional deadline. Only the caller whose update changed a row submits."""
    cursor = conn.execute(
        "UPDATE fee_cover_refunds SET state = 'submitted', last_ledger_seq = ?, updated_at = ?"
        " WHERE accept_tx_hash = ? AND state = 'owed'",
        (last_ledger_seq, _now(now), accept_tx_hash),
    )
    conn.commit()
    return cursor.rowcount == 1


def record_payout(
    conn: sqlite3.Connection,
    accept_tx_hash: str,
    *,
    state: str,
    tx_hash: str | None,
    last_ledger_seq: int | None,
    now: int | None = None,
) -> None:
    if state not in ("confirmed", "failed", "submitted"):
        raise ValueError(f"not a payout outcome: {state}")
    conn.execute(
        "UPDATE fee_cover_refunds SET state = ?, payout_tx_hash = COALESCE(?, payout_tx_hash),"
        " last_ledger_seq = COALESCE(?, last_ledger_seq),"
        " reason = CASE WHEN ? = 'failed' THEN 'payout_failed' ELSE reason END, updated_at = ?"
        " WHERE accept_tx_hash = ? AND state = 'submitted'",
        (state, tx_hash, last_ledger_seq, state, _now(now), accept_tx_hash),
    )
    conn.commit()


def return_to_owed(conn: sqlite3.Connection, accept_tx_hash: str, now: int | None = None) -> bool:
    """Only for a payout refused BEFORE anything was submitted (ClaimNotSubmitted)."""
    cursor = conn.execute(
        "UPDATE fee_cover_refunds SET state = 'owed', last_ledger_seq = NULL, updated_at = ?"
        " WHERE accept_tx_hash = ? AND state = 'submitted'",
        (_now(now), accept_tx_hash),
    )
    conn.commit()
    return cursor.rowcount == 1


def requeue_failed(conn: sqlite3.Connection, accept_tx_hash: str, now: int | None = None) -> str:
    """failed -> owed for an operator who fixed the cause. A failed payout can
    never validate later, so paying again cannot double-pay. Refused when the
    campaign budget no longer has room for it."""
    ts = _now(now)
    with _immediate(conn):
        row = conn.execute(
            "SELECT * FROM fee_cover_refunds WHERE accept_tx_hash = ?", (accept_tx_hash,)
        ).fetchone()
        if row is None or row["state"] != "failed":
            return "not_failed"
        campaign = get_campaign(conn, int(row["campaign_id"]))
        if campaign is None or committed_drops(conn, campaign.id) + int(row["refund_drops"]) > campaign.budget_drops:
            return "budget_exhausted"
        conn.execute(
            "UPDATE fee_cover_refunds SET state = 'owed', reason = NULL, payout_tx_hash = NULL,"
            " last_ledger_seq = NULL, updated_at = ? WHERE accept_tx_hash = ?",
            (ts, accept_tx_hash),
        )
    return "requeued"


def view_for_offer(conn: sqlite3.Connection, offer_index: str) -> dict[str, Any] | None:
    """The single fee_cover object the API returns: the refund's state once a
    refund row exists, the promise's state before that."""
    refund = get_refund_by_offer(conn, offer_index)
    if refund is not None:
        drops = int(refund["refund_drops"])
        return {
            "state": refund["state"],
            "drops": drops,
            "xrp": market_ops.drops_to_xrp_str(str(drops)),
            "reason": refund["reason"],
            "payout_tx_hash": refund["payout_tx_hash"],
        }
    promise = get_promise(conn, offer_index)
    if promise is None:
        return None
    drops = int(promise["promised_drops"]) if promise["state"] == "open" else 0
    return {
        "state": promise["state"],
        "drops": drops,
        "xrp": market_ops.drops_to_xrp_str(str(drops)),
        "reason": promise["reason"],
        "payout_tx_hash": None,
    }


def status_summary(conn: sqlite3.Connection, network: str, now: int | None = None) -> dict[str, Any]:
    ts = _now(now)
    campaign = current_campaign(conn, network) or latest_campaign(conn, network)
    if campaign is None:
        state = "never_started"
    elif campaign.status == "active":
        state = "active" if campaign.is_live(ts) else "expired"
    else:
        state = "stopped"
    out: dict[str, Any] = {
        "network": network,
        "state": state,
        "campaign": asdict(campaign) if campaign is not None else None,
        "committed_drops": 0,
        "paid_drops": 0,
        "remaining_drops": 0,
        "open_promises": 0,
        "refunds_by_state": {},
        "declines_by_reason": {},
        "top_wallets": [],
    }
    if campaign is None:
        return out
    cid = campaign.id
    committed = committed_drops(conn, cid)
    out["committed_drops"] = committed
    out["paid_drops"] = paid_drops(conn, cid)
    out["remaining_drops"] = max(0, campaign.budget_drops - committed)
    out["open_promises"] = int(
        conn.execute(
            "SELECT COUNT(*) FROM fee_cover_promises WHERE campaign_id = ? AND state = 'open'", (cid,)
        ).fetchone()[0]
    )
    out["refunds_by_state"] = {
        str(r[0]): int(r[1])
        for r in conn.execute(
            "SELECT state, COUNT(*) FROM fee_cover_refunds WHERE campaign_id = ? GROUP BY state", (cid,)
        )
    }
    declines: dict[str, int] = {}
    for table in ("fee_cover_promises", "fee_cover_refunds"):
        for reason, count in conn.execute(
            f"SELECT reason, COUNT(*) FROM {table} WHERE campaign_id = ? AND state = 'declined'"  # noqa: S608
            " GROUP BY reason",
            (cid,),
        ):
            declines[str(reason)] = declines.get(str(reason), 0) + int(count)
    out["declines_by_reason"] = declines
    out["top_wallets"] = [
        {"wallet": str(w), "drops": int(d)}
        for w, d in conn.execute(
            "SELECT bidder, SUM(refund_drops) FROM fee_cover_refunds"
            " WHERE campaign_id = ? AND state IN ('owed','submitted','confirmed')"
            " GROUP BY bidder ORDER BY SUM(refund_drops) DESC LIMIT 5",
            (cid,),
        )
    ]
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_fee_cover_store.py -v`
Expected: all pass (12 from Task 1 + 16 new = 28).

- [ ] **Step 5: Format, type-check, commit**

```bash
.venv/bin/ruff format lfg_core/fee_cover_store.py tests/test_fee_cover_store.py
.venv/bin/ruff check --fix lfg_core/fee_cover_store.py tests/test_fee_cover_store.py
.venv/bin/mypy lfg_core/fee_cover_store.py
git add lfg_core/fee_cover_store.py tests/test_fee_cover_store.py
git commit -m "feat(fee-cover): promises, refunds and double-pay-proof payout transitions"
```

---
### Task 3: Pure rules — listing pick, promise amount, refund from a validated accept

**Files:**
- Create: `lfg_core/fee_cover.py`
- Create: `tests/fixtures/fee_cover/cafe_brokered_accept.json`
- Modify: `lfg_core/market_store.py` (add `live_listings_for_nft`, directly after `live_listing_for_nft`)
- Test: `tests/test_fee_cover_rules.py`

**Interfaces:**
- Consumes (Tasks 1–2): `fee_cover_store.BPS`, `Campaign`, `RefundDecision`.
  Also `brokers.resolve(destination, nft_id) -> {"name","url","broker_rate"} |
  None` and `brokers.clearing_drops(ask_drops, rate) -> int`.
- Produces:
  - `ROYALTY_CEILING_BPS = 5000`
  - `RECORDED_PROMISE_DECLINES: frozenset[str]`
  - `ExternalListing(offer_index, seller, ask_drops, broker, broker_rate)`,
    frozen, with `.clearing_drops -> int`.
  - `external_listing_for(rows, owner, nft_id) -> ExternalListing | None`
  - `promise_drops(bid_drops, broker_rate, coverage_bps) -> int`
  - `promise_decline_reason(*, campaign, listing, bid_drops, bidder,
    system_wallets) -> str | None`
  - `nft_issuer(nft_id) -> str`
  - `compute_refund(tx, *, promise, payer, broker_rate_for, linked) ->
    RefundDecision`. Here `tx` is the flat `get_tx` / archive shape (tx fields
    top-level plus `meta`, `hash`, `validated`). `promise` needs the keys
    `offer_index, bidder, nft_id, promised_drops, coverage_bps`.
    `broker_rate_for` is `(account) -> float | None`, and `linked` is
    `(bidder, seller) -> bool`.
  - `market_store.live_listings_for_nft(conn, nft_id) -> list[dict]`

- [ ] **Step 1: Create the real-accept fixture**

This is the verified #426 fill: bid `37B0856D…` of 5,075,000 drops, cafe fee
80,642, royalty 349,605, seller `rLfbT5…`, buyer `rHaMsA…`. Signatures and
memos are stripped. They are not needed, and a key named `SigningPubKey` next
to hex can trip gitleaks.

```bash
mkdir -p tests/fixtures/fee_cover
.venv/bin/python - <<'PY'
import json, sqlite3
HASH = "FED6256DC27A6D06C43E20ADA2B5D2FFF4AEAEE7560FADBF584CAE6396C1ACFA"
db = sqlite3.connect("file:/home/hamsa/LFG/history_mainnet.db?mode=ro", uri=True)
(raw,) = db.execute("SELECT raw_json FROM xrpl_txs WHERE tx_hash = ?", (HASH,)).fetchone()
tx = json.loads(raw)
for key in ("Memos", "SigningPubKey", "TxnSignature"):
    tx.pop(key, None)
assert tx["validated"] is True and tx["meta"]["TransactionResult"] == "tesSUCCESS"
with open("tests/fixtures/fee_cover/cafe_brokered_accept.json", "w") as f:
    f.write(json.dumps(tx, indent=2, sort_keys=True) + "\n")
PY
```

If `~/LFG/history_mainnet.db` is unavailable, fetch the same transaction from a
public mainnet node instead:
`curl -s -X POST https://xrplcluster.com/ -H 'Content-Type: application/json'
-d '{"method":"tx","params":[{"transaction":"FED6256D…ACFA","binary":false,"api_version":1}]}'`.
Write `result` with the same three keys popped.

- [ ] **Step 2: Write the failing tests**

```python
# tests/test_fee_cover_rules.py
"""Pure fee-cover rules (spec §Promise eligibility, §Refund computation)."""

from __future__ import annotations

import copy
import json
import sqlite3
from pathlib import Path

import pytest

from lfg_core import fee_cover, market_store
from lfg_core.fee_cover_store import Campaign

FIXTURE = Path(__file__).parent / "fixtures" / "fee_cover" / "cafe_brokered_accept.json"
CAFE = "rpx9JThQ2y37FaGeeJP7PXDUVEXY3PHZSC"
BIDDS = "rpZqTPC8GvrSvEfFsUuHkmPCg29GdQuXhC"
ISSUER = "rLfgoMintj3KBcs4s2XKtquvDwEte2kYfJ"
SELLER = "rLfbT5Vkbigi4gFUi1QUGu5udijV79i3tF"
BUYER = "rHaMsAjoAN21s1XG5TCAM6ErAefzrggsHf"
NFT_ID = "00191B58D1AE1BC312BEF9C68233FB0C8CF6A338F7C227BECADA906904943EEE"
BUY_OFFER = "37B0856D51DF75221A9140AF8FCD89E1AC3CAC703D36070BA7E2FE85567C4B7D"


@pytest.fixture(autouse=True)
def _no_clearing_buffer(monkeypatch):
    monkeypatch.delenv("BROKER_CLEARING_BUFFER_DROPS", raising=False)
    monkeypatch.delenv("BROKER_ALLOWLIST_PATH", raising=False)


def _tx():
    return json.loads(FIXTURE.read_text())


def _promise(**over):
    base = {"offer_index": BUY_OFFER, "bidder": BUYER, "nft_id": NFT_ID, "promised_drops": 80_642, "coverage_bps": 10_000}
    base.update(over)
    return base


def _rate(account):
    return {CAFE: 0.01589}.get(account)


def _refund(tx=None, *, promise=None, payer=ISSUER, rate=_rate, linked=lambda a, b: False):
    return fee_cover.compute_refund(
        tx if tx is not None else _tx(), promise=promise or _promise(), payer=payer, broker_rate_for=rate, linked=linked
    )


def _campaign(**over):
    base = {
        "id": 1, "network": "mainnet", "status": "active", "coverage_bps": 10_000, "budget_drops": 50_000_000,
        "wallet_cap_drops": 5_000_000, "wallet_window_seconds": 2_592_000, "min_bid_drops": 1_000_000,
        "started_at": 1, "started_by": "x", "ends_at": None, "stopped_at": None, "stopped_by": None,
    }
    base.update(over)
    return Campaign(**base)


def _row(offer_index, *, seller=SELLER, drops=4_990_000, destination=CAFE):
    return {"offer_index": offer_index, "seller": seller, "amount_drops": drops, "destination": destination}


# --- listing pick / promise ------------------------------------------------


def test_external_listing_for_picks_the_cheapest_measured_broker_row_by_the_owner():
    rows = [
        _row("PLAIN", destination=None),
        _row("OTHER_SELLER", seller="rSomeoneElse"),
        _row("BIDDS_UNMEASURED", drops=1_000_000, destination=BIDDS),
        _row("CAFE_DEAR", drops=6_000_000),
        _row("CAFE_CHEAP", drops=4_990_000),
        _row("UNKNOWN_DEST", drops=1, destination="rPrivatePeer"),
    ]
    listing = fee_cover.external_listing_for(rows, SELLER, NFT_ID)
    assert listing == fee_cover.ExternalListing("CAFE_CHEAP", SELLER, 4_990_000, CAFE, 0.01589)
    assert listing.clearing_drops == 5_070_572
    assert fee_cover.external_listing_for([_row("PLAIN", destination=None)], SELLER, NFT_ID) is None


def test_promise_drops_matches_the_verified_cafe_fee():
    assert fee_cover.promise_drops(5_075_000, 0.01589, 10_000) == 80_642
    assert fee_cover.promise_drops(5_075_000, 0.01589, 5_000) == 40_321
    assert fee_cover.promise_drops(5_075_000, 0.01589, 0) == 0


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"campaign": None}, "campaign_inactive"),
        ({"listing": None}, "not_external_listing"),
        ({"bidder": "rSystem"}, "system_wallet"),
        ({"bid_drops": 5_070_571}, "below_clearing"),
        ({"campaign": _campaign(min_bid_drops=6_000_000)}, "below_min_bid"),
        ({}, None),
    ],
)
def test_promise_decline_reason(kwargs, expected):
    args = {
        "campaign": _campaign(),
        "listing": fee_cover.ExternalListing("L", SELLER, 4_990_000, CAFE, 0.01589),
        "bid_drops": 5_070_572,
        "bidder": BUYER,
        "system_wallets": frozenset({"rSystem"}),
    }
    args.update(kwargs)
    assert fee_cover.promise_decline_reason(**args) == expected


def test_nft_issuer_decodes_the_token_id():
    assert fee_cover.nft_issuer(NFT_ID) == ISSUER


# --- refund computation ------------------------------------------------------


def test_real_cafe_fill_refunds_the_observed_fee():
    decision = _refund()
    assert decision.reason is None
    assert decision.refund_drops == 80_642
    assert decision.observed_fee_drops == 80_642
    assert decision.observed_royalty_drops == 349_605
    assert decision.seller == SELLER and decision.broker == CAFE
    assert decision.accept_tx_hash.startswith("FED6256D")


def test_coverage_is_taken_from_the_promise_snapshot():
    assert _refund(promise=_promise(coverage_bps=5_000)).refund_drops == 40_321


def test_refund_never_exceeds_the_promise():
    assert _refund(promise=_promise(promised_drops=10_000)).refund_drops == 10_000


def test_fake_broker_inflated_fee_is_capped_at_half_the_royalty():
    tx = _tx()
    tx["NFTokenBrokerFee"] = "1000000"
    decision = _refund(tx, promise=_promise(promised_drops=10_000_000))
    assert decision.refund_drops == 174_802  # 349_605 * 5000 // 10000


def test_unallowlisted_broker_is_not_broker_settled():
    assert _refund(rate=lambda account: None).reason == "not_broker_settled"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda tx: tx.pop("NFTokenBuyOffer"),
        lambda tx: tx.__setitem__("validated", False),
        lambda tx: tx["meta"].__setitem__("TransactionResult", "tecNO_PERMISSION"),
        lambda tx: tx.__setitem__("TransactionType", "Payment"),
        lambda tx: tx.__setitem__("NFTokenBuyOffer", "F" * 64),
    ],
)
def test_non_brokered_or_mismatched_accepts_are_declined(mutate):
    tx = _tx()
    mutate(tx)
    assert _refund(tx).reason == "not_broker_settled"


def test_wrong_bidder_is_not_broker_settled():
    assert _refund(promise=_promise(bidder="rNotTheBidder")).reason == "not_broker_settled"


def test_iou_broker_fee_is_unobserved():
    tx = _tx()
    tx["NFTokenBrokerFee"] = {"currency": "BRIX", "issuer": ISSUER, "value": "1"}
    assert _refund(tx).reason == "fee_unobserved"


def test_issuer_as_seller_is_declined():
    tx = _tx()
    for node in tx["meta"]["AffectedNodes"]:
        deleted = node.get("DeletedNode")
        if deleted and deleted.get("LedgerEntryType") == "NFTokenOffer" and int(deleted["FinalFields"]["Flags"]) & 1:
            deleted["FinalFields"]["Owner"] = ISSUER
    assert _refund(tx).reason == "issuer_party"


def test_missing_royalty_node_is_unobserved():
    tx = _tx()
    tx["meta"]["AffectedNodes"] = [
        n for n in tx["meta"]["AffectedNodes"]
        if (n.get("ModifiedNode") or {}).get("FinalFields", {}).get("Account") != ISSUER
    ]
    assert _refund(tx).reason == "royalty_unobserved"


def test_payer_that_is_not_the_issuer_is_unobserved():
    assert _refund(payer="rSomeOtherSigner").reason == "royalty_unobserved"


def test_linked_counterparties_are_declined():
    seen = []

    def linked(a, b):
        seen.append((a, b))
        return True

    assert _refund(linked=linked).reason == "linked_counterparty"
    assert seen == [(BUYER, SELLER)]


def test_zero_coverage_is_a_zero_refund():
    assert _refund(promise=_promise(coverage_bps=0)).reason == "zero_refund"


def test_declines_carry_zero_drops_and_do_not_mutate_input():
    tx = _tx()
    before = copy.deepcopy(tx)
    decision = _refund(tx, rate=lambda account: None)
    assert decision.refund_drops == 0
    assert tx == before


# --- market_store.live_listings_for_nft ------------------------------------


def test_live_listings_for_nft_returns_plain_and_destination_rows():
    conn = sqlite3.connect(":memory:")
    market_store.init_db(conn)
    for offer_index, destination, live in (("A", None, 1), ("B", CAFE, 1), ("C", CAFE, 0)):
        conn.execute(
            "INSERT INTO market_listings (offer_index, nft_id, kind, seller, amount_drops, destination, is_live)"
            " VALUES (?, ?, 'character', ?, 1000000, ?, ?)",
            (offer_index, NFT_ID, SELLER, destination, live),
        )
    rows = market_store.live_listings_for_nft(conn, NFT_ID)
    assert [r["offer_index"] for r in rows] == ["A", "B"]
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_fee_cover_rules.py -v`
Expected: FAIL with `ImportError: cannot import name 'fee_cover'`

- [ ] **Step 4: Write the implementation**

Add to `lfg_core/market_store.py`, directly below `live_listing_for_nft`:

```python
def live_listings_for_nft(conn: sqlite3.Connection, nft_id: str) -> list[dict[str, Any]]:
    """EVERY live listing row for `nft_id` — plain and destination-locked
    (the fee cover needs the external broker row that live_listing_for_nft's
    single-row answer may not return)."""
    conn.row_factory = sqlite3.Row
    return [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM market_listings WHERE nft_id = ? AND is_live = 1 ORDER BY offer_index",
            (nft_id,),
        )
    ]
```

Create `lfg_core/fee_cover.py`:

```python
# lfg_core/fee_cover.py
"""Pure fee-cover rules — who gets a promise, how much, and what a validated
accept refunds. No I/O: callers pass rows, the tx, and small callables.

Spec: docs/superpowers/specs/2026-09-14-marketplace-fee-cover-design.md.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from xrpl.core.addresscodec import encode_classic_address

from lfg_core import brokers
from lfg_core.fee_cover_store import BPS, Campaign, RefundDecision

# A refund is always strictly below the royalty received on the same sale, so
# no pattern of trades — including self-dealing through a fake broker account
# that sets NFTokenBrokerFee to the whole spread — can make LFG a net payer.
# A code constant on purpose: never an admin knob.
ROYALTY_CEILING_BPS = 5000

RECORDED_PROMISE_DECLINES = frozenset(
    {"system_wallet", "below_clearing", "below_min_bid", "budget_exhausted", "wallet_cap"}
)

_LSF_SELL_NFTOKEN = 0x00000001


@dataclass(frozen=True)
class ExternalListing:
    offer_index: str
    seller: str
    ask_drops: int
    broker: str
    broker_rate: float

    @property
    def clearing_drops(self) -> int:
        return brokers.clearing_drops(self.ask_drops, self.broker_rate)


def external_listing_for(
    rows: Sequence[Mapping[str, Any]], owner: str, nft_id: str
) -> ExternalListing | None:
    """The cheapest live listing by `owner` whose destination is an allowlisted
    broker with a MEASURED rate — the listing a Buy-now bid clears against."""
    best: ExternalListing | None = None
    for row in rows:
        destination = row.get("destination")
        drops = row.get("amount_drops")
        if not destination or row.get("seller") != owner:
            continue
        if not isinstance(drops, int) or isinstance(drops, bool) or drops <= 0:
            continue
        resolved = brokers.resolve(str(destination), nft_id)
        if resolved is None or resolved.get("broker_rate") is None:
            continue
        candidate = ExternalListing(
            str(row["offer_index"]), owner, drops, str(destination), float(resolved["broker_rate"])
        )
        if best is None or candidate.ask_drops < best.ask_drops:
            best = candidate
    return best


def promise_drops(bid_drops: int, broker_rate: float, coverage_bps: int) -> int:
    """The most this bid can be refunded: the broker's fee on it (rounded UP,
    exactly as cafe computes it) times coverage."""
    return math.ceil(bid_drops * broker_rate) * coverage_bps // BPS


def promise_decline_reason(
    *,
    campaign: Campaign | None,
    listing: ExternalListing | None,
    bid_drops: int,
    bidder: str,
    system_wallets: Collection[str],
) -> str | None:
    """Pure promise eligibility. Budget and wallet-cap headroom are decided in
    the store, inside its write lock."""
    if campaign is None:
        return "campaign_inactive"
    if listing is None:
        return "not_external_listing"
    if bidder in system_wallets:
        return "system_wallet"
    if bid_drops < listing.clearing_drops:
        return "below_clearing"
    if bid_drops < campaign.min_bid_drops:
        return "below_min_bid"
    return None


def nft_issuer(nft_id: str) -> str:
    """The issuer AccountID encoded in bytes 4..24 of an NFTokenID."""
    return str(encode_classic_address(bytes.fromhex(nft_id[8:48])))


def _deleted_offers(meta: Mapping[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    sell: dict[str, Any] | None = None
    buy: dict[str, Any] | None = None
    for node in meta.get("AffectedNodes") or []:
        deleted = node.get("DeletedNode") if isinstance(node, Mapping) else None
        if not isinstance(deleted, Mapping) or deleted.get("LedgerEntryType") != "NFTokenOffer":
            continue
        fields = dict(deleted.get("FinalFields") or {})
        fields["LedgerIndex"] = deleted.get("LedgerIndex")
        if int(fields.get("Flags") or 0) & _LSF_SELL_NFTOKEN:
            sell = fields
        else:
            buy = fields
    return sell, buy


def _balance_delta(meta: Mapping[str, Any], account: str) -> int | None:
    for node in meta.get("AffectedNodes") or []:
        modified = node.get("ModifiedNode") if isinstance(node, Mapping) else None
        if not isinstance(modified, Mapping) or modified.get("LedgerEntryType") != "AccountRoot":
            continue
        final = modified.get("FinalFields") or {}
        if final.get("Account") != account:
            continue
        previous = modified.get("PreviousFields") or {}
        try:
            return int(final["Balance"]) - int(previous["Balance"])
        except (KeyError, TypeError, ValueError):
            return None
    return None


def compute_refund(
    tx: Mapping[str, Any],
    *,
    promise: Mapping[str, Any],
    payer: str,
    broker_rate_for: Callable[[str], float | None],
    linked: Callable[[str, str], bool],
) -> RefundDecision:
    """What a validated accept refunds against its promise — derived from the
    transaction and its metadata, never from any listed price."""
    tx_hash = str(tx.get("hash") or "")
    raw_meta = tx.get("meta")
    meta: Mapping[str, Any] = raw_meta if isinstance(raw_meta, Mapping) else {}
    broker = tx.get("Account") if isinstance(tx.get("Account"), str) else None
    sell, buy = _deleted_offers(meta)
    seller = sell.get("Owner") if sell else None
    bidder = str(promise["bidder"])

    def decline(reason: str, *, fee: int | None = None, royalty: int | None = None) -> RefundDecision:
        return RefundDecision(tx_hash, seller, broker, fee, royalty, 0, reason)

    brokered = (
        tx.get("validated") is True
        and meta.get("TransactionResult") == "tesSUCCESS"
        and tx.get("TransactionType") == "NFTokenAcceptOffer"
        and bool(tx.get("NFTokenSellOffer"))
        and tx.get("NFTokenBuyOffer") == promise["offer_index"]
        and sell is not None
        and buy is not None
        and buy.get("Owner") == bidder
        and broker is not None
        and broker_rate_for(broker) is not None
    )
    if not brokered:
        return decline("not_broker_settled")
    raw_fee = tx.get("NFTokenBrokerFee")
    if not isinstance(raw_fee, str) or not raw_fee.isdigit():
        return decline("fee_unobserved")
    fee = int(raw_fee)
    issuer = nft_issuer(str(promise["nft_id"]))
    if issuer in (seller, bidder):
        return decline("issuer_party", fee=fee)
    royalty = _balance_delta(meta, issuer)
    if issuer != payer or royalty is None or royalty <= 0:
        return decline("royalty_unobserved", fee=fee, royalty=royalty)
    if seller and linked(bidder, str(seller)):
        return decline("linked_counterparty", fee=fee, royalty=royalty)
    refund = min(
        int(promise["promised_drops"]),
        fee * int(promise["coverage_bps"]) // BPS,
        royalty * ROYALTY_CEILING_BPS // BPS,
    )
    if refund < 1:
        return decline("zero_refund", fee=fee, royalty=royalty)
    return RefundDecision(tx_hash, seller, broker, fee, royalty, refund, None)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_fee_cover_rules.py tests/test_market_bids.py tests/test_market_api.py -q`
Expected: all pass. The new file has 28 tests; the market suites are
unchanged.

- [ ] **Step 6: Format, type-check, commit**

```bash
.venv/bin/ruff format lfg_core/fee_cover.py lfg_core/market_store.py tests/test_fee_cover_rules.py
.venv/bin/ruff check --fix lfg_core/fee_cover.py lfg_core/market_store.py tests/test_fee_cover_rules.py
.venv/bin/mypy lfg_core/fee_cover.py lfg_core/market_store.py
git add lfg_core/fee_cover.py lfg_core/market_store.py tests/test_fee_cover_rules.py tests/fixtures/fee_cover/cafe_brokered_accept.json
git commit -m "feat(fee-cover): pure promise and refund rules from the validated accept"
```

---
### Task 4: Memo action + XRPL payout, recovery lookup, balance helper

**Files:**
- Modify: `lfg_core/memos.py` (add `ACTION_FEE_COVER` and include it in `_ACTIONS`)
- Modify: `lfg_core/config.py` (add `FEE_COVER_LEDGER_MARGIN` directly under `BRIX_CLAIM_LEDGER_MARGIN`)
- Modify: `lfg_core/xrpl_ops.py`:
  - Extract a shared account_tx memo scan from `find_claim_payment`.
  - Add `fee_cover_memo_tag`, `send_fee_cover_refund`,
    `_is_genuine_xrp_refund`, `find_fee_cover_payment` and
    `get_xrp_balance_drops`.
- Test: `tests/test_fee_cover_xrpl.py`

**Interfaces:**
- Consumes: the existing `ClaimPayment(state, tx_hash, last_ledger_seq)` and
  `ClaimNotSubmitted`. They are reused as-is: the tri-state contract is
  identical.
- Produces:
  - `memos.ACTION_FEE_COVER = "fee-cover"`
  - `config.FEE_COVER_LEDGER_MARGIN: int`
  - `xrpl_ops.fee_cover_memo_tag(accept_tx_hash) -> str`
  - `async xrpl_ops.send_fee_cover_refund(destination, drops, accept_tx_hash,
    *, campaign_id, max_last_ledger_seq=None) -> ClaimPayment`
  - `async xrpl_ops.find_fee_cover_payment(accept_tx_hash, *, destination,
    drops, min_ledger) -> str | None`
  - `async xrpl_ops.get_xrp_balance_drops(address) -> int | None`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_fee_cover_xrpl.py
"""Fee-cover refund Payment, its recovery lookup, and the balance helper."""

from __future__ import annotations

import asyncio

import pytest

from lfg_core import config, memos, xrpl_ops

ACCEPT = "FED6256DC27A6D06C43E20ADA2B5D2FFF4AEAEE7560FADBF584CAE6396C1ACFA"
BUYER = "rHaMsAjoAN21s1XG5TCAM6ErAefzrggsHf"


def _run(coro):
    # A private loop: never disturbs (or depends on) the global event loop,
    # which asyncio.run() in other test modules leaves unset on Python 3.10.
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _decoded(tx):
    return [bytes.fromhex(m.memo_data).decode() for m in tx.memos]


class _Captured:
    tx = None


@pytest.fixture()
def capture(monkeypatch):
    box = _Captured()

    async def fake_submit(tx, wallet, client, label, **kwargs):
        box.tx = tx
        return {"hash": "PAYOUTHASH", "meta": {"TransactionResult": "tesSUCCESS"}}

    async def fake_ledger(client):
        return 1000

    monkeypatch.setattr(xrpl_ops, "_submit_and_confirm", fake_submit)
    monkeypatch.setattr(xrpl_ops, "_current_validated_ledger_index", fake_ledger)
    return box


def test_fee_cover_is_a_valid_memo_action():
    models = memos.build_memo_models(
        memos.INITIATOR_BACKEND, memos.PLATFORM_BACKEND, memos.ACTION_FEE_COVER, campaign="fee-cover-7"
    )
    assert "fee-cover" in [bytes.fromhex(m.memo_data).decode() for m in models]


def test_send_fee_cover_refund_builds_a_tagged_xrp_payment_from_the_signer(capture):
    result = _run(xrpl_ops.send_fee_cover_refund(BUYER, 80_642, ACCEPT, campaign_id=7))
    tx = capture.tx
    assert tx.account == config.SIGNING_ACCOUNT
    assert tx.destination == BUYER
    assert tx.amount == "80642"
    assert tx.source_tag == 2606160021
    assert tx.last_ledger_sequence == 1000 + config.FEE_COVER_LEDGER_MARGIN
    decoded = _decoded(tx)
    assert f"lfg:fee_cover:{ACCEPT}" in decoded
    assert "fee-cover" in decoded and "fee-cover-7" in decoded
    assert (result.state, result.tx_hash, result.last_ledger_seq) == ("confirmed", "PAYOUTHASH", tx.last_ledger_sequence)


def test_send_fee_cover_refund_never_outlives_the_recorded_deadline(capture):
    result = _run(xrpl_ops.send_fee_cover_refund(BUYER, 1, ACCEPT, campaign_id=7, max_last_ledger_seq=1005))
    assert capture.tx.last_ledger_sequence == 1005 == result.last_ledger_seq


def test_send_fee_cover_refund_maps_failed_and_unknown(monkeypatch, capture):
    async def failed(tx, wallet, client, label, **kwargs):
        return None

    monkeypatch.setattr(xrpl_ops, "_submit_and_confirm", failed)
    assert _run(xrpl_ops.send_fee_cover_refund(BUYER, 1, ACCEPT, campaign_id=7)).state == "failed"

    async def indeterminate(tx, wallet, client, label, **kwargs):
        raise xrpl_ops.IndeterminateResultError("timeout")

    monkeypatch.setattr(xrpl_ops, "_submit_and_confirm", indeterminate)
    unknown = _run(xrpl_ops.send_fee_cover_refund(BUYER, 1, ACCEPT, campaign_id=7))
    assert unknown.state == "unknown" and unknown.last_ledger_seq is not None


def test_send_fee_cover_refund_refuses_before_submitting(monkeypatch, capture):
    async def no_ledger(client):
        return None

    with pytest.raises(xrpl_ops.ClaimNotSubmitted):
        _run(xrpl_ops.send_fee_cover_refund(BUYER, 0, ACCEPT, campaign_id=7))
    monkeypatch.setattr(xrpl_ops, "_current_validated_ledger_index", no_ledger)
    with pytest.raises(xrpl_ops.ClaimNotSubmitted):
        _run(xrpl_ops.send_fee_cover_refund(BUYER, 1, ACCEPT, campaign_id=7))
    assert capture.tx is None


def _entry(*, account=None, destination=BUYER, delivered="80642", result="tesSUCCESS", validated=True,
           tx_type="Payment", memo=f"lfg:fee_cover:{ACCEPT}", tx_hash="PAYOUTHASH"):
    return {
        "hash": tx_hash,
        "validated": validated,
        "meta": {"TransactionResult": result, "delivered_amount": delivered},
        "tx": {
            "TransactionType": tx_type,
            "Account": account or config.SIGNING_ACCOUNT,
            "Destination": destination,
            "Amount": delivered,
            "Memos": [{"Memo": {"MemoData": memo.encode().hex().upper()}}],
        },
    }


@pytest.fixture()
def account_tx(monkeypatch):
    box = {"entries": []}

    class _Resp:
        def __init__(self, result):
            self.result = result

    def fake_request(self, request):
        return _Resp({"transactions": box["entries"], "marker": None})

    monkeypatch.setattr(xrpl_ops.JsonRpcClient, "request", fake_request, raising=False)
    return box


def _find():
    return _run(xrpl_ops.find_fee_cover_payment(ACCEPT, destination=BUYER, drops=80_642, min_ledger=6000))


def test_find_fee_cover_payment_accepts_a_genuine_refund(account_tx):
    account_tx["entries"] = [_entry()]
    assert _find() == "PAYOUTHASH"


@pytest.mark.parametrize(
    "overrides",
    [
        {"account": "rStrangerSendingAForgedMemo"},
        {"destination": "rSomeoneElse"},
        {"delivered": "80641"},
        {"result": "tecUNFUNDED_PAYMENT"},
        {"validated": False},
        {"tx_type": "AccountSet"},
        {"memo": "lfg:fee_cover:OTHER"},
    ],
)
def test_find_fee_cover_payment_rejects_anything_not_our_refund(account_tx, overrides):
    account_tx["entries"] = [_entry(**overrides)]
    assert _find() is None


def test_get_xrp_balance_drops(monkeypatch):
    class _Resp:
        def __init__(self, ok, result):
            self._ok = ok
            self.result = result

        def is_successful(self):
            return self._ok

    responses = [_Resp(True, {"account_data": {"Balance": "215611627"}}), _Resp(False, {"error": "actNotFound"})]

    async def fake_request(self, request):
        return responses.pop(0)

    monkeypatch.setattr(xrpl_ops.AsyncJsonRpcClient, "request", fake_request, raising=False)
    assert _run(xrpl_ops.get_xrp_balance_drops("rIssuer")) == 215_611_627
    assert _run(xrpl_ops.get_xrp_balance_drops("rIssuer")) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_fee_cover_xrpl.py -v`
Expected: FAIL with `AttributeError: module 'lfg_core.memos' has no attribute 'ACTION_FEE_COVER'`

- [ ] **Step 3: Write the implementation**

`lfg_core/memos.py`: after `ACTION_BRIX_CLAIM = "brix-claim"  # #48 …`, add:

```python
ACTION_FEE_COVER = "fee-cover"  # marketplace fee-cover refund from the issuer (spec 2026-09-14)
```

and add `ACTION_FEE_COVER,` to the `_ACTIONS` frozenset (after `ACTION_BRIX_CLAIM,`).

`lfg_core/config.py`: directly under the `BRIX_CLAIM_LEDGER_MARGIN = …` line, add:

```python
# Ledgers of LastLedgerSequence headroom on a fee-cover refund Payment — what
# makes an indeterminate refund decidable by recovery.
FEE_COVER_LEDGER_MARGIN = int(os.getenv("FEE_COVER_LEDGER_MARGIN", "40"))
```

`lfg_core/xrpl_ops.py`: replace the body of `find_claim_payment` (from
`sender = distributor_address()` to the end of the function) with a call to a
new shared scanner. Define the scanner directly above `find_claim_payment`:

```python
async def _find_memo_tagged_tx(
    sender: str,
    tag: str,
    is_genuine: Callable[[dict[str, Any]], bool],
    min_ledger: int | None,
    label: str,
) -> str | None:
    """Hash of the first `sender` account_tx entry carrying memo `tag` that
    `is_genuine` accepts, or None if absent. Shared by BRIX claim and fee-cover
    recovery: the memo makes a payout findable, `is_genuine` makes it
    trustworthy (memos are user-writable). Absence is NOT proof of failure."""
    client = JsonRpcClient(config.JSON_RPC_URL)
    # Bounded: a payout cannot predate the ledger window it was built for.
    scan_from = -1 if min_ledger is None else max(1, min_ledger - _CLAIM_SCAN_LEDGER_SLACK)
    marker: Any = None
    while True:
        request = AccountTx(account=sender, limit=200, marker=marker, ledger_index_min=scan_from)
        response = await asyncio.to_thread(client.request, request)
        result = response.result
        if not isinstance(result, dict):
            raise RuntimeError(f"account_tx returned an unexpected payload: {result!r}")
        for entry in result.get("transactions", []):
            tx = entry.get("tx") or entry.get("tx_json") or {}
            if not _carries_memo(tx, tag):
                continue
            if not is_genuine(entry):
                logging.warning(
                    "%s: ignoring a transaction carrying memo %s that is not a genuine payout",
                    label,
                    tag,
                )
                continue
            tx_hash = entry.get("hash") or tx.get("hash")
            if not isinstance(tx_hash, str) or not tx_hash:
                # Real and validated but unnameable. Returning None would read
                # as "absent", which past LastLedgerSequence means "failed" —
                # and that would pay twice.
                raise RuntimeError(f"{label}: payout for {tag} found on-ledger but carries no usable hash")
            return tx_hash
        marker = result.get("marker")
        if not marker:
            return None
```

and make `find_claim_payment` delegate (keep its signature and docstring):

```python
    sender = distributor_address()
    return await _find_memo_tagged_tx(
        sender,
        claim_memo_tag(claim_id),
        lambda entry: _is_genuine_claim_payout(entry, wallet, amount, sender),
        min_ledger,
        f"find_claim_payment(claim {claim_id})",
    )
```

`Callable` is already imported in `xrpl_ops.py`; if not, import it from
`collections.abc`. Then append the fee-cover helpers at the end of the BRIX
claim section (after `find_claim_payment`):

```python
# --- Marketplace fee cover (spec 2026-09-14-marketplace-fee-cover-design.md) ---


def fee_cover_memo_tag(accept_tx_hash: str) -> str:
    """The on-chain marker that makes a fee-cover refund self-identifying."""
    return f"lfg:fee_cover:{accept_tx_hash}"


async def send_fee_cover_refund(
    destination: str,
    drops: int,
    accept_tx_hash: str,
    *,
    campaign_id: int,
    max_last_ledger_seq: int | None = None,
) -> ClaimPayment:
    """Refund `drops` XRP from the signing account (mainnet: the NFT issuer via
    its regular key — the account the royalty landed in) to `destination`.

    Same tri-state contract as send_brix_claim. LastLedgerSequence is pinned
    before submission and clamped to the deadline the caller already recorded,
    so the stored deadline is an upper bound at every instant."""
    if int(drops) <= 0:
        raise ClaimNotSubmitted("a fee-cover refund must be at least 1 drop")
    wallet = Wallet.from_seed(config.SEED)
    client = JsonRpcClient(config.JSON_RPC_URL)
    current = await _current_validated_ledger_index(client)
    if current is None:
        raise ClaimNotSubmitted("could not read the validated ledger index; refusing to submit a refund")
    last_ledger_seq = current + config.FEE_COVER_LEDGER_MARGIN
    if max_last_ledger_seq is not None:
        last_ledger_seq = min(last_ledger_seq, max_last_ledger_seq)

    payment = Payment(
        account=config.SIGNING_ACCOUNT,  # explicit: SEED is a regular key on mainnet
        destination=destination,
        amount=str(int(drops)),
        source_tag=config.SOURCE_TAG,
        last_ledger_sequence=last_ledger_seq,
        memos=[
            *memos.build_memo_models(
                memos.INITIATOR_BACKEND,
                memos.PLATFORM_BACKEND,
                memos.ACTION_FEE_COVER,
                campaign=f"fee-cover-{int(campaign_id)}",
            ),
            Memo(memo_data=fee_cover_memo_tag(accept_tx_hash).encode().hex().upper()),
        ],
    )
    try:
        result = await _submit_and_confirm(payment, wallet, client, "send_fee_cover_refund")
    except IndeterminateResultError:
        logging.warning("send_fee_cover_refund: %s outcome indeterminate", accept_tx_hash)
        return ClaimPayment("unknown", None, last_ledger_seq)
    except asyncio.CancelledError:
        raise
    except Exception:
        logging.exception("send_fee_cover_refund: %s raised after the deadline was set", accept_tx_hash)
        return ClaimPayment("unknown", None, last_ledger_seq)
    if result is None:
        return ClaimPayment("failed", None, last_ledger_seq)
    tx_hash = result.get("hash")
    return ClaimPayment("confirmed", tx_hash if isinstance(tx_hash, str) else None, last_ledger_seq)


def _is_genuine_xrp_refund(entry: dict[str, Any], destination: str, drops: int, sender: str) -> bool:
    if not entry.get("validated"):
        return False
    meta = entry.get("meta") or entry.get("metaData") or {}
    if not isinstance(meta, dict) or meta.get("TransactionResult") != "tesSUCCESS":
        return False
    tx = entry.get("tx") or entry.get("tx_json") or {}
    if tx.get("TransactionType") != "Payment" or tx.get("Account") != sender:
        return False
    if tx.get("Destination") != destination:
        return False
    delivered = meta.get("delivered_amount", tx.get("DeliverMax", tx.get("Amount")))
    return isinstance(delivered, str) and delivered.isdigit() and int(delivered) == int(drops)


async def find_fee_cover_payment(
    accept_tx_hash: str, *, destination: str, drops: int, min_ledger: int | None
) -> str | None:
    """Hash of the genuine on-ledger refund for `accept_tx_hash`, or None."""
    sender = config.SIGNING_ACCOUNT
    return await _find_memo_tagged_tx(
        sender,
        fee_cover_memo_tag(accept_tx_hash),
        lambda entry: _is_genuine_xrp_refund(entry, destination, drops, sender),
        min_ledger,
        f"find_fee_cover_payment({accept_tx_hash})",
    )


async def get_xrp_balance_drops(address: str) -> int | None:
    """Total XRP balance of `address` in drops (not net of reserve), or None
    when the lookup failed — the admin status shows it next to budget left."""
    try:
        client = AsyncJsonRpcClient(config.JSON_RPC_URL)
        response = await client.request(AccountInfo(account=address, ledger_index="validated"))
    except Exception as e:
        logging.warning(f"get_xrp_balance_drops({address}) lookup failed: {e}")
        return None
    if not response.is_successful():
        return None
    balance = (response.result.get("account_data") or {}).get("Balance")
    return int(balance) if isinstance(balance, str) and balance.isdigit() else None
```

- [ ] **Step 4: Run tests to verify they pass, including the refactored claim lookup**

Run: `.venv/bin/python -m pytest tests/test_fee_cover_xrpl.py tests/test_brix_claim_flow.py tests/test_memos.py -q`
Expected: all pass. `test_fee_cover_xrpl.py` has 14 tests. The BRIX claim
tests must be unchanged; they pin the extracted scanner.

- [ ] **Step 5: Format, type-check, commit**

```bash
.venv/bin/ruff format lfg_core/memos.py lfg_core/config.py lfg_core/xrpl_ops.py tests/test_fee_cover_xrpl.py
.venv/bin/ruff check --fix lfg_core/memos.py lfg_core/config.py lfg_core/xrpl_ops.py tests/test_fee_cover_xrpl.py
.venv/bin/mypy lfg_core/xrpl_ops.py lfg_core/memos.py lfg_core/config.py
git add lfg_core/memos.py lfg_core/config.py lfg_core/xrpl_ops.py tests/test_fee_cover_xrpl.py
git commit -m "feat(fee-cover): tagged XRP refund payment with memo-matched recovery"
```

---
### Task 5: Settlement — archive lookup, settle, pay, recover, sweep

**Files:**
- Create: `lfg_core/fee_cover_settle.py`
- Test: `tests/test_fee_cover_settle.py`

**Interfaces:**
- Consumes:
  - Tasks 1–2: `fee_cover_store.connect`, `get_promise`, `open_promises`,
    `view_for_offer`, `fill_promise`, `release_promise`, `get_refund`,
    `refunds_in_state`, `claim_for_payout`, `record_payout`, `return_to_owed`,
    `start_campaign`, `record_promise`, `PromiseInput`, `Knobs`.
  - Task 3: `fee_cover.compute_refund`.
  - Task 4: `xrpl_ops.ClaimPayment`, `xrpl_ops.ClaimNotSubmitted`.
  - Existing: `history_store.init_history_db`, `insert_tx`,
    `insert_nft_event`, `get_archive_state`.
- Produces:
  - `RIPPLE_EPOCH_OFFSET = 946_684_800`, `ACCEPT_LOOKBACK_SECONDS = 3600`
  - `FeeCoverDeps(network, app_db_path, history_db_path, payer,
    ledger_margin, get_tx, send_refund, find_refund_payment, current_ledger,
    broker_rate_for, linked, now=_unix_now)`
  - Pure archive helpers:
    - `normalize_tx(result) -> dict`, which flattens an API v2 `tx_json`.
    - `find_accept_tx(hconn, nft_id, offer_index, since_unix) -> dict | None`
    - `cancel_seen(hconn, nft_id, offer_index) -> bool`
    - `archive_blocker(hconn, network, unix_ts) -> str | None`
  - Async orchestration:
    - `settle_promise(deps, offer_index) -> dict | None`, which returns the
      fee-cover view.
    - `pay_refund(deps, accept_tx_hash) -> str`, which returns the resulting
      state (`"missing"` when there is no row).
    - `recover_refunds(deps) -> dict[str, str]`
    - `sweep_once(deps, *, attempts: dict[str, int], max_attempts: int,
      on_giveup: Callable[[dict], None]) -> SweepReport`
  - `SweepReport(settled, released, paid, recovered)`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_fee_cover_settle.py
"""Fee-cover settlement orchestration with injected ledger fakes."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from lfg_core import fee_cover_settle as settle
from lfg_core import fee_cover_store as store
from lfg_core import history_store, xrpl_ops

FIXTURE = Path(__file__).parent / "fixtures" / "fee_cover" / "cafe_brokered_accept.json"
NET = "mainnet"
CAFE = "rpx9JThQ2y37FaGeeJP7PXDUVEXY3PHZSC"
ISSUER = "rLfgoMintj3KBcs4s2XKtquvDwEte2kYfJ"
SELLER = "rLfbT5Vkbigi4gFUi1QUGu5udijV79i3tF"
BUYER = "rHaMsAjoAN21s1XG5TCAM6ErAefzrggsHf"
NFT_ID = "00191B58D1AE1BC312BEF9C68233FB0C8CF6A338F7C227BECADA906904943EEE"
BUY_OFFER = "37B0856D51DF75221A9140AF8FCD89E1AC3CAC703D36070BA7E2FE85567C4B7D"
SELL_OFFER = "1C827D06CDE70456A31AA10FC2169356964093BE4BF42B18DAD551518A186B35"
ACCEPT = "FED6256DC27A6D06C43E20ADA2B5D2FFF4AEAEE7560FADBF584CAE6396C1ACFA"
ACCEPT_TS = 1_787_421_471
NOW = 1_787_500_000


def _tx():
    return json.loads(FIXTURE.read_text())


class Ledger:
    """Records calls; behaviour is set per test."""

    def __init__(self):
        self.get_tx_calls: list[str] = []
        self.sent: list[dict] = []
        self.tx = _tx()
        self.get_tx_error: Exception | None = None
        self.payment = xrpl_ops.ClaimPayment("confirmed", "PAYOUT", 7_040)
        self.send_error: BaseException | None = None
        self.found: dict[str, str] = {}
        self.find_error: Exception | None = None
        self.validated_index: int | None = 7_000

    async def get_tx(self, tx_hash):
        self.get_tx_calls.append(tx_hash)
        if self.get_tx_error:
            raise self.get_tx_error
        return self.tx

    async def send_refund(self, destination, drops, accept_tx_hash, *, campaign_id, max_last_ledger_seq=None):
        self.sent.append({"destination": destination, "drops": drops, "accept": accept_tx_hash,
                          "campaign_id": campaign_id, "max_lls": max_last_ledger_seq})
        await asyncio.sleep(0)
        if self.send_error:
            raise self.send_error
        return self.payment

    async def find_refund_payment(self, accept_tx_hash, *, destination, drops, min_ledger):
        if self.find_error:
            raise self.find_error
        return self.found.get(accept_tx_hash)

    async def current_ledger(self):
        return self.validated_index


@pytest.fixture()
def env(tmp_path):
    ledger = Ledger()
    deps = settle.FeeCoverDeps(
        network=NET,
        app_db_path=str(tmp_path / "app.db"),
        history_db_path=str(tmp_path / "history.db"),
        payer=ISSUER,
        ledger_margin=40,
        get_tx=ledger.get_tx,
        send_refund=ledger.send_refund,
        find_refund_payment=ledger.find_refund_payment,
        current_ledger=ledger.current_ledger,
        broker_rate_for=lambda account, nft_id: {CAFE: 0.01589}.get(account),
        linked=lambda a, b: False,
        now=lambda: NOW,
    )
    conn = store.connect(deps.app_db_path)
    knobs = store.Knobs(coverage_bps=10_000, budget_drops=1_000_000, wallet_cap_drops=1_000_000, min_bid_drops=0)
    campaign, _ = store.start_campaign(conn, network=NET, actor="t", knobs=knobs, duration_seconds=None, now=ACCEPT_TS - 600)
    conn.close()
    return deps, ledger, campaign


def _promise(deps, campaign, offer_index=BUY_OFFER, bidder=BUYER, expiration=900_000_000):
    conn = store.connect(deps.app_db_path)
    try:
        inp = store.PromiseInput(offer_index=offer_index, network=NET, nft_id=NFT_ID, bidder=bidder,
                                 bid_drops=5_075_000, ask_drops=4_990_000, broker=CAFE, broker_rate=0.01589,
                                 bid_expiration=expiration)
        return store.record_promise(conn, campaign, inp, promised_drops=80_642, decline_reason=None, now=ACCEPT_TS - 60)
    finally:
        conn.close()


def _archive_accept(deps, tx=None, ts=ACCEPT_TS):
    tx = tx or _tx()
    hconn = history_store.init_history_db(deps.history_db_path)
    history_store.insert_tx(hconn, tx_hash=tx["hash"], ledger_index=tx.get("ledger_index"), close_time=ts,
                            tx_type="NFTokenAcceptOffer", account=tx["Account"], source_tag=tx.get("SourceTag"),
                            raw_json=json.dumps(tx))
    history_store.insert_nft_event(hconn, {"tx_hash": tx["hash"], "nft_id": NFT_ID, "event": "sale",
                                           "from_addr": SELLER, "to_addr": BUYER, "price_drops": 5_075_000,
                                           "ts": ts, "offer_index": SELL_OFFER})
    hconn.commit()
    hconn.close()


def _archive_state(deps, *, close_time=NOW, baseline=1, gap=None):
    hconn = history_store.init_history_db(deps.history_db_path)
    hconn.execute(
        "INSERT OR REPLACE INTO archive_state (network, genesis_hash, baseline_complete, validated_close_time,"
        " continuity_gap_reason, updated_at) VALUES (?, 'genesis', ?, ?, ?, 1)",
        (NET, baseline, close_time, gap),
    )
    hconn.commit()
    hconn.close()


def _refund(deps, key=ACCEPT):
    conn = store.connect(deps.app_db_path)
    try:
        return store.get_refund(conn, key)
    finally:
        conn.close()


def _run(coro):
    # A private loop: never disturbs (or depends on) the global event loop,
    # which asyncio.run() in other test modules leaves unset on Python 3.10.
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# --- archive helpers ----------------------------------------------------------


def test_find_accept_tx_matches_only_this_buy_offer_after_since(env):
    deps, _, _ = env
    _archive_accept(deps)
    hconn = history_store.init_history_db(deps.history_db_path)
    try:
        assert settle.find_accept_tx(hconn, NFT_ID, BUY_OFFER, ACCEPT_TS - 10)["hash"] == ACCEPT
        assert settle.find_accept_tx(hconn, NFT_ID, "F" * 64, ACCEPT_TS - 10) is None
        assert settle.find_accept_tx(hconn, NFT_ID, BUY_OFFER, ACCEPT_TS + 1) is None
    finally:
        hconn.close()


def test_cancel_seen_splits_grouped_offer_indexes(env):
    deps, _, _ = env
    hconn = history_store.init_history_db(deps.history_db_path)
    history_store.insert_nft_event(hconn, {"tx_hash": "CANCEL", "nft_id": NFT_ID, "event": "offer_cancel",
                                           "ts": ACCEPT_TS, "offer_index": f"AAAA,{BUY_OFFER}"})
    hconn.commit()
    try:
        assert settle.cancel_seen(hconn, NFT_ID, BUY_OFFER) is True
        assert settle.cancel_seen(hconn, NFT_ID, "AAAA") is True
        assert settle.cancel_seen(hconn, NFT_ID, "BBBB") is False
    finally:
        hconn.close()


def test_archive_blocker(env):
    deps, _, _ = env
    hconn = history_store.init_history_db(deps.history_db_path)
    try:
        assert settle.archive_blocker(hconn, NET, 100) == "no archive_state row"
    finally:
        hconn.close()
    for kwargs, expected in (
        ({"baseline": 0}, "baseline not complete"),
        ({"gap": "restart"}, "continuity gap recorded"),
        ({"close_time": 100}, "archive not yet validated past the expiry"),
        ({}, None),
    ):
        _archive_state(deps, **kwargs)
        hconn = history_store.init_history_db(deps.history_db_path)
        try:
            assert settle.archive_blocker(hconn, NET, 100) == expected
        finally:
            hconn.close()


def test_normalize_tx_flattens_api_v2():
    v2 = {"tx_json": {"Account": CAFE, "TransactionType": "NFTokenAcceptOffer"}, "meta": {"x": 1},
          "hash": ACCEPT, "validated": True}
    flat = settle.normalize_tx(v2)
    assert flat["Account"] == CAFE and flat["meta"] == {"x": 1} and flat["hash"] == ACCEPT and "tx_json" not in flat
    assert settle.normalize_tx(_tx())["Account"] == CAFE


# --- settle -------------------------------------------------------------------


def test_settle_promise_writes_an_owed_refund_from_the_archived_accept(env):
    deps, ledger, _ = env
    _promise(deps, env[2])
    _archive_accept(deps)
    view = _run(settle.settle_promise(deps, BUY_OFFER))
    assert view == {"state": "owed", "drops": 80_642, "xrp": "0.080642", "reason": None, "payout_tx_hash": None}
    assert ledger.get_tx_calls == [ACCEPT]
    assert _refund(deps)["observed_royalty_drops"] == 349_605


def test_settle_promise_accepts_an_api_v2_get_tx_result(env):
    deps, ledger, _ = env
    _promise(deps, env[2])
    _archive_accept(deps)
    tx = _tx()
    meta = tx.pop("meta")
    ledger.tx = {"tx_json": tx, "meta": meta, "hash": tx.pop("hash"), "validated": tx.pop("validated")}
    assert _run(settle.settle_promise(deps, BUY_OFFER))["state"] == "owed"


def test_settle_promise_without_an_archived_accept_stays_open(env):
    deps, ledger, _ = env
    _promise(deps, env[2])
    assert _run(settle.settle_promise(deps, BUY_OFFER))["state"] == "open"
    assert ledger.get_tx_calls == []


def test_settle_promise_lookup_failure_stays_open(env):
    deps, ledger, _ = env
    _promise(deps, env[2])
    _archive_accept(deps)
    ledger.get_tx_error = RuntimeError("rpc down")
    assert _run(settle.settle_promise(deps, BUY_OFFER))["state"] == "open"


# --- pay ------------------------------------------------------------------------


def _owed(deps, campaign):
    _promise(deps, campaign)
    _archive_accept(deps)
    _run(settle.settle_promise(deps, BUY_OFFER))


def test_pay_refund_confirms(env):
    deps, ledger, campaign = env
    _owed(deps, campaign)
    assert _run(settle.pay_refund(deps, ACCEPT)) == "confirmed"
    assert ledger.sent == [
        {"destination": BUYER, "drops": 80_642, "accept": ACCEPT, "campaign_id": campaign.id, "max_lls": 7_400}
    ]
    row = _refund(deps)
    assert (row["state"], row["payout_tx_hash"], row["last_ledger_seq"]) == ("confirmed", "PAYOUT", 7_040)


def test_concurrent_pay_refund_submits_exactly_once(env):
    deps, ledger, campaign = env
    _owed(deps, campaign)

    async def both():
        return await asyncio.gather(settle.pay_refund(deps, ACCEPT), settle.pay_refund(deps, ACCEPT))

    states = _run(both())
    assert len(ledger.sent) == 1
    assert "confirmed" in states


def test_pay_refund_not_submitted_returns_to_owed(env):
    deps, ledger, campaign = env
    _owed(deps, campaign)
    ledger.send_error = xrpl_ops.ClaimNotSubmitted("no ledger")
    assert _run(settle.pay_refund(deps, ACCEPT)) == "owed"
    assert _refund(deps)["state"] == "owed" and _refund(deps)["last_ledger_seq"] is None


def test_pay_refund_unknown_stays_submitted_with_the_payment_deadline(env):
    deps, ledger, campaign = env
    _owed(deps, campaign)
    ledger.payment = xrpl_ops.ClaimPayment("unknown", None, 7_040)
    assert _run(settle.pay_refund(deps, ACCEPT)) == "submitted"
    row = _refund(deps)
    assert row["state"] == "submitted" and row["last_ledger_seq"] == 7_040


def test_pay_refund_unexpected_error_leaves_it_submitted_for_recovery(env):
    deps, ledger, campaign = env
    _owed(deps, campaign)
    ledger.send_error = RuntimeError("boom")
    assert _run(settle.pay_refund(deps, ACCEPT)) == "submitted"
    assert _refund(deps)["last_ledger_seq"] == 7_400  # the provisional deadline


def test_pay_refund_without_a_ledger_index_does_not_claim(env):
    deps, ledger, campaign = env
    _owed(deps, campaign)
    ledger.validated_index = None
    assert _run(settle.pay_refund(deps, ACCEPT)) == "owed"
    assert ledger.sent == []


def test_pay_refund_missing_row(env):
    deps, _, _ = env
    assert _run(settle.pay_refund(deps, "NOPE")) == "missing"


# --- recover ----------------------------------------------------------------------


def _submitted(deps, ledger, campaign, lls=7_040):
    _owed(deps, campaign)
    ledger.payment = xrpl_ops.ClaimPayment("unknown", None, lls)
    _run(settle.pay_refund(deps, ACCEPT))


def test_recover_found_confirms(env):
    deps, ledger, campaign = env
    _submitted(deps, ledger, campaign)
    ledger.found[ACCEPT] = "FOUNDHASH"
    assert _run(settle.recover_refunds(deps)) == {ACCEPT: "confirmed"}
    assert _refund(deps)["payout_tx_hash"] == "FOUNDHASH"


def test_recover_absent_before_deadline_is_untouched_and_after_is_failed(env):
    deps, ledger, campaign = env
    _submitted(deps, ledger, campaign, lls=7_040)
    ledger.validated_index = 7_040
    assert _run(settle.recover_refunds(deps)) == {}
    ledger.validated_index = 7_041
    assert _run(settle.recover_refunds(deps)) == {ACCEPT: "failed"}
    assert _refund(deps)["reason"] == "payout_failed"


def test_recover_lookup_error_is_untouched(env):
    deps, ledger, campaign = env
    _submitted(deps, ledger, campaign)
    ledger.validated_index = 99_999
    ledger.find_error = RuntimeError("account_tx down")
    assert _run(settle.recover_refunds(deps)) == {}
    assert _refund(deps)["state"] == "submitted"


def test_recover_without_submitted_rows_never_reads_the_ledger(env):
    deps, ledger, _ = env

    async def boom():
        raise AssertionError("must not be called")

    deps.current_ledger = boom
    assert _run(settle.recover_refunds(deps)) == {}


# --- sweep ------------------------------------------------------------------------


def test_sweep_settles_pays_and_releases_expired(env):
    deps, ledger, campaign = env
    _promise(deps, campaign)
    _archive_accept(deps)
    _promise(deps, campaign, offer_index="E" * 64, bidder="rOtherBidder", expiration=800_000_000)
    _archive_state(deps, close_time=NOW)
    report = _run(settle.sweep_once(deps, attempts={}, max_attempts=5, on_giveup=lambda row: None))
    assert (report.settled, report.released, report.paid) == (1, 1, 1)
    conn = store.connect(deps.app_db_path)
    try:
        assert store.get_promise(conn, "E" * 64)["reason"] == "expired"
        assert store.get_refund(conn, ACCEPT)["state"] == "confirmed"
    finally:
        conn.close()


def test_sweep_releases_a_cancelled_bid(env):
    deps, _, campaign = env
    _promise(deps, campaign)
    hconn = history_store.init_history_db(deps.history_db_path)
    history_store.insert_nft_event(hconn, {"tx_hash": "CANCEL", "nft_id": NFT_ID, "event": "offer_cancel",
                                           "ts": ACCEPT_TS, "offer_index": BUY_OFFER})
    hconn.commit()
    hconn.close()
    report = _run(settle.sweep_once(deps, attempts={}, max_attempts=5, on_giveup=lambda row: None))
    assert report.released == 1


def test_sweep_never_releases_an_expired_bid_across_an_archive_gap(env):
    deps, _, campaign = env
    _promise(deps, campaign, offer_index="E" * 64, bidder="rOtherBidder", expiration=800_000_000)
    _archive_state(deps, close_time=NOW, gap="listener restart")
    report = _run(settle.sweep_once(deps, attempts={}, max_attempts=5, on_giveup=lambda row: None))
    assert report.released == 0


def test_open_promises_are_honored_after_stop(env):
    deps, ledger, campaign = env
    _promise(deps, campaign)
    _archive_accept(deps)
    conn = store.connect(deps.app_db_path)
    store.stop_campaign(conn, network=NET, actor="t", now=ACCEPT_TS)
    conn.close()
    report = _run(settle.sweep_once(deps, attempts={}, max_attempts=5, on_giveup=lambda row: None))
    assert (report.settled, report.paid) == (1, 1)
    assert _refund(deps)["state"] == "confirmed"


def test_sweep_gives_up_on_a_refund_after_max_attempts(env):
    deps, ledger, campaign = env
    _owed(deps, campaign)
    ledger.send_error = xrpl_ops.ClaimNotSubmitted("still no ledger")
    attempts: dict[str, int] = {}
    gave_up: list[dict] = []
    for _ in range(3):
        _run(settle.sweep_once(deps, attempts=attempts, max_attempts=2, on_giveup=gave_up.append))
    assert len(ledger.sent) == 2
    assert [row["accept_tx_hash"] for row in gave_up] == [ACCEPT]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_fee_cover_settle.py -v`
Expected: FAIL with `ImportError: cannot import name 'fee_cover_settle'`

- [ ] **Step 3: Write the implementation**

```python
# lfg_core/fee_cover_settle.py
"""Fee-cover settlement: resolve a promise's accept from the history archive,
pay the refund, and recover indeterminate payouts. All I/O is injected via
FeeCoverDeps so every path is testable without a ledger.

Spec: docs/superpowers/specs/2026-09-14-marketplace-fee-cover-design.md
(§Durability, §Fill detection and promise release).

sqlite connections are opened inside the worker thread that uses them
(asyncio.to_thread), never shared across threads.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from lfg_core import fee_cover, fee_cover_store, history_store, xrpl_ops

RIPPLE_EPOCH_OFFSET = 946_684_800
# A promise is written when its bid validates; its accept cannot predate that,
# but clocks differ between the service and ledger close times — allow slack.
ACCEPT_LOOKBACK_SECONDS = 3600


def _unix_now() -> int:
    return int(time.time())


@dataclass
class FeeCoverDeps:
    network: str
    app_db_path: str
    history_db_path: str
    payer: str
    ledger_margin: int
    get_tx: Callable[[str], Awaitable[dict[str, Any]]]
    send_refund: Callable[..., Awaitable[xrpl_ops.ClaimPayment]]
    find_refund_payment: Callable[..., Awaitable[str | None]]
    current_ledger: Callable[[], Awaitable[int | None]]
    broker_rate_for: Callable[[str, str], float | None]
    linked: Callable[[str, str], bool]
    now: Callable[[], int] = _unix_now


@dataclass
class SweepReport:
    settled: int = 0
    released: int = 0
    paid: int = 0
    recovered: int = 0


# --- archive helpers (sync, caller-owned connection) --------------------------


def normalize_tx(result: dict[str, Any]) -> dict[str, Any]:
    """Flatten a `tx` result to the archive shape: API v2 nests the tx fields
    under tx_json; v1 and the history archive keep them top-level."""
    inner = result.get("tx_json")
    if not isinstance(inner, dict):
        return dict(result)
    flat = dict(inner)
    for key, value in result.items():
        if key != "tx_json":
            flat[key] = value
    return flat


def find_accept_tx(
    hconn: sqlite3.Connection, nft_id: str, offer_index: str, since_unix: int
) -> dict[str, Any] | None:
    row = hconn.execute(
        "SELECT x.raw_json FROM nft_events e JOIN xrpl_txs x ON x.tx_hash = e.tx_hash"
        " WHERE e.nft_id = ? AND e.event = 'sale' AND e.ts >= ?"
        " AND json_extract(x.raw_json, '$.NFTokenBuyOffer') = ? LIMIT 1",
        (nft_id, since_unix, offer_index),
    ).fetchone()
    if row is None:
        return None
    tx = json.loads(row[0])
    return tx if isinstance(tx, dict) else None


def cancel_seen(hconn: sqlite3.Connection, nft_id: str, offer_index: str) -> bool:
    """Cancel events group every offer closed for a token into one
    comma-joined offer_index (history_events, #411)."""
    for (joined,) in hconn.execute(
        "SELECT offer_index FROM nft_events WHERE nft_id = ? AND event = 'offer_cancel'"
        " AND offer_index IS NOT NULL",
        (nft_id,),
    ):
        if offer_index in str(joined).split(","):
            return True
    return False


def archive_blocker(hconn: sqlite3.Connection, network: str, unix_ts: int) -> str | None:
    """Why this archive cannot prove what happened up to `unix_ts`, or None.
    Same provenance test as epoch_state.certify_epoch."""
    state = history_store.get_archive_state(hconn, network)
    if state is None:
        return "no archive_state row"
    if not state.baseline_complete:
        return "baseline not complete"
    if (
        state.continuity_gap_at is not None
        or state.continuity_gap_after is not None
        or state.continuity_gap_before is not None
        or state.continuity_gap_reason is not None
    ):
        return "continuity gap recorded"
    if state.validated_close_time is None or state.validated_close_time <= unix_ts:
        return "archive not yet validated past the expiry"
    return None


# --- sync units run in worker threads -------------------------------------------


def _open_promise_and_accept(
    deps: FeeCoverDeps, offer_index: str
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None]:
    conn = fee_cover_store.connect(deps.app_db_path)
    try:
        promise = fee_cover_store.get_promise(conn, offer_index)
        view = fee_cover_store.view_for_offer(conn, offer_index)
    finally:
        conn.close()
    if promise is None or promise["state"] != "open":
        return promise, None, view
    hconn = history_store.init_history_db(deps.history_db_path)
    try:
        accept = find_accept_tx(
            hconn, str(promise["nft_id"]), offer_index, int(promise["created_at"]) - ACCEPT_LOOKBACK_SECONDS
        )
    finally:
        hconn.close()
    return promise, accept, view


def _decide_and_fill(deps: FeeCoverDeps, promise: dict[str, Any], validated: dict[str, Any]) -> dict[str, Any] | None:
    nft_id = str(promise["nft_id"])
    decision = fee_cover.compute_refund(
        validated,
        promise=promise,
        payer=deps.payer,
        broker_rate_for=lambda account: deps.broker_rate_for(account, nft_id),
        linked=deps.linked,
    )
    conn = fee_cover_store.connect(deps.app_db_path)
    try:
        fee_cover_store.fill_promise(conn, str(promise["offer_index"]), decision, now=deps.now())
        return fee_cover_store.view_for_offer(conn, str(promise["offer_index"]))
    finally:
        conn.close()


def _refund_row(deps: FeeCoverDeps, key: str) -> dict[str, Any] | None:
    conn = fee_cover_store.connect(deps.app_db_path)
    try:
        return fee_cover_store.get_refund(conn, key)
    finally:
        conn.close()


def _rows(deps: FeeCoverDeps, state: str) -> list[dict[str, Any]]:
    conn = fee_cover_store.connect(deps.app_db_path)
    try:
        return fee_cover_store.refunds_in_state(conn, deps.network, state)
    finally:
        conn.close()


def _open_promises(deps: FeeCoverDeps) -> list[dict[str, Any]]:
    conn = fee_cover_store.connect(deps.app_db_path)
    try:
        return fee_cover_store.open_promises(conn, deps.network)
    finally:
        conn.close()


def _claim(deps: FeeCoverDeps, key: str, last_ledger_seq: int) -> bool:
    conn = fee_cover_store.connect(deps.app_db_path)
    try:
        return fee_cover_store.claim_for_payout(conn, key, last_ledger_seq, now=deps.now())
    finally:
        conn.close()


def _record(deps: FeeCoverDeps, key: str, state: str, tx_hash: str | None, last_ledger_seq: int | None) -> None:
    conn = fee_cover_store.connect(deps.app_db_path)
    try:
        fee_cover_store.record_payout(
            conn, key, state=state, tx_hash=tx_hash, last_ledger_seq=last_ledger_seq, now=deps.now()
        )
    finally:
        conn.close()


def _unclaim(deps: FeeCoverDeps, key: str) -> None:
    conn = fee_cover_store.connect(deps.app_db_path)
    try:
        fee_cover_store.return_to_owed(conn, key, now=deps.now())
    finally:
        conn.close()


def _release_reason(deps: FeeCoverDeps, promise: dict[str, Any]) -> str | None:
    """'cancelled' / 'expired' when the archive proves the bid can never fill;
    None (leave open) otherwise — fail closed."""
    nft_id = str(promise["nft_id"])
    offer_index = str(promise["offer_index"])
    hconn = history_store.init_history_db(deps.history_db_path)
    try:
        since = int(promise["created_at"]) - ACCEPT_LOOKBACK_SECONDS
        if find_accept_tx(hconn, nft_id, offer_index, since) is not None:
            return None  # filled: settle_promise owns it (e.g. get_tx was down)
        if cancel_seen(hconn, nft_id, offer_index):
            return "cancelled"
        expiration = promise["bid_expiration"]
        if expiration is None:
            return None
        expiry_unix = int(expiration) + RIPPLE_EPOCH_OFFSET
        if deps.now() <= expiry_unix or archive_blocker(hconn, deps.network, expiry_unix) is not None:
            return None
        return "expired"
    finally:
        hconn.close()


def _release(deps: FeeCoverDeps, offer_index: str, reason: str) -> bool:
    conn = fee_cover_store.connect(deps.app_db_path)
    try:
        return fee_cover_store.release_promise(conn, offer_index, reason, now=deps.now())
    finally:
        conn.close()


# --- async orchestration ---------------------------------------------------------


async def settle_promise(deps: FeeCoverDeps, offer_index: str) -> dict[str, Any] | None:
    """Resolve one open promise against its archived accept; returns the
    fee-cover view. Leaves the promise open on anything unproven."""
    promise, accept, view = await asyncio.to_thread(_open_promise_and_accept, deps, offer_index)
    if promise is None or promise["state"] != "open" or accept is None:
        return view
    tx_hash = accept.get("hash")
    if not isinstance(tx_hash, str) or not tx_hash:
        return view
    try:
        result = await deps.get_tx(tx_hash)
    except Exception:
        logging.warning("fee cover: get_tx(%s) failed; promise %s stays open", tx_hash, offer_index, exc_info=True)
        return view
    if not isinstance(result, dict) or result.get("validated") is not True:
        return view
    validated = normalize_tx(result)
    validated["hash"] = validated.get("hash") or tx_hash
    return await asyncio.to_thread(_decide_and_fill, deps, promise, validated)


async def pay_refund(deps: FeeCoverDeps, accept_tx_hash: str) -> str:
    row = await asyncio.to_thread(_refund_row, deps, accept_tx_hash)
    if row is None:
        return "missing"
    if row["state"] != "owed":
        return str(row["state"])
    current = await deps.current_ledger()
    if current is None:
        return "owed"
    provisional = current + deps.ledger_margin * 10
    if not await asyncio.to_thread(_claim, deps, accept_tx_hash, provisional):
        latest = await asyncio.to_thread(_refund_row, deps, accept_tx_hash)
        return str(latest["state"]) if latest else "missing"
    try:
        payment = await deps.send_refund(
            str(row["bidder"]),
            int(row["refund_drops"]),
            accept_tx_hash,
            campaign_id=int(row["campaign_id"]),
            max_last_ledger_seq=provisional,
        )
    except xrpl_ops.ClaimNotSubmitted as exc:
        logging.warning("fee cover refund %s not submitted: %s", accept_tx_hash, exc)
        await asyncio.to_thread(_unclaim, deps, accept_tx_hash)
        return "owed"
    except asyncio.CancelledError:
        raise
    except Exception:
        # The provisional deadline is already recorded, so recovery can decide.
        logging.exception("fee cover refund %s raised; leaving it submitted for recovery", accept_tx_hash)
        return "submitted"
    state = "submitted" if payment.state == "unknown" else payment.state
    await asyncio.to_thread(_record, deps, accept_tx_hash, state, payment.tx_hash, payment.last_ledger_seq)
    return state


async def recover_refunds(deps: FeeCoverDeps) -> dict[str, str]:
    """confirmed when the memo-tagged refund is on-ledger; failed only when it
    is absent AND the validated ledger has passed the row's deadline."""
    rows = await asyncio.to_thread(_rows, deps, "submitted")
    if not rows:
        return {}
    validated = await deps.current_ledger()
    if validated is None:
        return {}
    outcomes: dict[str, str] = {}
    for row in rows:
        key = str(row["accept_tx_hash"])
        last_ledger_seq = row["last_ledger_seq"]
        try:
            found = await deps.find_refund_payment(
                key, destination=str(row["bidder"]), drops=int(row["refund_drops"]), min_ledger=last_ledger_seq
            )
        except Exception:
            logging.warning("fee cover recovery: account_tx lookup failed for %s", key, exc_info=True)
            continue
        if found:
            await asyncio.to_thread(_record, deps, key, "confirmed", found, None)
            outcomes[key] = "confirmed"
            continue
        if last_ledger_seq is None or validated <= int(last_ledger_seq):
            continue
        await asyncio.to_thread(_record, deps, key, "failed", None, None)
        outcomes[key] = "failed"
    return outcomes


async def sweep_once(
    deps: FeeCoverDeps,
    *,
    attempts: dict[str, int],
    max_attempts: int,
    on_giveup: Callable[[dict[str, Any]], None],
) -> SweepReport:
    report = SweepReport()
    for promise in await asyncio.to_thread(_open_promises, deps):
        offer_index = str(promise["offer_index"])
        view = await settle_promise(deps, offer_index)
        if view is not None and view["state"] != "open":
            report.settled += 1
            continue
        reason = await asyncio.to_thread(_release_reason, deps, promise)
        if reason is not None and await asyncio.to_thread(_release, deps, offer_index, reason):
            report.released += 1
    for row in await asyncio.to_thread(_rows, deps, "owed"):
        key = str(row["accept_tx_hash"])
        if attempts.get(key, 0) >= max_attempts:
            continue
        state = await pay_refund(deps, key)
        if state == "owed":
            attempts[key] = attempts.get(key, 0) + 1
            if attempts[key] >= max_attempts:
                on_giveup(row)
        else:
            attempts.pop(key, None)
            if state in ("confirmed", "submitted", "failed"):
                report.paid += 1
    report.recovered = len(await recover_refunds(deps))
    return report
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_fee_cover_settle.py -v`
Expected: 24 passed.

- [ ] **Step 5: Format, type-check, commit**

```bash
.venv/bin/ruff format lfg_core/fee_cover_settle.py tests/test_fee_cover_settle.py
.venv/bin/ruff check --fix lfg_core/fee_cover_settle.py tests/test_fee_cover_settle.py
.venv/bin/mypy lfg_core/fee_cover_settle.py
git add lfg_core/fee_cover_settle.py tests/test_fee_cover_settle.py
git commit -m "feat(fee-cover): settle, pay, recover and sweep with injected ledger deps"
```

---
### Task 6: Service wiring — quote at bid start, promise at finalize, settle on poll, My bids

**Files:**
- Modify: `lfg_core/market_flow.py` (`BidSession.fee_cover` + `to_dict`)
- Modify: `lfg_service/app.py`:
  - Imports.
  - A new fee-cover helper block after `_close_bid_sync`.
  - `handle_market_bid_start`.
  - The `bid` branch of `_advance_market_session`.
  - `handle_market_bids_mine`.
- Test: `tests/test_fee_cover_api.py`

**Interfaces:**
- Consumes:
  - Tasks 1–5: `fee_cover_store.connect`, `live_campaign`, `headroom_reason`,
    `record_promise`, `PromiseInput`, `view_for_offer`, `Campaign`.
  - `fee_cover.external_listing_for`, `promise_decline_reason`,
    `promise_drops`, `ExternalListing`.
  - `fee_cover_settle.FeeCoverDeps`, `settle_promise`.
  - `market_store.live_listings_for_nft`.
  - `xrpl_ops.send_fee_cover_refund`, `find_fee_cover_payment`, `get_tx`,
    `current_validated_ledger_index`.
- Produces (used by Tasks 7, 8, 10):
  - `_fee_cover_db() -> str`
  - `_fee_cover_deps() -> fee_cover_settle.FeeCoverDeps`
  - `_fee_cover_system_wallets() -> frozenset[str]`
  - `_fee_cover_linked(bidder, seller) -> bool`
  - `_fee_cover_safe(fn, *args)`
  - `_fee_cover_view(offer_index)`, `_fee_cover_views(offer_indexes)`
  - Session state: `BidSession.fee_cover: dict | None`. The status and start
    responses carry `fee_cover`, and `my_bids` rows carry `fee_cover`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_fee_cover_api.py
"""Fee cover wired into the bid flow (spec §Lifecycle, §Fill detection)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from aiohttp.test_utils import make_mocked_request

from lfg_core import fee_cover_settle, fee_cover_store, history_store
from lfg_core.economy_store import _ECONOMY_SCHEMA
from lfg_core.market_store import BuyOffer, MarketListing, close_bid, init_bid_schema, upsert_bid, upsert_listing
from lfg_core.market_store import init_db as init_market_db
from lfg_core.nft_index import OnchainNft
from lfg_core.nft_index import init_db as init_onchain_db
from lfg_core.nft_index import upsert as upsert_onchain_nft
from lfg_service import app as server
from webapp import mock_economy

FIXTURE = Path(__file__).parent / "fixtures" / "fee_cover" / "cafe_brokered_accept.json"
CAFE = "rpx9JThQ2y37FaGeeJP7PXDUVEXY3PHZSC"
BIDDER = "rBidderAddress0000000000000000000"
OWNER = "rOwnerAddress000000000000000000000"
CHAR = "000800001E43B0783E006F30078A64A8628F4B1B22879C8EB1CAF8C700000001"
ISSUER = "rLfgoMintj3KBcs4s2XKtquvDwEte2kYfJ"
FIX_BUYER = "rHaMsAjoAN21s1XG5TCAM6ErAefzrggsHf"
FIX_NFT = "00191B58D1AE1BC312BEF9C68233FB0C8CF6A338F7C227BECADA906904943EEE"
FIX_BUY_OFFER = "37B0856D51DF75221A9140AF8FCD89E1AC3CAC703D36070BA7E2FE85567C4B7D"
ACCEPT_TS = 1_787_421_471


def _run(coro):
    # A private loop: never disturbs (or depends on) the global event loop,
    # which asyncio.run() in other test modules leaves unset on Python 3.10.
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _body(resp):
    return json.loads(resp.body.decode())


def _post(path, body):
    req = make_mocked_request("POST", path)

    async def _json():
        return body

    req.json = _json  # type: ignore[method-assign]
    return req


class _StatusReq:
    headers: dict = {}

    def __init__(self, session_id):
        self.match_info = {"session_id": session_id}
        self._store = {}

    def __getitem__(self, k):
        return self._store[k]

    def __setitem__(self, k, v):
        self._store[k] = v


@pytest.fixture
def env(tmp_path, monkeypatch):
    onchain = str(tmp_path / "onchain_testnet.db")
    conn = init_onchain_db(onchain)
    conn.executescript(_ECONOMY_SCHEMA)
    init_market_db(conn)
    init_bid_schema(conn)
    conn.commit()
    conn.close()
    app_db = str(tmp_path / "app.db")
    monkeypatch.setenv("ONCHAIN_DB_PATH", onchain)
    monkeypatch.delenv("BROKER_CLEARING_BUFFER_DROPS", raising=False)
    monkeypatch.delenv("BROKER_ALLOWLIST_PATH", raising=False)
    monkeypatch.setattr(server.config, "XRPL_NETWORK", "testnet")
    monkeypatch.setattr(server.config, "ECONOMY_NETWORK", "testnet")
    monkeypatch.setattr(server.config, "WEBAPP_DEV_MODE", True)
    monkeypatch.setattr(mock_economy, "DEV_OWNER", BIDDER)
    monkeypatch.setattr(server, "_use_market_mock", lambda: False)
    monkeypatch.setattr(server, "_fee_cover_db", lambda: app_db)
    server._MARKET_CACHE.clear()
    server.market_sessions.clear()
    yield {"onchain": onchain, "app_db": app_db, "tmp": tmp_path}
    server._MARKET_CACHE.clear()
    server.market_sessions.clear()


def _seed_char_with_cafe_listing(onchain, *, ask=4_990_000, destination=CAFE):
    conn = init_onchain_db(onchain)
    upsert_onchain_nft(conn, OnchainNft(nft_id=CHAR, nft_number=1, owner=OWNER, is_burned=False, mutable=True,
                                        uri_hex="", body="Ape", attributes=[], image="", video="", ledger_index=1))
    upsert_listing(conn, MarketListing(offer_index="E" * 64, nft_id=CHAR, kind="character", seller=OWNER,
                                       amount_drops=ask, destination=destination, created_ledger=1, created_ts=1))
    conn.commit()
    conn.close()


def _campaign(app_db, now=None):
    conn = fee_cover_store.connect(app_db)
    try:
        knobs = fee_cover_store.Knobs(coverage_bps=10_000, budget_drops=50_000_000, wallet_cap_drops=5_000_000,
                                      min_bid_drops=1_000_000)
        campaign, _ = fee_cover_store.start_campaign(conn, network="testnet", actor="t", knobs=knobs,
                                                     duration_seconds=None, now=now)
        return campaign
    finally:
        conn.close()


def _fake_payload(monkeypatch):
    async def fake(*args, **kwargs):
        return {"qr_url": "https://qr", "xumm_url": "https://xumm.app/sign/U1", "uuid": "U1"}

    monkeypatch.setattr(server.xumm_ops, "create_buy_offer_payload", fake)


def test_bid_start_quotes_the_fee_for_a_clearing_bid_on_a_cafe_listing(env, monkeypatch):
    _seed_char_with_cafe_listing(env["onchain"])
    _campaign(env["app_db"])
    _fake_payload(monkeypatch)
    resp = _run(server.handle_market_bid_start(_post("/api/market/bid", {"nft_id": CHAR, "price_xrp": "5.075"})))
    assert resp.status == 200
    assert _body(resp)["fee_cover"] == {
        "state": "quoted", "drops": 80_642, "xrp": "0.080642", "reason": None, "payout_tx_hash": None,
    }


def test_bid_start_has_no_fee_cover_without_a_campaign_or_external_listing(env, monkeypatch):
    _fake_payload(monkeypatch)
    _seed_char_with_cafe_listing(env["onchain"])
    resp = _run(server.handle_market_bid_start(_post("/api/market/bid", {"nft_id": CHAR, "price_xrp": "5.075"})))
    assert _body(resp)["fee_cover"] is None  # no campaign
    server.market_sessions.clear()
    _campaign(env["app_db"])
    conn = init_onchain_db(env["onchain"])
    conn.execute("UPDATE market_listings SET destination = NULL")
    conn.commit()
    conn.close()
    resp = _run(server.handle_market_bid_start(_post("/api/market/bid", {"nft_id": CHAR, "price_xrp": "5.075"})))
    assert _body(resp)["fee_cover"] is None  # plain listing: an ordinary bid


def test_bid_start_quote_declines_below_clearing(env, monkeypatch):
    _seed_char_with_cafe_listing(env["onchain"])
    _campaign(env["app_db"])
    _fake_payload(monkeypatch)
    resp = _run(server.handle_market_bid_start(_post("/api/market/bid", {"nft_id": CHAR, "price_xrp": "5"})))
    assert _body(resp)["fee_cover"]["state"] == "declined"
    assert _body(resp)["fee_cover"]["reason"] == "below_clearing"
    assert _body(resp)["fee_cover"]["drops"] == 0


def test_bid_start_survives_a_fee_cover_failure(env, monkeypatch):
    _seed_char_with_cafe_listing(env["onchain"])
    _fake_payload(monkeypatch)

    def boom(*args):
        raise RuntimeError("fee cover db locked")

    monkeypatch.setattr(server, "_fee_cover_quote", boom)
    resp = _run(server.handle_market_bid_start(_post("/api/market/bid", {"nft_id": CHAR, "price_xrp": "5.075"})))
    assert resp.status == 200
    assert _body(resp)["fee_cover"] is None


def test_bid_finalize_records_the_promise_and_the_poll_reports_it(env, monkeypatch):
    _seed_char_with_cafe_listing(env["onchain"])
    _campaign(env["app_db"])
    session = server.market_flow.BidSession(discord_id="dev", wallet_address=BIDDER, nft_id=CHAR, owner=OWNER,
                                            amount_drops=5_075_000, platform="discord")
    server.market_sessions[session.id] = session

    async def fake_advance(s):
        if s.state == server.market_flow.DONE:
            return None
        s.state = server.market_flow.DONE
        s.offer_index = "D" * 64
        return {"offer_index": "D" * 64, "nft_id": CHAR, "bidder": BIDDER, "amount_drops": 5_075_000,
                "expiration": 900_000_000}

    monkeypatch.setattr(server.market_flow, "advance_bid_session", fake_advance)
    body = _body(_run(server.handle_market_bid_status(_StatusReq(session.id))))
    assert body["fill"] == "live"
    assert body["fee_cover"]["state"] == "open" and body["fee_cover"]["drops"] == 80_642
    conn = fee_cover_store.connect(env["app_db"])
    try:
        promise = fee_cover_store.get_promise(conn, "D" * 64)
    finally:
        conn.close()
    assert promise["bid_expiration"] == 900_000_000 and promise["broker"] == CAFE and promise["ask_drops"] == 4_990_000


def test_poll_after_the_fill_settles_the_refund(env, monkeypatch):
    campaign = _campaign(env["app_db"], now=ACCEPT_TS - 600)
    conn = fee_cover_store.connect(env["app_db"])
    fee_cover_store.record_promise(
        conn, campaign,
        fee_cover_store.PromiseInput(offer_index=FIX_BUY_OFFER, network="testnet", nft_id=FIX_NFT, bidder=FIX_BUYER,
                                     bid_drops=5_075_000, ask_drops=4_990_000, broker=CAFE, broker_rate=0.01589,
                                     bid_expiration=900_000_000),
        promised_drops=80_642, decline_reason=None, now=ACCEPT_TS - 60,
    )
    conn.close()
    oconn = init_onchain_db(env["onchain"])
    upsert_bid(oconn, BuyOffer(offer_index=FIX_BUY_OFFER, nft_id=FIX_NFT, bidder=FIX_BUYER, amount_drops=5_075_000))
    close_bid(oconn, FIX_BUY_OFFER, "accepted")
    oconn.commit()
    oconn.close()
    tx = json.loads(FIXTURE.read_text())
    history = str(env["tmp"] / "history.db")
    hconn = history_store.init_history_db(history)
    history_store.insert_tx(hconn, tx_hash=tx["hash"], ledger_index=1, close_time=ACCEPT_TS,
                            tx_type="NFTokenAcceptOffer", account=CAFE, source_tag=None, raw_json=json.dumps(tx))
    history_store.insert_nft_event(hconn, {"tx_hash": tx["hash"], "nft_id": FIX_NFT, "event": "sale", "ts": ACCEPT_TS})
    hconn.commit()
    hconn.close()

    async def get_tx(tx_hash):
        return tx

    async def never(*args, **kwargs):
        raise AssertionError("no payout on the poll path")

    deps = fee_cover_settle.FeeCoverDeps(
        network="testnet", app_db_path=env["app_db"], history_db_path=history, payer=ISSUER, ledger_margin=40,
        get_tx=get_tx, send_refund=never, find_refund_payment=never, current_ledger=never,
        broker_rate_for=lambda account, nft_id: {CAFE: 0.01589}.get(account), linked=lambda a, b: False,
    )
    monkeypatch.setattr(server, "_fee_cover_deps", lambda: deps)
    session = server.market_flow.BidSession(discord_id="dev", wallet_address=FIX_BUYER, nft_id=FIX_NFT,
                                            owner="rLfbT5Vkbigi4gFUi1QUGu5udijV79i3tF", amount_drops=5_075_000,
                                            platform="discord")
    session.state = server.market_flow.DONE
    session.offer_index = FIX_BUY_OFFER
    session.fee_cover = {"state": "open", "drops": 80_642, "xrp": "0.080642", "reason": None, "payout_tx_hash": None}
    server.market_sessions[session.id] = session
    body = _body(_run(server.handle_market_bid_status(_StatusReq(session.id))))
    assert body["fill"] == "accepted"
    assert body["fee_cover"]["state"] == "owed" and body["fee_cover"]["drops"] == 80_642


def test_bids_mine_rows_carry_fee_cover(env):
    _seed_char_with_cafe_listing(env["onchain"])
    campaign = _campaign(env["app_db"])
    oconn = init_onchain_db(env["onchain"])
    upsert_bid(oconn, BuyOffer(offer_index="D" * 64, nft_id=CHAR, bidder=BIDDER, amount_drops=5_075_000))
    oconn.commit()
    oconn.close()
    conn = fee_cover_store.connect(env["app_db"])
    fee_cover_store.record_promise(
        conn, campaign,
        fee_cover_store.PromiseInput(offer_index="D" * 64, network="testnet", nft_id=CHAR, bidder=BIDDER,
                                     bid_drops=5_075_000, ask_drops=4_990_000, broker=CAFE, broker_rate=0.01589,
                                     bid_expiration=None),
        promised_drops=80_642, decline_reason=None,
    )
    conn.close()
    body = _body(_run(server.handle_market_bids_mine(make_mocked_request("GET", "/api/market/bids/mine"))))
    assert body["my_bids"][0]["fee_cover"]["state"] == "open"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_fee_cover_api.py -v`
Expected: FAIL. The fixture's `monkeypatch.setattr(server, "_fee_cover_db", …)`
raises `AttributeError: <module 'lfg_service.app'> has no attribute
'_fee_cover_db'`.

- [ ] **Step 3: Write the implementation**

`lfg_core/market_flow.py` — in `BidSession`, after the `fill` field, add:

```python
    # Fee cover (spec 2026-09-14): the quote at start, then the promise/refund
    # view — {"state", "drops", "xrp", "reason", "payout_tx_hash"} | None.
    fee_cover: dict[str, Any] | None = None
```

and add `"fee_cover": self.fee_cover,` to `BidSession.to_dict()` after `"fill": self.fill,`.

`lfg_service/app.py`:
- Add `fee_cover`, `fee_cover_settle` and `fee_cover_store` to the
  `from lfg_core import (...)` block, alphabetically.
- Add the helper block below directly after `_close_bid_sync`:

```python
# --- Marketplace fee cover (docs/superpowers/specs/2026-09-14-marketplace-fee-cover-design.md) ---
# LFG refunds an allowlisted broker's fee on bids placed through LFG that the
# broker's bot settles. Everything here is best-effort with respect to the bid
# flow: a fee-cover failure must never fail a bid.


def _fee_cover_db() -> str:
    return db_path.app_db_path(config.XRPL_NETWORK)


def _fee_cover_system_wallets() -> frozenset[str]:
    configured = frozenset(w for w in (config.BRIX_DISTRIBUTOR_ADDRESS,) if w)
    return sponsored_mint.excluded_wallets() | system_wallets.with_durable(configured)


def _fee_cover_linked(bidder: str, seller: str) -> bool:
    """Same logical user (identity bucket) or the same non-exchange activation
    funder (#461). Fail-open on lookup errors: the refund is capped below the
    royalty, so a missed link can never make LFG a net payer."""
    try:
        bucket = identity_store.bucket_for_wallet(bidder)
    except identity_store.BucketLookupError:
        bucket = None
    if bucket is not None and seller in cast(list[str], bucket.get("wallets") or []):
        return True
    conn = sqlite3.connect(_fee_cover_db())
    try:
        funding.ensure_schema(conn)
        funder_a = funding.cached_funder(conn, bidder)
        funder_b = funding.cached_funder(conn, seller)
    finally:
        conn.close()
    return funder_a is not None and funder_a == funder_b and funder_a not in funding.EXCHANGES


def _fee_cover_deps() -> fee_cover_settle.FeeCoverDeps:
    return fee_cover_settle.FeeCoverDeps(
        network=config.XRPL_NETWORK,
        app_db_path=_fee_cover_db(),
        history_db_path=history_store.history_db_path(config.XRPL_NETWORK),
        payer=config.SIGNING_ACCOUNT,
        ledger_margin=config.FEE_COVER_LEDGER_MARGIN,
        get_tx=xrpl_ops.get_tx,
        send_refund=xrpl_ops.send_fee_cover_refund,
        find_refund_payment=xrpl_ops.find_fee_cover_payment,
        current_ledger=xrpl_ops.current_validated_ledger_index,
        broker_rate_for=lambda account, nft_id: (brokers.resolve(account, nft_id) or {}).get("broker_rate"),
        linked=_fee_cover_linked,
    )


def _fee_cover_safe(fn: Callable[..., Any], *args: Any) -> Any:
    try:
        return fn(*args)
    except Exception:
        logging.warning("fee cover: %s failed", getattr(fn, "__name__", fn), exc_info=True)
        return None


def _fee_cover_evaluate(
    nft_id: str, owner: str, bidder: str, bid_drops: int
) -> tuple[fee_cover_store.Campaign | None, fee_cover.ExternalListing | None, str | None, int]:
    conn = fee_cover_store.connect(_fee_cover_db())
    try:
        campaign = fee_cover_store.live_campaign(conn, config.XRPL_NETWORK)
    finally:
        conn.close()
    listing: fee_cover.ExternalListing | None = None
    if campaign is not None:
        oconn = nft_index.init_db(nft_index.index_db_path(_market_network("character")))
        try:
            market_store.init_db(oconn)
            listing = fee_cover.external_listing_for(
                market_store.live_listings_for_nft(oconn, nft_id), owner, nft_id
            )
        finally:
            oconn.close()
    reason = fee_cover.promise_decline_reason(
        campaign=campaign,
        listing=listing,
        bid_drops=bid_drops,
        bidder=bidder,
        system_wallets=_fee_cover_system_wallets(),
    )
    drops = (
        fee_cover.promise_drops(bid_drops, listing.broker_rate, campaign.coverage_bps)
        if campaign is not None and listing is not None
        else 0
    )
    return campaign, listing, reason, drops


def _fee_cover_quote(nft_id: str, owner: str, bidder: str, bid_drops: int) -> dict[str, Any] | None:
    """Advisory quote at POST /api/market/bid — reserves nothing."""
    campaign, listing, reason, drops = _fee_cover_evaluate(nft_id, owner, bidder, bid_drops)
    if campaign is None or listing is None:
        return None  # an ordinary bid
    if reason is None:
        conn = fee_cover_store.connect(_fee_cover_db())
        try:
            reason = fee_cover_store.headroom_reason(
                conn, campaign, config.XRPL_NETWORK, bidder, drops, int(time.time())
            )
        finally:
            conn.close()
    shown = drops if reason is None else 0
    return {
        "state": "quoted" if reason is None else "declined",
        "drops": shown,
        "xrp": market_ops.drops_to_xrp_str(str(shown)),
        "reason": reason,
        "payout_tx_hash": None,
    }


def _fee_cover_record_promise(session: Any, bid_row: dict[str, Any]) -> dict[str, Any] | None:
    """The bid is on-ledger: write the promise (budget reservation), re-evaluated
    against fresh state. Returns the view, or None for an ordinary bid."""
    bid_drops = int(bid_row["amount_drops"])
    bidder = str(bid_row["bidder"])
    campaign, listing, reason, drops = _fee_cover_evaluate(session.nft_id, session.owner, bidder, bid_drops)
    if campaign is None or listing is None:
        return None
    offer_index = str(bid_row["offer_index"])
    conn = fee_cover_store.connect(_fee_cover_db())
    try:
        row = fee_cover_store.record_promise(
            conn,
            campaign,
            fee_cover_store.PromiseInput(
                offer_index=offer_index,
                network=config.XRPL_NETWORK,
                nft_id=session.nft_id,
                bidder=bidder,
                bid_drops=bid_drops,
                ask_drops=listing.ask_drops,
                broker=listing.broker,
                broker_rate=listing.broker_rate,
                bid_expiration=bid_row.get("expiration"),
            ),
            promised_drops=drops,
            decline_reason=reason,
        )
        return None if row is None else fee_cover_store.view_for_offer(conn, offer_index)
    finally:
        conn.close()


def _fee_cover_view(offer_index: str) -> dict[str, Any] | None:
    conn = fee_cover_store.connect(_fee_cover_db())
    try:
        return fee_cover_store.view_for_offer(conn, offer_index)
    finally:
        conn.close()


def _fee_cover_views(offer_indexes: list[str]) -> dict[str, dict[str, Any]]:
    conn = fee_cover_store.connect(_fee_cover_db())
    try:
        views = {oi: fee_cover_store.view_for_offer(conn, oi) for oi in offer_indexes}
    finally:
        conn.close()
    return {oi: v for oi, v in views.items() if v is not None}


async def _fee_cover_settle_safe(offer_index: str) -> dict[str, Any] | None:
    try:
        return await fee_cover_settle.settle_promise(_fee_cover_deps(), offer_index)
    except Exception:
        logging.warning("fee cover: settling %s failed", offer_index, exc_info=True)
        return None
```

`Callable`, `cast`, `sqlite3` and `time` are already imported in `app.py`.

In `handle_market_bid_start`, directly before `market_sessions[session.id] = session`, add:

```python
    session.fee_cover = await loop.run_in_executor(
        None, _fee_cover_safe, _fee_cover_quote, nft_id, owner, wallet, amount_drops
    )
```

In `_advance_market_session`, replace the whole `elif prefix == "bid":` branch
with:

```python
    elif prefix == "bid":
        bid_row = await market_flow.advance_bid_session(session)
        if bid_row is not None:
            await loop.run_in_executor(None, _write_bid_row, _market_network("character"), bid_row)
            # Fee cover: the bid is on-ledger, so its promise is written now.
            session.fee_cover = await loop.run_in_executor(
                None, _fee_cover_safe, _fee_cover_record_promise, session, bid_row
            )
        # #426: once the bid is on-ledger, report whether the indexed buy
        # offer is still live or has been consumed (a broker's brokered
        # accept closes it 'accepted' via the listener) — the external
        # "Buy now" UI polls this and never claims success before the
        # ledger shows the fill. None = no indexed row (yet); never guessed.
        if session.state == market_flow.DONE and session.offer_index:
            session.fill = await loop.run_in_executor(
                None, _bid_fill_state, _market_network("character"), session.offer_index
            )
            if session.fee_cover is not None:
                if session.fill == "accepted":
                    settled = await _fee_cover_settle_safe(session.offer_index)
                    session.fee_cover = settled or session.fee_cover
                else:
                    refreshed = await loop.run_in_executor(
                        None, _fee_cover_safe, _fee_cover_view, session.offer_index
                    )
                    session.fee_cover = refreshed or session.fee_cover
```

In `handle_market_bids_mine`, replace the final `return web.json_response(...)` with:

```python
    views = (
        await asyncio.get_event_loop().run_in_executor(
            None, _fee_cover_safe, _fee_cover_views, [r["offer_index"] for r in mine]
        )
        or {}
    )
    return web.json_response(
        {
            "my_bids": [{**_serialize_bid(r), "fee_cover": views.get(r["offer_index"])} for r in mine],
            "bids_on_my_nfts": [_serialize_bid(r) for r in incoming],
        }
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_fee_cover_api.py tests/test_market_api.py tests/test_market_bids.py -q`
Expected: all pass. `test_fee_cover_api.py` has 7 tests; the existing market
suites are unchanged.

- [ ] **Step 5: Format, type-check, commit**

```bash
.venv/bin/ruff format lfg_core/market_flow.py lfg_service/app.py tests/test_fee_cover_api.py
.venv/bin/ruff check --fix lfg_core/market_flow.py lfg_service/app.py tests/test_fee_cover_api.py
.venv/bin/mypy lfg_service/app.py lfg_core/market_flow.py
git add lfg_core/market_flow.py lfg_service/app.py tests/test_fee_cover_api.py
git commit -m "feat(fee-cover): quote, promise and settle inside the bid flow"
```

---
### Task 7: Sweep loop and startup recovery

**Files:**
- Modify: `lfg_service/app.py`:
  - Add `_fee_cover_attempts`, `_write_fee_cover_giveup` and `sweep_fee_cover`
    directly above `_settlement_sweep_loop`.
  - Add one more guarded call inside the loop.
  - `_start_settlement_sweep` becomes unconditional.
  - Add a new `_start_fee_cover_recovery` hook and register it in
    `create_app`.
- Modify: `tests/test_market_trait_flow.py` (`test_start_settlement_sweep_skipped_when_economy_disabled`)
- Modify: `tests/test_sign_result_endpoint.py` (`test_sweep_loop_stays_off_with_both_disabled`)
- Test: `tests/test_fee_cover_api.py` (append)

**Interfaces:**
- Consumes:
  - Task 5: `fee_cover_settle.sweep_once(deps, *, attempts, max_attempts,
    on_giveup) -> SweepReport` and `fee_cover_settle.recover_refunds(deps)`.
  - Task 6: `_fee_cover_deps()`.
  - Existing: `_SWEEP_MAX_ATTEMPTS`, `config.ECONOMY_RECORDS_DIR`.
- Produces:
  - `sweep_fee_cover() -> None` (async)
  - `_write_fee_cover_giveup(row: dict) -> None`
  - `_fee_cover_attempts: dict[str, int]`
  - `_start_fee_cover_recovery(app) -> None` (async)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_fee_cover_api.py`:

```python
# --- Task 7: sweep + startup recovery -------------------------------------


class _Stop(BaseException):
    """Escapes the loop's `except Exception` guards to end one iteration."""


def test_sweep_fee_cover_runs_sweep_once_with_service_deps(monkeypatch):
    seen = {}
    sentinel = object()
    monkeypatch.setattr(server, "_fee_cover_deps", lambda: sentinel)

    async def fake_sweep_once(deps, *, attempts, max_attempts, on_giveup):
        seen.update(deps=deps, attempts=attempts, max_attempts=max_attempts, on_giveup=on_giveup)
        return fee_cover_settle.SweepReport()

    monkeypatch.setattr(server.fee_cover_settle, "sweep_once", fake_sweep_once)
    _run(server.sweep_fee_cover())
    assert seen["deps"] is sentinel
    assert seen["attempts"] is server._fee_cover_attempts
    assert seen["max_attempts"] == server._SWEEP_MAX_ATTEMPTS
    assert seen["on_giveup"] is server._write_fee_cover_giveup


def test_settlement_loop_runs_the_fee_cover_sweep_with_the_economy_off(monkeypatch):
    calls = []
    monkeypatch.setattr(server.config, "ECONOMY_ENABLED", False)

    async def sign_requests():
        calls.append("sign")

    async def fee_cover_sweep():
        calls.append("fee_cover")
        raise _Stop

    monkeypatch.setattr(server, "sweep_sign_requests", sign_requests)
    monkeypatch.setattr(server, "sweep_fee_cover", fee_cover_sweep)
    with pytest.raises(_Stop):
        _run(server._settlement_sweep_loop())
    assert calls == ["sign", "fee_cover"]


def test_a_crashing_fee_cover_sweep_does_not_kill_the_loop(monkeypatch):
    calls = []
    monkeypatch.setattr(server.config, "ECONOMY_ENABLED", False)

    async def sign_requests():
        calls.append("sign")
        if len(calls) > 1:
            raise _Stop

    async def fee_cover_sweep():
        calls.append("fee_cover")
        raise RuntimeError("db locked")

    monkeypatch.setattr(server, "sweep_sign_requests", sign_requests)
    monkeypatch.setattr(server, "sweep_fee_cover", fee_cover_sweep)
    monkeypatch.setattr(server, "_SWEEP_PERIOD_SECONDS", 0)
    with pytest.raises(_Stop):
        _run(server._settlement_sweep_loop())
    assert calls == ["sign", "fee_cover", "sign"]


def test_giveup_journal_is_written(tmp_path, monkeypatch):
    monkeypatch.setattr(server.config, "ECONOMY_RECORDS_DIR", str(tmp_path))
    server._write_fee_cover_giveup(
        {"accept_tx_hash": "ACC", "offer_index": "OFF", "bidder": "rB", "refund_drops": 80_642}
    )
    record = json.loads((tmp_path / "fee-cover-giveup-ACC.json").read_text())
    assert record["status"] == "abandoned" and record["refund_drops"] == 80_642


def test_startup_recovery_resolves_submitted_refunds(monkeypatch):
    called = []
    sentinel = object()
    monkeypatch.setattr(server, "_fee_cover_deps", lambda: sentinel)

    async def fake_recover(deps):
        called.append(deps)
        return {"ACC": "confirmed"}

    monkeypatch.setattr(server.fee_cover_settle, "recover_refunds", fake_recover)

    async def go():
        app: dict = {}
        await server._start_fee_cover_recovery(app)
        await app["fee_cover_recovery_task"]

    _run(go())
    assert called == [sentinel]
    assert server._start_fee_cover_recovery in server.create_app().on_startup
```

In `tests/test_market_trait_flow.py`, replace
`test_start_settlement_sweep_skipped_when_economy_disabled` with:

```python
def test_start_settlement_sweep_runs_even_when_economy_disabled(monkeypatch):
    # Fee cover (spec 2026-09-14): open promises must settle on any stack, so
    # the loop always starts; each sweep inside keeps its own gate.
    monkeypatch.setattr(server.config, "ECONOMY_ENABLED", False)

    async def go():
        app: dict = {}
        await server._start_settlement_sweep(app)
        task = app.get("settlement_sweep_task")
        assert isinstance(task, asyncio.Task)
        await server._stop_settlement_sweep(app)
        assert task.cancelled() or task.done()

    _run(go())
```

In `tests/test_sign_result_endpoint.py`, replace
`test_sweep_loop_stays_off_with_both_disabled` with:

```python
def test_sweep_loop_starts_even_with_both_disabled(monkeypatch):
    # Fee cover (spec 2026-09-14) needs the loop on every stack.
    monkeypatch.setattr(app.config, "ECONOMY_ENABLED", False)
    monkeypatch.setattr(app.config, "REOWN_PROJECT_ID", "")
    started = {}

    async def _go():
        holder: dict = {}
        await app._start_settlement_sweep(holder)
        started["task"] = holder.get("settlement_sweep_task")
        if started["task"] is not None:
            started["task"].cancel()

    _run(_go())
    assert started["task"] is not None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_fee_cover_api.py tests/test_market_trait_flow.py::test_start_settlement_sweep_runs_even_when_economy_disabled tests/test_sign_result_endpoint.py::test_sweep_loop_starts_even_with_both_disabled -v`
Expected: the new tests FAIL. There is no `sweep_fee_cover` attribute, and the
loop is not started with the flags off. Task 6's tests still pass.

- [ ] **Step 3: Write the implementation**

In `lfg_service/app.py`, directly above `async def _settlement_sweep_loop()`:

```python
_fee_cover_attempts: dict[str, int] = {}


def _write_fee_cover_giveup(row: dict[str, Any]) -> None:
    """Journal (ECONOMY_RECORDS_DIR) that the sweep stopped retrying an owed
    fee-cover refund. Nothing is lost: the row stays `owed` for an operator."""
    try:
        os.makedirs(config.ECONOMY_RECORDS_DIR, exist_ok=True)
        path = os.path.join(config.ECONOMY_RECORDS_DIR, f"fee-cover-giveup-{row['accept_tx_hash']}.json")
        with open(path, "w") as f:
            json.dump(
                {
                    "accept_tx_hash": row["accept_tx_hash"],
                    "offer_index": row["offer_index"],
                    "bidder": row["bidder"],
                    "refund_drops": row["refund_drops"],
                    "attempts": _SWEEP_MAX_ATTEMPTS,
                    "status": "abandoned",
                },
                f,
                indent=2,
            )
    except Exception:
        logging.error(f"failed to write fee cover giveup record: {traceback.format_exc()}")


async def sweep_fee_cover() -> None:
    """Settle filled promises, release dead ones, pay owed refunds, recover
    submitted ones (spec §Fill detection and promise release)."""
    report = await fee_cover_settle.sweep_once(
        _fee_cover_deps(),
        attempts=_fee_cover_attempts,
        max_attempts=_SWEEP_MAX_ATTEMPTS,
        on_giveup=_write_fee_cover_giveup,
    )
    if report.settled or report.released or report.paid or report.recovered:
        logging.info("fee cover sweep: %s", report)
```

Inside `_settlement_sweep_loop`, directly after the `sweep_sign_requests()`
try/except and before `await asyncio.sleep(_SWEEP_PERIOD_SECONDS)`:

```python
        try:
            await sweep_fee_cover()
        except Exception:
            logging.error(f"fee cover sweep loop crashed: {traceback.format_exc()}")
```

Replace `_start_settlement_sweep` with:

```python
async def _start_settlement_sweep(app: web.Application) -> None:
    """aiohttp on_startup hook: schedule the settlement sweep as a background
    task for the lifetime of the app.

    Always started: the fee-cover sweep must settle open promises and pay owed
    refunds on every stack, even one with the economy and WalletConnect off.
    Each sweep inside the loop keeps its own gate, and all are cheap no-ops
    when they have no rows.
    """
    app["settlement_sweep_task"] = asyncio.get_event_loop().create_task(_settlement_sweep_loop())
```

Directly after `_start_brix_claim_recovery`, add:

```python
async def _start_fee_cover_recovery(app: web.Application) -> None:
    """aiohttp on_startup hook: resolve fee-cover refunds left `submitted` by a
    crash. Fire-and-forget and fully guarded — never delays startup, and reads
    the ledger only when a submitted row exists."""

    async def _run() -> None:
        try:
            outcomes = await fee_cover_settle.recover_refunds(_fee_cover_deps())
            if outcomes:
                logging.info("fee cover recovery resolved %s refund(s): %s", len(outcomes), outcomes)
        except Exception:
            logging.warning("fee cover recovery failed at startup", exc_info=True)

    app["fee_cover_recovery_task"] = asyncio.get_event_loop().create_task(_run())
```

In `create_app`, directly after `app.on_startup.append(_start_brix_claim_recovery)`:

```python
    app.on_startup.append(_start_fee_cover_recovery)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_fee_cover_api.py tests/test_market_trait_flow.py tests/test_sign_result_endpoint.py -q`
Expected: all pass. `test_fee_cover_api.py` now has 12 tests.

- [ ] **Step 5: Format, type-check, commit**

```bash
.venv/bin/ruff format lfg_service/app.py tests/test_fee_cover_api.py tests/test_market_trait_flow.py tests/test_sign_result_endpoint.py
.venv/bin/ruff check --fix lfg_service/app.py tests/test_fee_cover_api.py tests/test_market_trait_flow.py tests/test_sign_result_endpoint.py
.venv/bin/mypy lfg_service/app.py
git add lfg_service/app.py tests/test_fee_cover_api.py tests/test_market_trait_flow.py tests/test_sign_result_endpoint.py
git commit -m "feat(fee-cover): settlement sweep and startup refund recovery"
```

---
### Task 8: Admin endpoints + SDK client methods

**Files:**
- Modify: `lfg_service/app.py`:
  - Admin helpers and four handlers, directly after
    `handle_sponsored_mint_status`.
  - Routes next to the sponsored-mint admin routes.
- Modify: `surfaces/_client/client.py` (four methods after `sponsored_mint_stop`)
- Test: `tests/test_fee_cover_admin_api.py`

**Interfaces:**
- Consumes:
  - Tasks 1–2: `fee_cover_store.connect`, `Knobs`, `start_campaign`,
    `update_campaign`, `stop_campaign`, `status_summary`.
  - Task 4: `xrpl_ops.get_xrp_balance_drops`.
  - Task 6: `_fee_cover_db`.
  - Existing: `require_service_token`, `_require_discord_surface`.
- Produces (used by Task 9):
  - `GET /api/admin/fee-cover/status`
  - `POST /api/admin/fee-cover/start` with body `{actor, coverage_pct,
    budget_xrp, wallet_cap_xrp, min_bid_xrp, duration_hours?}`
  - `POST /api/admin/fee-cover/update` with the same body minus
    `duration_hours`
  - `POST /api/admin/fee-cover/stop` with body `{actor}`
  - Response: `status_summary` plus `result` (on mutations) and
    `issuer_balance_drops: int | None`. 400 `{"code": "bad_request"}` on bad
    input.
  - SDK: `LFGServiceClient.fee_cover_status()`,
    `fee_cover_start(actor, **fields)`, `fee_cover_update(actor, **fields)`,
    `fee_cover_stop(actor)`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_fee_cover_admin_api.py
"""Discord-only admin endpoints for the fee-cover campaign + SDK contract."""

from __future__ import annotations

import asyncio
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from lfg_service import app as server
from surfaces._client.client import LFGServiceClient

DISCORD = {"Authorization": "Bearer tok-d"}
TELEGRAM = {"Authorization": "Bearer tok-t"}
KNOBS = {"coverage_pct": "100", "budget_xrp": "50", "wallet_cap_xrp": "5", "min_bid_xrp": "1"}


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _Request:
    def __init__(self, headers, body=None):
        self.headers = headers
        self._body = body or {}
        self._store = {}

    def __getitem__(self, key):
        return self._store[key]

    def __setitem__(self, key, value):
        self._store[key] = value

    async def json(self):
        return self._body


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("SERVICE_TOKEN_DISCORD", "tok-d")
    monkeypatch.setenv("SERVICE_TOKEN_TELEGRAM", "tok-t")
    app_db = str(tmp_path / "app.db")
    monkeypatch.setattr(server, "_fee_cover_db", lambda: app_db)
    monkeypatch.setattr(server.config, "XRPL_NETWORK", "testnet")

    async def balance(address):
        return 123_000_000

    monkeypatch.setattr(server.xrpl_ops, "get_xrp_balance_drops", balance)


def _call(name, headers=DISCORD, body=None):
    resp = _run(getattr(server, name)(_Request(headers, body)))
    return resp.status, json.loads(resp.body)


_HANDLERS = (
    ("handle_fee_cover_status", None),
    ("handle_fee_cover_start", {"actor": "discord:1", **KNOBS}),
    ("handle_fee_cover_update", {"actor": "discord:1", **KNOBS}),
    ("handle_fee_cover_stop", {"actor": "discord:1"}),
)


@pytest.mark.parametrize(("name", "body"), _HANDLERS)
def test_requires_a_service_token(name, body):
    assert _call(name, headers={}, body=body)[0] == 401


@pytest.mark.parametrize(("name", "body"), _HANDLERS)
def test_rejects_non_discord_surfaces(name, body):
    status, payload = _call(name, headers=TELEGRAM, body=body)
    assert status == 403 and payload["code"] == "wrong_surface"


def test_status_before_any_campaign():
    status, payload = _call("handle_fee_cover_status")
    assert status == 200
    assert payload["state"] == "never_started" and payload["campaign"] is None
    assert payload["issuer_balance_drops"] == 123_000_000


@pytest.mark.parametrize(
    "body",
    [
        {**KNOBS},  # no actor
        {"actor": " ", **KNOBS},
        {"actor": "discord:1", **KNOBS, "coverage_pct": "100.5"},
        {"actor": "discord:1", **KNOBS, "coverage_pct": "12.345"},
        {"actor": "discord:1", **KNOBS, "budget_xrp": "0"},
        {"actor": "discord:1", **KNOBS, "budget_xrp": "abc"},
        {"actor": "discord:1", **KNOBS, "wallet_cap_xrp": "NaN"},
        {"actor": "discord:1", **KNOBS, "min_bid_xrp": "0.0000001"},
        {"actor": "discord:1", **KNOBS, "duration_hours": "-1"},
    ],
)
def test_start_rejects_bad_input(body):
    status, payload = _call("handle_fee_cover_start", body=body)
    assert status == 400 and payload["code"] == "bad_request"


def test_start_update_stop_lifecycle():
    status, payload = _call("handle_fee_cover_start", body={"actor": "discord:1", **KNOBS, "duration_hours": "2"})
    assert status == 200 and payload["result"] == "started" and payload["state"] == "active"
    campaign = payload["campaign"]
    assert (campaign["coverage_bps"], campaign["budget_drops"], campaign["wallet_cap_drops"], campaign["min_bid_drops"]) == (
        10_000, 50_000_000, 5_000_000, 1_000_000,
    )
    assert campaign["ends_at"] == campaign["started_at"] + 7_200
    assert _call("handle_fee_cover_start", body={"actor": "discord:2", **KNOBS})[1]["result"] == "already_active"

    _, payload = _call("handle_fee_cover_update", body={"actor": "discord:1", **KNOBS, "coverage_pct": "50", "min_bid_xrp": "0"})
    assert payload["result"] == "updated"
    assert payload["campaign"]["coverage_bps"] == 5_000 and payload["campaign"]["min_bid_drops"] == 0

    _, payload = _call("handle_fee_cover_stop", body={"actor": "discord:1"})
    assert payload["result"] == "stopped" and payload["state"] == "stopped"
    assert _call("handle_fee_cover_stop", body={"actor": "discord:1"})[1]["result"] == "already_inactive"
    assert _call("handle_fee_cover_update", body={"actor": "discord:1", **KNOBS})[1]["result"] == "no_live_campaign"


def test_routes_are_registered():
    routes = {(r.method, r.resource.canonical) for r in server.create_app().router.routes() if r.resource}
    assert ("GET", "/api/admin/fee-cover/status") in routes
    for action in ("start", "update", "stop"):
        assert ("POST", f"/api/admin/fee-cover/{action}") in routes


def test_sdk_fee_cover_methods_send_the_service_token_and_payload():
    async def inner():
        app = web.Application()
        calls = []

        async def handler(request):
            body = await request.json() if request.method == "POST" else None
            calls.append((request.method, request.path, request.headers.get("Authorization"), body))
            return web.json_response({"state": "active"})

        app.router.add_get("/api/admin/fee-cover/status", handler)
        for action in ("start", "update", "stop"):
            app.router.add_post(f"/api/admin/fee-cover/{action}", handler)
        test_server = TestServer(app)
        await test_server.start_server()
        base = str(test_server.make_url("")).rstrip("/")
        client = LFGServiceClient(base, "svc-test", "discord", base_delay=0.0)
        async with client:
            assert (await client.fee_cover_status())["state"] == "active"
            await client.fee_cover_start("discord:42", coverage_pct="100", budget_xrp="50")
            await client.fee_cover_update("discord:42", coverage_pct="50")
            await client.fee_cover_stop("discord:42")
        await test_server.close()
        assert calls == [
            ("GET", "/api/admin/fee-cover/status", "Bearer svc-test", None),
            ("POST", "/api/admin/fee-cover/start", "Bearer svc-test",
             {"actor": "discord:42", "coverage_pct": "100", "budget_xrp": "50"}),
            ("POST", "/api/admin/fee-cover/update", "Bearer svc-test", {"actor": "discord:42", "coverage_pct": "50"}),
            ("POST", "/api/admin/fee-cover/stop", "Bearer svc-test", {"actor": "discord:42"}),
        ]

    _run(inner())
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_fee_cover_admin_api.py -v`
Expected: FAIL with `AttributeError: module 'lfg_service.app' has no attribute 'handle_fee_cover_status'`

- [ ] **Step 3: Write the implementation**

In `lfg_service/app.py`, directly after `handle_sponsored_mint_status`. Check
that `Decimal` and `InvalidOperation` are imported from `decimal`, and add
`InvalidOperation` if it is missing.

```python
# --- Fee cover campaign admin (Discord /admin only) ------------------------


async def _admin_json(request: Any) -> dict[str, Any]:
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return {}
    return body if isinstance(body, dict) else {}


def _xrp_field_drops(body: dict[str, Any], name: str, *, allow_zero: bool) -> int:
    try:
        value = Decimal(str(body.get(name)))
    except (InvalidOperation, ValueError):
        raise ValueError(f"{name} must be a number") from None
    if not value.is_finite() or value < 0 or (value == 0 and not allow_zero):
        raise ValueError(f"{name} must be {'zero or more' if allow_zero else 'greater than zero'}")
    drops = value * 1_000_000
    if drops != drops.to_integral_value():
        raise ValueError(f"{name} has more than 6 decimal places")
    return int(drops)


def _fee_cover_knobs(body: dict[str, Any]) -> fee_cover_store.Knobs:
    try:
        pct = Decimal(str(body.get("coverage_pct")))
    except (InvalidOperation, ValueError):
        raise ValueError("coverage_pct must be a number") from None
    if not pct.is_finite():
        raise ValueError("coverage_pct must be a number")
    bps = pct * 100
    if bps != bps.to_integral_value() or not 0 <= bps <= fee_cover_store.BPS:
        raise ValueError("coverage_pct must be between 0 and 100 with at most 2 decimals")
    knobs = fee_cover_store.Knobs(
        coverage_bps=int(bps),
        budget_drops=_xrp_field_drops(body, "budget_xrp", allow_zero=False),
        wallet_cap_drops=_xrp_field_drops(body, "wallet_cap_xrp", allow_zero=False),
        min_bid_drops=_xrp_field_drops(body, "min_bid_xrp", allow_zero=True),
    )
    knobs.validate()
    return knobs


def _fee_cover_duration(body: dict[str, Any]) -> int | None:
    raw = body.get("duration_hours")
    if raw is None or raw == "":
        return None
    try:
        hours = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        raise ValueError("duration_hours must be a number") from None
    if not hours.is_finite() or hours <= 0:
        raise ValueError("duration_hours must be greater than zero")
    return int(hours * 3600)


def _bad_request(message: str) -> web.Response:
    return web.json_response({"error": message, "code": "bad_request"}, status=400)


def _fee_cover_admin_run(action: Callable[[sqlite3.Connection], str | None]) -> dict[str, Any]:
    conn = fee_cover_store.connect(_fee_cover_db())
    try:
        result = action(conn)
        status = fee_cover_store.status_summary(conn, config.XRPL_NETWORK)
    finally:
        conn.close()
    if result is not None:
        status["result"] = result
    return status


async def _fee_cover_admin_response(action: Callable[[sqlite3.Connection], str | None]) -> web.Response:
    status = await asyncio.get_event_loop().run_in_executor(None, _fee_cover_admin_run, action)
    status["issuer_balance_drops"] = await xrpl_ops.get_xrp_balance_drops(config.SIGNING_ACCOUNT)
    return web.json_response(status)


@require_service_token
async def handle_fee_cover_status(request):
    denied = _require_discord_surface(request)
    if denied is not None:
        return denied
    return await _fee_cover_admin_response(lambda conn: None)


@require_service_token
async def handle_fee_cover_start(request):
    denied = _require_discord_surface(request)
    if denied is not None:
        return denied
    body = await _admin_json(request)
    actor = body.get("actor")
    if not isinstance(actor, str) or not actor.strip():
        return _bad_request("actor is required")
    try:
        knobs = _fee_cover_knobs(body)
        duration = _fee_cover_duration(body)
    except ValueError as e:
        return _bad_request(str(e))
    return await _fee_cover_admin_response(
        lambda conn: fee_cover_store.start_campaign(
            conn, network=config.XRPL_NETWORK, actor=actor, knobs=knobs, duration_seconds=duration
        )[1]
    )


@require_service_token
async def handle_fee_cover_update(request):
    denied = _require_discord_surface(request)
    if denied is not None:
        return denied
    body = await _admin_json(request)
    actor = body.get("actor")
    if not isinstance(actor, str) or not actor.strip():
        return _bad_request("actor is required")
    try:
        knobs = _fee_cover_knobs(body)
    except ValueError as e:
        return _bad_request(str(e))
    return await _fee_cover_admin_response(
        lambda conn: fee_cover_store.update_campaign(conn, network=config.XRPL_NETWORK, actor=actor, knobs=knobs)[1]
    )


@require_service_token
async def handle_fee_cover_stop(request):
    denied = _require_discord_surface(request)
    if denied is not None:
        return denied
    body = await _admin_json(request)
    actor = body.get("actor")
    if not isinstance(actor, str) or not actor.strip():
        return _bad_request("actor is required")
    return await _fee_cover_admin_response(
        lambda conn: fee_cover_store.stop_campaign(conn, network=config.XRPL_NETWORK, actor=actor)[1]
    )
```

Routes in `create_app`, directly after the three `/api/admin/sponsored-mint/*` routes:

```python
    app.router.add_get("/api/admin/fee-cover/status", handle_fee_cover_status)
    app.router.add_post("/api/admin/fee-cover/start", handle_fee_cover_start)
    app.router.add_post("/api/admin/fee-cover/update", handle_fee_cover_update)
    app.router.add_post("/api/admin/fee-cover/stop", handle_fee_cover_stop)
```

`surfaces/_client/client.py`, directly after `sponsored_mint_stop`:

```python
    # ---- admin: marketplace fee cover (spec 2026-09-14) ----

    async def fee_cover_status(self) -> dict[str, Any]:
        return await self._request("GET", "/api/admin/fee-cover/status", token=self._service_token)

    async def fee_cover_start(self, actor: str, **fields: str) -> dict[str, Any]:
        return await self._request(
            "POST", "/api/admin/fee-cover/start", token=self._service_token, json={"actor": actor, **fields}
        )

    async def fee_cover_update(self, actor: str, **fields: str) -> dict[str, Any]:
        return await self._request(
            "POST", "/api/admin/fee-cover/update", token=self._service_token, json={"actor": actor, **fields}
        )

    async def fee_cover_stop(self, actor: str) -> dict[str, Any]:
        return await self._request(
            "POST", "/api/admin/fee-cover/stop", token=self._service_token, json={"actor": actor}
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_fee_cover_admin_api.py tests/test_sponsored_admin.py -q`
Expected: all pass. `test_fee_cover_admin_api.py` has 21 tests: 4 + 4
parametrized auth, 1 status, 9 bad-input, 1 lifecycle, 1 routes, 1 SDK.

- [ ] **Step 5: Format, type-check, commit**

```bash
.venv/bin/ruff format lfg_service/app.py surfaces/_client/client.py tests/test_fee_cover_admin_api.py
.venv/bin/ruff check --fix lfg_service/app.py surfaces/_client/client.py tests/test_fee_cover_admin_api.py
.venv/bin/mypy lfg_service/app.py surfaces/_client/client.py
git add lfg_service/app.py surfaces/_client/client.py tests/test_fee_cover_admin_api.py
git commit -m "feat(fee-cover): Discord-only admin endpoints and SDK methods"
```

---
### Task 9: Discord `/admin` fee-cover sub-panel

**Files:**
- Create: `surfaces/discord_bot/fee_cover_admin.py`
- Modify: `surfaces/discord_bot/admin.py`:
  - Import `fee_cover_admin`.
  - Add a "💸 Fee Cover" button (row 3) to `AdminView`.
  - Add a bullet to the panel description.
- Test: `tests/test_discord_fee_cover_admin.py`

**Interfaces:**
- Consumes:
  - Task 8: `svc.fee_cover_status()`, `fee_cover_start(actor, **fields)`,
    `fee_cover_update(actor, **fields)`, `fee_cover_stop(actor)`. The status
    payload is `status_summary` plus `result` and `issuer_balance_drops`.
  - `admin._admin_interaction_check` and `admin.log_admin_action`, passed in
    to avoid an import cycle.
- Produces:
  - `parse_form(*, coverage, budget, wallet_cap, min_bid, duration) ->
    dict[str, str]`
  - `fee_cover_status_embed(status) -> discord.Embed`
  - `FeeCoverModal(mode, check, log)`
  - `FeeCoverView(check, log)`
  - `AdminView.fee_cover_button`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_discord_fee_cover_admin.py
"""Discord /admin fee-cover sub-panel (spec §Admin panel)."""

import asyncio
import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from surfaces._client.errors import ServiceError


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _status(**over):
    base = {
        "network": "mainnet",
        "state": "active",
        "campaign": {
            "id": 1, "coverage_bps": 10_000, "budget_drops": 50_000_000, "wallet_cap_drops": 5_000_000,
            "wallet_window_seconds": 2_592_000, "min_bid_drops": 1_000_000, "ends_at": None,
        },
        "committed_drops": 80_642,
        "paid_drops": 0,
        "remaining_drops": 49_919_358,
        "open_promises": 1,
        "refunds_by_state": {"owed": 1},
        "declines_by_reason": {"below_clearing": 2},
        "top_wallets": [{"wallet": "rHaMsAjoAN21s1XG5TCAM6ErAefzrggsHf", "drops": 80_642}],
        "issuer_balance_drops": 123_000_000,
    }
    base.update(over)
    return base


class _Svc:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    async def fee_cover_status(self):
        self.calls.append(("status",))
        return _status()

    async def fee_cover_start(self, actor, **fields):
        self.calls.append(("start", actor, fields))
        return _status(result="started")

    async def fee_cover_update(self, actor, **fields):
        self.calls.append(("update", actor, fields))
        return _status(result="updated")

    async def fee_cover_stop(self, actor):
        self.calls.append(("stop", actor))
        if self.fail:
            raise ServiceError("service unavailable", status=503)
        return _status(state="stopped", result="stopped")


def _interaction(administrator=True):
    record = {"sent": [], "followups": [], "deferred": 0, "modals": []}

    async def defer(ephemeral=True):
        record["deferred"] += 1

    async def send_message(content=None, embed=None, ephemeral=False):
        record["sent"].append((content, embed))

    async def send_modal(modal):
        record["modals"].append(modal)

    async def followup_send(content=None, embed=None, view=None, ephemeral=True):
        record["followups"].append({"content": content, "embed": embed, "view": view})

    inter = SimpleNamespace(
        user=SimpleNamespace(id=9, guild_permissions=SimpleNamespace(administrator=administrator)),
        client=object(),
        response=SimpleNamespace(defer=defer, send_message=send_message, send_modal=send_modal,
                                 is_done=lambda: False),
        followup=SimpleNamespace(send=followup_send),
    )
    return inter, record


@pytest.fixture
def mods(monkeypatch):
    for k, v in {
        "DISCORD_BOT_TOKEN": "t",
        "ADMIN_LOG_CHANNEL_ID": "1",
        "LFG_SERVICE_URL": "http://svc",
        "SERVICE_TOKEN_DISCORD": "s",
        "SEED": "sEdSKaCy2JT7JaM7v95H9SxkhP9wS2r",
        "XUMM_API_KEY": "k",
        "XUMM_API_SECRET": "s",
        "TOKEN_ISSUER_ADDRESS": "rIssuer",
        "TOKEN_CURRENCY_HEX": "ABC",
    }.items():
        monkeypatch.setenv(k, v)
    import surfaces.discord_bot.config as cfg

    importlib.reload(cfg)
    import surfaces.discord_bot.admin as admin
    import surfaces.discord_bot.fee_cover_admin as fca

    return admin, fca


def test_parse_form_normalizes_numbers_and_omits_blank_duration(mods):
    _, fca = mods
    assert fca.parse_form(coverage="100", budget="50.50", wallet_cap="5", min_bid="0", duration=" ") == {
        "coverage_pct": "100", "budget_xrp": "50.5", "wallet_cap_xrp": "5", "min_bid_xrp": "0",
    }
    assert fca.parse_form(coverage="50", budget="1", wallet_cap="1", min_bid="1", duration="24")["duration_hours"] == "24"


@pytest.mark.parametrize(
    "fields",
    [
        {"coverage": "abc", "budget": "1", "wallet_cap": "1", "min_bid": "0", "duration": ""},
        {"coverage": "101", "budget": "1", "wallet_cap": "1", "min_bid": "0", "duration": ""},
        {"coverage": "100", "budget": "0", "wallet_cap": "1", "min_bid": "0", "duration": ""},
        {"coverage": "100", "budget": "1", "wallet_cap": "1", "min_bid": "-1", "duration": ""},
        {"coverage": "100", "budget": "1", "wallet_cap": "1", "min_bid": "0", "duration": "0"},
    ],
)
def test_parse_form_rejects_bad_input(mods, fields):
    _, fca = mods
    with pytest.raises(ValueError):
        fca.parse_form(**fields)


def test_status_embed_renders_the_operator_numbers(mods):
    _, fca = mods
    fields = {f.name: f.value for f in fca.fee_cover_status_embed(_status()).fields}
    assert fields["Campaign"] == "Active"
    assert fields["Coverage"] == "100% of broker fee"
    assert fields["Budget"] == "0.080642 / 50 XRP committed"
    assert fields["Paid"] == "0 XRP"
    assert fields["Remaining"] == "49.919358 XRP"
    assert fields["Per-wallet cap"] == "5 XRP / 30d"
    assert fields["Min bid"] == "1 XRP"
    assert fields["Ends"] == "No end"
    assert fields["Refunds"] == "owed 1"
    assert fields["Declines"] == "below_clearing 2"
    assert fields["Top wallets"] == "`rHaMsAjoAN…` 0.080642 XRP"
    assert fields["Issuer XRP balance"] == "123 XRP"
    never = {f.name: f.value for f in fca.fee_cover_status_embed(
        _status(state="never_started", campaign=None, issuer_balance_drops=None)).fields}
    assert never["Campaign"] == "Never Started" and "Coverage" not in never
    assert never["Issuer XRP balance"] == "Balance lookup failed"


def test_start_modal_submits_the_form_and_logs(mods, monkeypatch):
    admin, fca = mods
    svc = _Svc()
    log = AsyncMock()
    monkeypatch.setattr(fca, "svc", svc)
    modal = fca.FeeCoverModal("start", admin._admin_interaction_check, log)
    modal.budget._value = "25"
    modal.duration._value = "48"
    inter, record = _interaction()
    _run(modal.on_submit(inter))
    assert svc.calls == [("start", "discord:9", {"coverage_pct": "100", "budget_xrp": "25", "wallet_cap_xrp": "5",
                                                  "min_bid_xrp": "1", "duration_hours": "48"})]
    log.assert_awaited_once()
    assert record["followups"][-1]["embed"] is not None


def test_update_modal_ignores_duration(mods, monkeypatch):
    admin, fca = mods
    svc = _Svc()
    monkeypatch.setattr(fca, "svc", svc)
    modal = fca.FeeCoverModal("update", admin._admin_interaction_check, AsyncMock())
    modal.duration._value = "48"
    inter, _ = _interaction()
    _run(modal.on_submit(inter))
    assert svc.calls[0][0] == "update" and "duration_hours" not in svc.calls[0][2]


def test_modal_rejects_bad_input_without_calling_the_service(mods, monkeypatch):
    admin, fca = mods
    svc = _Svc()
    monkeypatch.setattr(fca, "svc", svc)
    modal = fca.FeeCoverModal("start", admin._admin_interaction_check, AsyncMock())
    modal.coverage._value = "150"
    inter, record = _interaction()
    _run(modal.on_submit(inter))
    assert svc.calls == []
    assert record["sent"][0][0].startswith("❌")


def test_modal_denies_non_administrators(mods, monkeypatch):
    admin, fca = mods
    svc = _Svc()
    monkeypatch.setattr(fca, "svc", svc)
    modal = fca.FeeCoverModal("start", admin._admin_interaction_check, AsyncMock())
    inter, record = _interaction(administrator=False)
    _run(modal.on_submit(inter))
    assert svc.calls == []
    assert record["sent"] == [("Administrator permission required.", None)]


def test_view_buttons(mods, monkeypatch):
    admin, fca = mods
    svc = _Svc()
    log = AsyncMock()
    monkeypatch.setattr(fca, "svc", svc)
    view = fca.FeeCoverView(admin._admin_interaction_check, log)

    inter, record = _interaction()
    _run(view.start_button.callback(inter))
    assert isinstance(record["modals"][0], fca.FeeCoverModal) and record["modals"][0].mode == "start"

    inter, record = _interaction()
    _run(view.update_button.callback(inter))
    assert record["modals"][0].mode == "update"

    inter, record = _interaction()
    _run(view.stop_button.callback(inter))
    assert svc.calls[-1] == ("stop", "discord:9")
    log.assert_awaited_once()
    assert record["followups"][-1]["embed"] is not None

    inter, record = _interaction()
    _run(view.refresh_button.callback(inter))
    assert svc.calls[-1] == ("status",)
    log.assert_awaited_once()  # refresh does not log


def test_stop_service_error_is_reported(mods, monkeypatch):
    admin, fca = mods
    monkeypatch.setattr(fca, "svc", _Svc(fail=True))
    view = fca.FeeCoverView(admin._admin_interaction_check, AsyncMock())
    inter, record = _interaction()
    _run(view.stop_button.callback(inter))
    assert record["followups"][-1]["content"].startswith("❌")


def test_admin_panel_button_opens_the_sub_panel(mods, monkeypatch):
    admin, fca = mods
    svc = _Svc()
    monkeypatch.setattr(admin, "svc", svc)
    view = admin.AdminView()
    inter, record = _interaction()
    _run(view.fee_cover_button.callback(inter))
    assert svc.calls == [("status",)]
    sent = record["followups"][-1]
    assert isinstance(sent["view"], fca.FeeCoverView)
    assert sent["embed"] is not None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_discord_fee_cover_admin.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'surfaces.discord_bot.fee_cover_admin'`

- [ ] **Step 3: Write the implementation**

```python
# surfaces/discord_bot/fee_cover_admin.py
"""Discord /admin sub-panel for the marketplace fee-cover campaign.

Spec: docs/superpowers/specs/2026-09-14-marketplace-fee-cover-design.md
(§Admin panel). The service is the authority: this module parses the
operator's form, calls the service, and renders its status. The admin gate
and the audit-log poster are passed in, so this module never imports admin.py
(which imports it).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from decimal import Decimal, InvalidOperation
from typing import Any

import discord
from discord import Embed
from discord.ui import Button, Modal, TextInput, View

from surfaces._client.errors import ServiceError
from surfaces.discord_bot.bot import svc

AdminCheck = Callable[[discord.Interaction], Awaitable[bool]]
AdminLog = Callable[[Any, str], Awaitable[None]]


def _xrp(drops: Any) -> str:
    return format((Decimal(int(drops)) / Decimal(1_000_000)).normalize(), "f")


def parse_form(*, coverage: str, budget: str, wallet_cap: str, min_bid: str, duration: str) -> dict[str, str]:
    """Validate the modal's text fields into the service payload. Raises
    ValueError with an operator-readable message."""

    def number(label: str, raw: str, *, allow_zero: bool = False, maximum: Decimal | None = None) -> str:
        try:
            value = Decimal((raw or "").strip())
        except InvalidOperation:
            raise ValueError(f"{label} must be a number") from None
        if not value.is_finite() or value < 0 or (value == 0 and not allow_zero):
            raise ValueError(f"{label} must be {'zero or more' if allow_zero else 'greater than zero'}")
        if maximum is not None and value > maximum:
            raise ValueError(f"{label} must be at most {maximum}")
        return format(value.normalize(), "f")

    out = {
        "coverage_pct": number("Coverage %", coverage, allow_zero=True, maximum=Decimal(100)),
        "budget_xrp": number("Budget", budget),
        "wallet_cap_xrp": number("Per-wallet cap", wallet_cap),
        "min_bid_xrp": number("Minimum bid", min_bid, allow_zero=True),
    }
    if duration and duration.strip():
        out["duration_hours"] = number("Duration hours", duration)
    return out


def fee_cover_status_embed(status: dict[str, Any]) -> Embed:
    embed = Embed(title="💸 Fee Cover Status", color=0x9C84EF)
    embed.add_field(name="Campaign", value=str(status.get("state", "unknown")).replace("_", " ").title(), inline=True)
    campaign = status.get("campaign")
    if campaign:
        embed.add_field(name="Coverage", value=f"{int(campaign['coverage_bps']) / 100:g}% of broker fee", inline=True)
        embed.add_field(
            name="Budget",
            value=f"{_xrp(status.get('committed_drops', 0))} / {_xrp(campaign['budget_drops'])} XRP committed",
            inline=True,
        )
        embed.add_field(name="Paid", value=f"{_xrp(status.get('paid_drops', 0))} XRP", inline=True)
        embed.add_field(name="Remaining", value=f"{_xrp(status.get('remaining_drops', 0))} XRP", inline=True)
        embed.add_field(
            name="Per-wallet cap",
            value=f"{_xrp(campaign['wallet_cap_drops'])} XRP / {int(campaign['wallet_window_seconds']) // 86_400}d",
            inline=True,
        )
        embed.add_field(name="Min bid", value=f"{_xrp(campaign['min_bid_drops'])} XRP", inline=True)
        ends_at = campaign.get("ends_at")
        embed.add_field(name="Ends", value=f"<t:{ends_at}:f>" if ends_at else "No end", inline=True)
    embed.add_field(name="Open promises", value=str(status.get("open_promises", 0)), inline=True)
    refunds = status.get("refunds_by_state") or {}
    embed.add_field(
        name="Refunds", value=" · ".join(f"{k} {v}" for k, v in sorted(refunds.items())) or "None", inline=False
    )
    declines = status.get("declines_by_reason") or {}
    embed.add_field(
        name="Declines", value=" · ".join(f"{k} {v}" for k, v in sorted(declines.items())) or "None", inline=False
    )
    top = status.get("top_wallets") or []
    embed.add_field(
        name="Top wallets",
        value="\n".join(f"`{w['wallet'][:10]}…` {_xrp(w['drops'])} XRP" for w in top) or "None",
        inline=False,
    )
    balance = status.get("issuer_balance_drops")
    embed.add_field(
        name="Issuer XRP balance",
        value=f"{_xrp(balance)} XRP" if balance is not None else "Balance lookup failed",
        inline=True,
    )
    return embed


class FeeCoverModal(Modal, title="Fee cover campaign"):
    coverage: TextInput[Any] = TextInput(label="Coverage (% of marketplace fee)", default="100", max_length=6)
    budget: TextInput[Any] = TextInput(label="Budget (XRP)", default="50", max_length=14)
    wallet_cap: TextInput[Any] = TextInput(label="Per-wallet cap (XRP per 30 days)", default="5", max_length=14)
    min_bid: TextInput[Any] = TextInput(label="Minimum bid (XRP)", default="1", max_length=14)
    duration: TextInput[Any] = TextInput(
        label="Duration hours (Start only; blank = no end)", required=False, max_length=6
    )

    def __init__(self, mode: str, check: AdminCheck, log: AdminLog):
        super().__init__()
        self.mode = mode
        self._check = check
        self._log = log

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await self._check(interaction):
            return
        try:
            fields = parse_form(
                coverage=self.coverage.value,
                budget=self.budget.value,
                wallet_cap=self.wallet_cap.value,
                min_bid=self.min_bid.value,
                duration=self.duration.value if self.mode == "start" else "",
            )
        except ValueError as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        actor = f"discord:{interaction.user.id}"
        try:
            if self.mode == "start":
                status = await svc.fee_cover_start(actor, **fields)
            else:
                status = await svc.fee_cover_update(actor, **fields)
        except ServiceError as e:
            logging.error("fee cover %s failed: %s", self.mode, e)
            await interaction.followup.send(f"❌ Fee cover {self.mode} failed: {e.message}", ephemeral=True)
            return
        await self._log(interaction.client, f"💸 Fee cover {self.mode} ({status.get('result')}) by {actor}: {fields}")
        await interaction.followup.send(embed=fee_cover_status_embed(status), ephemeral=True)


class FeeCoverView(View):
    def __init__(self, check: AdminCheck, log: AdminLog):
        super().__init__(timeout=600)
        self._check = check
        self._log = log

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await self._check(interaction)

    @discord.ui.button(label="▶️ Start", style=discord.ButtonStyle.success)
    async def start_button(self, interaction: discord.Interaction, button: Button[Any]) -> None:
        await interaction.response.send_modal(FeeCoverModal("start", self._check, self._log))

    @discord.ui.button(label="✏️ Update", style=discord.ButtonStyle.primary)
    async def update_button(self, interaction: discord.Interaction, button: Button[Any]) -> None:
        await interaction.response.send_modal(FeeCoverModal("update", self._check, self._log))

    @discord.ui.button(label="⏹️ Stop", style=discord.ButtonStyle.danger)
    async def stop_button(self, interaction: discord.Interaction, button: Button[Any]) -> None:
        await interaction.response.defer(ephemeral=True)
        actor = f"discord:{interaction.user.id}"
        try:
            status = await svc.fee_cover_stop(actor)
        except ServiceError as e:
            logging.error("fee cover stop failed: %s", e)
            await interaction.followup.send(f"❌ Fee cover stop failed: {e.message}", ephemeral=True)
            return
        await self._log(interaction.client, f"💸 Fee cover stop ({status.get('result')}) by {actor}")
        await interaction.followup.send(embed=fee_cover_status_embed(status), ephemeral=True)

    @discord.ui.button(label="🔄 Refresh", style=discord.ButtonStyle.secondary)
    async def refresh_button(self, interaction: discord.Interaction, button: Button[Any]) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            status = await svc.fee_cover_status()
        except ServiceError as e:
            await interaction.followup.send(f"❌ Fee cover status failed: {e.message}", ephemeral=True)
            return
        await interaction.followup.send(embed=fee_cover_status_embed(status), ephemeral=True)
```

In `surfaces/discord_bot/admin.py`:
- Add `from surfaces.discord_bot import fee_cover_admin` directly before
  `from surfaces.discord_bot.bot import svc, tree` (isort order).
- Add this method to `AdminView`, after `sponsored_refresh_button`:

```python
    @discord.ui.button(label="💸 Fee Cover", style=discord.ButtonStyle.secondary, row=3)
    async def fee_cover_button(self, interaction: discord.Interaction, button: Button[Any]):
        await interaction.response.defer(ephemeral=True)
        try:
            status = await svc.fee_cover_status()
        except ServiceError as e:
            logging.error("Fee cover status failed: %s", e)
            await interaction.followup.send(f"❌ Failed to load fee cover: {e.message}", ephemeral=True)
            return
        await interaction.followup.send(
            embed=fee_cover_admin.fee_cover_status_embed(status),
            view=fee_cover_admin.FeeCoverView(_admin_interaction_check, log_admin_action),
            ephemeral=True,
        )
```

In `admin_command`, add the line `"• 💸 Fee Cover - Marketplace fee-cover campaign\n"`
to the embed description, after the Burn NFT bullet.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_discord_fee_cover_admin.py tests/test_discord_admin_x_toggle.py -q`
Expected: all pass. `test_discord_fee_cover_admin.py` has 14 tests; the
existing admin panel tests are unchanged.

- [ ] **Step 5: Format, type-check, commit**

```bash
.venv/bin/ruff format surfaces/discord_bot/fee_cover_admin.py surfaces/discord_bot/admin.py tests/test_discord_fee_cover_admin.py
.venv/bin/ruff check --fix surfaces/discord_bot/fee_cover_admin.py surfaces/discord_bot/admin.py tests/test_discord_fee_cover_admin.py
.venv/bin/mypy surfaces/discord_bot/fee_cover_admin.py surfaces/discord_bot/admin.py
git add surfaces/discord_bot/fee_cover_admin.py surfaces/discord_bot/admin.py tests/test_discord_fee_cover_admin.py
git commit -m "feat(fee-cover): Discord /admin sub-panel to start, tune and stop the campaign"
```

---
### Task 10: Browse — fee-cover estimate on external rows (post-cache)

**Files:**
- Modify: `lfg_service/app.py`:
  - Add `_fee_cover_browse_state` and `_apply_fee_cover_estimate` to the Task
    6 fee-cover helper block.
  - Apply the estimate at the end of `handle_market_listings`.
- Test: `tests/test_fee_cover_api.py` (append)

**Interfaces:**
- Consumes:
  - Tasks 1–6: `fee_cover_store.live_campaign`, `committed_drops`,
    `fee_cover.promise_drops`, `_fee_cover_db`, `_fee_cover_safe`.
  - Existing serialized external row fields `clearing_drops` and
    `broker_rate`.
- Produces: external character rows on
  `GET /api/market/listings?include_external=1` gain `fee_cover_drops: int`
  and `fee_cover_xrp: str` when a live campaign covers them (used by Task 11).

- [ ] **Step 1: Write the failing tests** (append to `tests/test_fee_cover_api.py`)

```python
# --- Task 10: browse estimate -------------------------------------------------


def _browse():
    req = make_mocked_request("GET", "/api/market/listings?include_external=1")
    rows = _body(_run(server.handle_market_listings(req)))["rows"]
    return next(r for r in rows if r.get("source") == "external")


def test_browse_external_row_carries_the_fee_cover_estimate(env):
    _seed_char_with_cafe_listing(env["onchain"])
    _campaign(env["app_db"])
    row = _browse()
    assert row["clearing_drops"] == 5_070_572
    assert row["fee_cover_drops"] == 80_572  # ceil(5_070_572 × 0.01589) at 100% coverage
    assert row["fee_cover_xrp"] == "0.080572"


def test_browse_estimate_follows_the_campaign_not_the_cache(env):
    _seed_char_with_cafe_listing(env["onchain"])
    assert "fee_cover_drops" not in _browse()  # no campaign; cache now warm
    _campaign(env["app_db"])
    assert _browse()["fee_cover_drops"] == 80_572  # same cached rows, fresh campaign read
    conn = fee_cover_store.connect(env["app_db"])
    fee_cover_store.stop_campaign(conn, network="testnet", actor="t")
    conn.close()
    assert "fee_cover_drops" not in _browse()


def test_browse_estimate_is_hidden_without_budget_headroom(env):
    _seed_char_with_cafe_listing(env["onchain"])
    conn = fee_cover_store.connect(env["app_db"])
    knobs = fee_cover_store.Knobs(coverage_bps=10_000, budget_drops=80_571, wallet_cap_drops=5_000_000, min_bid_drops=0)
    fee_cover_store.start_campaign(conn, network="testnet", actor="t", knobs=knobs, duration_seconds=None)
    conn.close()
    assert "fee_cover_drops" not in _browse()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_fee_cover_api.py -k browse -v`
Expected: FAIL with `KeyError: 'fee_cover_drops'` (the first and second tests).

- [ ] **Step 3: Write the implementation**

Add to the fee-cover helper block in `lfg_service/app.py`:

```python
def _fee_cover_browse_state() -> tuple[int, int, int] | None:
    """(coverage_bps, remaining_budget_drops, min_bid_drops) of the live
    campaign, or None. Read on every browse, AFTER the 60 s market cache, so
    a Stop is reflected immediately."""
    conn = fee_cover_store.connect(_fee_cover_db())
    try:
        campaign = fee_cover_store.live_campaign(conn, config.XRPL_NETWORK)
        if campaign is None:
            return None
        remaining = campaign.budget_drops - fee_cover_store.committed_drops(conn, campaign.id)
    finally:
        conn.close()
    return campaign.coverage_bps, remaining, campaign.min_bid_drops


def _apply_fee_cover_estimate(out: dict[str, Any], state: tuple[int, int, int]) -> None:
    """Public, unauthenticated estimate for a Buy-now at the clearing price.
    Ignores per-wallet caps (the client copy says "limits apply")."""
    coverage_bps, remaining, min_bid = state
    clearing = out.get("clearing_drops")
    rate = out.get("broker_rate")
    if not isinstance(clearing, int) or rate is None or clearing < min_bid:
        return
    estimate = fee_cover.promise_drops(clearing, float(rate), coverage_bps)
    if 0 < estimate <= remaining:
        out["fee_cover_drops"] = estimate
        out["fee_cover_xrp"] = market_ops.drops_to_xrp_str(str(estimate))
```

In `handle_market_listings`, replace the final
`return web.json_response({"rows": rows_out, "total": len(filtered)})` with:

```python
    if include_external and kind == "character":
        fee_cover_state = await asyncio.get_event_loop().run_in_executor(
            None, _fee_cover_safe, _fee_cover_browse_state
        )
        if fee_cover_state is not None:
            for out in rows_out:
                _apply_fee_cover_estimate(out, fee_cover_state)
    return web.json_response({"rows": rows_out, "total": len(filtered)})
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_fee_cover_api.py tests/test_market_api.py -q`
Expected: all pass. `test_fee_cover_api.py` now has 15 tests.

- [ ] **Step 5: Format, type-check, commit**

```bash
.venv/bin/ruff format lfg_service/app.py tests/test_fee_cover_api.py
.venv/bin/ruff check --fix lfg_service/app.py tests/test_fee_cover_api.py
.venv/bin/mypy lfg_service/app.py
git add lfg_service/app.py tests/test_fee_cover_api.py
git commit -m "feat(fee-cover): browse estimate on external Buy-now rows"
```

---
### Task 11: Client — fee-cover copy, refund polling, cache-busters

**Files:**
- Modify: `webapp/client/market_pure.js`:
  - `mapListingRow.feeCoverXrp`.
  - New `feeCoverNote`, `FEE_COVER_DECLINE_COPY`, `feeCoverLine` and
    `feeCoverPending`.
  - `externalFillCopy` gains a third argument.
- Modify: `webapp/client/app.js`: the detail fee note, `buyExternalNow`,
  `marketExternalBuyRender`, `watchExternalFill`, and the `market_pure.js?v=`
  import.
- Modify: `webapp/client/index.html` (`app.js?v=`)
- Modify: `tests/test_app_js_deeplink.py`, `tests/test_harvest_pure_js.py` (pinned `app.js?v=`)
- Test: `tests/test_fee_cover_client.py`

**Interfaces:**
- Consumes:
  - Task 6: bid session and status JSON `fee_cover` (`{state, drops, xrp,
    reason, payout_tx_hash}` or null).
  - Task 10: listing row `fee_cover_xrp`.
- Produces:
  - `feeCoverNote(vm) -> string`
  - `feeCoverLine(feeCover) -> string`
  - `feeCoverPending(feeCover) -> boolean`
  - `externalFillCopy(marketplace, fill, feeCover = null)`
  - `FEE_COVER_DECLINE_COPY`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_fee_cover_client.py
"""Fee-cover client copy (executes market_pure.js under Node, like
tests/test_market_pure_js.py) plus app.js wiring assertions."""

import json
import os
import re
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULE_REL = "./webapp/client/market_pure.js"
NODE = shutil.which("node")


def run_js(expr):
    if NODE is None:
        pytest.skip("node is not installed on this host")
    script = (
        f"import * as M from {json.dumps(MODULE_REL)};\n"
        f"const result = ({expr});\n"
        "console.log(JSON.stringify(result === undefined ? null : result));\n"
    )
    proc = subprocess.run([NODE, "--input-type=module"], input=script, capture_output=True, text=True, cwd=ROOT, timeout=15)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


EXTERNAL_VM = '{external: true, clearingXrp: "5.070572", amountXrp: "4.99", marketplace: "xrp.cafe", feeCoverXrp: "0.080572"}'


def _fc(state, reason=None):
    return json.dumps({"state": state, "drops": 80642, "xrp": "0.080642", "reason": reason, "payout_tx_hash": None})


def test_map_listing_row_carries_the_estimate():
    assert run_js('M.mapListingRow({kind: "character", fee_cover_xrp: "0.080572"}).feeCoverXrp') == "0.080572"
    assert run_js('M.mapListingRow({kind: "character"}).feeCoverXrp') is None


def test_fee_cover_note():
    assert run_js(f"M.feeCoverNote({EXTERNAL_VM})") == (
        "LFG refunds xrp.cafe's 0.080572 XRP fee after it settles, so you pay the 4.99 XRP ask (campaign limits apply)."
    )
    assert run_js('M.feeCoverNote({external: true, clearingXrp: "5", feeCoverXrp: null})') == ""
    assert run_js('M.feeCoverNote({external: false, clearingXrp: "5", feeCoverXrp: "1"})') == ""


@pytest.mark.parametrize(
    ("state", "reason", "expected"),
    [
        ("quoted", None, "LFG covers the marketplace fee on this purchase (campaign limits apply)."),
        ("open", None, "Fee refund of 0.080642 XRP reserved."),
        ("owed", None, "Your 0.080642 XRP fee refund is on its way."),
        ("submitted", None, "Your 0.080642 XRP fee refund is on its way."),
        ("confirmed", None, "Refunded 0.080642 XRP."),
        ("declined", "wallet_cap", "This purchase isn't covered: you've reached this campaign's per-wallet limit."),
        ("declined", "something_new", "This purchase isn't covered: not eligible."),
        ("failed", None, "We couldn't send your fee refund. Contact support."),
        ("released", None, ""),
    ],
)
def test_fee_cover_line(state, reason, expected):
    assert run_js(f"M.feeCoverLine({_fc(state, reason)})") == expected


def test_fee_cover_line_and_pending_handle_null():
    assert run_js("M.feeCoverLine(null)") == ""
    assert run_js("M.feeCoverPending(null)") is False
    assert [run_js(f"M.feeCoverPending({_fc(s)})") for s in ("open", "owed", "submitted", "confirmed", "declined")] == [
        True, True, True, False, False,
    ]


def test_external_fill_copy_appends_the_refund_line_and_is_unchanged_without_it():
    two_arg = run_js('M.externalFillCopy("xrp.cafe", "accepted")')
    three_arg_null = run_js('M.externalFillCopy("xrp.cafe", "accepted", null)')
    assert two_arg == three_arg_null
    with_refund = run_js(f'M.externalFillCopy("xrp.cafe", "accepted", {_fc("owed")})')
    assert with_refund["title"] == two_arg["title"] and with_refund["done"] is True
    assert with_refund["text"] == two_arg["text"] + " Your 0.080642 XRP fee refund is on its way."


def test_every_decline_reason_has_copy():
    reasons = run_js("Object.keys(M.FEE_COVER_DECLINE_COPY).sort()")
    assert reasons == sorted([
        "campaign_inactive", "below_clearing", "below_min_bid", "system_wallet", "budget_exhausted", "wallet_cap",
        "not_broker_settled", "fee_unobserved", "issuer_party", "royalty_unobserved", "linked_counterparty",
        "zero_refund",
    ])


def _app_js():
    with open(os.path.join(ROOT, "webapp", "client", "app.js"), encoding="utf-8") as f:
        return f.read()


def test_app_js_renders_refund_state_and_keeps_polling_for_it():
    src = _app_js()
    assert "marketPure.externalFillCopy(marketplace, s.fill, s.fee_cover)" in src  # the done render
    assert "marketPure.externalFillCopy(marketplace, fill, s.fee_cover)" in src  # the fill watcher
    assert src.count("marketPure.feeCoverNote(vm) || marketPure.externalFeeNote(vm)") == 2
    assert "FEE_COVER_REFUND_WAIT_MS" in src
    assert "marketPure.feeCoverPending(s.fee_cover)" in src
    assert "marketPure.feeCoverLine(s.fee_cover)" in src


def test_market_pure_import_and_app_js_cache_busters_move_together():
    src = _app_js()
    import_v = int(re.search(r"market_pure\.js\?v=(\d+)", src).group(1))
    with open(os.path.join(ROOT, "webapp", "client", "index.html"), encoding="utf-8") as f:
        app_v = int(re.search(r"app\.js\?v=(\d+)", f.read()).group(1))
    assert import_v >= 28 and app_v >= 89
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_fee_cover_client.py -v`
Expected: FAIL. `feeCoverNote` / `feeCoverLine` are undefined (the Node script
throws), and the app.js assertions fail.

- [ ] **Step 3: Implement `market_pure.js`**

In `mapListingRow`, directly after `brokerRate: row.broker_rate ?? null,`:

```js
    // Fee cover (spec 2026-09-14): what LFG refunds of the marketplace fee on a
    // Buy-now through LFG — null when no live campaign covers this row.
    feeCoverXrp: row.fee_cover_xrp ?? null,
```

Directly after `externalFeeNote`:

```js
/**
 * Fee cover: replaces externalFeeNote when a live campaign refunds the
 * marketplace fee on this Buy-now. Empty string when not covered.
 */
export function feeCoverNote(vm) {
  if (!vm.external || vm.clearingXrp == null || vm.feeCoverXrp == null) return '';
  const who = vm.marketplace ? `${vm.marketplace}'s` : "the marketplace's";
  const ask = vm.amountXrp != null ? `, so you pay the ${vm.amountXrp} XRP ask` : '';
  return `LFG refunds ${who} ${vm.feeCoverXrp} XRP fee after it settles${ask} (campaign limits apply).`;
}

/** Fee cover: why a purchase is not covered (keys = server decline reasons). */
export const FEE_COVER_DECLINE_COPY = {
  campaign_inactive: 'the fee-cover campaign has ended',
  below_clearing: 'the offer was below the instant-fill price',
  below_min_bid: "the purchase is below the campaign's minimum",
  system_wallet: 'project wallets are excluded',
  budget_exhausted: "the campaign's budget is used up",
  wallet_cap: "you've reached this campaign's per-wallet limit",
  not_broker_settled: 'the sale was not settled by the marketplace',
  fee_unobserved: 'no marketplace fee was charged',
  issuer_party: 'the collection wallet was a party to the sale',
  royalty_unobserved: "the sale's royalty could not be verified",
  linked_counterparty: 'the buyer and seller wallets are linked',
  zero_refund: 'there was no fee to refund',
};

/**
 * Fee cover: one sentence for the bid's fee_cover state (from
 * GET /api/market/bid/{id}); empty when there is nothing to say.
 */
export function feeCoverLine(feeCover) {
  if (!feeCover) return '';
  const xrp = feeCover.xrp;
  switch (feeCover.state) {
    case 'quoted':
      return 'LFG covers the marketplace fee on this purchase (campaign limits apply).';
    case 'open':
      return `Fee refund of ${xrp} XRP reserved.`;
    case 'owed':
    case 'submitted':
      return `Your ${xrp} XRP fee refund is on its way.`;
    case 'confirmed':
      return `Refunded ${xrp} XRP.`;
    case 'declined':
      return `This purchase isn't covered: ${FEE_COVER_DECLINE_COPY[feeCover.reason] || 'not eligible'}.`;
    case 'failed':
      return "We couldn't send your fee refund. Contact support.";
    default:
      return '';
  }
}

/** Fee cover: a refund is still on its way (keep polling after the fill). */
export function feeCoverPending(feeCover) {
  return !!feeCover && ['open', 'owed', 'submitted'].includes(feeCover.state);
}
```

Rename the existing `export function externalFillCopy(marketplace, fill)` to a
non-exported `function externalFillBase(marketplace, fill)`, keeping its body
unchanged. Directly above it, add the exported wrapper (keep the existing
doc comment on the wrapper):

```js
export function externalFillCopy(marketplace, fill, feeCover = null) {
  const base = externalFillBase(marketplace, fill);
  const line = feeCoverLine(feeCover);
  return line ? { ...base, text: `${base.text} ${line}` } : base;
}
```

- [ ] **Step 4: Implement `app.js`**

In the listing detail renderer, replace
`feeNote.textContent = marketPure.externalFeeNote(vm);` with:

```js
    feeNote.textContent = marketPure.feeCoverNote(vm) || marketPure.externalFeeNote(vm);
```

In `buyExternalNow`, replace the confirm `text:` line with:

```js
    text: `${vm.clearingXrp} XRP — ${marketPure.feeCoverNote(vm) || marketPure.externalFeeNote(vm)} Your offer expires on its own if it isn't taken (nothing is held).`,
```

Replace `marketExternalBuyRender` and `watchExternalFill` with the versions
below, and add the constant next to `EXTERNAL_FILL_POLL_MS`:

```js
const FEE_COVER_REFUND_WAIT_MS = 2 * 60 * 1000;

function marketExternalBuyRender(marketplace) {
  return (s) => {
    if (s.state === 'done') {
      const copy = marketPure.externalFillCopy(marketplace, s.fill, s.fee_cover);
      const refundWait = s.fill === 'accepted' && marketPure.feeCoverPending(s.fee_cover);
      // Start the fill watch AFTER showFlow() has painted this render (it
      // bumps flowRenderGen; a watch armed before it would see itself as
      // superseded and stop).
      if (!copy.done || refundWait) setTimeout(() => watchExternalFill(s.id, marketplace, Date.now(), null), 0);
      return { ...copy, spinner: !copy.done };
    }
    const base = marketBidRender(s);
    if (s.state === 'awaiting_signature') {
      const quote = marketPure.feeCoverLine(s.fee_cover);
      const text = `Scan to sign your ${marketplace || 'marketplace'} offer in Xaman.${quote ? ` ${quote}` : ''}`;
      return { ...base, title: '🛒 Buy now', text: signText(s.push, text) };
    }
    return base;
  };
}

function watchExternalFill(sessionId, marketplace, startedAt, acceptedAt) {
  clearTimeout(externalFillTimer);
  const gen = marketFlowGen;
  const ownerGen = flowRenderGen;
  externalFillTimer = setTimeout(async () => {
    if (gen !== marketFlowGen || ownerGen !== flowRenderGen) return; // another flow took the panel
    if (el('flow-panel').hidden) return; // user navigated away
    let s;
    try {
      s = await api(MARKET_STATUS_PATH.bid(sessionId));
    } catch (e) {
      if (gen === marketFlowGen && ownerGen === flowRenderGen) watchExternalFill(sessionId, marketplace, startedAt, acceptedAt);
      return;
    }
    if (gen !== marketFlowGen || ownerGen !== flowRenderGen) return;
    const unfilled = s.fill == null || s.fill === 'live';
    const fill = unfilled && (Date.now() - startedAt) >= EXTERNAL_FILL_WAIT_MS ? 'waiting' : s.fill;
    const copy = marketPure.externalFillCopy(marketplace, fill, s.fee_cover);
    showFlow({ ...copy, spinner: !copy.done });
    // Fee cover: after the fill, keep polling briefly so "on its way" becomes
    // "Refunded" in place; past the wait, "on its way" is the honest final copy
    // (the settlement sweep pays it).
    const acceptedSince = s.fill === 'accepted' ? (acceptedAt ?? Date.now()) : null;
    const refundWait = acceptedSince != null
      && marketPure.feeCoverPending(s.fee_cover)
      && (Date.now() - acceptedSince) < FEE_COVER_REFUND_WAIT_MS;
    if (!copy.done || refundWait) watchExternalFill(sessionId, marketplace, startedAt, acceptedSince);
  }, EXTERNAL_FILL_POLL_MS);
}
```

- [ ] **Step 5: Bump the cache-busters in lockstep**

Both are trivial bumps, but they must move together (see the
`lfg-cache-buster-direct-push` convention: an ES-module import `?v=` in
`app.js` must bump with the `app.js?v=` in `index.html`).

```bash
grep -n "market_pure.js?v=" webapp/client/app.js      # e.g. ?v=27 -> bump by one
grep -n "app.js?v=" webapp/client/index.html          # e.g. ?v=88 -> bump by one
grep -rn "app.js?v=" tests/                            # update every exact pin to the new value
```

Edit each hit to the next integer: `app.js`'s `market_pure.js?v=N` → `N+1`,
`index.html`'s `app.js?v=M` → `M+1`, and the exact `"app.js?v=M"` pins in
`tests/test_app_js_deeplink.py` and `tests/test_harvest_pure_js.py` → `M+1`.
The `>= 77` floor in `tests/test_brix_card_dom.py` needs no change.

- [ ] **Step 6: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_fee_cover_client.py tests/test_market_pure_js.py tests/test_market_panel_dom.py tests/test_app_js_deeplink.py tests/test_harvest_pure_js.py tests/test_brix_card_dom.py -q`
Expected: all pass. `test_fee_cover_client.py` has 16 tests.

- [ ] **Step 7: Commit**

```bash
git add webapp/client/market_pure.js webapp/client/app.js webapp/client/index.html tests/test_fee_cover_client.py tests/test_app_js_deeplink.py tests/test_harvest_pure_js.py
git commit -m "feat(fee-cover): Buy-now copy and refund state in the Activity"
```

---
### Task 12: Ops — recovery/requeue script, report + audit, docs

**Files:**
- Modify: `lfg_core/fee_cover.py` (add `audit_violations`)
- Modify: `lfg_core/fee_cover_settle.py` (add `service_deps`)
- Modify: `lfg_service/app.py` (`_fee_cover_deps` delegates to `service_deps`)
- Create: `scripts/recover_fee_cover_refunds.py`
- Create: `scripts/fee_cover_report.py`
- Modify: `ecosystem.prod.config.js`, `ecosystem.staging.config.js` (audit cron entries)
- Modify: `CLAUDE.md` (env var line + "Marketplace fee cover" section)
- Test: `tests/test_fee_cover_scripts.py`

**Interfaces:**
- Consumes:
  - Tasks 1–7: `fee_cover_store.connect`, `status_summary`, `requeue_failed`,
    `BPS`.
  - `fee_cover.ROYALTY_CEILING_BPS`.
  - `fee_cover_settle.FeeCoverDeps`, `recover_refunds`.
  - `_fee_cover_linked`.
- Produces:
  - `fee_cover.audit_violations(conn, network) -> list[str]`
  - `fee_cover_settle.service_deps(network, *, linked) -> FeeCoverDeps`
  - `scripts.recover_fee_cover_refunds.main(argv) -> int`: 0 ok, 1 requeue
    refused, 2 network mismatch.
  - `scripts.fee_cover_report.main(argv) -> int`: 0 clean, 1 audit
    violations.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_fee_cover_scripts.py
"""Fee-cover ops tooling: audit invariants, recovery/requeue CLI, report CLI."""

from __future__ import annotations

import asyncio

import pytest

from lfg_core import config, db_path, fee_cover, fee_cover_settle
from lfg_core import fee_cover_store as store
from scripts import fee_cover_report, recover_fee_cover_refunds

NET = "testnet"
CAFE = "rpx9JThQ2y37FaGeeJP7PXDUVEXY3PHZSC"


@pytest.fixture()
def app_db(tmp_path, monkeypatch):
    path = str(tmp_path / "app.db")
    monkeypatch.setattr(db_path, "app_db_path", lambda network=None: path)
    monkeypatch.setattr(config, "XRPL_NETWORK", NET)
    return path


def _refund(path, *, refund=80_642, fee=80_642, royalty=349_605, state="confirmed", budget=1_000_000):
    conn = store.connect(path)
    knobs = store.Knobs(coverage_bps=10_000, budget_drops=budget, wallet_cap_drops=10_000_000, min_bid_drops=0)
    campaign, _ = store.start_campaign(conn, network=NET, actor="t", knobs=knobs, duration_seconds=None, now=1000)
    inp = store.PromiseInput(offer_index="BID1", network=NET, nft_id="NFT1", bidder="rB", bid_drops=5_075_000,
                             ask_drops=4_990_000, broker=CAFE, broker_rate=0.01589, bid_expiration=None)
    store.record_promise(conn, campaign, inp, promised_drops=80_642, decline_reason=None, now=1001)
    store.fill_promise(conn, "BID1", store.RefundDecision("ACC1", "rS", CAFE, fee, royalty, 80_642, None), now=1002)
    conn.execute("UPDATE fee_cover_refunds SET refund_drops = ?, state = ?", (refund, state))
    conn.commit()
    return conn


def test_audit_is_clean_for_a_correct_refund(app_db):
    conn = _refund(app_db)
    assert fee_cover.audit_violations(conn, NET) == []


def test_audit_flags_every_broken_invariant(app_db):
    conn = _refund(app_db, refund=200_000, fee=80_642, royalty=100_000, budget=150_000)
    problems = fee_cover.audit_violations(conn, NET)
    assert any("exceeds promise" in p for p in problems)
    assert any("exceeds observed fee" in p for p in problems)
    assert any("50% of observed royalty" in p for p in problems)
    assert any("exceeds budget" in p for p in problems)


def test_recover_cli_refuses_a_network_mismatch(app_db):
    assert recover_fee_cover_refunds.main(["--network", "mainnet"]) == 2


def test_recover_cli_requeues_a_parked_failure(app_db):
    conn = _refund(app_db, state="failed")
    conn.close()
    assert recover_fee_cover_refunds.main(["--network", NET, "--requeue", "ACC1"]) == 0
    conn = store.connect(app_db)
    assert store.get_refund(conn, "ACC1")["state"] == "owed"
    assert recover_fee_cover_refunds.main(["--network", NET, "--requeue", "ACC1"]) == 1  # not failed any more


def test_recover_cli_runs_chain_recovery(app_db, monkeypatch, capsys):
    seen = []

    async def fake_recover(deps):
        seen.append(deps)
        return {"ACC1": "confirmed"}

    def private_loop_run(coro):
        # main() uses asyncio.run(), which leaves the global event loop unset
        # on Python 3.10 and breaks later modules that call get_event_loop().
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    monkeypatch.setattr(fee_cover_settle, "recover_refunds", fake_recover)
    monkeypatch.setattr(asyncio, "run", private_loop_run)
    assert recover_fee_cover_refunds.main(["--network", NET]) == 0
    assert isinstance(seen[0], fee_cover_settle.FeeCoverDeps) and seen[0].network == NET
    assert "ACC1: confirmed" in capsys.readouterr().out


def test_report_cli_exit_code_follows_the_audit(app_db, capsys):
    conn = _refund(app_db)
    conn.close()
    assert fee_cover_report.main(["--network", NET, "--audit"]) == 0
    out = capsys.readouterr().out
    assert "state: active" in out and "committed" in out
    conn = store.connect(app_db)
    conn.execute("UPDATE fee_cover_refunds SET refund_drops = 999999")
    conn.commit()
    conn.close()
    assert fee_cover_report.main(["--network", NET, "--audit"]) == 1
    assert "AUDIT FAIL" in capsys.readouterr().out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_fee_cover_scripts.py -v`
Expected: FAIL with `ImportError: cannot import name 'fee_cover_report' from 'scripts'`

- [ ] **Step 3: Implement `audit_violations` and `service_deps`**

Append to `lfg_core/fee_cover.py` (add `import sqlite3` to its imports):

```python
def audit_violations(conn: sqlite3.Connection, network: str) -> list[str]:
    """Every committed refund must stay within its promise, its observed fee ×
    coverage, and ROYALTY_CEILING_BPS of its observed royalty; no campaign may
    pay more than its budget. Empty list = clean."""
    problems: list[str] = []
    for accept, refund, fee, royalty, promised, coverage in conn.execute(
        "SELECT r.accept_tx_hash, r.refund_drops, r.observed_fee_drops, r.observed_royalty_drops,"
        " p.promised_drops, p.coverage_bps FROM fee_cover_refunds r"
        " JOIN fee_cover_promises p ON p.offer_index = r.offer_index"
        " WHERE r.network = ? AND r.state IN ('owed','submitted','confirmed')",
        (network,),
    ):
        if refund > promised:
            problems.append(f"{accept}: refund {refund} exceeds promise {promised}")
        if fee is None or refund > fee * coverage // BPS:
            problems.append(f"{accept}: refund {refund} exceeds observed fee x coverage")
        if royalty is None or refund > royalty * ROYALTY_CEILING_BPS // BPS:
            problems.append(f"{accept}: refund {refund} exceeds {ROYALTY_CEILING_BPS // 100}% of observed royalty")
    for campaign_id, budget, paid in conn.execute(
        "SELECT c.id, c.budget_drops, COALESCE(SUM(r.refund_drops), 0) FROM fee_cover_campaigns c"
        " LEFT JOIN fee_cover_refunds r ON r.campaign_id = c.id AND r.state = 'confirmed'"
        " WHERE c.network = ? GROUP BY c.id",
        (network,),
    ):
        if paid > budget:
            problems.append(f"campaign {campaign_id}: confirmed {paid} exceeds budget {budget}")
    return problems
```

Append to `lfg_core/fee_cover_settle.py` (add `brokers`, `config` and
`db_path` to its `from lfg_core import …` line):

```python
def service_deps(network: str, *, linked: Callable[[str, str], bool]) -> FeeCoverDeps:
    """The production wiring: real ledger calls, the network's app DB and
    history archive, refunds signed by config.SIGNING_ACCOUNT."""
    return FeeCoverDeps(
        network=network,
        app_db_path=db_path.app_db_path(network),
        history_db_path=history_store.history_db_path(network),
        payer=config.SIGNING_ACCOUNT,
        ledger_margin=config.FEE_COVER_LEDGER_MARGIN,
        get_tx=xrpl_ops.get_tx,
        send_refund=xrpl_ops.send_fee_cover_refund,
        find_refund_payment=xrpl_ops.find_fee_cover_payment,
        current_ledger=xrpl_ops.current_validated_ledger_index,
        broker_rate_for=lambda account, nft_id: (brokers.resolve(account, nft_id) or {}).get("broker_rate"),
        linked=linked,
    )
```

In `lfg_service/app.py`, replace the body of `_fee_cover_deps` with a delegation.
The `app_db_path` override keeps tests that monkeypatch `_fee_cover_db` honest:

```python
def _fee_cover_deps() -> fee_cover_settle.FeeCoverDeps:
    deps = fee_cover_settle.service_deps(config.XRPL_NETWORK, linked=_fee_cover_linked)
    deps.app_db_path = _fee_cover_db()
    return deps
```

- [ ] **Step 4: Create the scripts**

```python
#!/usr/bin/env python3
# scripts/recover_fee_cover_refunds.py
"""Resolve fee-cover refunds left `submitted` by a crash, or requeue a parked failure.

  .venv/bin/python scripts/recover_fee_cover_refunds.py --network mainnet
  .venv/bin/python scripts/recover_fee_cover_refunds.py --network mainnet --requeue <accept_tx_hash>

Recovery never guesses: a refund is confirmed only when its memo-tagged Payment
is found in the signing account's account_tx, and failed only when it is absent
AND the validated ledger has passed its LastLedgerSequence. Also runs at service
startup, so ordinary restarts self-heal.

--requeue moves a `failed` refund back to `owed` (the sweep then pays it) after
the operator fixed the cause — e.g. topped up the issuer. A failed payment can
never validate later, so this cannot double-pay; it is refused when the
campaign budget no longer has room.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)

from lfg_core import config, db_path, fee_cover_settle, fee_cover_store  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Recover or requeue marketplace fee-cover refunds.")
    parser.add_argument("--network", required=True, choices=("testnet", "mainnet"))
    parser.add_argument("--requeue", metavar="ACCEPT_TX_HASH")
    args = parser.parse_args(argv)
    if args.network != config.XRPL_NETWORK:
        print(f"refusing: --network {args.network} does not match XRPL_NETWORK={config.XRPL_NETWORK}", file=sys.stderr)
        return 2
    if args.requeue:
        conn = fee_cover_store.connect(db_path.app_db_path(args.network))
        try:
            result = fee_cover_store.requeue_failed(conn, args.requeue)
        finally:
            conn.close()
        print(f"{args.requeue}: {result}")
        return 0 if result == "requeued" else 1
    deps = fee_cover_settle.service_deps(args.network, linked=lambda a, b: False)
    outcomes = asyncio.run(fee_cover_settle.recover_refunds(deps))
    for key, state in sorted(outcomes.items()):
        print(f"{key}: {state}")
    print(f"resolved {len(outcomes)} refund(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

```python
#!/usr/bin/env python3
# scripts/fee_cover_report.py
"""Fee-cover campaign report: budget burn-down, promises/refunds by state and
reason, refunds by wallet (where abuse shows up first). --audit exits non-zero
when any refund breaks its invariants (promise, observed fee × coverage, 50% of
observed royalty) or a campaign paid more than its budget.

  .venv/bin/python scripts/fee_cover_report.py --network mainnet [--audit]
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)

from lfg_core import db_path, fee_cover, fee_cover_store, market_ops  # noqa: E402


def _xrp(drops: int) -> str:
    return market_ops.drops_to_xrp_str(str(int(drops)))


def render(status: dict[str, Any], violations: list[str] | None) -> str:
    lines = [f"network: {status['network']}", f"state: {status['state']}"]
    campaign = status.get("campaign")
    if campaign:
        lines += [
            f"campaign: #{campaign['id']} coverage {campaign['coverage_bps'] / 100:g}% "
            f"budget {_xrp(campaign['budget_drops'])} XRP, cap {_xrp(campaign['wallet_cap_drops'])} XRP/wallet",
            f"committed: {_xrp(status['committed_drops'])} XRP  paid: {_xrp(status['paid_drops'])} XRP  "
            f"remaining: {_xrp(status['remaining_drops'])} XRP",
            f"open promises: {status['open_promises']}",
            f"refunds by state: {status['refunds_by_state']}",
            f"declines by reason: {status['declines_by_reason']}",
            "top wallets:",
            *[f"  {w['wallet']}  {_xrp(w['drops'])} XRP" for w in status["top_wallets"]],
        ]
    if violations is not None:
        lines.append("AUDIT PASS" if not violations else "AUDIT FAIL")
        lines += [f"  {v}" for v in violations]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Marketplace fee-cover report.")
    parser.add_argument("--network", required=True, choices=("testnet", "mainnet"))
    parser.add_argument("--audit", action="store_true")
    args = parser.parse_args(argv)
    conn = fee_cover_store.connect(db_path.app_db_path(args.network))
    try:
        status = fee_cover_store.status_summary(conn, args.network)
        violations = fee_cover.audit_violations(conn, args.network) if args.audit else None
    finally:
        conn.close()
    print(render(status, violations))
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_fee_cover_scripts.py tests/test_fee_cover_api.py -q`
Expected: all pass. `test_fee_cover_scripts.py` has 6 tests, and
`test_fee_cover_api.py` still passes after the `_fee_cover_deps` delegation.

- [ ] **Step 6: Ecosystem cron entries and docs**

`ecosystem.prod.config.js`, after the `lfg-market-sweep` entry:

```js
    // Fee cover (spec 2026-09-14): nightly refund audit — exits non-zero on any refund
    // above its promise / observed fee / 50% of observed royalty. Parks "stopped" between runs.
    { name: "lfg-fee-cover-audit", cwd: CWD, script: "scripts/fee_cover_report.py", interpreter: PY, args: ["--network", "mainnet", "--audit"], cron_restart: "40 3 * * *", autorestart: false },
```

`ecosystem.staging.config.js`, after the `stg-market-sweep` entry:

```js
    { name: "stg-fee-cover-audit", cwd: CWD, script: "scripts/fee_cover_report.py", interpreter: PY, args: ["--network", "testnet", "--audit"], cron_restart: "40 3 * * *", autorestart: false },
```

`CLAUDE.md`:
- In the environment-variable block, after the `BROKER_CLEARING_BUFFER_DROPS`
  line, add:

```
FEE_COVER_LEDGER_MARGIN=40                                  # optional (fee cover); ledgers of LastLedgerSequence headroom on a fee-cover refund Payment — what makes an indeterminate refund decidable
```

- In the "In-app marketplace (#44)" section, directly before the
  `**Trait settlement:**` paragraph, add:

```markdown
**Marketplace fee cover** (spec `docs/superpowers/specs/2026-09-14-marketplace-fee-cover-design.md`):
a Discord `/admin` → 💸 Fee Cover campaign refunds an allowlisted broker's fee
(cafe's measured 1.589%) on bids placed THROUGH LFG that the broker's bot
settles — so Buy-now costs the ask. Promise at bid finalize reserves budget
(`fee_cover_promises`, app DB); refund = min(promise, observed
`NFTokenBrokerFee` × coverage, 50% of observed royalty) from the validated
accept (`fee_cover_refunds`, PK accept hash — double-pay impossible); paid as
a tagged XRP `Payment` from `SIGNING_ACCOUNT` with memo action `fee-cover` +
`lfg:fee_cover:<accept hash>`. Stop blocks new promises and honors open ones.
The settlement loop now always starts (it settles/pays/releases fee-cover rows
on any stack). Indeterminate payouts resolve via startup recovery;
definitive failures park `failed` — fix the cause, then
`scripts/recover_fee_cover_refunds.py --network <net> --requeue <accept_hash>`.
Report/audit: `scripts/fee_cover_report.py --network <net> --audit` (pm2
`lfg-fee-cover-audit` / `stg-fee-cover-audit`, 03:40 UTC — registering it is an
ops step: `pm2 start ecosystem.prod.config.js --only lfg-fee-cover-audit && pm2 save`).
Refund Payments count in `sourcetag_metrics` `xrp_payment_volume.out_drops`;
they are not marketplace volume. Ships with no campaign active.
```

- [ ] **Step 7: Run the full gate and commit**

```bash
.venv/bin/ruff format lfg_core/fee_cover.py lfg_core/fee_cover_settle.py lfg_service/app.py scripts/recover_fee_cover_refunds.py scripts/fee_cover_report.py tests/test_fee_cover_scripts.py
.venv/bin/ruff check --fix lfg_core/fee_cover.py lfg_core/fee_cover_settle.py lfg_service/app.py scripts/recover_fee_cover_refunds.py scripts/fee_cover_report.py tests/test_fee_cover_scripts.py
.venv/bin/mypy lfg_core/fee_cover.py lfg_core/fee_cover_settle.py lfg_service/app.py scripts/recover_fee_cover_refunds.py scripts/fee_cover_report.py
.venv/bin/python -m pytest -q
git add lfg_core/fee_cover.py lfg_core/fee_cover_settle.py lfg_service/app.py scripts/recover_fee_cover_refunds.py scripts/fee_cover_report.py tests/test_fee_cover_scripts.py ecosystem.prod.config.js ecosystem.staging.config.js CLAUDE.md
git commit -m "feat(fee-cover): recovery/requeue CLI, report + audit, ops docs"
```

Expected: the full pytest suite passes.

---

## After the last task

1. **Push and open a reviewed PR.** Push `feat/marketplace-fee-cover` and run
   `gh pr create` (ready, not draft). The body links the spec and this plan,
   lists the ops steps (register the audit crons; campaign ships OFF) and
   carries no AI attribution.
2. **Close out both review bots.** Wait for Greptile and CodeRabbit. For every
   actionable finding, fix it and reply on its thread naming the commit.
3. **Merge**, which auto-deploys staging.
4. **Staging rehearsal** (testnet has no cafe):
   - Allowlist a test broker via `BROKER_ALLOWLIST_PATH` with `"broker_rate":
     0.01589`.
   - Start a campaign with a small budget in `/admin`.
   - List a character destination-locked to the test broker.
   - Bid at `clearing_xrp` from the Activity, broker the accept from a script
     with `NFTokenBrokerFee`, and confirm the refund Payment lands with both
     memos.
   - Confirm `scripts/fee_cover_report.py --network testnet --audit` passes.
5. **Promote to prod** with `scripts/promote.sh`. Leave the campaign stopped
   until the user starts it.
