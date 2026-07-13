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

import lfg_core.free_mint as free_mint  # noqa: E402
import lfg_core.user_db as user_db  # noqa: E402
import scripts.free_mint_admin as admin  # noqa: E402


def _setup(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    # DATABASE is bound by value at import in each module; patch every binding.
    monkeypatch.setattr(user_db, "DATABASE", str(db))
    monkeypatch.setattr(free_mint, "DATABASE", str(db))
    monkeypatch.setattr(admin, "DATABASE", str(db))
    free_mint.ensure_tables()


def test_grant_then_revoke(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    admin.grant("discord", "u1", "testnet", "rA")
    assert free_mint.is_eligible("discord", "u1", "testnet") is False
    admin.revoke("discord", "u1", "testnet")
    # revoke clears any claim (reserved or claimed) so the identity can re-claim
    rows = admin.list_claims("testnet")
    assert all(r["platform_user_id"] != "u1" for r in rows)


def test_grant_is_claimed_status(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    admin.grant("discord", "u2", "testnet", "rB")
    rows = admin.list_claims("testnet")
    assert any(r["platform_user_id"] == "u2" and r["status"] == "claimed" for r in rows)
