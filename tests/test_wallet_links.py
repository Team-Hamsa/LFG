import os
import sqlite3

# Env guard: freeze lfg_core.config constants before import, regardless of
# collection order (see tests/test_server_identity_wiring.py).
os.environ.setdefault("XUMM_API_KEY", "test")
os.environ.setdefault("XUMM_API_SECRET", "test")
os.environ.setdefault("SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2")
os.environ.setdefault("TOKEN_ISSUER_ADDRESS", "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
os.environ.setdefault("TOKEN_CURRENCY_HEX", "4C46474F00000000000000000000000000000000")
os.environ.setdefault("BUNNY_CDN_ACCESS_KEY", "test")
os.environ.setdefault("BUNNY_CDN_STORAGE_ZONE", "test")
os.environ.setdefault("LAYER_SOURCE", "local")
os.environ.setdefault("BUNNY_PULL_ZONE", "nft.pullzone.example")

import lfg_core.user_db as user_db  # noqa: E402
import lfg_service.identity as identity  # noqa: E402


def _setup(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    monkeypatch.setattr(user_db, "DATABASE", str(db))
    monkeypatch.setattr(identity, "DATABASE", str(db))
    identity.ensure_identities_table()
    return str(db)


def test_link_appends_history_without_clobbering(tmp_path, monkeypatch):
    db = _setup(tmp_path, monkeypatch)
    identity.link("discord", "u1", "alice", "rWALLET_A")
    identity.link("discord", "u1", "alice", "rWALLET_B")  # switch wallets
    conn = sqlite3.connect(db)
    # active pointer updated
    active = conn.execute(
        "SELECT wallet FROM identities WHERE platform='discord' AND platform_user_id='u1'"
    ).fetchone()[0]
    assert active == "rWALLET_B"
    # both wallets retained in history
    hist = {
        r[0]
        for r in conn.execute(
            "SELECT wallet FROM wallet_links WHERE platform='discord' AND platform_user_id='u1'"
        )
    }
    assert hist == {"rWALLET_A", "rWALLET_B"}


def test_relinking_seen_wallet_is_noop(tmp_path, monkeypatch):
    db = _setup(tmp_path, monkeypatch)
    identity.link("discord", "u1", "alice", "rWALLET_A")
    identity.link("discord", "u1", "alice", "rWALLET_A")
    conn = sqlite3.connect(db)
    n = conn.execute(
        "SELECT COUNT(*) FROM wallet_links WHERE platform='discord' AND platform_user_id='u1'"
    ).fetchone()[0]
    assert n == 1
