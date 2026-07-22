# Tests for scripts/sourcetag_metrics.py
import importlib
import json
import os
import sqlite3
import sys

import pytest

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


def _valid_payload(**overrides):
    """A complete, schema-valid payload; override individual fields per test."""
    payload = {
        "source_tag": TAG,
        "network": "testnet",
        "total_tagged_txs": 5,
        "unique_wallets": 2,
        "by_type": {"Payment": 5},
        "daily": [{"date": "2026-07-20", "count": 5}],
        "excluded": sorted(stm.OPERATOR_WALLETS),
        "first_tagged_tx": "2026-07-20",
        "archive_max_close_time": "2026-07-20T12:00:00+00:00",
        "as_of": "2026-07-22T00:20:00+00:00",
    }
    payload.update(overrides)
    return payload


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


def test_validate_payload_accepts_a_real_payload(tmp_path):
    path = _db(tmp_path, [("h1", DAY0, "Payment", USER_A, TAG)])
    stm.validate_payload(stm.collect(path, "testnet"))  # must not raise


def test_validate_payload_rejects_unknown_keys(tmp_path):
    path = _db(tmp_path, [("h1", DAY0, "Payment", USER_A, TAG)])
    payload = stm.collect(path, "testnet")
    payload["raw_tx"] = {"Account": "rX", "secret": "shhh"}
    with pytest.raises(ValueError, match="unexpected key"):
        stm.validate_payload(payload)


def test_validate_payload_rejects_non_whitelisted_value_shapes(tmp_path):
    path = _db(tmp_path, [("h1", DAY0, "Payment", USER_A, TAG)])
    payload = stm.collect(path, "testnet")
    payload["network"] = "sEdTM1uX8pu2do5XvTnutH6HsouMaM2"  # looks like a seed
    with pytest.raises(ValueError):
        stm.validate_payload(payload)


def test_push_refuses_to_call_gh_when_validation_fails():
    def runner(cmd, **kw):
        raise AssertionError("must not touch the network on an invalid payload")

    with pytest.raises(ValueError):
        stm.push_to_github(_valid_payload(sneaky="x"), runner=runner)


def test_validate_payload_rejects_missing_required_key():
    payload = _valid_payload()
    del payload["excluded"]
    with pytest.raises(ValueError, match="excluded"):
        stm.validate_payload(payload)


def test_validate_payload_rejects_malformed_as_of():
    payload = _valid_payload(as_of="now")
    with pytest.raises(ValueError, match="as_of"):
        stm.validate_payload(payload)


def test_is_unchanged_ignores_as_of():
    a = {"total_tagged_txs": 5, "as_of": "2026-07-22T00:00:00+00:00"}
    b = json.dumps({"total_tagged_txs": 5, "as_of": "2026-07-23T00:00:00+00:00"}, indent=2)
    assert stm.is_unchanged(a, b) is True

    c = json.dumps({"total_tagged_txs": 6, "as_of": "2026-07-22T00:00:00+00:00"}, indent=2)
    assert stm.is_unchanged(a, c) is False
    assert stm.is_unchanged(a, None) is False


def test_push_skips_when_unchanged_and_makes_no_write_call():
    calls = []
    payload = _valid_payload()
    # Same payload, differing only in as_of, as the "existing remote" content.
    remote = _valid_payload(as_of="2026-07-21T00:00:00+00:00")

    def runner(cmd, **kw):
        calls.append(cmd)
        import subprocess as sp

        if "-X" in cmd and "PUT" in cmd:
            raise AssertionError("must not PUT when unchanged")
        body = json.dumps(
            {
                "sha": "abc123",
                "content": stm._b64(stm._serialize(remote)),
            }
        )
        return sp.CompletedProcess(cmd, 0, stdout=body, stderr="")

    made = stm.push_to_github(payload, runner=runner)
    assert made is False
    assert any("GET" in c or "contents" in " ".join(c) for c in calls)


def test_push_puts_with_existing_sha_when_changed():
    seen = {}
    remote = _valid_payload(total_tagged_txs=1)

    def runner(cmd, **kw):
        import subprocess as sp

        if "PUT" in cmd:
            seen["put"] = cmd
            seen["input"] = kw.get("input")
            return sp.CompletedProcess(cmd, 0, stdout="{}", stderr="")
        body = json.dumps(
            {
                "sha": "abc123",
                "content": stm._b64(stm._serialize(remote)),
            }
        )
        return sp.CompletedProcess(cmd, 0, stdout=body, stderr="")

    made = stm.push_to_github(_valid_payload(total_tagged_txs=99), runner=runner)
    assert made is True
    assert "abc123" in seen["input"]
    assert "main" in seen["input"]


def test_push_creates_file_when_absent_remotely():
    seen = {}

    def runner(cmd, **kw):
        import subprocess as sp

        if "PUT" in cmd:
            seen["input"] = kw.get("input")
            return sp.CompletedProcess(cmd, 0, stdout="{}", stderr="")
        # gh exits non-zero on 404
        return sp.CompletedProcess(cmd, 1, stdout="", stderr="Not Found (HTTP 404)")

    made = stm.push_to_github(_valid_payload(total_tagged_txs=1), runner=runner)
    assert made is True
    assert '"sha"' not in seen["input"]


def test_out_writes_file(tmp_path):
    path = _db(tmp_path, [("h1", DAY0, "Payment", USER_A, TAG)])
    dest = tmp_path / "metrics" / "sourcetag.json"
    rc = stm.main(["--network", "testnet", "--db", path, "--out", str(dest)])
    assert rc == 0
    assert json.loads(dest.read_text())["total_tagged_txs"] == 1


def test_missing_db_exits_nonzero_without_writing(tmp_path):
    dest = tmp_path / "out.json"
    rc = stm.main(["--network", "testnet", "--db", str(tmp_path / "nope.db"), "--out", str(dest)])
    assert rc != 0
    assert not dest.exists()
