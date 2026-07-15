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
    shop_store.update_order(conn, "s1", status="pending_accept", nft_id="ABC",
                            offer_index="OFF1", now_ts=1001)
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
    assert [o["session_id"] for o in shop_store.orders_pending_expiry(conn, older_than_ts=1000)] == ["old"]
    assert [o["session_id"] for o in shop_store.orders_unsettled(conn)] == ["done"]
