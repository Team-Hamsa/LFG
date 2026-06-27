# lfg_core/economy_store.py
# Persistence for the trait economy: the frozen genesis baseline plus the
# (initially empty) live-state tables (Closets, standalone trait tokens). Lives
# in the same per-network onchain_{network}.db as the nft_index.

from __future__ import annotations

import json
import sqlite3
from typing import Any

from lfg_core import trait_economy

# Written into genesis_meta as the final step of a freeze; genesis_exists keys
# off this flag alone, so a partially-written (e.g. interrupted) genesis never
# reads as complete.
_GENESIS_COMPLETE_KEY = "genesis_complete"

_ECONOMY_SCHEMA = """
CREATE TABLE IF NOT EXISTS trait_genesis (
    slot          TEXT,
    value         TEXT,
    genesis_count INTEGER,
    PRIMARY KEY (slot, value)
);
CREATE TABLE IF NOT EXISTS edition_bodies (
    edition    INTEGER PRIMARY KEY,
    body_value TEXT,
    body_class TEXT
);
CREATE TABLE IF NOT EXISTS genesis_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS closet_assets (
    owner TEXT,
    slot  TEXT,
    value TEXT,
    count INTEGER,
    PRIMARY KEY (owner, slot, value)
);
CREATE TABLE IF NOT EXISTS closet_bodies (
    owner   TEXT,
    edition INTEGER PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS trait_tokens (
    nft_id TEXT PRIMARY KEY,
    owner  TEXT,
    slot   TEXT,
    value  TEXT
);
CREATE TABLE IF NOT EXISTS closet_tokens (
    owner      TEXT PRIMARY KEY,
    nft_id     TEXT,
    uri_hex    TEXT,
    status     TEXT DEFAULT 'pending_accept',
    offer_id   TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS supply_changes (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    kind              TEXT,   -- 'mint' (supply +) | 'burn' (supply -)
    edition           INTEGER,
    body_value        TEXT,
    body_class        TEXT,
    trait_deltas_json TEXT,   -- {"slot|value": signed_count, ...}
    actor             TEXT,
    reason            TEXT,
    applied_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


def _migrate_bucket_tables(conn: sqlite3.Connection) -> None:
    """One-time copy of legacy bucket_* rows into the closet_* tables (for index
    DBs created before the Bucket→Closet rename). Copies the shared base columns;
    new closet_tokens columns (status/offer_id) take their schema defaults."""
    have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for old, new, cols in (
        ("bucket_assets", "closet_assets", "owner, slot, value, count"),
        ("bucket_bodies", "closet_bodies", "owner, edition"),
        ("bucket_tokens", "closet_tokens", "owner, nft_id, uri_hex"),
    ):
        if old in have:
            conn.execute(f"INSERT OR IGNORE INTO {new} ({cols}) SELECT {cols} FROM {old}")
    conn.commit()


def init_economy_schema(conn: sqlite3.Connection) -> None:
    """Create the genesis + live-state tables if absent, and migrate legacy bucket_* tables."""
    conn.executescript(_ECONOMY_SCHEMA)
    conn.commit()
    _migrate_bucket_tables(conn)


def genesis_exists(conn: sqlite3.Connection) -> bool:
    """True only if a genesis was fully written. Keyed off the genesis_complete
    flag (not the presence of rows in either table), so an interrupted freeze is
    never mistaken for a complete one."""
    cur = conn.execute("SELECT 1 FROM genesis_meta WHERE key = ?", (_GENESIS_COMPLETE_KEY,))
    return cur.fetchone() is not None


def clear_genesis(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM trait_genesis")
    conn.execute("DELETE FROM edition_bodies")
    conn.execute("DELETE FROM genesis_meta")
    conn.commit()


def freeze_genesis(
    conn: sqlite3.Connection, genesis: trait_economy.Genesis, meta: dict[str, str]
) -> None:
    """Persist a genesis baseline, atomically replacing any existing one.

    The DELETEs, the INSERTs, and the genesis_complete flag all land in a single
    transaction (one commit at the end), so a crash mid-freeze leaves the prior
    genesis intact rather than an empty/partial one."""
    conn.execute("DELETE FROM trait_genesis")
    conn.execute("DELETE FROM edition_bodies")
    conn.execute("DELETE FROM genesis_meta")
    conn.executemany(
        "INSERT INTO trait_genesis (slot, value, genesis_count) VALUES (?, ?, ?)",
        [(slot, value, count) for (slot, value), count in genesis.trait_counts.items()],
    )
    conn.executemany(
        "INSERT INTO edition_bodies (edition, body_value, body_class) VALUES (?, ?, ?)",
        [(ed, bv, bc) for ed, (bv, bc) in genesis.edition_bodies.items()],
    )
    conn.executemany(
        "INSERT INTO genesis_meta (key, value) VALUES (?, ?)",
        list(meta.items()),
    )
    # The completeness flag is the last write before the single commit.
    conn.execute(
        "INSERT INTO genesis_meta (key, value) VALUES (?, ?)",
        (_GENESIS_COMPLETE_KEY, "1"),
    )
    conn.commit()


def read_genesis(conn: sqlite3.Connection) -> trait_economy.Genesis:
    trait_counts: dict[tuple[str, str], int] = {
        (str(slot), str(value)): int(count)
        for slot, value, count in conn.execute(
            "SELECT slot, value, genesis_count FROM trait_genesis"
        )
    }
    edition_bodies: dict[int, tuple[str, str]] = {
        int(ed): (str(bv), str(bc))
        for ed, bv, bc in conn.execute("SELECT edition, body_value, body_class FROM edition_bodies")
    }
    return trait_economy.Genesis(trait_counts=trait_counts, edition_bodies=edition_bodies)


def read_meta(conn: sqlite3.Connection, key: str) -> str | None:
    cur = conn.execute("SELECT value FROM genesis_meta WHERE key = ?", (key,))
    row = cur.fetchone()
    return None if row is None else str(row[0])


def read_closet_assets(conn: sqlite3.Connection) -> list[tuple[str, str, str, int]]:
    return [
        (str(owner), str(slot), str(value), int(count))
        for owner, slot, value, count in conn.execute(
            "SELECT owner, slot, value, count FROM closet_assets"
        )
    ]


def read_closet_bodies(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    return [
        (str(owner), int(edition))
        for owner, edition in conn.execute("SELECT owner, edition FROM closet_bodies")
    ]


def read_trait_tokens(conn: sqlite3.Connection) -> list[tuple[str, str, str, str]]:
    return [
        (str(nft_id), str(owner), str(slot), str(value))
        for nft_id, owner, slot, value in conn.execute(
            "SELECT nft_id, owner, slot, value FROM trait_tokens"
        )
    ]


# --- Phase 2: per-user Closet contents + supply-change ledger ---


def set_closet_contents(
    conn: sqlite3.Connection,
    owner: str,
    assets: list[tuple[str, str, int]],
    bodies: list[int],
) -> None:
    """Replace ALL of `owner`'s loose-asset and loose-body rows in one
    transaction. Used by both the flows (optimistic write) and the listener
    (rebuild from the Closet NFToken's metadata). Rows with count <= 0 are
    dropped so the mirror never carries empty entries."""
    conn.execute("DELETE FROM closet_assets WHERE owner = ?", (owner,))
    conn.execute("DELETE FROM closet_bodies WHERE owner = ?", (owner,))
    conn.executemany(
        "INSERT INTO closet_assets (owner, slot, value, count) VALUES (?, ?, ?, ?)",
        [(owner, slot, value, count) for slot, value, count in assets if count > 0],
    )
    conn.executemany(
        "INSERT INTO closet_bodies (owner, edition) VALUES (?, ?)",
        [(owner, edition) for edition in bodies],
    )
    conn.commit()


def set_closet_token(conn: sqlite3.Connection, owner: str, nft_id: str, uri_hex: str) -> None:
    """Record (or update) the on-ledger Closet NFToken id + current URI for an owner."""
    conn.execute(
        """
        INSERT INTO closet_tokens (owner, nft_id, uri_hex, updated_at)
        VALUES (?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(owner) DO UPDATE SET
            nft_id=excluded.nft_id, uri_hex=excluded.uri_hex, updated_at=CURRENT_TIMESTAMP
        """,
        (owner, nft_id, uri_hex),
    )
    conn.commit()


def get_closet_token(conn: sqlite3.Connection, owner: str) -> tuple[str, str] | None:
    """The (nft_id, uri_hex) of an owner's Closet NFToken, or None if unminted."""
    cur = conn.execute("SELECT nft_id, uri_hex FROM closet_tokens WHERE owner = ?", (owner,))
    row = cur.fetchone()
    return None if row is None else (str(row[0]), str(row[1]))


def record_supply_change(
    conn: sqlite3.Connection,
    kind: str,
    edition: int | None,
    body_value: str,
    body_class: str,
    trait_deltas: dict[str, int],
    actor: str,
    reason: str,
) -> None:
    """Append one intentional supply change (kind 'mint' grows supply, 'burn'
    shrinks it). trait_deltas keys are "slot|value", values are signed counts."""
    conn.execute(
        """
        INSERT INTO supply_changes
            (kind, edition, body_value, body_class, trait_deltas_json, actor, reason)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (kind, edition, body_value, body_class, json.dumps(trait_deltas), actor, reason),
    )
    conn.commit()


def read_supply_changes(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every supply-change row, oldest first, with trait_deltas parsed back to a dict."""
    rows = conn.execute(
        "SELECT kind, edition, body_value, body_class, trait_deltas_json, actor, reason "
        "FROM supply_changes ORDER BY id"
    )
    out: list[dict[str, Any]] = []
    for kind, edition, body_value, body_class, deltas_json, actor, reason in rows:
        out.append(
            {
                "kind": str(kind),
                "edition": None if edition is None else int(edition),
                "body_value": str(body_value),
                "body_class": str(body_class),
                "trait_deltas": dict(json.loads(deltas_json)) if deltas_json else {},
                "actor": str(actor),
                "reason": str(reason),
            }
        )
    return out
