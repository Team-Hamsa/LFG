# lfg_core/economy_store.py
# Persistence for the trait economy: the frozen genesis baseline plus the
# (initially empty) live-state tables (Buckets, standalone trait tokens). Lives
# in the same per-network onchain_{network}.db as the nft_index.

from __future__ import annotations

import sqlite3

from lfg_core import trait_economy

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
CREATE TABLE IF NOT EXISTS bucket_assets (
    owner TEXT,
    slot  TEXT,
    value TEXT,
    count INTEGER,
    PRIMARY KEY (owner, slot, value)
);
CREATE TABLE IF NOT EXISTS bucket_bodies (
    owner   TEXT,
    edition INTEGER PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS trait_tokens (
    nft_id TEXT PRIMARY KEY,
    owner  TEXT,
    slot   TEXT,
    value  TEXT
);
"""


def init_economy_schema(conn: sqlite3.Connection) -> None:
    """Create the genesis + live-state tables if absent."""
    conn.executescript(_ECONOMY_SCHEMA)
    conn.commit()


def genesis_exists(conn: sqlite3.Connection) -> bool:
    cur = conn.execute("SELECT 1 FROM trait_genesis LIMIT 1")
    if cur.fetchone() is not None:
        return True
    cur = conn.execute("SELECT 1 FROM edition_bodies LIMIT 1")
    return cur.fetchone() is not None


def clear_genesis(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM trait_genesis")
    conn.execute("DELETE FROM edition_bodies")
    conn.execute("DELETE FROM genesis_meta")
    conn.commit()


def freeze_genesis(
    conn: sqlite3.Connection, genesis: trait_economy.Genesis, meta: dict[str, str]
) -> None:
    """Persist a genesis baseline (replacing any existing one)."""
    clear_genesis(conn)
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


def read_bucket_assets(conn: sqlite3.Connection) -> list[tuple[str, str, str, int]]:
    return [
        (str(owner), str(slot), str(value), int(count))
        for owner, slot, value, count in conn.execute(
            "SELECT owner, slot, value, count FROM bucket_assets"
        )
    ]


def read_bucket_bodies(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    return [
        (str(owner), int(edition))
        for owner, edition in conn.execute("SELECT owner, edition FROM bucket_bodies")
    ]


def read_trait_tokens(conn: sqlite3.Connection) -> list[tuple[str, str, str, str]]:
    return [
        (str(nft_id), str(owner), str(slot), str(value))
        for nft_id, owner, slot, value in conn.execute(
            "SELECT nft_id, owner, slot, value FROM trait_tokens"
        )
    ]
