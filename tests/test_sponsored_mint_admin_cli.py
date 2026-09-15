"""scripts/sponsored_mint_admin.py — Discord-independent campaign start/stop/status.

The CLI is the outage path for the sponsored free-mint switch: it calls the
same `lfg_core.sponsored_mint` functions the `/api/admin/sponsored-mint/*`
handlers do, against the box-local DBs, so a Discord gateway outage can no
longer lock the operator out of the campaign.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))

import sponsored_mint_admin as cli  # noqa: E402

from lfg_core import archive_reverify, config, db_path, history_store, sponsored_mint  # noqa: E402


@pytest.fixture()
def dbs(monkeypatch, tmp_path):
    app_db = str(tmp_path / "app_testnet.db")
    hist_db = str(tmp_path / "history_testnet.db")
    monkeypatch.setattr(config, "XRPL_NETWORK", "testnet")
    monkeypatch.setattr(cli.getpass, "getuser", lambda: "josh")
    monkeypatch.setattr(db_path, "app_db_path", lambda net: app_db)
    monkeypatch.setattr(history_store, "history_db_path", lambda net: hist_db)
    history_store.init_history_db(hist_db).close()
    return app_db, hist_db


@pytest.fixture()
def fake_reverify(monkeypatch):
    """Stub the on-ledger sweep: records the call, returns ok, archive usable."""
    calls: dict = {}

    async def reverify(conn, request_fn, *, network):
        calls["network"] = network
        return archive_reverify.ReverifyResult(True, None, 123, "p")

    async def usable(path, *, network, **kw):
        calls["waited"] = network
        return True

    monkeypatch.setattr(archive_reverify, "reverify_archive", reverify)
    monkeypatch.setattr(archive_reverify, "wait_for_archive_usable", usable)
    monkeypatch.setattr(cli, "_clio_client", lambda: _NullClient())
    return calls


class _NullClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None


def run(*argv: str) -> int:
    sys.argv = ["sponsored_mint_admin.py", *argv]
    return cli.main()


def audit_rows(app_db: str) -> list[tuple[str, str, str]]:
    conn = sqlite3.connect(app_db)
    try:
        return conn.execute(
            "SELECT actor, action, result FROM free_mint_audit ORDER BY id"
        ).fetchall()
    finally:
        conn.close()


def test_start_activates_campaign_and_reverifies(dbs, fake_reverify, capsys):
    app_db, hist_db = dbs
    assert run("start", "--network", "testnet") == 0
    out = capsys.readouterr().out
    assert '"state": "active"' in out
    assert "reverify: ok" in out
    status = sponsored_mint.campaign_status(app_db, hist_db, network="testnet")
    assert status.state == "active"
    assert fake_reverify == {"network": "testnet", "waited": "testnet"}
    # Audit rows attribute the change to the CLI, never to a Discord actor.
    actors = {a for a, _, _ in audit_rows(app_db)}
    assert actors == {"cli:josh"}


def test_start_reports_failed_reverify_but_leaves_campaign_active(dbs, monkeypatch, capsys):
    app_db, hist_db = dbs

    async def bad(conn, request_fn, *, network):
        return archive_reverify.ReverifyResult(False, "no_baseline", None, None)

    monkeypatch.setattr(archive_reverify, "reverify_archive", bad)
    monkeypatch.setattr(cli, "_clio_client", lambda: _NullClient())
    assert run("start", "--network", "testnet") == 3
    assert "reverify: failed: no_baseline" in capsys.readouterr().out
    assert sponsored_mint.campaign_status(app_db, hist_db, network="testnet").state == "active"
    assert ("cli:josh", "archive_reverify", "failed: no_baseline") in audit_rows(app_db)


def test_start_skip_reverify(dbs, monkeypatch, capsys):
    async def boom(*a, **k):
        raise AssertionError("reverify must not run")

    monkeypatch.setattr(archive_reverify, "reverify_archive", boom)
    # Exit 4, not 0: the archive was never checked, so "admission open" is unknown.
    assert run("start", "--network", "testnet", "--skip-reverify") == 4
    assert "UNKNOWN" in capsys.readouterr().out


def test_stop_and_status(dbs, fake_reverify, capsys):
    app_db, hist_db = dbs
    assert run("start", "--network", "testnet") == 0
    assert run("stop", "--network", "testnet") == 0
    capsys.readouterr()
    assert run("status", "--network", "testnet") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["state"] == "stopped"
    assert payload["network"] == "testnet"


def test_refuses_network_mismatch(dbs, fake_reverify, capsys):
    assert run("start", "--network", "mainnet") == 2
    assert "XRPL_NETWORK is testnet" in capsys.readouterr().out
    app_db, hist_db = dbs
    assert sponsored_mint.campaign_status(app_db, hist_db, network="testnet").state == "off"


def test_actor_is_os_login_not_flag(dbs, fake_reverify):
    app_db, _ = dbs
    assert run("stop", "--network", "testnet", "--note", "outage drill") == 0
    assert [a for a, _, _ in audit_rows(app_db)] == ["cli:josh:outage drill"]


def test_start_reverify_crash_exits_3_and_audits(dbs, monkeypatch, capsys):
    app_db, hist_db = dbs

    async def crash(conn, request_fn, *, network):
        raise OSError("clio unreachable")

    monkeypatch.setattr(archive_reverify, "reverify_archive", crash)
    monkeypatch.setattr(cli, "_clio_client", lambda: _NullClient())
    assert run("start", "--network", "testnet") == 3
    assert "internal_error: OSError: clio unreachable" in capsys.readouterr().out
    assert sponsored_mint.campaign_status(app_db, hist_db, network="testnet").state == "active"
    rows = audit_rows(app_db)
    assert any(
        a == "cli:josh" and act == "archive_reverify" and "internal_error" in r
        for a, act, r in rows
    )


def test_start_surfaces_library_refusal(dbs, fake_reverify, monkeypatch, capsys):
    def refuse(*a, **k):
        raise ValueError("campaign already active")

    monkeypatch.setattr(sponsored_mint, "start_campaign", refuse)
    assert run("start", "--network", "testnet") == 1
    assert "campaign already active" in capsys.readouterr().out
