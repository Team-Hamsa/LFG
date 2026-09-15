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

# Stored as forward_tx_hash when price - fee is zero (nothing to send).
HASH_NOTHING_DUE = "nothing-due"

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


_LIVE_BID_STATES = (PENDING_ESCROW, OPEN, MATCHED, CANCELLING)


def has_live_bid(conn: sqlite3.Connection, owner: str, slot: str, value: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM closet_orders WHERE side = 'bid' AND owner = ? AND slot = ? AND value = ? "
        "AND state IN (?, ?, ?, ?) LIMIT 1",
        (owner, slot, value, *_LIVE_BID_STATES),
    ).fetchone()
    return row is not None


def create_pending_bid(
    conn: sqlite3.Connection,
    *,
    owner: str,
    slot: str,
    value: str,
    price_brix: str,
    platform: str | None,
    condition: str,
    fulfillment_enc: str,
    cancel_after: int,
    payload_uuid: str | None,
    xumm_url: str | None,
    qr_url: str | None,
    push: str | None,
    now: int | None = None,
) -> dict[str, Any]:
    ts = now if now is not None else _now()
    oid = new_id()
    try:
        with _immediate(conn):
            if not closet_active(conn, owner):
                raise OrderError("closet_required", "Create and claim your Closet first.")
            conn.execute(
                "INSERT INTO closet_orders (id, side, owner, slot, value, price_brix, state, created_ts, "
                "updated_ts, platform, payload_uuid, xumm_url, qr_url, push, condition, "
                "fulfillment_enc, cancel_after) VALUES (?, 'bid', ?, ?, ?, ?, 'pending_escrow', ?, ?, ?, "
                "?, ?, ?, ?, ?, ?, ?)",
                (
                    oid,
                    owner,
                    slot,
                    value,
                    price_brix,
                    ts,
                    ts,
                    platform,
                    payload_uuid,
                    xumm_url,
                    qr_url,
                    push,
                    condition,
                    fulfillment_enc,
                    cancel_after,
                ),
            )
    except sqlite3.IntegrityError as exc:
        raise OrderError(
            "bid_exists", "you already have a live bid on this trait — cancel it first"
        ) from exc
    order = get_order(conn, oid)
    assert order is not None
    return order


def mark_bid_open(
    conn: sqlite3.Connection, order_id: str, *, escrow_tx_hash: str, escrow_owner_seq: int
) -> dict[str, Any]:
    with _immediate(conn):
        order = get_order(conn, order_id)
        if order is None or order["side"] != SIDE_BID:
            raise OrderError("not_found", "bid not found")
        if order["state"] != PENDING_ESCROW:
            raise OrderError("not_open", f"this bid is {order['state']}")
        conn.execute(
            "UPDATE closet_orders SET state = 'open', escrow_tx_hash = ?, escrow_owner_seq = ?, "
            "updated_ts = ? WHERE id = ?",
            (escrow_tx_hash, escrow_owner_seq, _now(), order_id),
        )
    opened = get_order(conn, order_id)
    assert opened is not None
    return opened


def begin_cancel_bid(
    conn: sqlite3.Connection, order_id: str, *, owner: str | None, reason: str
) -> dict[str, Any]:
    with _immediate(conn):
        order = get_order(conn, order_id)
        if (
            order is None
            or order["side"] != SIDE_BID
            or (owner is not None and order["owner"] != owner)
        ):
            raise OrderError("not_found", "bid not found")
        if order["state"] != OPEN:
            raise OrderError(
                "not_open", f"only an open bid can be cancelled (this one is {order['state']})"
            )
        conn.execute(
            "UPDATE closet_orders SET state = 'cancelling', cancel_reason = ?, updated_ts = ? WHERE id = ?",
            (reason, _now(), order_id),
        )
    got = get_order(conn, order_id)
    assert got is not None
    return got


def _insert_fill(
    conn: sqlite3.Connection,
    *,
    ask_order_id: str | None,
    bid_order_id: str | None,
    funds_source: str,
    seller: str,
    buyer: str,
    slot: str,
    value: str,
    price_brix: str,
    fee_brix: str,
    overshoot_brix: str,
    platform: str | None,
    ts: int,
) -> str:
    fid = new_id()
    conn.execute(
        "INSERT INTO closet_fills (id, ask_order_id, bid_order_id, funds_source, seller, buyer, slot, "
        "value, price_brix, fee_brix, overshoot_brix, state, platform, created_ts, updated_ts) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'funds_pending', ?, ?, ?)",
        (
            fid,
            ask_order_id,
            bid_order_id,
            funds_source,
            seller,
            buyer,
            slot,
            value,
            price_brix,
            fee_brix,
            overshoot_brix,
            platform,
            ts,
            ts,
        ),
    )
    return fid


def _best_counter(conn: sqlite3.Connection, order: dict[str, Any]) -> dict[str, Any] | None:
    other = SIDE_BID if order["side"] == SIDE_ASK else SIDE_ASK
    rows = _many(
        conn,
        "SELECT * FROM closet_orders WHERE state = 'open' AND side = ? AND slot = ? AND value = ? AND owner != ?",
        (other, order["slot"], order["value"], order["owner"]),
    )
    mine = Decimal(order["price_brix"])
    if order["side"] == SIDE_ASK:
        eligible = [r for r in rows if Decimal(r["price_brix"]) >= mine]
        eligible.sort(key=lambda r: (-Decimal(r["price_brix"]), r["created_ts"], r["id"]))
    else:
        eligible = [r for r in rows if Decimal(r["price_brix"]) <= mine]
        eligible.sort(key=lambda r: (Decimal(r["price_brix"]), r["created_ts"], r["id"]))
    return eligible[0] if eligible else None


def cross_incoming(
    conn: sqlite3.Connection, order_id: str, *, fee_bps: int, now: int | None = None
) -> dict[str, Any] | None:
    """Auto-cross a just-opened order against the best resting counter-order.
    The RESTING order's price is the fill price; a bid above it records the
    difference as overshoot (refunded at settlement). Both orders -> matched
    and an escrow-funded fill is created, in one transaction."""
    ts = now if now is not None else _now()
    fid: str | None = None
    with _immediate(conn):
        incoming = get_order(conn, order_id)
        if incoming is None or incoming["state"] != OPEN:
            return None
        resting = _best_counter(conn, incoming)
        if resting is None:
            return None
        ask, bid = (incoming, resting) if incoming["side"] == SIDE_ASK else (resting, incoming)
        price = resting["price_brix"]
        conn.execute(
            "UPDATE closet_orders SET state = 'matched', updated_ts = ? WHERE id IN (?, ?)",
            (ts, ask["id"], bid["id"]),
        )
        fid = _insert_fill(
            conn,
            ask_order_id=ask["id"],
            bid_order_id=bid["id"],
            funds_source=FUNDS_ESCROW,
            seller=ask["owner"],
            buyer=bid["owner"],
            slot=ask["slot"],
            value=ask["value"],
            price_brix=price,
            fee_brix=fee_for(price, fee_bps),
            overshoot_brix=fmt_brix(Decimal(bid["price_brix"]) - Decimal(price)),
            platform=incoming["platform"],
            ts=ts,
        )
    return get_fill(conn, fid)


def fill_bid(
    conn: sqlite3.Connection,
    bid_id: str,
    filler: str,
    *,
    fee_bps: int,
    platform: str | None,
    now: int | None = None,
) -> dict[str, Any]:
    ts = now if now is not None else _now()
    with _immediate(conn):
        bid = get_order(conn, bid_id)
        if bid is None or bid["side"] != SIDE_BID:
            raise OrderError("not_found", "bid not found")
        if bid["state"] != OPEN:
            raise OrderError("not_open", "this bid is no longer open")
        if bid["owner"] == filler:
            raise OrderError("self_cross", "you can't fill your own bid")
        if not closet_active(conn, filler):
            raise OrderError("closet_required", "Create and claim your Closet first.")
        if available_count(conn, filler, bid["slot"], bid["value"]) < 1:
            raise OrderError(
                "not_available", f"no unlisted '{bid['value']}' ({bid['slot']}) in your Closet"
            )
        conn.execute(
            "UPDATE closet_orders SET state = 'matched', updated_ts = ? WHERE id = ?", (ts, bid_id)
        )
        fid = _insert_fill(
            conn,
            ask_order_id=None,
            bid_order_id=bid_id,
            funds_source=FUNDS_ESCROW,
            seller=filler,
            buyer=bid["owner"],
            slot=bid["slot"],
            value=bid["value"],
            price_brix=bid["price_brix"],
            fee_brix=fee_for(bid["price_brix"], fee_bps),
            overshoot_brix="0",
            platform=platform,
            ts=ts,
        )
    fill = get_fill(conn, fid)
    assert fill is not None
    return fill


def create_take_fill(
    conn: sqlite3.Connection,
    ask_id: str,
    buyer: str,
    *,
    fee_bps: int,
    platform: str | None,
    now: int | None = None,
) -> dict[str, Any]:
    """A buyer starts paying for an ask. The ask is NOT reserved: the first
    validated payment wins (claim_ask_for_payment), later ones are refunded."""
    ts = now if now is not None else _now()
    with _immediate(conn):
        ask = get_order(conn, ask_id)
        if ask is None or ask["side"] != SIDE_ASK:
            raise OrderError("not_found", "ask not found")
        if ask["state"] != OPEN:
            raise OrderError("not_open", "this trait was just sold or unlisted")
        if ask["owner"] == buyer:
            raise OrderError("self_cross", "you can't buy your own listing")
        if not closet_active(conn, buyer):
            raise OrderError("closet_required", "Create and claim your Closet first.")
        fid = _insert_fill(
            conn,
            ask_order_id=ask_id,
            bid_order_id=None,
            funds_source=FUNDS_PAYMENT,
            seller=ask["owner"],
            buyer=buyer,
            slot=ask["slot"],
            value=ask["value"],
            price_brix=ask["price_brix"],
            fee_brix=fee_for(ask["price_brix"], fee_bps),
            overshoot_brix="0",
            platform=platform,
            ts=ts,
        )
    fill = get_fill(conn, fid)
    assert fill is not None
    return fill


def claim_ask_for_payment(conn: sqlite3.Connection, fill_id: str, payment_tx_hash: str) -> str:
    """Funds for a take landed. FUNDED if the ask was still open (ask ->
    matched), else REFUND_PENDING. Raises sqlite3.IntegrityError if this
    payment hash already funds another fill."""
    with _immediate(conn):
        fill = get_fill(conn, fill_id)
        assert fill is not None
        if fill["state"] != FUNDS_PENDING:
            return str(fill["state"])
        ask = get_order(conn, fill["ask_order_id"])
        ts = _now()
        if ask is not None and ask["state"] == OPEN and ask["owner"] == fill["seller"]:
            conn.execute(
                "UPDATE closet_orders SET state = 'matched', updated_ts = ? WHERE id = ?",
                (ts, ask["id"]),
            )
            conn.execute(
                "UPDATE closet_fills SET state = 'funded', payment_tx_hash = ?, updated_ts = ? WHERE id = ?",
                (payment_tx_hash, ts, fill_id),
            )
            return FUNDED
        conn.execute(
            "UPDATE closet_fills SET state = 'refund_pending', payment_tx_hash = ?, refund_to = ?, "
            "refund_brix = ?, error = ?, updated_ts = ? WHERE id = ?",
            (
                payment_tx_hash,
                fill["buyer"],
                fill["price_brix"],
                "the listing was no longer available",
                ts,
                fill_id,
            ),
        )
        return REFUND_PENDING


def fill_in_amount(fill: dict[str, Any]) -> str:
    """BRIX the app wallet receives for this fill (escrow = the whole bid)."""
    if fill["funds_source"] == FUNDS_ESCROW:
        return fmt_brix(Decimal(fill["price_brix"]) + Decimal(fill["overshoot_brix"]))
    return str(fill["price_brix"])


def fill_blocker(conn: sqlite3.Connection, fill_id: str) -> str | None:
    fill = get_fill(conn, fill_id)
    assert fill is not None
    if not closet_active(conn, fill["buyer"]):
        return "the buyer has no active Closet"
    if holding_count(conn, fill["seller"], fill["slot"], fill["value"]) < 1:
        return "the seller no longer holds the trait"
    return None


def abort_unfunded_fill(
    conn: sqlite3.Connection, fill_id: str, reason: str, *, bid_state: str | None = None
) -> None:
    """A pre-funding fill can't proceed: nothing moved on-ledger. Release the
    orders — a short seller's ask is cancelled, a buyer without a Closet has
    their bid sent to cancelling (so its escrow is returned)."""
    with _immediate(conn):
        fill = get_fill(conn, fill_id)
        assert fill is not None
        ts = _now()
        conn.execute(
            "UPDATE closet_fills SET state = 'failed', error = ?, updated_ts = ? WHERE id = ?",
            (reason, ts, fill_id),
        )
        seller_short = holding_count(conn, fill["seller"], fill["slot"], fill["value"]) < 1
        if fill["ask_order_id"]:
            conn.execute(
                "UPDATE closet_orders SET state = ?, updated_ts = ? WHERE id = ? AND state = 'matched'",
                (CANCELLED if seller_short else OPEN, ts, fill["ask_order_id"]),
            )
        if fill["bid_order_id"]:
            if bid_state is not None:
                new_state, cancel_reason = bid_state, None
            elif not closet_active(conn, fill["buyer"]):
                new_state, cancel_reason = CANCELLING, "no_closet"
            else:
                new_state, cancel_reason = OPEN, None
            conn.execute(
                "UPDATE closet_orders SET state = ?, cancel_reason = COALESCE(?, cancel_reason), "
                "updated_ts = ? WHERE id = ? AND state = 'matched'",
                (new_state, cancel_reason, ts, fill["bid_order_id"]),
            )


def move_asset(conn: sqlite3.Connection, fill_id: str, *, now: int | None = None) -> str:
    """Settlement step 2 — ONE transaction: seller -1, buyer +1, both orders
    filled, fill asset_moved. If the buyer lost their Closet or the seller no
    longer holds the unit, nothing moves and the fill goes refund_pending.
    Callers MUST hold owner_lock for seller and buyer (economy flows
    full-overwrite closet_assets from a snapshot read under that lock)."""
    ts = now if now is not None else _now()
    with _immediate(conn):
        fill = get_fill(conn, fill_id)
        assert fill is not None
        if fill["state"] != FUNDED:
            return str(fill["state"])
        buyer_ok = closet_active(conn, fill["buyer"])
        seller_ok = holding_count(conn, fill["seller"], fill["slot"], fill["value"]) >= 1
        if not (buyer_ok and seller_ok):
            reason = (
                "the buyer has no active Closet"
                if not buyer_ok
                else "the seller no longer holds the trait"
            )
            conn.execute(
                "UPDATE closet_fills SET state = 'refund_pending', refund_to = ?, refund_brix = ?, error = ?, "
                "updated_ts = ? WHERE id = ?",
                (fill["buyer"], fill_in_amount(fill), reason, ts, fill_id),
            )
            if fill["ask_order_id"]:
                conn.execute(
                    "UPDATE closet_orders SET state = ?, updated_ts = ? WHERE id = ?",
                    (CANCELLED if not seller_ok else OPEN, ts, fill["ask_order_id"]),
                )
            if fill["bid_order_id"]:  # its escrow is already finished: the refund returns the BRIX
                conn.execute(
                    "UPDATE closet_orders SET state = 'cancelled', cancel_reason = 'undeliverable', "
                    "updated_ts = ? WHERE id = ?",
                    (ts, fill["bid_order_id"]),
                )
            return REFUND_PENDING
        key = (fill["slot"], fill["value"])
        conn.execute(
            "UPDATE closet_assets SET count = count - 1 WHERE owner = ? AND slot = ? AND value = ?",
            (fill["seller"], *key),
        )
        conn.execute(
            "DELETE FROM closet_assets WHERE owner = ? AND slot = ? AND value = ? AND count <= 0",
            (fill["seller"], *key),
        )
        conn.execute(
            "INSERT INTO closet_assets (owner, slot, value, count) VALUES (?, ?, ?, 1) "
            "ON CONFLICT(owner, slot, value) DO UPDATE SET count = count + 1",
            (fill["buyer"], *key),
        )
        for oid in (fill["ask_order_id"], fill["bid_order_id"]):
            if oid:
                conn.execute(
                    "UPDATE closet_orders SET state = 'filled', updated_ts = ? WHERE id = ?",
                    (ts, oid),
                )
        conn.execute(
            "UPDATE closet_fills SET state = 'asset_moved', updated_ts = ? WHERE id = ?",
            (ts, fill_id),
        )
    return ASSET_MOVED


def mark_side_mirrored(conn: sqlite3.Connection, fill_id: str, side: str) -> None:
    if side not in ("seller", "buyer"):
        raise ValueError(side)
    with _immediate(conn):
        conn.execute(
            f"UPDATE closet_fills SET {side}_mirrored = 1, updated_ts = ? WHERE id = ?",  # noqa: S608
            (_now(), fill_id),
        )
        conn.execute(
            "UPDATE closet_fills SET state = 'mirrored' WHERE id = ? AND state = 'paid' "
            "AND seller_mirrored = 1 AND buyer_mirrored = 1",
            (fill_id,),
        )


_UNMIRRORED = "(state IN ('asset_moved', 'paid') OR (state = 'indeterminate' AND pending_phase IN ('forward', 'overshoot')))"


def has_unmirrored_fill(conn: sqlite3.Connection, owner: str) -> bool:
    """True while this owner's DB contents are ahead of their Closet token.
    The listener/backfill must not rebuild closet_assets for an owner with an unmirrored fill.
    Otherwise pre-fill Closet metadata resurrects the moved unit (duplication)."""
    row = conn.execute(
        "SELECT 1 FROM closet_fills WHERE ((seller = ? AND seller_mirrored = 0) OR "
        f"(buyer = ? AND buyer_mirrored = 0)) AND {_UNMIRRORED} LIMIT 1",  # noqa: S608
        (owner, owner),
    ).fetchone()
    return row is not None


def open_orders_for_meta(conn: sqlite3.Connection, owner: str) -> list[dict[str, Any]] | None:
    rows = _many(
        conn,
        "SELECT side, slot, value, price_brix FROM closet_orders WHERE owner = ? "
        "AND state IN ('open', 'matched') ORDER BY created_ts, id",
        (owner,),
    )
    return rows or None


def orders_for_owner(conn: sqlite3.Connection, owner: str, fills_limit: int = 20) -> dict[str, Any]:
    orders = _many(
        conn,
        "SELECT id, side, slot, value, price_brix, state, created_ts, cancel_after, error FROM closet_orders "
        "WHERE owner = ? AND state IN ('pending_escrow', 'open', 'matched', 'cancelling') ORDER BY created_ts DESC",
        (owner,),
    )
    fills = _many(
        conn,
        "SELECT id, seller, buyer, slot, value, price_brix, fee_brix, overshoot_brix, state, error, created_ts "
        "FROM closet_fills WHERE seller = ? OR buyer = ? ORDER BY created_ts DESC LIMIT ?",
        (owner, owner, fills_limit),
    )
    return {"orders": orders, "fills": fills}


def bids_on_holdings(conn: sqlite3.Connection, owner: str) -> list[dict[str, Any]]:
    """#496: open bids someone else placed on a key this wallet can deliver —
    from a loose Closet copy (fill directly) or else an extracted trait token
    (deposit first)."""
    bids = _many(
        conn,
        "SELECT id, owner, slot, value, price_brix, cancel_after, created_ts FROM closet_orders "
        "WHERE side = 'bid' AND state = 'open' AND owner != ?",
        (owner,),
    )
    bids.sort(key=lambda r: (-Decimal(r["price_brix"]), r["created_ts"], r["id"]))
    tokens: dict[tuple[str, str], str] = {}
    for nft_id, slot, value in conn.execute(
        "SELECT nft_id, slot, value FROM trait_tokens WHERE owner = ? ORDER BY nft_id", (owner,)
    ):
        tokens.setdefault((slot, value), nft_id)
    enc = encumbrance(conn, owner)
    out: list[dict[str, Any]] = []
    for b in bids:
        key = (b["slot"], b["value"])
        pub = {k: b[k] for k in ("id", "slot", "value", "price_brix", "cancel_after")}
        if holding_count(conn, owner, *key) - enc.get(key, 0) >= 1:
            out.append({**pub, "source": "closet", "nft_id": None})
        elif key in tokens:
            out.append({**pub, "source": "token", "nft_id": tokens[key]})
    return out


def orders_needing_attention(conn: sqlite3.Connection) -> list[str]:
    return [
        r[0]
        for r in conn.execute(
            "SELECT id FROM closet_orders WHERE side = 'bid' AND state IN ('pending_escrow', 'open', 'cancelling') "
            "ORDER BY updated_ts"
        )
    ]


def fills_needing_attention(conn: sqlite3.Connection) -> list[str]:
    return [
        r[0]
        for r in conn.execute(
            "SELECT id FROM closet_fills WHERE state NOT IN ('mirrored', 'refunded', 'failed') ORDER BY updated_ts"
        )
    ]
