# Tests for the network-aware app DB path (lfg_nfts.db must never be shared
# between testnet and mainnet — a testnet mint once pushed the mainnet
# edition counter from 3536 to 3572).
import os
import sqlite3
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DISCORD_BOT_TOKEN", "x")
os.environ.setdefault("XUMM_API_KEY", "x")
os.environ.setdefault("XUMM_API_SECRET", "x")
os.environ.setdefault("BUNNY_CDN_ACCESS_KEY", "x")
os.environ.setdefault("BUNNY_CDN_STORAGE_ZONE", "x")
os.environ.setdefault("SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2")
os.environ.setdefault("TOKEN_ISSUER_ADDRESS", "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
os.environ.setdefault("TOKEN_CURRENCY_HEX", "4C46474F00000000000000000000000000000000")
os.environ.setdefault("XRPL_NETWORK", "testnet")
os.environ.setdefault("BUNNY_PULL_ZONE", "nft.pullzone.example")

from lfg_core import db_helpers
from lfg_core import user_db
from lfg_core import config


def test_default_db_path_is_network_suffixed():
    # The suite runs with XRPL_NETWORK=testnet; the default app DB must not
    # be the legacy mainnet file.
    assert config.app_db_path("mainnet") == "lfg_nfts.db"
    assert config.app_db_path("testnet") == "lfg_nfts_testnet.db"
    # config.DB_PATH freezes at first import (whole-suite order varies which
    # network that is) — assert consistency, not a specific network.
    assert config.DB_PATH == config.app_db_path(config.XRPL_NETWORK)


def test_db_path_env_override(monkeypatch):
    monkeypatch.setenv("DB_PATH", "/tmp/x.db")
    assert config.app_db_path("testnet") == "/tmp/x.db"


def test_get_next_nft_number_uses_config_db_path(tmp_path, monkeypatch):
    db = tmp_path / "app.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE LFG (nft_number INTEGER PRIMARY KEY, nft_id TEXT)")
    conn.execute("INSERT INTO LFG (nft_number) VALUES (41)")
    conn.commit()
    conn.close()
    monkeypatch.setattr(config, "DB_PATH", str(db))
    assert db_helpers.get_next_nft_number() == 42


def test_get_nft_data_uses_config_db_path(tmp_path, monkeypatch):
    db = tmp_path / "app.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE LFG (nft_number INTEGER PRIMARY KEY, nft_id TEXT, created_at TEXT)")
    conn.execute("INSERT INTO LFG (nft_number, nft_id) VALUES (7, 'abc')")
    conn.commit()
    conn.close()
    monkeypatch.setattr(config, "DB_PATH", str(db))
    row = db_helpers.get_nft_data(7)
    assert row and row["nft_id"] == "abc"


def test_init_db_runs_without_runtime_secrets(tmp_path):
    # init_db.py is a standalone bootstrap: it must work with ONLY
    # DB_PATH/XRPL_NETWORK set — no XUMM/Bunny/Discord secrets (Greptile P1
    # on #167). Run it in a scrubbed environment from a secretless cwd so a
    # future import of lfg_core.config (or a stray .env pickup) fails loudly.
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    db = tmp_path / "bootstrap.db"
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": repo_root,
        "DB_PATH": str(db),
        "XRPL_NETWORK": "testnet",
    }
    result = subprocess.run(
        [sys.executable, os.path.join(repo_root, "init_db.py")],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    conn = sqlite3.connect(db)
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert {"LFG", "burned_nfts"} <= tables


def test_user_db_uses_config_db_path(tmp_path, monkeypatch):
    db = tmp_path / "users.db"
    monkeypatch.setattr(user_db, "DATABASE", str(db))
    user_db.create_users_table()
    assert user_db.register_user("1", "alice", "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
    user = user_db.get_user("1")
    assert user and user["address"] == "rrrrrrrrrrrrrrrrrrrrrhoLvTp"
