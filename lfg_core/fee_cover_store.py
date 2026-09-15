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


def live_campaign(
    conn: sqlite3.Connection, network: str, now: int | None = None
) -> Campaign | None:
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
            audit(
                conn,
                network=network,
                actor=who,
                action="start",
                at=ts,
                campaign_id=current.id,
                result="already_active",
            )
            return current, "already_active"
        if current is not None:
            # An active row past its end: retire it so the one-active index
            # admits the new campaign.
            conn.execute(
                "UPDATE fee_cover_campaigns SET status = 'stopped', stopped_at = ?, stopped_by = 'system'"
                " WHERE id = ?",
                (current.ends_at, current.id),
            )
            audit(
                conn,
                network=network,
                actor="system",
                action="expire",
                at=ts,
                campaign_id=current.id,
                result="expired",
            )
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
        audit(
            conn,
            network=network,
            actor=who,
            action="start",
            at=ts,
            campaign_id=campaign_id,
            result="started",
            details={**asdict(knobs), "duration_seconds": duration_seconds},
        )
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
            audit(
                conn,
                network=network,
                actor=who,
                action="update",
                at=ts,
                campaign_id=current.id if current else None,
                result="no_live_campaign",
                details=asdict(knobs),
            )
            return None, "no_live_campaign"
        conn.execute(
            "UPDATE fee_cover_campaigns SET coverage_bps = ?, budget_drops = ?, wallet_cap_drops = ?,"
            " min_bid_drops = ? WHERE id = ?",
            (
                knobs.coverage_bps,
                knobs.budget_drops,
                knobs.wallet_cap_drops,
                knobs.min_bid_drops,
                current.id,
            ),
        )
        audit(
            conn,
            network=network,
            actor=who,
            action="update",
            at=ts,
            campaign_id=current.id,
            result="updated",
            details=asdict(knobs),
        )
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
            audit(
                conn,
                network=network,
                actor=who,
                action="stop",
                at=ts,
                campaign_id=latest.id if latest else None,
                result="already_inactive",
            )
            return latest, "already_inactive"
        conn.execute(
            "UPDATE fee_cover_campaigns SET status = 'stopped', stopped_at = ?, stopped_by = ? WHERE id = ?",
            (ts, who, current.id),
        )
        audit(
            conn,
            network=network,
            actor=who,
            action="stop",
            at=ts,
            campaign_id=current.id,
            result="stopped",
        )
    return get_campaign(conn, current.id), "stopped"
