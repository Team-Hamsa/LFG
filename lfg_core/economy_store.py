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
    owner          TEXT PRIMARY KEY,
    nft_id         TEXT,
    uri_hex        TEXT,
    status         TEXT DEFAULT 'pending_accept',
    offer_id       TEXT,
    mirror_pending INTEGER DEFAULT 0,
    updated_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
    nft_id            TEXT,   -- token this change accounts for (burn idempotency key, #322)
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


def _migrate_closet_columns(conn: sqlite3.Connection) -> None:
    """Self-migrate closet_tokens columns added after the table first shipped.
    `mirror_pending` (#184) flags an owner whose on-chain Closet was modified
    but whose local mirror write failed (`complete_pending_mirror`); an index DB
    created before this column needs it added. ADD COLUMN is idempotent-guarded
    by the PRAGMA check."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(closet_tokens)")}
    if "mirror_pending" not in cols:
        conn.execute("ALTER TABLE closet_tokens ADD COLUMN mirror_pending INTEGER DEFAULT 0")
    conn.commit()


def _migrate_supply_changes_columns(conn: sqlite3.Connection) -> None:
    """Self-migrate supply_changes columns added after the table first shipped.
    `nft_id` (#322) stamps the burned/minted token on a row so out-of-band burn
    recording can be idempotent per token (see supply_change_exists_for_nft).
    ADD COLUMN is idempotent-guarded by the PRAGMA check."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(supply_changes)")}
    if "nft_id" not in cols:
        conn.execute("ALTER TABLE supply_changes ADD COLUMN nft_id TEXT")
    # At most ONE burn row per stamped token, enforced by the database itself:
    # the standalone existence check + INSERT is not atomic across writers
    # (listener vs an apply-mode reconciler), so the INSERT (OR IGNORE, see
    # record_supply_change) must be the arbiter. Partial index — NULL nft_id
    # (legacy/flow rows) stays unconstrained. Safe on existing DBs: the column
    # is new in the same release, so no stamped duplicates can pre-exist.
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_supply_changes_burn_nft "
        "ON supply_changes(nft_id) WHERE kind='burn' AND nft_id IS NOT NULL"
    )
    conn.commit()


def init_economy_schema(conn: sqlite3.Connection) -> None:
    """Create the genesis + live-state tables if absent, and migrate legacy bucket_* tables."""
    conn.executescript(_ECONOMY_SCHEMA)
    conn.commit()
    _migrate_bucket_tables(conn)
    _migrate_closet_columns(conn)
    _migrate_supply_changes_columns(conn)


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


def upsert_trait_token(
    conn: sqlite3.Connection, nft_id: str, owner: str, slot: str, value: str
) -> None:
    """Insert/replace a standalone trait NFToken row (PK nft_id). Used by the
    listener (mint/transfer rebuild) and the extract flow (optimistic write)."""
    conn.execute(
        """
        INSERT INTO trait_tokens (nft_id, owner, slot, value) VALUES (?, ?, ?, ?)
        ON CONFLICT(nft_id) DO UPDATE SET owner=excluded.owner, slot=excluded.slot, value=excluded.value
        """,
        (nft_id, owner, slot, value),
    )
    conn.commit()


def delete_trait_token(conn: sqlite3.Connection, nft_id: str) -> None:
    conn.execute("DELETE FROM trait_tokens WHERE nft_id = ?", (nft_id,))
    conn.commit()


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
    # A full authoritative rewrite of the mirror (flow optimistic write or
    # listener/backfill rebuild-from-token) makes the DB consistent with the
    # on-chain Closet again, so clear any outstanding mirror_pending flag (#184)
    # in the SAME transaction — a rollback (half-applied mirror) leaves it set.
    conn.execute("UPDATE closet_tokens SET mirror_pending = 0 WHERE owner = ?", (owner,))
    conn.commit()


def delete_closet(conn: sqlite3.Connection, owner: str) -> None:
    """Remove an owner's Closet token record and all of its loose contents.

    Used to scrub bogus rows keyed under the issuer address: the issuer is never
    a legitimate Closet owner-of-record, so a prior (pre-#178/#190) backfill that
    recorded a pending issuer-held Closet under the issuer must be cleaned up on
    reconcile rather than left to strand the real user's Closet."""
    conn.execute("DELETE FROM closet_assets WHERE owner = ?", (owner,))
    conn.execute("DELETE FROM closet_bodies WHERE owner = ?", (owner,))
    conn.execute("DELETE FROM closet_tokens WHERE owner = ?", (owner,))
    conn.commit()


def set_closet_token(
    conn: sqlite3.Connection,
    owner: str,
    nft_id: str,
    uri_hex: str,
    status: str = "pending_accept",
    offer_id: str | None = None,
) -> None:
    """Record/update an owner's Closet NFToken id, URI, lifecycle status, and the
    outstanding accept offer id (kept so the UI can re-show the Xaman accept)."""
    conn.execute(
        """
        INSERT INTO closet_tokens (owner, nft_id, uri_hex, status, offer_id, updated_at)
        VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(owner) DO UPDATE SET
            nft_id=excluded.nft_id, uri_hex=excluded.uri_hex,
            status=excluded.status, offer_id=excluded.offer_id, updated_at=CURRENT_TIMESTAMP
        """,
        (owner, nft_id, uri_hex, status, offer_id),
    )
    conn.commit()


def set_closet_status(conn: sqlite3.Connection, owner: str, status: str) -> None:
    conn.execute(
        "UPDATE closet_tokens SET status=?, updated_at=CURRENT_TIMESTAMP WHERE owner=?",
        (status, owner),
    )
    conn.commit()


def get_closet_record(
    conn: sqlite3.Connection, owner: str
) -> tuple[str, str, str, str | None] | None:
    """(nft_id, uri_hex, status, offer_id) for an owner's Closet, or None."""
    cur = conn.execute(
        "SELECT nft_id, uri_hex, status, offer_id FROM closet_tokens WHERE owner=?", (owner,)
    )
    row = cur.fetchone()
    return None if row is None else (str(row[0]), str(row[1]), str(row[2]), row[3])


def get_closet_token(conn: sqlite3.Connection, owner: str) -> tuple[str, str] | None:
    """The (nft_id, uri_hex) of an owner's Closet NFToken, or None if unminted."""
    cur = conn.execute("SELECT nft_id, uri_hex FROM closet_tokens WHERE owner = ?", (owner,))
    row = cur.fetchone()
    return None if row is None else (str(row[0]), str(row[1]))


def set_mirror_pending(conn: sqlite3.Connection, owner: str, pending: bool) -> None:
    """Mark (or clear) an owner's Closet DB mirror as stale-but-consistent (#184):
    set when an on-chain Closet modify committed but the local mirror write failed
    (`complete_pending_mirror`), so the next op refuses to full-write the token off
    the stale mirror. Cleared by `set_closet_contents` on the next authoritative
    rewrite (flow success or listener/backfill rebuild)."""
    conn.execute(
        "UPDATE closet_tokens SET mirror_pending = ? WHERE owner = ?",
        (1 if pending else 0, owner),
    )
    conn.commit()


def get_mirror_pending(conn: sqlite3.Connection, owner: str) -> bool:
    """True if the owner's Closet DB mirror is flagged stale (see set_mirror_pending)."""
    cur = conn.execute("SELECT mirror_pending FROM closet_tokens WHERE owner = ?", (owner,))
    row = cur.fetchone()
    return bool(row is not None and row[0])


def record_supply_change(
    conn: sqlite3.Connection,
    kind: str,
    edition: int | None,
    body_value: str,
    body_class: str,
    trait_deltas: dict[str, int],
    actor: str,
    reason: str,
    nft_id: str | None = None,
) -> None:
    """Append one intentional supply change (kind 'mint' grows supply, 'burn'
    shrinks it). trait_deltas keys are "slot|value", values are signed counts.
    `nft_id` (optional) stamps the token the change accounts for, so burn
    recording can be idempotent per token (#322): the partial unique index
    idx_supply_changes_burn_nft makes a duplicate stamped burn INSERT an
    atomic no-op (OR IGNORE) even across concurrent connections — the
    existence pre-check in callers is an optimisation, not the guarantee."""
    conn.execute(
        """
        INSERT OR IGNORE INTO supply_changes
            (kind, edition, body_value, body_class, trait_deltas_json, actor, reason, nft_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (kind, edition, body_value, body_class, json.dumps(trait_deltas), actor, reason, nft_id),
    )
    conn.commit()


def supply_change_exists_for_edition(conn: sqlite3.Connection, edition: int) -> bool:
    """True if ANY supply_changes row names this edition — i.e. the ledger has
    ever seen it grow or shrink (a popped edition necessarily has a burn row)."""
    row = conn.execute(
        "SELECT 1 FROM supply_changes WHERE edition = ? LIMIT 1", (edition,)
    ).fetchone()
    return row is not None


def supply_change_exists_for_nft(conn: sqlite3.Connection, nft_id: str, kind: str = "burn") -> bool:
    """True if a supply_changes row of `kind` already accounts for this token —
    the idempotency gate that stops the listener/reconciler double-counting a
    burn a flow (e.g. the legacy flag-24 harvest upgrade) already logged.
    A pre-migration DB (no nft_id column, e.g. opened read-only for a dry-run)
    has no stamped rows by definition -> False."""
    try:
        cur = conn.execute(
            "SELECT 1 FROM supply_changes WHERE kind=? AND nft_id=? LIMIT 1", (kind, nft_id)
        )
    except sqlite3.OperationalError:
        return False
    return cur.fetchone() is not None


def read_supply_changes(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every supply-change row, oldest first, with trait_deltas parsed back to a dict."""
    try:
        rows = conn.execute(
            "SELECT kind, edition, body_value, body_class, trait_deltas_json, actor, reason, "
            "nft_id FROM supply_changes ORDER BY id"
        )
    except sqlite3.OperationalError:
        # Pre-migration DB opened read-only (dry-run tooling): no nft_id column.
        rows = conn.execute(
            "SELECT kind, edition, body_value, body_class, trait_deltas_json, actor, reason, "
            "NULL FROM supply_changes ORDER BY id"
        )
    out: list[dict[str, Any]] = []
    for kind, edition, body_value, body_class, deltas_json, actor, reason, nft_id in rows:
        out.append(
            {
                "kind": str(kind),
                "edition": None if edition is None else int(edition),
                "body_value": str(body_value),
                "body_class": str(body_class),
                "trait_deltas": dict(json.loads(deltas_json)) if deltas_json else {},
                "actor": str(actor),
                "reason": str(reason),
                "nft_id": None if nft_id is None else str(nft_id),
            }
        )
    return out
