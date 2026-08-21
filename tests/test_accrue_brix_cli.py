"""accrue_brix.py drives run_archive_accrual — no per-token RPC sweep (#411)."""

from __future__ import annotations

import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))

import accrue_brix  # noqa: E402

from lfg_core import brix_drip, config  # noqa: E402


@pytest.fixture()
def fake_dbs(monkeypatch, tmp_path):
    from lfg_core import history_store, nft_index

    monkeypatch.setattr(history_store, "history_db_path", lambda net: str(tmp_path / f"h_{net}.db"))
    monkeypatch.setattr(nft_index, "index_db_path", lambda net: str(tmp_path / f"o_{net}.db"))
    monkeypatch.setattr(config, "XRPL_NETWORK", "testnet")

    async def ok(conn, network):
        return None

    monkeypatch.setattr(brix_drip, "verify_endpoint_chain", ok)
    return tmp_path


def test_cli_calls_archive_accrual_and_never_fetches_offers(monkeypatch, fake_dbs, capsys):
    called = {}

    def fake_run(conn, network, system_accounts, today=None, *, eligible, **kw):
        called["args"] = (network, today)
        called["eligible"] = eligible
        return [brix_drip.EpochReport("2026-08-18", 3, 1, 0, 0, 0, 0)]

    async def boom(*a, **k):
        raise AssertionError("live offer sweep must not run")

    monkeypatch.setattr(brix_drip, "run_archive_accrual", fake_run)
    monkeypatch.setattr(brix_drip, "fetch_sell_offer_state", boom)
    monkeypatch.setattr(
        sys, "argv", ["accrue_brix.py", "--network", "testnet", "--date", "2026-08-19"]
    )
    assert accrue_brix.main() == 0
    assert called["args"] == ("testnet", "2026-08-19")
    assert "accrued=3" in capsys.readouterr().out


def test_cli_prints_deferral_reason(monkeypatch, fake_dbs, capsys):
    monkeypatch.setattr(
        brix_drip,
        "run_archive_accrual",
        lambda *a, **k: [
            brix_drip.EpochReport(
                "2026-08-18", 0, 0, 0, 0, 0, 0, deferred="continuity gap recorded (x)"
            )
        ],
    )
    monkeypatch.setattr(sys, "argv", ["accrue_brix.py", "--network", "testnet"])
    assert accrue_brix.main() == 0
    out = capsys.readouterr().out
    assert "DEFERRED" in out and "continuity gap" in out


def test_cli_hints_rederive_on_unknown(monkeypatch, fake_dbs, capsys):
    monkeypatch.setattr(
        brix_drip,
        "run_archive_accrual",
        lambda *a, **k: [brix_drip.EpochReport("2026-08-18", 0, 0, 0, 0, 0, 7)],
    )
    monkeypatch.setattr(sys, "argv", ["accrue_brix.py", "--network", "testnet"])
    assert accrue_brix.main() == 0
    assert "derive_history_events.py" in capsys.readouterr().out


def test_cli_refuses_network_mismatch(monkeypatch, fake_dbs, capsys):
    monkeypatch.setattr(sys, "argv", ["accrue_brix.py", "--network", "mainnet"])
    assert accrue_brix.main() == 2
