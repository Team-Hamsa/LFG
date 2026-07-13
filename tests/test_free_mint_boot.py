import os
import sqlite3

os.environ.setdefault("XUMM_API_KEY", "test")
os.environ.setdefault("XUMM_API_SECRET", "test")
os.environ.setdefault("SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2")
os.environ.setdefault("TOKEN_ISSUER_ADDRESS", "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
os.environ.setdefault("TOKEN_CURRENCY_HEX", "4C46474F00000000000000000000000000000000")
os.environ.setdefault("BUNNY_CDN_ACCESS_KEY", "test")
os.environ.setdefault("BUNNY_CDN_STORAGE_ZONE", "test")
os.environ.setdefault("LAYER_SOURCE", "local")
os.environ.setdefault("BUNNY_PULL_ZONE", "nft.pullzone.example")

import lfg_core.free_mint as free_mint  # noqa: E402
import lfg_core.user_db as user_db  # noqa: E402


def test_ensure_tables_creates_claims(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    monkeypatch.setattr(user_db, "DATABASE", str(db))
    monkeypatch.setattr(free_mint, "DATABASE", str(db))
    free_mint.ensure_tables()
    cols = {
        r[1]
        for r in sqlite3.connect(str(db)).execute("PRAGMA table_info(free_mint_claims)")
    }
    assert {"platform", "platform_user_id", "network", "wallet", "nft_number", "status"} <= cols
