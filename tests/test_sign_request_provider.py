"""sign_requests.provider (agent users spec §1)."""

import sqlite3
import time

from lfg_core.signing import store


def _fresh(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "DATABASE", str(tmp_path / "sign.db"))
    store.ensure_table()


def test_create_records_the_provider(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    row = store.create(
        wallet="", purpose="signin", txjson=None, nonce="n", ttl_seconds=60, provider="agent"
    )
    assert store.get(row["id"])["provider"] == "agent"
    legacy = store.create(wallet="", purpose="signin", txjson=None, nonce="m", ttl_seconds=60)
    assert store.get(legacy["id"])["provider"] is None


def test_the_column_self_migrates_on_an_existing_table(monkeypatch, tmp_path):
    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE sign_requests (id TEXT PRIMARY KEY, wallet TEXT NOT NULL, purpose TEXT"
        " NOT NULL, txjson TEXT, nonce TEXT, state TEXT NOT NULL DEFAULT 'pending', txid TEXT,"
        " result_json TEXT, ip TEXT, created_at REAL NOT NULL, expires_at REAL NOT NULL)"
    )
    conn.execute(
        "INSERT INTO sign_requests (id, wallet, purpose, created_at, expires_at)"
        " VALUES ('wc-old', '', 'signin', 0, 9e9)"
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(store, "DATABASE", path)
    store.ensure_table()
    store.ensure_table()  # idempotent
    assert store.get("wc-old")["provider"] is None


def test_an_agent_request_past_its_ttl_is_expired_by_the_sweep(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    row = store.create(
        wallet="rW",
        purpose="tx",
        txjson={"Account": "rW"},
        nonce=None,
        ttl_seconds=-1,
        provider="agent",
    )
    store.expire_stale(time.time())
    assert store.get(row["id"])["state"] == "expired"
