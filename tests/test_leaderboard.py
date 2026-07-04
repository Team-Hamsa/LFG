# Tests for lfg_core/leaderboard.py
import os
import sys
import sqlite3
from datetime import datetime, timezone

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

from lfg_core import history_store, leaderboard  # noqa: E402

ISSUER = "rIssuer"
SYS = frozenset({ISSUER})


def _dbs():
    h = history_store.init_history_db(":memory:")
    o = sqlite3.connect(":memory:")
    o.row_factory = sqlite3.Row
    o.execute(
        "CREATE TABLE onchain_nfts (nft_id TEXT PRIMARY KEY, nft_number INT,"
        " owner TEXT, is_burned INT DEFAULT 0, attributes_json TEXT, image TEXT)"
    )
    return h, o


def _ev(h, **kw):
    base = dict(
        tx_hash=kw.get("tx_hash", str(id(kw))),
        nft_id="N1",
        nft_number=1,
        event="mint",
        from_addr=None,
        to_addr=None,
        price_drops=None,
        price_token=None,
        ledger_index=1,
        ts=0,
    )
    base.update(kw)
    history_store.insert_nft_event(h, base)
    h.commit()


def test_period_bounds_today_and_anchored_month():
    now = int(datetime(2026, 7, 4, 15, 0, tzinfo=timezone.utc).timestamp())
    s, e = leaderboard.period_bounds("today", None, now=now)
    assert s == int(datetime(2026, 7, 4, tzinfo=timezone.utc).timestamp()) and e == now
    s, e = leaderboard.period_bounds("month", "2026-01-01", now=now)
    assert (s, e) == (
        int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()),
        int(datetime(2026, 2, 1, tzinfo=timezone.utc).timestamp()),
    )
    s, e = leaderboard.period_bounds("week", "2026-06-30", now=now)  # Tue -> Mon 06-29
    assert s == int(datetime(2026, 6, 29, tzinfo=timezone.utc).timestamp())
    assert leaderboard.period_bounds("all", None, now=now) == (0, now)


def test_period_bounds_unknown_raises():
    now = int(datetime(2026, 7, 4, tzinfo=timezone.utc).timestamp())
    try:
        leaderboard.period_bounds("century", None, now=now)
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_users_nfts_alltime_and_windowed():
    h, o = _dbs()
    o.executemany(
        "INSERT INTO onchain_nfts (nft_id, nft_number, owner) VALUES (?,?,?)",
        [("N1", 1, "rA"), ("N2", 2, "rA"), ("N3", 3, "rB"), ("N4", 4, ISSUER)],
    )
    rows = leaderboard.compute(
        "users_nfts", h, o, start_ts=0, end_ts=99, network="testnet", system_accounts=SYS
    )
    assert [(r["wallet"], r["value"]) for r in rows] == [("rA", 2), ("rB", 1)]
    _ev(h, tx_hash="t1", event="sale", from_addr="rB", to_addr="rA", ts=50)
    rows = leaderboard.compute(
        "users_nfts", h, o, start_ts=40, end_ts=60, network="testnet", system_accounts=SYS
    )
    assert rows == [{"wallet": "rA", "nft_id": None, "nft_number": None, "value": 1}]


def test_users_swaps_and_nft_swaps():
    h, o = _dbs()
    for i, w in enumerate(["rA", "rA", "rB"]):
        _ev(h, tx_hash=f"m{i}", event="modify", to_addr=w, nft_id="N1", ts=5)
    rows = leaderboard.compute(
        "users_swaps", h, o, start_ts=0, end_ts=10, network="testnet", system_accounts=SYS
    )
    assert rows[0] == {"wallet": "rA", "nft_id": None, "nft_number": None, "value": 2}
    rows = leaderboard.compute(
        "nft_swaps", h, o, start_ts=0, end_ts=10, network="testnet", system_accounts=SYS
    )
    assert rows[0]["nft_id"] == "N1" and rows[0]["value"] == 3


def test_users_builds_counts_only_rebirths():
    h, o = _dbs()
    # edition 7: first token burned, second minted later, delivered to rA
    _ev(h, tx_hash="b", event="burn", nft_id="OLD", nft_number=7, from_addr="rX", ts=10)
    _ev(h, tx_hash="m", event="mint", nft_id="NEW", nft_number=7, to_addr=ISSUER, ts=20)
    _ev(
        h,
        tx_hash="d",
        event="transfer",
        nft_id="NEW",
        nft_number=7,
        from_addr=ISSUER,
        to_addr="rA",
        ts=30,
    )
    # non-rebirth issuer transfer must not count
    _ev(h, tx_hash="m2", event="mint", nft_id="N9", nft_number=9, to_addr=ISSUER, ts=20)
    _ev(
        h,
        tx_hash="d2",
        event="transfer",
        nft_id="N9",
        nft_number=9,
        from_addr=ISSUER,
        to_addr="rB",
        ts=30,
    )
    rows = leaderboard.compute(
        "users_builds", h, o, start_ts=0, end_ts=99, network="testnet", system_accounts=SYS
    )
    assert rows == [{"wallet": "rA", "nft_id": None, "nft_number": None, "value": 1}]


def test_compute_unknown_board_raises():
    h, o = _dbs()
    try:
        leaderboard.compute(
            "nope", h, o, start_ts=0, end_ts=1, network="testnet", system_accounts=SYS
        )
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_limit_parameter_threads_through():
    """Verify limit parameter is threaded through board functions, not capped at _LIMIT."""
    h, o = _dbs()
    # Insert 30 modify events by 30 distinct wallets
    for i in range(30):
        wallet = f"rWallet{i:02d}"
        _ev(h, tx_hash=f"t{i}", event="modify", to_addr=wallet, nft_id=f"N{i}", ts=5)

    # Test with limit=28: should return 28 rows, not capped at _LIMIT (25)
    rows = leaderboard.compute(
        "users_swaps", h, o, start_ts=0, end_ts=10, network="testnet", system_accounts=SYS, limit=28
    )
    assert len(rows) == 28, f"expected 28 rows with limit=28, got {len(rows)}"

    # Test default limit still gives 25
    rows_default = leaderboard.compute(
        "users_swaps", h, o, start_ts=0, end_ts=10, network="testnet", system_accounts=SYS
    )
    assert len(rows_default) == 25, f"expected 25 rows with default limit, got {len(rows_default)}"
