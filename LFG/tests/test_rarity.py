# Tests for the variable rarity engine (lfg_core/rarity.py).
import os
import sys
import sqlite3
import random
from datetime import datetime, timezone, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# Dummy env so lfg_core.config import doesn't fail (same trick as webapp/test_smoke.py)
os.environ.setdefault("DISCORD_BOT_TOKEN", "x")
os.environ.setdefault("XUMM_API_KEY", "x")
os.environ.setdefault("XUMM_API_SECRET", "x")
os.environ.setdefault("BUNNY_CDN_ACCESS_KEY", "x")
os.environ.setdefault("BUNNY_CDN_STORAGE_ZONE", "x")
os.environ.setdefault("SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2")  # dummy testnet seed
os.environ.setdefault("TOKEN_ISSUER_ADDRESS", "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
os.environ.setdefault("TOKEN_CURRENCY_HEX", "4C46474F00000000000000000000000000000000")
os.environ.setdefault("XRPL_NETWORK", "testnet")

from lfg_core import rarity  # noqa: E402

NOW = datetime(2026, 6, 12, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    # Minimal LFG + burned_nfts shaped like production
    c.execute("""CREATE TABLE LFG (
        nft_number INTEGER PRIMARY KEY, nft_id TEXT, discord_id TEXT,
        owner_address TEXT, metadata_url TEXT, image_url TEXT,
        Background TEXT, Back TEXT, Body TEXT, Clothing TEXT, Eyes TEXT,
        Eyebrows TEXT, Mouth TEXT, Hat TEXT, Accessory TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    c.execute("""CREATE TABLE burned_nfts (
        nft_number INTEGER PRIMARY KEY, nft_id TEXT, discord_id TEXT,
        burned_by TEXT, reason TEXT,
        burned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        original_mint_time TIMESTAMP)""")
    rarity.ensure_schema(c)
    yield c
    c.close()


def test_ensure_schema_creates_trait_rarity(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(trait_rarity)")}
    assert {"network", "body", "category", "trait", "live_count",
            "floor_weight", "boost_initial", "boost_step_hours",
            "boost_started_at", "enabled", "first_seen_at"} <= cols


def test_ensure_schema_adds_lfg_columns(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(LFG)")}
    assert "network" in cols and "body_type" in cols


def test_ensure_schema_idempotent(conn):
    rarity.ensure_schema(conn)  # second call must not raise
