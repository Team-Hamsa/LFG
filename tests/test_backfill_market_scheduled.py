# tests/test_backfill_market_scheduled.py
# Scheduled-run hardening for scripts/backfill_market.py (#288): structured
# drift logging, --report JSON drift log, and sqlite busy_timeout so a nightly
# cron can't crash on a lock held by the live listener.
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

import json  # noqa: E402
import logging  # noqa: E402
import sqlite3  # noqa: E402
import sys  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402

import pytest  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))

import backfill_market as bm  # noqa: E402

from lfg_core import nft_index  # noqa: E402


def _counts(**overrides: int) -> dict[str, int]:
    base = {
        "characters_swept": 10,
        "traits_swept": 2,
        "live_listings": 9,
        "closed_stale": 0,
        "fetch_failures": 0,
        "live_bids": 0,
        "bids_closed_stale": 0,
        "bid_fetch_failures": 0,
    }
    base.update(overrides)
    return base


# --- --report flag -----------------------------------------------------------


def test_report_flag_default_off():
    args = bm._build_parser().parse_args(["--network", "testnet"])
    assert args.report is False


def test_report_flag_accepted():
    args = bm._build_parser().parse_args(["--network", "testnet", "--report"])
    assert args.report is True


# --- drift WARNING line ------------------------------------------------------


def _drift_records(caplog):
    return [r for r in caplog.records if "backfill_market drift" in r.getMessage()]


def test_drift_line_warns_when_stale_nonzero(caplog):
    with caplog.at_level(logging.INFO):
        bm._log_summary("testnet", _counts(closed_stale=2))
    drift = _drift_records(caplog)
    assert drift and drift[0].levelno == logging.WARNING
    msg = drift[0].getMessage()
    assert "net=testnet" in msg
    assert "closed_stale=2" in msg
    assert "fetch_failures=0" in msg
    assert "bids_closed_stale=0" in msg
    assert "bid_fetch_failures=0" in msg


def test_drift_line_warns_on_fetch_failures(caplog):
    with caplog.at_level(logging.INFO):
        bm._log_summary("mainnet", _counts(bid_fetch_failures=3))
    drift = _drift_records(caplog)
    assert drift and drift[0].levelno == logging.WARNING


def test_no_drift_logs_info_not_warning(caplog):
    with caplog.at_level(logging.INFO):
        bm._log_summary("testnet", _counts())
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    # summary still emitted at INFO, including the greppable drift line
    assert _drift_records(caplog)


# --- --report JSON drift log -------------------------------------------------


def test_report_appends_json_line(tmp_path):
    path = tmp_path / "sub" / "drift.log"
    bm._append_drift_report(str(path), "testnet", _counts(closed_stale=1))
    bm._append_drift_report(str(path), "testnet", _counts())
    lines = path.read_text().strip().splitlines()
    assert len(lines) == 2
    rec = json.loads(lines[0])
    assert rec["network"] == "testnet"
    assert rec["closed_stale"] == 1
    assert "ts" in rec


# --- busy_timeout ------------------------------------------------------------


def test_init_db_sets_busy_timeout():
    assert bm.BUSY_TIMEOUT_MS == 30000
    conn = nft_index.init_db(":memory:", busy_timeout_ms=bm.BUSY_TIMEOUT_MS)
    try:
        (timeout,) = conn.execute("PRAGMA busy_timeout").fetchone()
        assert timeout == 30000
    finally:
        conn.close()


def test_init_db_default_leaves_timeout_untouched():
    conn = nft_index.init_db(":memory:")
    try:
        (timeout,) = conn.execute("PRAGMA busy_timeout").fetchone()
        assert timeout == 5000  # sqlite3.connect default (5s)
    finally:
        conn.close()


def test_init_db_busy_timeout_applies_before_schema_ddl(tmp_path):
    """CodeRabbit #288 regression: the timeout must be set BEFORE init_db's
    schema DDL. With a writer holding the lock, a tiny busy_timeout must fail
    fast (well under sqlite3.connect's 5s default handler — proving the PRAGMA
    governed the DDL), while a generous one must survive a briefly-held lock."""
    db = str(tmp_path / "onchain.db")
    # Create the FILE but not the index schema, so init_db's DDL genuinely
    # writes (the schema is IF NOT EXISTS — a fully-initialized db would make
    # the second init a no-op that never touches the lock).
    seed = sqlite3.connect(db)
    seed.execute("CREATE TABLE lock_anchor (id INTEGER)")
    seed.commit()
    seed.close()

    writer = sqlite3.connect(db)
    try:
        writer.execute("BEGIN IMMEDIATE")

        # (a) tiny timeout: DDL hits the lock and gives up fast, not at 5s.
        start = time.monotonic()
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            nft_index.init_db(db, busy_timeout_ms=100)
        assert time.monotonic() - start < 2.0

        # (b) generous timeout: init waits out a briefly-held lock.
        result: dict[str, object] = {}

        def _init():
            try:
                nft_index.init_db(db, busy_timeout_ms=bm.BUSY_TIMEOUT_MS).close()
                result["ok"] = True
            except Exception as e:  # pragma: no cover - failure detail
                result["error"] = repr(e)

        t = threading.Thread(target=_init)
        t.start()
        time.sleep(0.5)
        writer.rollback()  # release the lock while the init thread is waiting
        t.join(timeout=10)
        assert not t.is_alive()
        assert result.get("ok") is True, result
    finally:
        writer.close()
