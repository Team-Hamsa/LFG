"""POST /api/share/intent — the Share-on-X button beacon (exact giveaway
eligibility, unlike the Twitterbot-fetch proxy in share_clicks)."""

import asyncio
import json
import sqlite3

import pytest

from lfg_core import config, db_path
from lfg_service import app as server


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _FakeRequest:
    def __init__(self, body=None):
        self.headers = {}
        self._store = {"user": {"id": "u1", "platform": "discord"}}
        self._body = body

    def __setitem__(self, key, value):
        self._store[key] = value

    def __getitem__(self, key):
        return self._store[key]

    async def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    db = str(tmp_path / "app.db")
    monkeypatch.setattr(db_path, "app_db_path", lambda *a, **k: db)
    monkeypatch.setattr(config, "WEBAPP_DEV_MODE", True)  # require_wallet injects DEV_OWNER
    yield db


def _rows(db):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(
            "SELECT wallet, kind, nft_number, platform FROM share_intents"
        ).fetchall()
    except sqlite3.OperationalError:
        return []  # no write ever happened: table never created
    finally:
        conn.close()


def test_intent_records_wallet_kind_nft(_env):
    from webapp import mock_economy

    resp = _run(server.handle_share_intent(_FakeRequest({"kind": "mint", "nft_number": 4776})))
    assert resp.status == 200
    assert json.loads(resp.body) == {"ok": True}
    assert _rows(_env) == [(mock_economy.DEV_OWNER, "mint", 4776, "discord")]


def test_intent_rejects_unknown_kind(_env):
    resp = _run(server.handle_share_intent(_FakeRequest({"kind": "evil"})))
    assert resp.status == 400
    assert _rows(_env) == []


@pytest.mark.parametrize("bad", [True, "7", 1.5, -1])
def test_intent_rejects_non_int_nft_number(_env, bad):
    resp = _run(server.handle_share_intent(_FakeRequest({"kind": "mint", "nft_number": bad})))
    assert resp.status == 400


def test_intent_allows_null_nft_number_and_bad_json(_env):
    resp = _run(server.handle_share_intent(_FakeRequest({"kind": "swap"})))
    assert resp.status == 200
    assert _rows(_env)[0][1:3] == ("swap", None)
    # Unparseable body: still a (mint, null) beacon — never a 500.
    resp = _run(server.handle_share_intent(_FakeRequest(None)))
    assert resp.status == 200
