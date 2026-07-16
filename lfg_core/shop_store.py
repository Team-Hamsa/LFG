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

# #238 XRP fallback: self-migrating ALTERs (pattern from identities.user_token)
# so a DB created with the pre-#238 schema gains the new columns on open.
# pay_with is NULL on legacy rows — callers treat NULL as "BRIX".
_MIGRATIONS = (
    "ALTER TABLE shop_orders ADD COLUMN pay_with TEXT",
    "ALTER TABLE shop_orders ADD COLUMN price_xrp TEXT",
    "ALTER TABLE shop_orders ADD COLUMN buyback_done INTEGER DEFAULT 0",
)

_COLS = (
    "session_id",
    "buyer",
    "slot",
    "value",
    "price_brix",
    "nft_id",
    "offer_index",
    "status",
    "created_ts",
    "updated_ts",
    "pay_with",
    "price_xrp",
    "buyback_done",
)

VALID_STATUSES = frozenset(
    {"pending_mint", "pending_accept", "accepted", "settled", "expired", "failed"}
)


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(_SCHEMA)
    for stmt in _MIGRATIONS:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e).lower():
                raise
    conn.commit()


def create_order(
    conn: sqlite3.Connection,
    session_id: str,
    buyer: str,
    slot: str,
    value: str,
    price_brix: int,
    now_ts: int,
    pay_with: str = "BRIX",
    price_xrp: str | None = None,
) -> None:
    ensure_schema(conn)
    conn.execute(
        "INSERT INTO shop_orders (session_id, buyer, slot, value, price_brix,"
        " status, created_ts, updated_ts, pay_with, price_xrp)"
        " VALUES (?,?,?,?,?,'pending_mint',?,?,?,?)",
        (session_id, buyer, slot, value, price_brix, now_ts, now_ts, pay_with, price_xrp),
    )
    conn.commit()


def update_order(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    now_ts: int,
    status: str | None = None,
    nft_id: str | None = None,
    offer_index: str | None = None,
    pay_with: str | None = None,
    price_xrp: str | None = None,
    buyback_done: int | None = None,
) -> None:
    if status is not None and status not in VALID_STATUSES:
        raise ValueError(f"unknown shop order status: {status}")
    sets: list[str] = ["updated_ts=?"]
    params: list[int | str] = [now_ts]
    for col, val in (
        ("status", status),
        ("nft_id", nft_id),
        ("offer_index", offer_index),
        ("pay_with", pay_with),
        ("price_xrp", price_xrp),
        ("buyback_done", buyback_done),
    ):
        if val is not None:
            sets.append(f"{col}=?")
            params.append(val)
    conn.execute(
        f"UPDATE shop_orders SET {', '.join(sets)} WHERE session_id=?", (*params, session_id)
    )
    conn.commit()


def _rows(cur: sqlite3.Cursor) -> list[dict[str, Any]]:
    return [dict(zip(_COLS, r, strict=True)) for r in cur.fetchall()]


def get_order(conn: sqlite3.Connection, session_id: str) -> dict[str, Any] | None:
    ensure_schema(conn)
    rows = _rows(
        conn.execute(
            f"SELECT {', '.join(_COLS)} FROM shop_orders WHERE session_id=?", (session_id,)
        )
    )
    return rows[0] if rows else None


def orders_pending_expiry(conn: sqlite3.Connection, older_than_ts: int) -> list[dict[str, Any]]:
    ensure_schema(conn)
    return _rows(
        conn.execute(
            f"SELECT {', '.join(_COLS)} FROM shop_orders"
            " WHERE status='pending_accept' AND created_ts < ? ORDER BY created_ts",
            (older_than_ts,),
        )
    )


def orders_unsettled(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    ensure_schema(conn)
    return _rows(
        conn.execute(
            f"SELECT {', '.join(_COLS)} FROM shop_orders"
            " WHERE status='accepted' ORDER BY created_ts"
        )
    )
