"""Per-network ledger history archive: raw XRPL txs + derived NFT/BRIX events.

Raw `xrpl_txs` rows are the source of truth (verbatim {tx, meta} JSON);
`nft_events` / `brix_events` are derived, droppable, rebuildable. Follows the
same per-network-file posture as lfg_core/nft_index.py (onchain_<net>.db)."""

from __future__ import annotations

import os
import sqlite3
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS xrpl_txs (
    tx_hash      TEXT PRIMARY KEY,
    ledger_index INTEGER,
    close_time   INTEGER,
    tx_type      TEXT,
    account      TEXT,
    source_tag   INTEGER,
    raw_json     TEXT
);
CREATE INDEX IF NOT EXISTS idx_txs_time ON xrpl_txs(close_time);
CREATE INDEX IF NOT EXISTS idx_txs_type ON xrpl_txs(tx_type);

CREATE TABLE IF NOT EXISTS nft_events (
    tx_hash      TEXT,
    nft_id       TEXT,
    nft_number   INTEGER,
    event        TEXT,   -- mint|burn|transfer|sale|offer_create|offer_cancel|modify
    from_addr    TEXT,
    to_addr      TEXT,
    price_drops  INTEGER,
    price_token  TEXT,   -- JSON {currency, issuer, value} for IOU sales
    ledger_index INTEGER,
    ts           INTEGER,
    memo_action  TEXT,   -- provenance `action` memo (#54); NULL pre-schema
    PRIMARY KEY (tx_hash, nft_id)
);
CREATE INDEX IF NOT EXISTS idx_nftev_ts ON nft_events(ts);
CREATE INDEX IF NOT EXISTS idx_nftev_nft ON nft_events(nft_id);

CREATE TABLE IF NOT EXISTS brix_events (
    tx_hash      TEXT,
    account      TEXT,
    counterparty TEXT,
    delta        REAL,
    kind         TEXT,   -- payment|airdrop|amm_swap|amm_deposit|amm_withdraw|trustset|claim
    ts           INTEGER,
    PRIMARY KEY (tx_hash, account)
);
CREATE INDEX IF NOT EXISTS idx_brixev_ts ON brix_events(ts);

CREATE TABLE IF NOT EXISTS backfill_state (
    source     TEXT PRIMARY KEY,
    cursor     TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS balance_snapshots (
    snap_date TEXT,
    account   TEXT,
    brix      REAL,
    lp_tokens REAL,
    PRIMARY KEY (snap_date, account)
);
"""


def history_db_path(network: str) -> str:
    """Per-network history DB file; HISTORY_DB_PATH overrides."""
    override = os.getenv("HISTORY_DB_PATH")
    if override:
        return override
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(repo_root, f"history_{network}.db")


def init_history_db(path: str) -> sqlite3.Connection:
    """Initialize history DB with schema, Row factory, and WAL mode."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(_SCHEMA)
    # Self-migrate pre-existing DBs (CREATE TABLE IF NOT EXISTS skips them).
    cols = {r[1] for r in conn.execute("PRAGMA table_info(nft_events)")}
    if "memo_action" not in cols:
        conn.execute("ALTER TABLE nft_events ADD COLUMN memo_action TEXT")
    conn.commit()
    return conn


def insert_tx(
    conn: sqlite3.Connection,
    *,
    tx_hash: str,
    ledger_index: int | None,
    close_time: int | None,
    tx_type: str,
    account: str | None,
    source_tag: int | None,
    raw_json: str,
) -> bool:
    """Insert a transaction; return True if newly inserted, False if duplicate."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO xrpl_txs VALUES (?, ?, ?, ?, ?, ?, ?)",
        (tx_hash, ledger_index, close_time, tx_type, account, source_tag, raw_json),
    )
    return cur.rowcount > 0


def get_cursor(conn: sqlite3.Connection, source: str) -> str | None:
    """Get backfill cursor for a source, or None if not set."""
    row = conn.execute("SELECT cursor FROM backfill_state WHERE source=?", (source,)).fetchone()
    return row["cursor"] if row else None


def set_cursor(conn: sqlite3.Connection, source: str, cursor: str | None) -> None:
    """Set or clear backfill cursor for a source."""
    conn.execute(
        "INSERT INTO backfill_state (source, cursor, updated_at)"
        " VALUES (?, ?, CURRENT_TIMESTAMP)"
        " ON CONFLICT(source) DO UPDATE SET cursor=excluded.cursor,"
        " updated_at=CURRENT_TIMESTAMP",
        (source, cursor),
    )
    conn.commit()


_NFT_EV_COLS = (
    "tx_hash",
    "nft_id",
    "nft_number",
    "event",
    "from_addr",
    "to_addr",
    "price_drops",
    "price_token",
    "ledger_index",
    "ts",
    "memo_action",
)
_BRIX_EV_COLS = ("tx_hash", "account", "counterparty", "delta", "kind", "ts")


def insert_nft_event(conn: sqlite3.Connection, ev: dict[str, Any]) -> None:
    """Insert or replace an NFT event (derived table)."""
    conn.execute(
        f"INSERT OR REPLACE INTO nft_events ({','.join(_NFT_EV_COLS)})"
        f" VALUES ({','.join('?' * len(_NFT_EV_COLS))})",
        tuple(ev.get(c) for c in _NFT_EV_COLS),
    )


def insert_brix_event(conn: sqlite3.Connection, ev: dict[str, Any]) -> None:
    """Insert or replace a BRIX event (derived table)."""
    conn.execute(
        f"INSERT OR REPLACE INTO brix_events ({','.join(_BRIX_EV_COLS)})"
        f" VALUES ({','.join('?' * len(_BRIX_EV_COLS))})",
        tuple(ev.get(c) for c in _BRIX_EV_COLS),
    )


def clear_derived(conn: sqlite3.Connection) -> None:
    """Truncate derived event tables (nft_events, brix_events)."""
    conn.execute("DELETE FROM nft_events")
    conn.execute("DELETE FROM brix_events")


def upsert_snapshot(
    conn: sqlite3.Connection, snap_date: str, account: str, brix: float, lp_tokens: float
) -> None:
    """Insert or update a balance snapshot."""
    conn.execute(
        "INSERT INTO balance_snapshots VALUES (?, ?, ?, ?)"
        " ON CONFLICT(snap_date, account) DO UPDATE SET"
        " brix=excluded.brix, lp_tokens=excluded.lp_tokens",
        (snap_date, account, brix, lp_tokens),
    )
