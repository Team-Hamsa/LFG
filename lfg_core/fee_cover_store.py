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
import math
import sqlite3
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from typing import Any

from lfg_core import market_ops

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
  -- The validated ledger index when the payout was claimed. A refund is
  -- built after its claim, so it can never validate below this: it is the
  -- recovery scan floor, independent of any ledger-margin setting.
  claim_ledger INTEGER,
  created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fee_cover_refunds_state
  ON fee_cover_refunds(network, state);

-- One row per bid the service QUOTED as covered, written before the buyer
-- signs. It is the durable twin of the in-memory BidSession: enough to rebuild
-- that session and re-run its on-ledger checks, so a promise the buyer was
-- shown survives a failed promise write or a service restart. A quote reserves
-- no budget; the promise it resolves into does.
CREATE TABLE IF NOT EXISTS fee_cover_quotes (
  session_id TEXT PRIMARY KEY,
  network TEXT NOT NULL,
  payload_uuid TEXT NOT NULL,
  txid TEXT,
  nft_id TEXT NOT NULL, owner TEXT NOT NULL, bidder TEXT NOT NULL,
  bid_drops INTEGER NOT NULL,
  -- The bid's on-ledger Expiration (unix seconds) as its payload set it. The
  -- bid can fill until then, so the quote is never abandoned before it — and
  -- it is fixed per bid, not re-derived from a TTL setting that may change.
  bid_expires_at INTEGER NOT NULL,
  listing_json TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('pending','closed')),
  -- When the reconciler last considered this quote: selection rotates on it,
  -- so a batch of long-unresolved quotes can't starve newer ones.
  last_attempt_at INTEGER,
  outcome TEXT,
  offer_index TEXT,
  created_at INTEGER NOT NULL, closed_at INTEGER
);
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
    _migrate(conn)
    return conn


# Columns added after a table's first shape. CREATE TABLE IF NOT EXISTS never
# alters an existing table, so a DB initialised by an earlier build needs each
# one added explicitly; every add is idempotent.
_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("fee_cover_refunds", "claim_ledger", "INTEGER"),
    ("fee_cover_quotes", "bid_expires_at", "INTEGER"),
    ("fee_cover_quotes", "last_attempt_at", "INTEGER"),
)
# The expiry assumed for a quote recorded before bid_expires_at existed.
# Deliberately longer than any bid TTL this code sets (7 days by default), so
# a migrated quote is never abandoned while its bid could still fill.
_MIGRATED_QUOTE_EXPIRY_SECONDS = 30 * 86_400


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, decl in _ADDED_COLUMNS:
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    # A refund left without a claim_ledger scans all history in recovery,
    # which is slower but never misses a payout. A quote without an expiry
    # gets the conservative assumed one.
    conn.execute(
        "UPDATE fee_cover_quotes SET bid_expires_at = created_at + ? WHERE bid_expires_at IS NULL",
        (_MIGRATED_QUOTE_EXPIRY_SECONDS,),
    )
    # Built here, after its columns are guaranteed to exist. The earlier index
    # (without last_attempt_at) is replaced.
    conn.execute("DROP INDEX IF EXISTS idx_fee_cover_quotes_pending")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_fee_cover_quotes_rotation"
        " ON fee_cover_quotes(network, state, last_attempt_at, created_at)"
    )
    conn.commit()


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


def promise_drops(bid_drops: int, broker_rate: float, coverage_bps: int) -> int:
    """The most this bid can be refunded: the broker's fee on it (rounded UP,
    exactly as cafe computes it) times coverage. Lives here, next to BPS, so
    `record_promise` can re-derive it under its own write lock; re-exported by
    `fee_cover.promise_drops` for the pure-rule callers."""
    return math.ceil(bid_drops * broker_rate) * coverage_bps // BPS


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
        conn.execute(
            "SELECT * FROM fee_cover_promises WHERE offer_index = ?", (offer_index,)
        ).fetchone()
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
    decline_reason: str | None,
    now: int | None = None,
) -> dict[str, Any] | None:
    """Write the promise for one on-ledger bid (idempotent on offer_index).

    `decline_reason` is the pure-rule verdict (fee_cover.promise_decline_reason).
    Everything the CAMPAIGN decides is decided HERE, inside the write lock,
    against the campaign re-read under it: the size of the promise, the minimum
    bid, and budget/wallet-cap headroom. Two bids can therefore never both take
    the last of the budget, and an admin update that commits between the
    caller's read and this lock applies to this promise — the documented rule
    is that an update applies to new promises, and a promise is only new once
    it is written. Returns None — writing nothing — when the campaign stopped
    before the lock.
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
        promised_drops = promise_drops(inp.bid_drops, inp.broker_rate, fresh.coverage_bps)
        # `below_min_bid` is the one pure verdict that depends on the CAMPAIGN,
        # so the caller's is discarded and re-decided from `fresh` in BOTH
        # directions: a raised minimum declines a bid the stale read admitted,
        # and a lowered one admits a bid it declined. The other pure reasons
        # (system wallet, below clearing) don't depend on the campaign and
        # stand. They are also checked first, so a caller's `below_min_bid`
        # already means neither of them applied.
        reason = None if decline_reason == "below_min_bid" else decline_reason
        if reason is None and inp.bid_drops < fresh.min_bid_drops:
            reason = "below_min_bid"
        if reason is None:
            reason = headroom_reason(conn, fresh, inp.network, inp.bidder, promised_drops, ts)
        state = "open" if reason is None else "declined"
        conn.execute(
            "INSERT INTO fee_cover_promises (offer_index, campaign_id, network, nft_id, bidder, bid_drops,"
            " ask_drops, broker, broker_rate, coverage_bps, promised_drops, bid_expiration, state, reason,"
            " created_at, closed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                inp.offer_index,
                fresh.id,
                inp.network,
                inp.nft_id,
                inp.bidder,
                inp.bid_drops,
                inp.ask_drops,
                inp.broker,
                inp.broker_rate,
                fresh.coverage_bps,
                promised_drops,
                inp.bid_expiration,
                state,
                reason,
                ts,
                None if state == "open" else ts,
            ),
        )
    return get_promise(conn, inp.offer_index)


def release_promise(
    conn: sqlite3.Connection, offer_index: str, reason: str, now: int | None = None
) -> bool:
    cursor = conn.execute(
        "UPDATE fee_cover_promises SET state = 'released', reason = ?, closed_at = ?"
        " WHERE offer_index = ? AND state = 'open'",
        (reason, _now(now), offer_index),
    )
    conn.commit()
    return cursor.rowcount == 1


def get_refund(conn: sqlite3.Connection, accept_tx_hash: str) -> dict[str, Any] | None:
    return _row(
        conn.execute(
            "SELECT * FROM fee_cover_refunds WHERE accept_tx_hash = ?", (accept_tx_hash,)
        ).fetchone()
    )


def get_refund_by_offer(conn: sqlite3.Connection, offer_index: str) -> dict[str, Any] | None:
    return _row(
        conn.execute(
            "SELECT * FROM fee_cover_refunds WHERE offer_index = ?", (offer_index,)
        ).fetchone()
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
            "SELECT * FROM fee_cover_promises WHERE offer_index = ? AND state = 'open'",
            (offer_index,),
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
                decision.accept_tx_hash,
                offer_index,
                promise["campaign_id"],
                promise["network"],
                promise["bidder"],
                decision.seller,
                decision.broker,
                decision.observed_fee_drops,
                decision.observed_royalty_drops,
                decision.refund_drops if owed else 0,
                "owed" if owed else "declined",
                decision.reason,
                ts,
                ts,
            ),
        )
    return get_refund(conn, decision.accept_tx_hash)


def claim_for_payout(
    conn: sqlite3.Connection,
    accept_tx_hash: str,
    last_ledger_seq: int,
    *,
    claim_ledger: int,
    now: int | None = None,
) -> bool:
    """owed -> submitted in ONE conditional write that also records the
    provisional deadline and the claim ledger. Only the caller whose update
    changed a row submits.

    `claim_ledger` is the validated ledger index read just before the claim.
    Recovery scans for the payout from there. It's a fact about this row,
    not derived from `last_ledger_seq` minus whatever margin is configured
    later, so a changed `FEE_COVER_LEDGER_MARGIN` can never move the scan
    past a refund that landed."""
    cursor = conn.execute(
        "UPDATE fee_cover_refunds SET state = 'submitted', last_ledger_seq = ?, claim_ledger = ?,"
        " updated_at = ? WHERE accept_tx_hash = ? AND state = 'owed'",
        (last_ledger_seq, claim_ledger, _now(now), accept_tx_hash),
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
    reason: str | None = None,
) -> bool:
    """Record a payout outcome on a `submitted` row; True when a row moved.

    A `failed` row carries a reason: `payout_failed` (default — a validated
    definitive failure) or `payout_expired` (recovery found no payout and the
    ledger passed the deadline). Both are parked for an operator's --requeue.

    `confirmed` is the one outcome that may also land on an already-`failed`
    row. Recovery runs concurrently with the sweep and can park a row
    `payout_expired` on the strength of an absence while its payout is still
    in flight; if that payout then comes back confirmed, it is ground truth —
    the money moved — and must overwrite the guess. Refusing it would drop the
    hash, leaving the row requeueable and the refund payable twice. A `failed`
    verdict never overwrites anything for the same reason.
    """
    if state not in ("confirmed", "failed", "submitted"):
        raise ValueError(f"not a payout outcome: {state}")
    failed_reason = (reason or "payout_failed") if state == "failed" else None
    states = ("submitted", "failed") if state == "confirmed" else ("submitted",)
    cursor = conn.execute(
        "UPDATE fee_cover_refunds SET state = ?, payout_tx_hash = COALESCE(?, payout_tx_hash),"
        " last_ledger_seq = COALESCE(?, last_ledger_seq),"
        " reason = CASE WHEN ? = 'failed' THEN ? WHEN ? = 'confirmed' THEN NULL ELSE reason END,"
        " updated_at = ?"
        f" WHERE accept_tx_hash = ? AND state IN ({','.join('?' * len(states))})",
        (
            state,
            tx_hash,
            last_ledger_seq,
            state,
            failed_reason,
            state,
            _now(now),
            accept_tx_hash,
            *states,
        ),
    )
    conn.commit()
    return cursor.rowcount == 1


def return_to_owed(conn: sqlite3.Connection, accept_tx_hash: str, now: int | None = None) -> bool:
    """Only for a payout refused BEFORE anything was submitted (ClaimNotSubmitted)."""
    cursor = conn.execute(
        "UPDATE fee_cover_refunds SET state = 'owed', last_ledger_seq = NULL, claim_ledger = NULL,"
        " updated_at = ? WHERE accept_tx_hash = ? AND state = 'submitted'",
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
        if (
            campaign is None
            or committed_drops(conn, campaign.id) + int(row["refund_drops"]) > campaign.budget_drops
        ):
            return "budget_exhausted"
        conn.execute(
            "UPDATE fee_cover_refunds SET state = 'owed', reason = NULL, payout_tx_hash = NULL,"
            " last_ledger_seq = NULL, claim_ledger = NULL, updated_at = ? WHERE accept_tx_hash = ?",
            (ts, accept_tx_hash),
        )
    return "requeued"


@dataclass(frozen=True)
class Quote:
    session_id: str
    network: str
    payload_uuid: str
    txid: str | None
    nft_id: str
    owner: str
    bidder: str
    bid_drops: int
    bid_expires_at: int
    listing: dict[str, Any]
    created_at: int


def _quote(row: sqlite3.Row) -> Quote:
    return Quote(
        session_id=str(row["session_id"]),
        network=str(row["network"]),
        payload_uuid=str(row["payload_uuid"]),
        txid=None if row["txid"] is None else str(row["txid"]),
        nft_id=str(row["nft_id"]),
        owner=str(row["owner"]),
        bidder=str(row["bidder"]),
        bid_drops=int(row["bid_drops"]),
        bid_expires_at=int(row["bid_expires_at"]),
        listing=json.loads(row["listing_json"]),
        created_at=int(row["created_at"]),
    )


def record_quote(conn: sqlite3.Connection, quote: Quote) -> None:
    """Durably record a covered quote (idempotent on session_id)."""
    conn.execute(
        "INSERT OR IGNORE INTO fee_cover_quotes (session_id, network, payload_uuid, txid, nft_id,"
        " owner, bidder, bid_drops, bid_expires_at, listing_json, state, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
        (
            quote.session_id,
            quote.network,
            quote.payload_uuid,
            quote.txid,
            quote.nft_id,
            quote.owner,
            quote.bidder,
            quote.bid_drops,
            quote.bid_expires_at,
            json.dumps(quote.listing, sort_keys=True),
            quote.created_at,
        ),
    )
    conn.commit()


def get_quote(conn: sqlite3.Connection, session_id: str) -> dict[str, Any] | None:
    return _row(
        conn.execute(
            "SELECT * FROM fee_cover_quotes WHERE session_id = ?", (session_id,)
        ).fetchone()
    )


def set_quote_txid(conn: sqlite3.Connection, session_id: str, txid: str) -> None:
    """Remember the signed tx hash once the (signer-checked) session learns
    it, so a rebuilt session goes straight to the ledger without re-asking the
    wallet provider."""
    conn.execute(
        "UPDATE fee_cover_quotes SET txid = ? WHERE session_id = ? AND txid IS NULL",
        (txid, session_id),
    )
    conn.commit()


def close_quote(
    conn: sqlite3.Connection,
    session_id: str,
    outcome: str,
    *,
    offer_index: str | None = None,
    now: int | None = None,
) -> bool:
    """pending -> closed with its outcome ('promised' | 'uncovered' | 'failed' |
    'expired' | 'abandoned'). Only a pending quote moves."""
    cursor = conn.execute(
        "UPDATE fee_cover_quotes SET state = 'closed', outcome = ?, offer_index = ?, closed_at = ?"
        " WHERE session_id = ? AND state = 'pending'",
        (outcome, offer_index, _now(now), session_id),
    )
    conn.commit()
    return cursor.rowcount == 1


def pending_quotes(
    conn: sqlite3.Connection, network: str, *, created_before: int, limit: int
) -> list[Quote]:
    """Pending quotes created before `created_before` (the live poll path gets
    first go at a fresh one), least-recently-attempted first, then oldest. The
    caller `touch_quote`s every quote it considers, so selection rotates and a
    batch of long-unresolved quotes can never starve newer ones."""
    return [
        _quote(r)
        for r in conn.execute(
            "SELECT * FROM fee_cover_quotes WHERE network = ? AND state = 'pending'"
            " AND created_at < ? ORDER BY COALESCE(last_attempt_at, 0), created_at LIMIT ?",
            (network, created_before, limit),
        )
    ]


def touch_quote(conn: sqlite3.Connection, session_id: str, now: int | None = None) -> None:
    """Record that the reconciler considered this quote (resolved or not)."""
    conn.execute(
        "UPDATE fee_cover_quotes SET last_attempt_at = ? WHERE session_id = ?",
        (_now(now), session_id),
    )
    conn.commit()


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


def status_summary(
    conn: sqlite3.Connection, network: str, now: int | None = None
) -> dict[str, Any]:
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
            "SELECT COUNT(*) FROM fee_cover_promises WHERE campaign_id = ? AND state = 'open'",
            (cid,),
        ).fetchone()[0]
    )
    out["refunds_by_state"] = {
        str(r[0]): int(r[1])
        for r in conn.execute(
            "SELECT state, COUNT(*) FROM fee_cover_refunds WHERE campaign_id = ? GROUP BY state",
            (cid,),
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
