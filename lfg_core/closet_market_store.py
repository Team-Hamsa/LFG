"""Closet Market order book (#443): off-ledger asks, escrow-backed bids, fills.

Lives in the per-network onchain_<net>.db beside closet_assets, so a fill's
asset move and both order closes are ONE sqlite transaction. The DB is
authoritative for orders and fills; the Closet NFToken's `lfg_closet.orders`
block is a display mirror written by the next sync_closet.

Must not import economy_store / closet_token (economy_store imports this
module to ensure the schema) — closet_assets / closet_tokens are touched with
plain SQL here.
"""

from __future__ import annotations

import hashlib
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import ROUND_DOWN, Decimal
from typing import Any

_ACTIVE = "active"  # == closet_token.ACTIVE (asserted in tests)

SIDE_ASK = "ask"
SIDE_BID = "bid"

PENDING_ESCROW = "pending_escrow"
OPEN = "open"
MATCHED = "matched"
CANCELLING = "cancelling"
FILLED = "filled"
CANCELLED = "cancelled"
EXPIRED = "expired"
ORDER_STATES = frozenset({PENDING_ESCROW, OPEN, MATCHED, CANCELLING, FILLED, CANCELLED, EXPIRED})

FUNDS_PENDING = "funds_pending"
FUNDED = "funded"
ASSET_MOVED = "asset_moved"
PAID = "paid"
MIRRORED = "mirrored"
REFUND_PENDING = "refund_pending"
REFUNDED = "refunded"
INDETERMINATE = "indeterminate"
FAILED = "failed"
FILL_STATES = frozenset(
    {
        FUNDS_PENDING,
        FUNDED,
        ASSET_MOVED,
        PAID,
        MIRRORED,
        REFUND_PENDING,
        REFUNDED,
        INDETERMINATE,
        FAILED,
    }
)
TERMINAL_FILL_STATES = frozenset({MIRRORED, REFUNDED, FAILED})

FUNDS_PAYMENT = "payment"
FUNDS_ESCROW = "escrow"

# Stored when a tx is confirmed on-ledger but its hash could not be read.
# Never None: a None hash means "not done yet" to the state machine.
HASH_UNKNOWN = "confirmed-hash-unavailable"

_TAG_PHASES = frozenset(
    {"finish", "forward", "overshoot", "refund", "cancel", "cancel_finish", "cancel_refund"}
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS closet_orders (
    id               TEXT PRIMARY KEY,
    side             TEXT NOT NULL CHECK (side IN ('ask', 'bid')),
    owner            TEXT NOT NULL,
    slot             TEXT NOT NULL,
    value            TEXT NOT NULL,
    price_brix       TEXT NOT NULL,
    state            TEXT NOT NULL,
    created_ts       INTEGER NOT NULL,
    updated_ts       INTEGER NOT NULL,
    platform         TEXT,
    payload_uuid     TEXT,
    xumm_url         TEXT,
    qr_url           TEXT,
    push             TEXT,
    signed_txid      TEXT,
    escrow_tx_hash   TEXT,
    escrow_owner_seq INTEGER,
    condition        TEXT,
    fulfillment_enc  TEXT,
    cancel_after     INTEGER,
    cancel_reason    TEXT,
    cancel_finish_hash TEXT,
    cancel_refund_hash TEXT,
    pending_phase    TEXT,
    pending_lls      INTEGER,
    error            TEXT
);
CREATE INDEX IF NOT EXISTS idx_closet_orders_book ON closet_orders (state, side, slot, value);
CREATE INDEX IF NOT EXISTS idx_closet_orders_owner ON closet_orders (owner, state);
CREATE UNIQUE INDEX IF NOT EXISTS idx_closet_one_live_bid ON closet_orders (owner, slot, value)
    WHERE side = 'bid' AND state IN ('pending_escrow', 'open', 'matched', 'cancelling');
CREATE UNIQUE INDEX IF NOT EXISTS idx_closet_escrow_hash ON closet_orders (escrow_tx_hash)
    WHERE escrow_tx_hash IS NOT NULL;

CREATE TABLE IF NOT EXISTS closet_fills (
    id                 TEXT PRIMARY KEY,
    ask_order_id       TEXT,
    bid_order_id       TEXT,
    funds_source       TEXT NOT NULL CHECK (funds_source IN ('payment', 'escrow')),
    seller             TEXT NOT NULL,
    buyer              TEXT NOT NULL,
    slot               TEXT NOT NULL,
    value              TEXT NOT NULL,
    price_brix         TEXT NOT NULL,
    fee_brix           TEXT NOT NULL,
    overshoot_brix     TEXT NOT NULL DEFAULT '0',
    state              TEXT NOT NULL,
    platform           TEXT,
    payload_uuid       TEXT,
    xumm_url           TEXT,
    qr_url             TEXT,
    push               TEXT,
    signed_txid        TEXT,
    payment_tx_hash    TEXT,
    escrow_finish_hash TEXT,
    forward_tx_hash    TEXT,
    overshoot_tx_hash  TEXT,
    refund_tx_hash     TEXT,
    refund_to          TEXT,
    refund_brix        TEXT,
    pending_phase      TEXT,
    pending_lls        INTEGER,
    seller_mirrored    INTEGER NOT NULL DEFAULT 0,
    buyer_mirrored     INTEGER NOT NULL DEFAULT 0,
    attempts           INTEGER NOT NULL DEFAULT 0,
    error              TEXT,
    created_ts         INTEGER NOT NULL,
    updated_ts         INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_closet_fills_state ON closet_fills (state);
CREATE UNIQUE INDEX IF NOT EXISTS idx_closet_fill_payment ON closet_fills (payment_tx_hash)
    WHERE payment_tx_hash IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_closet_one_fill_per_bid ON closet_fills (bid_order_id)
    WHERE bid_order_id IS NOT NULL AND state NOT IN ('refunded', 'failed');
"""

_ORDER_MUTABLE = frozenset(
    {
        "state",
        "payload_uuid",
        "xumm_url",
        "qr_url",
        "push",
        "signed_txid",
        "escrow_tx_hash",
        "escrow_owner_seq",
        "cancel_reason",
        "cancel_finish_hash",
        "cancel_refund_hash",
        "pending_phase",
        "pending_lls",
        "error",
    }
)
_FILL_MUTABLE = frozenset(
    {
        "state",
        "payload_uuid",
        "xumm_url",
        "qr_url",
        "push",
        "signed_txid",
        "payment_tx_hash",
        "escrow_finish_hash",
        "forward_tx_hash",
        "overshoot_tx_hash",
        "refund_tx_hash",
        "refund_to",
        "refund_brix",
        "pending_phase",
        "pending_lls",
        "seller_mirrored",
        "buyer_mirrored",
        "attempts",
        "error",
    }
)


class OrderError(ValueError):
    """A refused order operation. `code` is the API error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    conn.commit()


def _now() -> int:
    return int(time.time())


def new_id() -> str:
    return uuid.uuid4().hex


def fmt_brix(d: Decimal) -> str:
    q = d.quantize(Decimal("0.000001"), rounding=ROUND_DOWN)
    text = format(q, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def fee_for(price_brix: str, bps: int) -> str:
    return fmt_brix(Decimal(price_brix) * bps / Decimal(10000))


def invoice_id(fill_id: str) -> str:
    """Payment.InvoiceID identifying a buyer's payment for one fill."""
    return hashlib.sha256(fill_id.encode()).hexdigest().upper()


def memo_tag(phase: str, ident: str) -> str:
    if phase not in _TAG_PHASES:
        raise ValueError(f"unknown closet market memo phase: {phase!r}")
    return f"lfg:closet_{phase}:{ident}"


@contextmanager
def _immediate(conn: sqlite3.Connection) -> Iterator[None]:
    """One serialized write transaction (BEGIN IMMEDIATE takes the write lock
    up front, so a check-then-write can't interleave with another writer)."""
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()


def _one(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...]) -> dict[str, Any] | None:
    cur = conn.execute(sql, params)
    row = cur.fetchone()
    if row is None:
        return None
    return dict(zip([c[0] for c in cur.description], row, strict=True))


def _many(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    cur = conn.execute(sql, params)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]


def get_order(conn: sqlite3.Connection, order_id: str) -> dict[str, Any] | None:
    return _one(conn, "SELECT * FROM closet_orders WHERE id = ?", (order_id,))


def get_fill(conn: sqlite3.Connection, fill_id: str) -> dict[str, Any] | None:
    return _one(conn, "SELECT * FROM closet_fills WHERE id = ?", (fill_id,))


def _update(
    table: str,
    allowed: frozenset[str],
    states: frozenset[str],
    conn: sqlite3.Connection,
    ident: str,
    fields: dict[str, Any],
) -> None:
    bad = set(fields) - allowed
    if bad:
        raise ValueError(f"{table}: fields not updatable: {sorted(bad)}")
    if "state" in fields and fields["state"] not in states:
        raise ValueError(f"{table}: unknown state {fields['state']!r}")
    if not fields:
        return
    sets = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(
        f"UPDATE {table} SET {sets}, updated_ts = ? WHERE id = ?",  # noqa: S608 - keys allowlisted
        (*fields.values(), _now(), ident),
    )
    conn.commit()


def update_order(conn: sqlite3.Connection, order_id: str, **fields: Any) -> None:
    _update("closet_orders", _ORDER_MUTABLE, ORDER_STATES, conn, order_id, fields)


def update_fill(conn: sqlite3.Connection, fill_id: str, **fields: Any) -> None:
    _update("closet_fills", _FILL_MUTABLE, FILL_STATES, conn, fill_id, fields)


def closet_active(conn: sqlite3.Connection, owner: str) -> bool:
    row = conn.execute("SELECT status FROM closet_tokens WHERE owner = ?", (owner,)).fetchone()
    return row is not None and row[0] == _ACTIVE


def holding_count(conn: sqlite3.Connection, owner: str, slot: str, value: str) -> int:
    row = conn.execute(
        "SELECT count FROM closet_assets WHERE owner = ? AND slot = ? AND value = ?",
        (owner, slot, value),
    ).fetchone()
    return int(row[0]) if row else 0


def encumbrance(conn: sqlite3.Connection, owner: str) -> dict[tuple[str, str], int]:
    """Units of each key promised to the market: open/matched asks, plus
    holder fills of a bid (no ask row) that haven't moved the asset yet."""
    out: dict[tuple[str, str], int] = {}
    for slot, value, n in conn.execute(
        "SELECT slot, value, COUNT(*) FROM closet_orders WHERE owner = ? AND side = 'ask' "
        "AND state IN ('open', 'matched') GROUP BY slot, value",
        (owner,),
    ):
        out[(slot, value)] = int(n)
    for slot, value, n in conn.execute(
        "SELECT slot, value, COUNT(*) FROM closet_fills WHERE seller = ? AND ask_order_id IS NULL "
        "AND (state IN ('funds_pending', 'funded') OR (state = 'indeterminate' AND pending_phase = 'finish')) "
        "GROUP BY slot, value",
        (owner,),
    ):
        out[(slot, value)] = out.get((slot, value), 0) + int(n)
    return out


def available_count(conn: sqlite3.Connection, owner: str, slot: str, value: str) -> int:
    return holding_count(conn, owner, slot, value) - encumbrance(conn, owner).get((slot, value), 0)


def create_ask(
    conn: sqlite3.Connection,
    *,
    owner: str,
    slot: str,
    value: str,
    price_brix: str,
    platform: str | None,
    now: int | None = None,
) -> dict[str, Any]:
    ts = now if now is not None else _now()
    oid = new_id()
    with _immediate(conn):
        if not closet_active(conn, owner):
            raise OrderError("closet_required", "Create and claim your Closet first.")
        if available_count(conn, owner, slot, value) < 1:
            raise OrderError("not_available", f"no unlisted '{value}' ({slot}) in your Closet")
        conn.execute(
            "INSERT INTO closet_orders (id, side, owner, slot, value, price_brix, state, created_ts, "
            "updated_ts, platform) VALUES (?, 'ask', ?, ?, ?, ?, 'open', ?, ?, ?)",
            (oid, owner, slot, value, price_brix, ts, ts, platform),
        )
    order = get_order(conn, oid)
    assert order is not None
    return order


def cancel_ask(conn: sqlite3.Connection, order_id: str, owner: str) -> dict[str, Any]:
    with _immediate(conn):
        order = get_order(conn, order_id)
        if order is None or order["side"] != SIDE_ASK or order["owner"] != owner:
            raise OrderError("not_found", "ask not found")
        if order["state"] != OPEN:
            raise OrderError("not_open", f"this ask is {order['state']}")
        conn.execute(
            "UPDATE closet_orders SET state = 'cancelled', updated_ts = ? WHERE id = ?",
            (_now(), order_id),
        )
    cancelled = get_order(conn, order_id)
    assert cancelled is not None
    return cancelled


def book_summary(
    conn: sqlite3.Connection, slot: str | None = None, value: str | None = None
) -> list[dict[str, Any]]:
    sql = "SELECT side, slot, value, price_brix FROM closet_orders WHERE state = 'open'"
    params: list[Any] = []
    if slot is not None:
        sql += " AND slot = ?"
        params.append(slot)
    if value is not None:
        sql += " AND value = ?"
        params.append(value)
    keys: dict[tuple[str, str], dict[str, Any]] = {}
    for side, s, v, price in conn.execute(sql, params):
        row = keys.setdefault(
            (s, v),
            {
                "slot": s,
                "value": v,
                "best_ask_brix": None,
                "ask_count": 0,
                "best_bid_brix": None,
                "bid_count": 0,
            },
        )
        if side == SIDE_ASK:
            row["ask_count"] += 1
            if row["best_ask_brix"] is None or Decimal(price) < Decimal(row["best_ask_brix"]):
                row["best_ask_brix"] = price
        else:
            row["bid_count"] += 1
            if row["best_bid_brix"] is None or Decimal(price) > Decimal(row["best_bid_brix"]):
                row["best_bid_brix"] = price
    return sorted(keys.values(), key=lambda r: (r["slot"], r["value"]))


def book_levels(conn: sqlite3.Connection, slot: str, value: str) -> dict[str, Any]:
    rows = _many(
        conn,
        "SELECT id, side, owner, price_brix, created_ts FROM closet_orders "
        "WHERE state = 'open' AND slot = ? AND value = ?",
        (slot, value),
    )
    asks = sorted(
        (r for r in rows if r["side"] == SIDE_ASK),
        key=lambda r: (Decimal(r["price_brix"]), r["created_ts"], r["id"]),
    )
    bids = sorted(
        (r for r in rows if r["side"] == SIDE_BID),
        key=lambda r: (-Decimal(r["price_brix"]), r["created_ts"], r["id"]),
    )

    def pub(r: dict[str, Any]) -> dict[str, Any]:
        return {"id": r["id"], "owner": r["owner"], "price_brix": r["price_brix"]}

    return {
        "slot": slot,
        "value": value,
        "asks": [pub(r) for r in asks],
        "bids": [pub(r) for r in bids],
    }
