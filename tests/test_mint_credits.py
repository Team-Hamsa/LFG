# tests/test_mint_credits.py
# Env-guard preamble: importing lfg_core.config freezes its constants (e.g.
# IMG_PROXY_ALLOWED_BASES, LAYER_SOURCE) at import time; set the same defaults
# test_smoke.py uses so collection order can't strand them.
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

from lfg_core import mint_credits  # noqa: E402


def test_add_and_get(tmp_path):
    db = str(tmp_path / "app.db")
    mint_credits.ensure_table(db)
    assert mint_credits.get_credits(db, "u1", "testnet") == 0
    assert mint_credits.add_credit(db, "u1", "testnet", 2) == 2
    assert mint_credits.add_credit(db, "u1", "testnet") == 3
    assert mint_credits.get_credits(db, "u1", "testnet") == 3


def test_credits_are_per_network_and_user(tmp_path):
    db = str(tmp_path / "app.db")
    mint_credits.ensure_table(db)
    mint_credits.add_credit(db, "u1", "testnet", 5)
    assert mint_credits.get_credits(db, "u1", "mainnet") == 0
    assert mint_credits.get_credits(db, "u2", "testnet") == 0
