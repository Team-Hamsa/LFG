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

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))

import backfill_market as bm  # noqa: E402


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


def test_prepare_conn_sets_busy_timeout():
    conn = sqlite3.connect(":memory:")
    try:
        bm._prepare_conn(conn)
        (timeout,) = conn.execute("PRAGMA busy_timeout").fetchone()
        assert timeout == 30000
    finally:
        conn.close()
