"""Per-network ledger history archive: raw XRPL txs + derived NFT/BRIX events.

Raw `xrpl_txs` rows are the source of truth (verbatim {tx, meta} JSON);
`nft_events` / `brix_events` are derived, droppable, rebuildable. Follows the
same per-network-file posture as lfg_core/nft_index.py (onchain_<net>.db)."""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
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
CREATE INDEX IF NOT EXISTS idx_txs_source_tag_account
    ON xrpl_txs(source_tag, account);

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
    offer_index  TEXT,    -- NFTokenOffer ledger-object index (#411); NULL pre-schema
    offer_flags  INTEGER, -- NFTokenOffer Flags (bit 0 = sell) (#411); NULL pre-schema
    PRIMARY KEY (tx_hash, nft_id)
);
CREATE INDEX IF NOT EXISTS idx_nftev_ts ON nft_events(ts);
CREATE INDEX IF NOT EXISTS idx_nftev_nft ON nft_events(nft_id);
CREATE INDEX IF NOT EXISTS idx_nftev_event_number ON nft_events(event, nft_number);

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

CREATE TABLE IF NOT EXISTS archive_state (
    network                 TEXT PRIMARY KEY,
    genesis_hash            TEXT NOT NULL,
    source_tag              INTEGER,
    baseline_complete       INTEGER NOT NULL DEFAULT 0,
    baseline_ledger_min     INTEGER,
    baseline_ledger_max     INTEGER,
    baseline_provenance     TEXT,
    baseline_coverage       TEXT,
    baseline_completed_at   INTEGER,
    validated_ledger_index  INTEGER,
    validated_close_time    INTEGER,
    heartbeat_at            INTEGER,
    continuity_gap_at       INTEGER,
    continuity_gap_after    INTEGER,
    continuity_gap_before   INTEGER,
    continuity_gap_reason   TEXT,
    updated_at              INTEGER NOT NULL,
    CHECK (baseline_complete IN (0, 1))
);

CREATE TABLE IF NOT EXISTS balance_snapshots (
    snap_date TEXT,
    account   TEXT,
    brix      REAL,
    lp_tokens REAL,
    PRIMARY KEY (snap_date, account)
);
"""


@dataclass(frozen=True)
class ArchiveState:
    network: str
    genesis_hash: str
    source_tag: int | None
    baseline_complete: bool
    baseline_ledger_min: int | None
    baseline_ledger_max: int | None
    baseline_provenance: str | None
    baseline_coverage: str | None
    baseline_completed_at: int | None
    validated_ledger_index: int | None
    validated_close_time: int | None
    heartbeat_at: int | None
    continuity_gap_at: int | None
    continuity_gap_after: int | None
    continuity_gap_before: int | None
    continuity_gap_reason: str | None
    updated_at: int


# The oldest ledger any XRPL node can serve. Ledgers 1-32569 were lost in a
# 2012 operational incident and exist nowhere — asking for one returns
# lgrNotFound on mainnet and testnet alike, and account_tx rejects a
# ledger_index_min below this with lgrIdxMalformed. Every full-history range
# therefore starts here, not at 1, and this ledger's hash is the stable
# per-chain identity anchor the archive binds itself to.
EARLIEST_AVAILABLE_LEDGER = 32570


@dataclass(frozen=True)
class EndpointSnapshot:
    """Chain identity and validated tip observed from one live endpoint."""

    genesis_hash: str
    validated_ledger_index: int


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
    for column, declaration in (("offer_index", "TEXT"), ("offer_flags", "INTEGER")):
        if column not in cols:
            conn.execute(f"ALTER TABLE nft_events ADD COLUMN {column} {declaration}")
    archive_cols = {r[1] for r in conn.execute("PRAGMA table_info(archive_state)")}
    archive_migrations = {
        "source_tag": "INTEGER",
        "baseline_coverage": "TEXT",
        "continuity_gap_at": "INTEGER",
        "continuity_gap_after": "INTEGER",
        "continuity_gap_before": "INTEGER",
        "continuity_gap_reason": "TEXT",
    }
    source_tag_was_missing = "source_tag" not in archive_cols
    for column, declaration in archive_migrations.items():
        if column not in archive_cols:
            conn.execute(f"ALTER TABLE archive_state ADD COLUMN {column} {declaration}")
    if source_tag_was_missing:
        # An old row has no durable record of which eligibility tag its external
        # audit covered. Preserve its evidence but require explicit recertification.
        #
        # The gap must carry a bound for that recertification to be POSSIBLE
        # (see invalidate_archive_continuity): an unbounded gap can never be
        # proven covered, so stamping one here would turn "recertify to
        # continue" into "this archive can never be certified again" for every
        # stack upgrading from the pre-source_tag schema.
        timestamp = int(time.time())
        conn.execute(
            "UPDATE archive_state SET baseline_complete = 0, continuity_gap_at = ?, "
            "continuity_gap_after = COALESCE("
            "  continuity_gap_after, validated_ledger_index, baseline_ledger_max), "
            "continuity_gap_reason = ?, updated_at = ?",
            (timestamp, "archive predates SourceTag provenance", timestamp),
        )
    conn.commit()
    return conn


def get_archive_state(conn: sqlite3.Connection, network: str) -> ArchiveState | None:
    """Read the authoritative provenance/freshness record for one archive."""

    row = conn.execute(
        """
        SELECT network, genesis_hash, source_tag, baseline_complete,
               baseline_ledger_min, baseline_ledger_max, baseline_provenance,
               baseline_coverage, baseline_completed_at, validated_ledger_index,
               validated_close_time, heartbeat_at, continuity_gap_at,
               continuity_gap_after, continuity_gap_before, continuity_gap_reason,
               updated_at
        FROM archive_state WHERE network = ?
        """,
        (network,),
    ).fetchone()
    if row is None:
        return None
    return ArchiveState(
        network=row["network"],
        genesis_hash=row["genesis_hash"],
        source_tag=row["source_tag"],
        baseline_complete=bool(row["baseline_complete"]),
        baseline_ledger_min=row["baseline_ledger_min"],
        baseline_ledger_max=row["baseline_ledger_max"],
        baseline_provenance=row["baseline_provenance"],
        baseline_coverage=row["baseline_coverage"],
        baseline_completed_at=row["baseline_completed_at"],
        validated_ledger_index=row["validated_ledger_index"],
        validated_close_time=row["validated_close_time"],
        heartbeat_at=row["heartbeat_at"],
        continuity_gap_at=row["continuity_gap_at"],
        continuity_gap_after=row["continuity_gap_after"],
        continuity_gap_before=row["continuity_gap_before"],
        continuity_gap_reason=row["continuity_gap_reason"],
        updated_at=row["updated_at"],
    )


def _validate_archive_identity(network: str, genesis_hash: str) -> tuple[str, str]:
    network = network.strip().lower()
    genesis_hash = genesis_hash.strip()
    if network not in {"mainnet", "testnet"}:
        raise ValueError(f"unsupported archive network: {network}")
    if not genesis_hash:
        raise ValueError("archive genesis hash is required")
    return network, genesis_hash


def _configured_source_tag() -> int:
    from lfg_core import config

    return config.SOURCE_TAG


def _validate_source_tag(source_tag: int | None) -> int:
    value = _configured_source_tag() if source_tag is None else source_tag
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 0xFFFFFFFF:
        raise ValueError("archive SourceTag must be an unsigned 32-bit integer")
    return value


def _ledger_index(result: dict[str, Any]) -> int:
    ledger = result.get("ledger")
    ledger_dict = ledger if isinstance(ledger, dict) else {}
    raw = result.get("ledger_index", ledger_dict.get("ledger_index", ledger_dict.get("seqNum")))
    if isinstance(raw, bool) or not isinstance(raw, (int, str)):
        raise ValueError("endpoint ledger response omitted a valid ledger index")
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError("endpoint ledger response omitted a valid ledger index") from exc
    if value < 0:
        raise ValueError("endpoint ledger index must be non-negative")
    return value


def _ledger_hash(result: dict[str, Any]) -> str:
    ledger = result.get("ledger")
    ledger_dict = ledger if isinstance(ledger, dict) else {}
    value = str(result.get("ledger_hash") or ledger_dict.get("hash") or "").strip()
    if not value:
        raise ValueError("endpoint ledger response omitted its hash")
    return value


async def fetch_endpoint_snapshot(
    request_fn: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
) -> EndpointSnapshot:
    """Read the earliest retrievable ledger and the validated tip from one session.

    The first is the chain-identity anchor: its hash is stable forever on a
    given chain and differs across chains, so it binds an archive to the ledger
    it was built from. A testnet reset changes it, which is exactly the
    confusion worth catching.

    It is NOT ledger 1. Ledgers 1-32569 were lost in 2012 and no node serves
    them — live mainnet AND testnet clio both answer `ledger_index: 1` with
    lgrNotFound, and `account_tx` rejects `ledger_index_min` below
    EARLIEST_AVAILABLE_LEDGER with lgrIdxMalformed."""

    genesis = await request_fn(
        {"method": "ledger", "ledger_index": EARLIEST_AVAILABLE_LEDGER, "transactions": False}
    )
    if _ledger_index(genesis) != EARLIEST_AVAILABLE_LEDGER:
        raise ValueError(
            f"endpoint did not return ledger {EARLIEST_AVAILABLE_LEDGER} for the identity request"
        )
    tip = await request_fn({"method": "ledger", "ledger_index": "validated", "transactions": False})
    return EndpointSnapshot(
        genesis_hash=_ledger_hash(genesis),
        validated_ledger_index=_ledger_index(tip),
    )


def record_archive_baseline(
    conn: sqlite3.Connection,
    *,
    network: str,
    genesis_hash: str,
    ledger_min: int,
    ledger_max: int,
    provenance: str,
    source_tag: int | None = None,
    coverage: str | None = None,
    completed_at: int | None = None,
) -> None:
    """Mark an audited SourceTag baseline complete for exactly one chain."""

    network, genesis_hash = _validate_archive_identity(network, genesis_hash)
    source_tag = _validate_source_tag(source_tag)
    provenance = provenance.strip()
    coverage = coverage.strip() if coverage is not None else None
    if ledger_min < 0 or ledger_max < ledger_min:
        raise ValueError("invalid archive baseline ledger range")
    if not provenance:
        raise ValueError("archive baseline provenance is required")
    if coverage == "":
        raise ValueError("archive baseline coverage cannot be blank")
    timestamp = int(time.time()) if completed_at is None else int(completed_at)
    existing = get_archive_state(conn, network)
    if existing is not None and existing.genesis_hash != genesis_hash:
        raise ValueError("archive genesis hash conflicts with persisted provenance")
    other = conn.execute(
        "SELECT network FROM archive_state WHERE network != ? LIMIT 1", (network,)
    ).fetchone()
    if other is not None:
        raise ValueError("one history archive cannot contain multiple XRPL networks")
    conn.execute(
        """
        INSERT INTO archive_state (
            network, genesis_hash, source_tag, baseline_complete,
            baseline_ledger_min, baseline_ledger_max, baseline_provenance,
            baseline_coverage, baseline_completed_at, validated_ledger_index,
            validated_close_time, heartbeat_at, continuity_gap_at,
            continuity_gap_after, continuity_gap_before, continuity_gap_reason,
            updated_at
        ) VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL, NULL, NULL, ?)
        ON CONFLICT(network) DO UPDATE SET
            source_tag = excluded.source_tag,
            -- A certification run proves coverage of [ledger_min, ledger_max]
            -- and NOTHING above it. The listener streams concurrently with the
            -- (often long) backfill, so a disconnect can stamp a gap whose
            -- upper bound lies past the certified tip. Clearing that
            -- unconditionally would assert coverage the sweep never had: the
            -- archive would read as certified-complete while missing the
            -- transactions from the gap, and a wallet that IS already tagged
            -- would look eligible and be handed a free mint.
            --
            -- So a gap clears only when the certified sweep reached at or past
            -- the gap's upper extent: `continuity_gap_before` when the stream
            -- resumed at a known ledger, otherwise `continuity_gap_after` (the
            -- last ledger we know we had, where continuity was lost). An
            -- account_tx sweep of [1, ledger_max] genuinely re-fetches
            -- everything below ledger_max, so reaching past the loss point is
            -- the proof. The lower bound needs no test because
            -- baseline_ledger_min is pinned to 1. A gap with no bounds at all
            -- can never be proven covered. A surviving gap keeps
            -- baseline_complete = 0, so archive_is_usable keeps failing closed
            -- until the operator re-certifies against a tip above the gap.
            baseline_complete = CASE
                WHEN continuity_gap_at IS NULL THEN 1
                WHEN COALESCE(continuity_gap_before, continuity_gap_after) IS NOT NULL
                     AND excluded.baseline_ledger_max
                         >= COALESCE(continuity_gap_before, continuity_gap_after) THEN 1
                ELSE 0
            END,
            baseline_ledger_min = excluded.baseline_ledger_min,
            baseline_ledger_max = excluded.baseline_ledger_max,
            baseline_provenance = excluded.baseline_provenance,
            baseline_coverage = excluded.baseline_coverage,
            baseline_completed_at = excluded.baseline_completed_at,
            validated_ledger_index = NULL,
            validated_close_time = NULL,
            heartbeat_at = NULL,
            continuity_gap_at = CASE
                WHEN COALESCE(continuity_gap_before, continuity_gap_after) IS NOT NULL
                     AND excluded.baseline_ledger_max
                         >= COALESCE(continuity_gap_before, continuity_gap_after) THEN NULL
                ELSE continuity_gap_at
            END,
            continuity_gap_after = CASE
                WHEN COALESCE(continuity_gap_before, continuity_gap_after) IS NOT NULL
                     AND excluded.baseline_ledger_max
                         >= COALESCE(continuity_gap_before, continuity_gap_after) THEN NULL
                ELSE continuity_gap_after
            END,
            continuity_gap_reason = CASE
                WHEN COALESCE(continuity_gap_before, continuity_gap_after) IS NOT NULL
                     AND excluded.baseline_ledger_max
                         >= COALESCE(continuity_gap_before, continuity_gap_after) THEN NULL
                ELSE continuity_gap_reason
            END,
            continuity_gap_before = CASE
                WHEN COALESCE(continuity_gap_before, continuity_gap_after) IS NOT NULL
                     AND excluded.baseline_ledger_max
                         >= COALESCE(continuity_gap_before, continuity_gap_after) THEN NULL
                ELSE continuity_gap_before
            END,
            updated_at = excluded.updated_at
        """,
        (
            network,
            genesis_hash,
            source_tag,
            ledger_min,
            ledger_max,
            provenance,
            coverage,
            timestamp,
            timestamp,
        ),
    )
    conn.commit()


def record_validated_ledger(
    conn: sqlite3.Connection,
    *,
    network: str,
    genesis_hash: str,
    ledger_index: int,
    close_time: int,
    source_tag: int | None = None,
    observed_at: int | None = None,
    commit: bool = True,
) -> None:
    """Advance the validated-ledger cursor and liveness heartbeat monotonically.

    `commit=False` leaves the write inside the caller's open transaction so a
    batched flush can commit evidence rows and this heartbeat advance
    atomically (#333) — the heartbeat must never persist past evidence that
    failed to."""

    network, genesis_hash = _validate_archive_identity(network, genesis_hash)
    source_tag = _validate_source_tag(source_tag)
    if ledger_index < 0 or close_time < 0:
        raise ValueError("validated ledger index and close time must be non-negative")
    timestamp = int(time.time()) if observed_at is None else int(observed_at)
    existing = get_archive_state(conn, network)
    if existing is not None and existing.genesis_hash != genesis_hash:
        raise ValueError("validated ledger genesis conflicts with archive baseline")
    if existing is not None and existing.source_tag not in {None, source_tag}:
        raise ValueError("validated ledger SourceTag conflicts with archive baseline")
    other = conn.execute(
        "SELECT network FROM archive_state WHERE network != ? LIMIT 1", (network,)
    ).fetchone()
    if other is not None:
        raise ValueError("one history archive cannot contain multiple XRPL networks")
    conn.execute(
        """
        INSERT INTO archive_state (
            network, genesis_hash, source_tag, baseline_complete, validated_ledger_index,
            validated_close_time, heartbeat_at, updated_at
        ) VALUES (?, ?, ?, 0, ?, ?, ?, ?)
        ON CONFLICT(network) DO UPDATE SET
            source_tag = COALESCE(archive_state.source_tag, excluded.source_tag),
            validated_ledger_index = CASE
                WHEN archive_state.validated_ledger_index IS NULL
                  OR excluded.validated_ledger_index > archive_state.validated_ledger_index
                THEN excluded.validated_ledger_index
                ELSE archive_state.validated_ledger_index END,
            validated_close_time = CASE
                WHEN archive_state.validated_ledger_index IS NULL
                  OR excluded.validated_ledger_index >= archive_state.validated_ledger_index
                THEN excluded.validated_close_time
                ELSE archive_state.validated_close_time END,
            heartbeat_at = excluded.heartbeat_at,
            updated_at = excluded.updated_at
        """,
        (network, genesis_hash, source_tag, ledger_index, close_time, timestamp, timestamp),
    )
    if commit:
        conn.commit()


def invalidate_archive_continuity(
    conn: sqlite3.Connection,
    *,
    network: str,
    reason: str,
    gap_after: int | None = None,
    gap_before: int | None = None,
    invalidated_at: int | None = None,
) -> None:
    """Fail closed after any interval the transaction stream cannot prove complete."""

    # Normalize exactly like record_archive_baseline/record_validated_ledger.
    # This is the one writer whose entire job is failing CLOSED, so a silent
    # miss is the worst possible failure mode: an unnormalized name ("Mainnet",
    # " mainnet") matches zero rows, the UPDATE reports success, and a
    # certified archive keeps baseline_complete = 1 having just lost
    # continuity — i.e. it fails open, and eligibility then runs against an
    # archive known to be incomplete.
    network = network.strip().lower()
    if network not in {"mainnet", "testnet"}:
        raise ValueError(f"unsupported archive network: {network}")
    reason = reason.strip()
    if not reason:
        raise ValueError("archive continuity invalidation reason is required")
    if gap_after is not None and gap_after < 0:
        raise ValueError("archive gap lower bound must be non-negative")
    if gap_before is not None and gap_before < 0:
        raise ValueError("archive gap upper bound must be non-negative")
    timestamp = int(time.time()) if invalidated_at is None else int(invalidated_at)
    cursor = conn.execute(
        """
        UPDATE archive_state
        SET baseline_complete = 0,
            continuity_gap_at = COALESCE(continuity_gap_at, ?),
            -- An unbounded gap is UNCLEARABLE: record_archive_baseline only
            -- clears a gap whose upper extent the certified sweep provably
            -- reached, and COALESCE(before, after) IS NULL fails that test on
            -- every future certification. So a gap reported with no bounds
            -- would fail closed forever, with no recovery short of hand-
            -- editing this table.
            --
            -- Callers legitimately have nothing to report: the listener
            -- derives its bound from validated_ledger_index, which
            -- record_archive_baseline sets to NULL, so any disconnect before
            -- the next validated-ledger write passes None. Fall back to the
            -- row's own knowledge instead of storing NULL — the certified
            -- tip is proven-covered ground, so a later sweep past it is
            -- honest proof the gap is covered. Only an archive with no
            -- certified baseline at all can still land unbounded, and that
            -- one already fails closed for want of a baseline.
            continuity_gap_after = COALESCE(
                continuity_gap_after, ?, validated_ledger_index, baseline_ledger_max
            ),
            continuity_gap_before = COALESCE(continuity_gap_before, ?),
            continuity_gap_reason = COALESCE(continuity_gap_reason, ?),
            updated_at = ?
        WHERE network = ?
        """,
        (timestamp, gap_after, gap_before, reason, timestamp, network),
    )
    conn.commit()
    if cursor.rowcount == 0:
        # No row for this network. There is nothing to fail closed ON — an
        # uncertified archive already fails closed in archive_is_usable — but
        # a silent zero-row update here would be indistinguishable from a
        # successful invalidation, so say so rather than returning quietly.
        logging.warning(
            "archive continuity invalidation for %s matched no archive_state row (%s); "
            "the archive is uncertified, so eligibility remains unavailable either way",
            network,
            reason,
        )


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
    "offer_index",
    "offer_flags",
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
