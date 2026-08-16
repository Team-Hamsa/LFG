# tests/test_service_link_flow.py
# The cross-surface link flow: a link=true sign-in attaches the 2nd surface to
# the same wallet-account and returns the full account view. Cross-surface
# isolation (mirrors test_signin_status_cross_platform_404) is preserved.
import asyncio
import json
import time

import lfg_service.app as app
from lfg_service.app import make_session_token


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _Req:
    def __init__(self, token, body=None, match_info=None):
        self.headers = {"Authorization": f"Bearer {token}"}
        self.match_info = match_info or {}
        self._body = body or {}
        self._store: dict = {}

    async def json(self):
        return self._body

    def __getitem__(self, k):
        return self._store[k]

    def __setitem__(self, k, v):
        self._store[k] = v


def test_signin_start_records_link_intent(monkeypatch):
    monkeypatch.setattr(app.config, "WEBAPP_DEV_MODE", False)

    async def fake_create(return_url=None):
        return {"uuid": "lk1", "xumm_url": "https://xumm.app/sign/abc"}

    monkeypatch.setattr(app.xumm_ops, "create_signin_payload", fake_create)
    app.signin_payloads.pop("lk1", None)
    token = make_session_token({"id": "T", "name": "tg", "platform": "telegram"})
    resp = _run(app.handle_signin_start(_Req(token, {"link": True})))
    assert resp.status == 200
    # response shape unchanged (no extra keys)
    body = json.loads(resp.body.decode())
    assert set(body) == {"uuid", "signin_link"}
    assert app.signin_payloads["lk1"]["link"] is True
    app.signin_payloads.pop("lk1", None)


def test_link_signed_attaches_and_returns_account(monkeypatch):
    monkeypatch.setattr(app.config, "WEBAPP_DEV_MODE", False)
    app.signin_payloads["lk2"] = {
        "platform": "telegram",
        "user_id": "T",
        "name": "tg",
        "link": True,
        "created_at": time.time(),
    }

    async def fake_status(uuid):
        return {"signed": True, "account": "rWALLET", "opened": True, "expired": False}

    monkeypatch.setattr(app.xumm_ops, "get_payload_status", fake_status)
    monkeypatch.setattr(app, "is_valid_classic_address", lambda w: True)
    monkeypatch.setattr(app.identity_store, "link", lambda *a, **k: True)
    monkeypatch.setattr(
        app.identity_store,
        "identities_for_wallet",
        lambda w: [
            {"platform": "discord", "platform_user_id": "D", "display_handle": "alice"},
            {"platform": "telegram", "platform_user_id": "T", "display_handle": "alice_tg"},
        ],
    )
    token = make_session_token({"id": "T", "name": "tg", "platform": "telegram"})
    resp = _run(app.handle_signin_status(_Req(token, match_info={"payload_uuid": "lk2"})))
    assert resp.status == 200
    body = json.loads(resp.body.decode())
    assert body["state"] == "signed"
    assert body["wallet"] == "rWALLET"
    assert body["account"]["wallet"] == "rWALLET"
    assert {i["platform"] for i in body["account"]["identities"]} == {"discord", "telegram"}
    app.signin_payloads.pop("lk2", None)


def test_link_cross_platform_ownership_404(monkeypatch):
    monkeypatch.setattr(app.config, "WEBAPP_DEV_MODE", False)
    app.signin_payloads["lk3"] = {
        "platform": "telegram",
        "user_id": "55",
        "name": "tg",
        "link": True,
        "created_at": time.time(),
    }
    # a discord:55 token must NOT complete the telegram:55 link payload
    token = make_session_token({"id": "55", "name": "d", "platform": "discord"})
    resp = _run(app.handle_signin_status(_Req(token, match_info={"payload_uuid": "lk3"})))
    assert resp.status == 404
    app.signin_payloads.pop("lk3", None)


def test_link_legacy_users_stays_discord_only(monkeypatch):
    monkeypatch.setattr(app.config, "WEBAPP_DEV_MODE", False)
    legacy = {"called": False}

    async def fake_status(uuid):
        return {"signed": True, "account": "rWALLET", "opened": True, "expired": False}

    monkeypatch.setattr(app.xumm_ops, "get_payload_status", fake_status)
    monkeypatch.setattr(app, "is_valid_classic_address", lambda w: True)
    monkeypatch.setattr(app.identity_store, "link", lambda *a, **k: True)
    monkeypatch.setattr(app.identity_store, "identities_for_wallet", lambda w: [])

    def fake_reg(uid, name, w):
        legacy["called"] = True
        return True

    monkeypatch.setattr(app, "register_user", fake_reg)

    # telegram link does NOT touch legacy Users
    app.signin_payloads["lk4"] = {
        "platform": "telegram",
        "user_id": "T",
        "name": "tg",
        "link": True,
        "created_at": time.time(),
    }
    token = make_session_token({"id": "T", "name": "tg", "platform": "telegram"})
    _run(app.handle_signin_status(_Req(token, match_info={"payload_uuid": "lk4"})))
    assert legacy["called"] is False

    # discord link DOES write legacy Users
    app.signin_payloads["lk5"] = {
        "platform": "discord",
        "user_id": "D",
        "name": "d",
        "link": True,
        "created_at": time.time(),
    }
    token = make_session_token({"id": "D", "name": "d", "platform": "discord"})
    _run(app.handle_signin_status(_Req(token, match_info={"payload_uuid": "lk5"})))
    assert legacy["called"] is True
    app.signin_payloads.pop("lk4", None)
    app.signin_payloads.pop("lk5", None)


def test_signin_without_link_flag_unchanged(monkeypatch):
    """Regression: a plain (non-link) sign-in response has NO account key —
    byte-identical to today."""
    monkeypatch.setattr(app.config, "WEBAPP_DEV_MODE", False)
    app.signin_payloads["lk6"] = {
        "platform": "telegram",
        "user_id": "T",
        "name": "tg",
        "created_at": time.time(),
    }

    async def fake_status(uuid):
        return {"signed": True, "account": "rWALLET", "opened": True, "expired": False}

    monkeypatch.setattr(app.xumm_ops, "get_payload_status", fake_status)
    monkeypatch.setattr(app, "is_valid_classic_address", lambda w: True)
    monkeypatch.setattr(app.identity_store, "link", lambda *a, **k: True)
    token = make_session_token({"id": "T", "name": "tg", "platform": "telegram"})
    resp = _run(app.handle_signin_status(_Req(token, match_info={"payload_uuid": "lk6"})))
    assert resp.status == 200
    body = json.loads(resp.body.decode())
    assert body == {"state": "signed", "wallet": "rWALLET"}
    assert "account" not in body
    app.signin_payloads.pop("lk6", None)
