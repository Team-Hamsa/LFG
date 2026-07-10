# Tests for lfg_core/history_store.py
import os
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


from lfg_core import history_store


def _conn(tmp_path):
    return history_store.init_history_db(str(tmp_path / "h.db"))


def test_insert_tx_idempotent(tmp_path):
    conn = _conn(tmp_path)
    kw = {
        "tx_hash": "AB" * 32,
        "ledger_index": 5,
        "close_time": 1700000000,
        "tx_type": "Payment",
        "account": "rSender",
        "source_tag": None,
        "raw_json": "{}",
    }
    assert history_store.insert_tx(conn, **kw) is True
    assert history_store.insert_tx(conn, **kw) is False
    n = conn.execute("SELECT COUNT(*) FROM xrpl_txs").fetchone()[0]
    assert n == 1


def test_cursor_roundtrip(tmp_path):
    conn = _conn(tmp_path)
    assert history_store.get_cursor(conn, "issuer_tx") is None
    history_store.set_cursor(conn, "issuer_tx", '{"ledger": 1}')
    assert history_store.get_cursor(conn, "issuer_tx") == '{"ledger": 1}'
    history_store.set_cursor(conn, "issuer_tx", None)
    assert history_store.get_cursor(conn, "issuer_tx") is None


def test_events_and_clear(tmp_path):
    conn = _conn(tmp_path)
    history_store.insert_nft_event(
        conn,
        {
            "tx_hash": "CD" * 32,
            "nft_id": "00" * 32,
            "nft_number": 7,
            "event": "mint",
            "from_addr": None,
            "to_addr": "rOwner",
            "price_drops": None,
            "price_token": None,
            "ledger_index": 9,
            "ts": 1700000001,
        },
    )
    history_store.insert_brix_event(
        conn,
        {
            "tx_hash": "CD" * 32,
            "account": "rOwner",
            "counterparty": "rIssuer",
            "delta": 5.0,
            "kind": "airdrop",
            "ts": 1700000001,
        },
    )
    assert conn.execute("SELECT COUNT(*) FROM nft_events").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM brix_events").fetchone()[0] == 1
    history_store.clear_derived(conn)
    assert conn.execute("SELECT COUNT(*) FROM nft_events").fetchone()[0] == 0


def test_snapshot_upsert(tmp_path):
    conn = _conn(tmp_path)
    history_store.upsert_snapshot(conn, "2026-07-04", "rA", 10.0, 1.5)
    history_store.upsert_snapshot(conn, "2026-07-04", "rA", 12.0, 1.5)
    row = conn.execute("SELECT brix FROM balance_snapshots").fetchone()
    assert row["brix"] == 12.0


def test_db_path_override(monkeypatch):
    monkeypatch.setenv("HISTORY_DB_PATH", "/tmp/x.db")
    assert history_store.history_db_path("mainnet") == "/tmp/x.db"
    monkeypatch.delenv("HISTORY_DB_PATH")
    assert history_store.history_db_path("mainnet").endswith("history_mainnet.db")


def test_nft_events_memo_action_self_migrates(tmp_path):
    """A pre-upgrade history DB (no memo_action column) must gain the column
    on init_history_db, and insert_nft_event must persist it."""
    import sqlite3

    path = str(tmp_path / "old.db")
    old = sqlite3.connect(path)
    old.executescript(
        "CREATE TABLE nft_events ("
        " tx_hash TEXT, nft_id TEXT, nft_number INTEGER, event TEXT,"
        " from_addr TEXT, to_addr TEXT, price_drops INTEGER, price_token TEXT,"
        " ledger_index INTEGER, ts INTEGER, PRIMARY KEY (tx_hash, nft_id))"
    )
    old.commit()
    old.close()

    conn = history_store.init_history_db(path)
    history_store.insert_nft_event(
        conn,
        {
            "tx_hash": "T1",
            "nft_id": "N1",
            "event": "mint",
            "memo_action": "assemble",
        },
    )
    row = conn.execute("SELECT memo_action FROM nft_events WHERE tx_hash='T1'").fetchone()
    assert row["memo_action"] == "assemble"


def test_nft_events_rebirth_index_exists(tmp_path):
    """The rebirth EXISTS subquery seeks burns by (event, nft_number); without
    a supporting index it degrades to O(n^2) as history grows (CodeRabbit
    #157). CREATE INDEX IF NOT EXISTS in _SCHEMA also retrofits old DBs."""
    conn = history_store.init_history_db(str(tmp_path / "h.db"))
    names = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='nft_events'"
        )
    }
    assert "idx_nftev_event_number" in names
