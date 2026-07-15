"""tests/test_shop_pricing.py — Trait Shop pricing formula, overrides, catalog.

Env-guard preamble (copy from test_shop_config.py): importing lfg_core.config
freezes its constants at import time; set the same defaults test_smoke.py uses
so collection order can't strand them.
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

from lfg_core import config, rarity, shop


def _conn():
    conn = sqlite3.connect(":memory:")
    rarity.ensure_schema(conn)
    shop.ensure_schema(conn)
    return conn


def _seed(conn, body, cat, trait, live, enabled=1, shop_count=0):
    conn.execute(
        "INSERT INTO trait_rarity (network, body, category, trait, live_count,"
        " floor_weight, enabled, shop_count) VALUES ('testnet',?,?,?,?,0.005,?,?)",
        (body, cat, trait, live, enabled, shop_count),
    )


def test_derived_price_formula():
    # share = (10+0+1)/(100+20) = 11/120; price = round(1.0/share) = 11
    assert shop.derived_price(10, 100, 0, 20) == 11


def test_derived_price_clamps():
    assert shop.derived_price(0, 10_000, 0, 2) == config.SHOP_MAX_BRIX  # ultra-rare capped
    assert shop.derived_price(99, 100, 0, 1) == config.SHOP_MIN_BRIX  # ultra-common floored


def test_quote_aggregates_bodies_and_counts_shop():
    conn = _conn()
    _seed(conn, "male", "Head", "Wizard Hat", 4)
    _seed(conn, "female", "Head", "Wizard Hat", 6, shop_count=2)
    _seed(conn, "male", "Head", "Cap", 90)
    # live_total=10 (+shop 2), category_total=100, population=3 rows
    # share=(10+2+1)/(100+3)=13/103 → price=round(1.0*103/13)=8
    assert shop.quote(conn, "testnet", "Head", "Wizard Hat") == 8


def test_quote_none_when_disabled_or_unknown():
    conn = _conn()
    _seed(conn, "male", "Head", "Halo", 1, enabled=0)
    assert shop.quote(conn, "testnet", "Head", "Halo") is None
    assert shop.quote(conn, "testnet", "Head", "Nope") is None


def test_override_precedence_and_exclusion():
    conn = _conn()
    _seed(conn, "male", "Head", "Wizard Hat", 4)
    shop.set_override(conn, "testnet", "Head", "Wizard Hat", price_override=777)
    assert shop.quote(conn, "testnet", "Head", "Wizard Hat") == 777
    shop.set_override(conn, "testnet", "Head", "Wizard Hat", excluded=True)
    assert shop.quote(conn, "testnet", "Head", "Wizard Hat") is None


def test_catalog_lists_enabled_non_excluded():
    conn = _conn()
    _seed(conn, "male", "Head", "Wizard Hat", 4)
    _seed(conn, "male", "Head", "Halo", 1, enabled=0)
    rows = shop.catalog(conn, "testnet")
    assert [r["value"] for r in rows] == ["Wizard Hat"]
    assert rows[0]["slot"] == "Head" and rows[0]["price_brix"] > 0
