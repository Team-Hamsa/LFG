"""tests/test_shop_store.py — shop_orders store lifecycle (#217).

Env-guard preamble (copy from test_shop_config.py): importing lfg_core at module
top freezes its constants; set the same defaults test_smoke.py uses so collection
order can't strand them.
"""

import os

os.environ.setdefault("XUMM_API_KEY", "test")
os.environ.setdefault("XUMM_API_SECRET", "test")
os.environ.setdefault("SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2")
os.environ.setdefault("TOKEN_ISSUER_ADDRESS", "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
os.environ.setdefault("TOKEN_CURRENCY_HEX", "4C46474F00000000000000000000000000000000")
os.environ.setdefault("BUNNY_CDN_ACCESS_KEY", "test")
os.environ.setdefault("BUNNY_CDN_STORAGE_ZONE", "test")
os.environ.setdefault("LAYER_SOURCE", "local")
os.environ.setdefault("BUNNY_PULL_ZONE", "nft.pullzone.example")

import sqlite3

from lfg_core import shop_store


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    shop_store.ensure_schema(conn)
    return conn


def test_order_lifecycle() -> None:
    conn = _conn()
    shop_store.create_order(conn, "s1", "rBuyer", "Head", "Wizard Hat", 42, now_ts=1000)
    o = shop_store.get_order(conn, "s1")
    assert o is not None
    assert o["status"] == "pending_mint" and o["price_brix"] == 42
    shop_store.update_order(
        conn, "s1", status="pending_accept", nft_id="ABC", offer_index="OFF1", now_ts=1001
    )
    o = shop_store.get_order(conn, "s1")
    assert o is not None
    assert (o["status"], o["nft_id"], o["offer_index"]) == ("pending_accept", "ABC", "OFF1")


def test_expiry_and_unsettled_queries() -> None:
    conn = _conn()
    shop_store.create_order(conn, "old", "rA", "Eyes", "Laser", 10, now_ts=100)
    shop_store.update_order(conn, "old", status="pending_accept", now_ts=100)
    shop_store.create_order(conn, "new", "rB", "Eyes", "Laser", 10, now_ts=5000)
    shop_store.update_order(conn, "new", status="pending_accept", now_ts=5000)
    shop_store.create_order(conn, "done", "rC", "Eyes", "Laser", 10, now_ts=100)
    shop_store.update_order(conn, "done", status="accepted", now_ts=200)
    assert [
        o["session_id"] for o in shop_store.orders_pending_expiry(conn, older_than_ts=1000)
    ] == ["old"]
    assert [o["session_id"] for o in shop_store.orders_unsettled(conn)] == ["done"]


# ---------------------------------------------------------------------------
# #238 XRP fallback: pay_with / price_xrp / buyback_done columns
# ---------------------------------------------------------------------------

_OLD_SCHEMA = """CREATE TABLE shop_orders (
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


def test_migration_adds_columns_to_old_schema_db() -> None:
    """A DB created with the pre-#238 schema self-migrates on ensure_schema:
    existing rows read back with pay_with NULL (treated as BRIX by callers),
    price_xrp NULL, buyback_done 0."""
    conn = sqlite3.connect(":memory:")
    conn.execute(_OLD_SCHEMA)
    conn.execute(
        "INSERT INTO shop_orders (session_id, buyer, slot, value, price_brix,"
        " status, created_ts, updated_ts) VALUES ('legacy','rA','Head','Hat',10,"
        "'settled',100,100)"
    )
    conn.commit()

    shop_store.ensure_schema(conn)
    shop_store.ensure_schema(conn)  # idempotent — second call must not raise

    o = shop_store.get_order(conn, "legacy")
    assert o is not None
    assert o["pay_with"] is None
    assert o["price_xrp"] is None
    assert o["buyback_done"] == 0


def test_create_order_defaults_and_xrp_round_trip() -> None:
    conn = _conn()
    shop_store.create_order(conn, "b1", "rA", "Head", "Hat", 10, now_ts=100)
    o = shop_store.get_order(conn, "b1")
    assert o is not None
    assert (o["pay_with"], o["price_xrp"], o["buyback_done"]) == ("BRIX", None, 0)

    shop_store.create_order(
        conn, "x1", "rB", "Head", "Hat", 10, now_ts=100, pay_with="XRP", price_xrp="0.105000"
    )
    o = shop_store.get_order(conn, "x1")
    assert o is not None
    assert (o["pay_with"], o["price_xrp"], o["buyback_done"]) == ("XRP", "0.105000", 0)


def test_update_order_new_fields() -> None:
    conn = _conn()
    shop_store.create_order(conn, "u1", "rA", "Head", "Hat", 10, now_ts=100)
    shop_store.update_order(
        conn, "u1", now_ts=200, pay_with="XRP", price_xrp="1.500000", buyback_done=1
    )
    o = shop_store.get_order(conn, "u1")
    assert o is not None
    assert (o["pay_with"], o["price_xrp"], o["buyback_done"]) == ("XRP", "1.500000", 1)


def test_query_helpers_return_new_fields() -> None:
    conn = _conn()
    shop_store.create_order(
        conn, "q1", "rA", "Eyes", "Laser", 10, now_ts=100, pay_with="XRP", price_xrp="2.000000"
    )
    shop_store.update_order(conn, "q1", status="pending_accept", now_ts=100)
    (row,) = shop_store.orders_pending_expiry(conn, older_than_ts=1000)
    assert row["pay_with"] == "XRP" and row["price_xrp"] == "2.000000"
    shop_store.update_order(conn, "q1", status="accepted", now_ts=200)
    (row,) = shop_store.orders_unsettled(conn)
    assert row["pay_with"] == "XRP" and row["buyback_done"] == 0
