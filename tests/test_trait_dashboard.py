import os

# Env-guard preamble: set before any lfg_core.config import so module-level
# constants freeze with valid values even when this file is collected before
# webapp/test_smoke.py (see tests/test_event_endpoints.py).
os.environ.setdefault("XUMM_API_KEY", "test")
os.environ.setdefault("XUMM_API_SECRET", "test")
os.environ.setdefault("SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2")
os.environ.setdefault("TOKEN_ISSUER_ADDRESS", "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
os.environ.setdefault("TOKEN_CURRENCY_HEX", "4C46474F00000000000000000000000000000000")
os.environ.setdefault("BUNNY_CDN_ACCESS_KEY", "test")
os.environ.setdefault("BUNNY_CDN_STORAGE_ZONE", "test")
os.environ.setdefault("LAYER_SOURCE", "local")
os.environ.setdefault("BUNNY_PULL_ZONE", "nft.pullzone.example")

import asyncio
import sqlite3
import struct
import zlib

from lfg_core import rarity

# --- helpers ---------------------------------------------------------------


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _seed(db_path, network, rows):
    """rows: list of (body, category, trait, live_count, enabled)."""
    conn = sqlite3.connect(db_path)
    rarity.ensure_schema(conn)
    for body, cat, trait, count, enabled in rows:
        conn.execute(
            """INSERT INTO trait_rarity (network, body, category, trait,
               live_count, floor_weight, enabled) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (network, body, cat, trait, count, 0.005, enabled),
        )
    conn.commit()
    conn.close()


def _png_1x1(path):
    """Write a minimal valid 1x1 PNG (no external deps)."""

    def chunk(tag, data):
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
    raw = b"\x00\xff\x00\x00\xff"  # one filtered RGBA pixel
    idat = zlib.compress(raw)
    with open(path, "wb") as f:
        f.write(sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b""))


# --- Task 1: fetch_rows ----------------------------------------------------


def test_fetch_rows_computes_share_weight_and_status(tmp_path):
    from scripts import trait_dashboard as td

    db = str(tmp_path / "m.db")
    _seed(
        db,
        "mainnet",
        [
            ("ape", "Eyes", "Laser", 3, 1),
            ("ape", "Eyes", "Star", 1, 1),
            ("ape", "Eyes", "Off", 0, 0),  # disabled, zero-count
        ],
    )
    out = td.fetch_rows("mainnet", db_path=db)
    rows = {r["trait"]: r for r in out["rows"]}
    assert rows["Laser"]["live_count"] == 3
    assert round(rows["Laser"]["share"], 1) == 75.0  # 3 / 4
    assert rows["Laser"]["weight"] > rows["Star"]["weight"]  # proportional
    assert rows["Off"]["enabled"] is False
    assert out["bodies"] == ["ape"] and out["categories"] == ["Eyes"]


def test_fetch_rows_filters(tmp_path):
    from scripts import trait_dashboard as td

    db = str(tmp_path / "m.db")
    _seed(
        db,
        "mainnet",
        [
            ("ape", "Eyes", "Laser", 3, 1),
            ("ape", "Eyes", "Star", 1, 1),
            ("male", "Hat", "Crown", 2, 1),
            ("ape", "Eyes", "Off", 0, 0),
        ],
    )
    assert {r["trait"] for r in td.fetch_rows("mainnet", db_path=db, q="la")["rows"]} == {"Laser"}
    assert {r["trait"] for r in td.fetch_rows("mainnet", db_path=db, body="male")["rows"]} == {
        "Crown"
    }
    assert {r["trait"] for r in td.fetch_rows("mainnet", db_path=db, status="disabled")["rows"]} == {
        "Off"
    }
    assert "Off" in {r["trait"] for r in td.fetch_rows("mainnet", db_path=db, status="problems")["rows"]}
