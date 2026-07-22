# Tests for scripts/sourcetag_metrics.py
import importlib
import json
import os
import sqlite3
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
os.environ.setdefault("BUNNY_PULL_ZONE", "")
os.environ.setdefault("LAYER_SOURCE", "local")
os.environ.setdefault("ECONOMY_ENABLED", "1")

from lfg_core import config, history_store  # noqa: E402

stm = importlib.import_module("scripts.sourcetag_metrics")

TAG = config.SOURCE_TAG
USER_A = "rUserAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
USER_B = "rUserBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
OPERATOR = "rHU8nu9zSnCpkL3gShG4aGawHzaRVfmKwQ"

# 2026-07-20T12:00:00Z and 2026-07-22T12:00:00Z, as UNIX seconds. These are
# stored verbatim: close_time in xrpl_txs is unix, not the ripple epoch.
DAY0 = 1784548800
DAY2 = DAY0 + 2 * 86400


def _db(tmp_path, rows):
    """rows: (hash, close_time, tx_type, account, source_tag)"""
    path = str(tmp_path / "history_testnet.db")
    conn = history_store.init_history_db(path)
    conn.executemany(
        "INSERT INTO xrpl_txs (tx_hash, ledger_index, close_time, tx_type,"
        " account, source_tag, raw_json) VALUES (?,1,?,?,?,?,'{}')",
        rows,
    )
    conn.commit()
    conn.close()
    return path


def test_counts_all_tagged_txs_but_excludes_our_wallets_from_unique(tmp_path):
    path = _db(
        tmp_path,
        [
            ("h1", DAY0, "NFTokenMint", config.SIGNING_ACCOUNT, TAG),
            ("h2", DAY0, "NFTokenAcceptOffer", USER_A, TAG),
            ("h3", DAY2, "NFTokenAcceptOffer", USER_B, TAG),
            ("h4", DAY2, "Payment", OPERATOR, TAG),
            ("h5", DAY2, "Payment", USER_A, None),  # untagged, must not count
        ],
    )
    out = stm.collect(path, "testnet")

    # every tagged row counts, including the backend-signed mint
    assert out["total_tagged_txs"] == 4
    # ...but only non-project signers are unique wallets
    assert out["unique_wallets"] == 2
    assert out["source_tag"] == TAG
    assert out["network"] == "testnet"


def test_by_type_is_descending_and_covers_all_tagged_rows(tmp_path):
    path = _db(
        tmp_path,
        [
            ("h1", DAY0, "NFTokenMint", config.SIGNING_ACCOUNT, TAG),
            ("h2", DAY0, "NFTokenMint", config.SIGNING_ACCOUNT, TAG),
            ("h3", DAY0, "NFTokenAcceptOffer", USER_A, TAG),
        ],
    )
    out = stm.collect(path, "testnet")
    assert list(out["by_type"].items()) == [("NFTokenMint", 2), ("NFTokenAcceptOffer", 1)]


def test_daily_series_is_gap_filled_and_uses_unix_close_time(tmp_path):
    path = _db(
        tmp_path,
        [
            ("h1", DAY0, "NFTokenMint", USER_A, TAG),
            ("h2", DAY2, "NFTokenMint", USER_A, TAG),
        ],
    )
    out = stm.collect(path, "testnet")
    # DAY0 is 2026-07-20; the intervening day must appear as a zero
    assert out["daily"] == [
        {"date": "2026-07-20", "count": 1},
        {"date": "2026-07-21", "count": 0},
        {"date": "2026-07-22", "count": 1},
    ]
    assert out["first_tagged_tx"] == "2026-07-20"


def test_excluded_addresses_are_reported(tmp_path):
    path = _db(tmp_path, [("h1", DAY0, "Payment", USER_A, TAG)])
    out = stm.collect(path, "testnet")
    assert config.SIGNING_ACCOUNT in out["excluded"]
    assert OPERATOR in out["excluded"]
    assert out["excluded"] == sorted(out["excluded"])


def test_no_tagged_rows_yields_zeros_not_a_crash(tmp_path):
    path = _db(tmp_path, [("h1", DAY0, "Payment", USER_A, None)])
    out = stm.collect(path, "testnet")
    assert out["total_tagged_txs"] == 0
    assert out["unique_wallets"] == 0
    assert out["by_type"] == {}
    assert out["daily"] == []
    assert out["first_tagged_tx"] is None
    assert json.dumps(out)  # serialisable
