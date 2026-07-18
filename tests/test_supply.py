# tests/test_supply.py
# Env-guard preamble: importing lfg_core.config freezes its constants (e.g.
# IMG_PROXY_ALLOWED_BASES, LAYER_SOURCE) at import time; set the same defaults
# test_smoke.py uses so collection order can't strand them. (Copy the block
# verbatim from tests/test_server_identity_wiring.py — same keys/values.)
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

import sqlite3  # noqa: E402

import pytest  # noqa: E402

from lfg_core import config, supply  # noqa: E402


def _seed(path, n_live, n_burned=0):
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE onchain_nfts (nft_id TEXT PRIMARY KEY, nft_number INTEGER, "
        "is_burned INTEGER DEFAULT 0)"
    )
    for i in range(n_live):
        conn.execute("INSERT INTO onchain_nfts VALUES (?,?,0)", (f"live{i}", i))
    for i in range(n_burned):
        conn.execute("INSERT INTO onchain_nfts VALUES (?,?,1)", (f"burn{i}", 10000 + i))
    conn.commit()
    conn.close()


def test_current_supply_counts_only_live(tmp_path, monkeypatch):
    db = tmp_path / "onchain_testnet.db"
    _seed(str(db), n_live=42, n_burned=7)
    monkeypatch.setattr(supply.nft_index, "index_db_path", lambda net: str(db))
    assert supply.current_supply("testnet") == 42


def test_remaining_headroom(tmp_path, monkeypatch):
    db = tmp_path / "onchain_testnet.db"
    _seed(str(db), n_live=9995)
    monkeypatch.setattr(supply.nft_index, "index_db_path", lambda net: str(db))
    monkeypatch.setattr(config, "MAX_COLLECTION_SIZE", 10000)
    assert supply.remaining_headroom("testnet") == 5


def test_headroom_never_negative(tmp_path, monkeypatch):
    db = tmp_path / "onchain_testnet.db"
    _seed(str(db), n_live=10005)
    monkeypatch.setattr(supply.nft_index, "index_db_path", lambda net: str(db))
    monkeypatch.setattr(config, "MAX_COLLECTION_SIZE", 10000)
    assert supply.remaining_headroom("testnet") == 0


def test_current_supply_missing_table_fails_closed(tmp_path, monkeypatch):
    # #226 review: a missing onchain_nfts table (mispathed/unbuilt index on a
    # DEPLOYED collection) must not read as supply 0 — that would admit up to
    # MAX_COLLECTION_SIZE extra mints. Propagate; headroom.try_reserve turns
    # any raise into a 0 grant (fail closed).
    import pytest

    db = tmp_path / "onchain_testnet.db"
    conn = sqlite3.connect(str(db))
    conn.close()  # empty db file, no tables at all
    monkeypatch.setattr(supply.nft_index, "index_db_path", lambda net: str(db))
    with pytest.raises(sqlite3.OperationalError):
        supply.current_supply("testnet")


def test_current_supply_locked_index_raises(tmp_path, monkeypatch):
    """Review on #226: only the missing-table bootstrap case may read as 0.
    Any OTHER OperationalError — 'database is locked' from the listener /
    backfill writers — must PROPAGATE so headroom.try_reserve fails closed
    (grant 0) instead of seeing supply 0 and over-granting past the cap."""
    db = tmp_path / "onchain_testnet.db"
    _seed(str(db), n_live=5)
    monkeypatch.setattr(supply.nft_index, "index_db_path", lambda net: str(db))

    class _LockedConn:
        def execute(self, *a, **kw):
            raise sqlite3.OperationalError("database is locked")

        def close(self):
            pass

    class _FakeSqlite:
        OperationalError = sqlite3.OperationalError

        @staticmethod
        def connect(path):
            return _LockedConn()

    # Scoped to the supply module (not the global stdlib sqlite3).
    monkeypatch.setattr(supply, "sqlite3", _FakeSqlite)
    with pytest.raises(sqlite3.OperationalError):
        supply.current_supply("testnet")
