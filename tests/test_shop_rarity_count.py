"""tests/test_shop_rarity_count.py — trait_rarity.shop_count column + increment helper.

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

from lfg_core import rarity


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    rarity.ensure_schema(conn)
    return conn


def test_shop_count_column_migrates() -> None:
    conn = _conn()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(trait_rarity)")}
    assert "shop_count" in cols


def test_increment_and_recalc_preserves() -> None:
    conn = _conn()
    conn.execute(
        "INSERT INTO trait_rarity (network, body, category, trait, live_count, floor_weight)"
        " VALUES ('testnet', 'male', 'Head', 'Wizard Hat', 3, 0.005)"
    )
    rarity.increment_shop_count(conn, "testnet", "Head", "Wizard Hat")
    (n,) = conn.execute("SELECT shop_count FROM trait_rarity WHERE trait='Wizard Hat'").fetchone()
    assert n == 1
    rarity.recalculate_rarity(conn, "testnet")  # zeroes+recounts live_count only
    (n,) = conn.execute("SELECT shop_count FROM trait_rarity WHERE trait='Wizard Hat'").fetchone()
    assert n == 1


def test_increment_inserts_sentinel_row_when_absent() -> None:
    conn = _conn()
    rarity.increment_shop_count(conn, "testnet", "Eyes", "Laser")
    row = conn.execute(
        "SELECT body, shop_count FROM trait_rarity WHERE category='Eyes' AND trait='Laser'"
    ).fetchone()
    assert row == (rarity.BODY_SENTINEL, 1)
