import asyncio
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
    def __init__(self, token, match_info=None):
        self.headers = {"Authorization": f"Bearer {token}"}
        self.match_info = match_info or {}
        self._store: dict = {}

    def __getitem__(self, k):
        return self._store[k]

    def __setitem__(self, k, v):
        self._store[k] = v


def test_signin_start_tags_platform(monkeypatch):
    monkeypatch.setattr(app.config, "WEBAPP_DEV_MODE", False)

    async def fake_create(return_url=None):
        return {"uuid": "u1", "xumm_url": "https://xumm.app/sign/abc"}

    monkeypatch.setattr(app.xumm_ops, "create_signin_payload", fake_create)
    app.signin_payloads.pop("u1", None)
    token = make_session_token({"id": "55", "name": "tg", "platform": "telegram"})
    resp = _run(app.handle_signin_start(_Req(token)))
    assert resp.status == 200
    rec = app.signin_payloads["u1"]
    assert rec["platform"] == "telegram" and rec["user_id"] == "55"
    app.signin_payloads.pop("u1", None)


def test_signin_status_cross_platform_404(monkeypatch):
    monkeypatch.setattr(app.config, "WEBAPP_DEV_MODE", False)
    app.signin_payloads["u2"] = {
        "platform": "telegram",
        "user_id": "55",
        "name": "tg",
        "created_at": time.time(),
    }
    # a discord:55 token must NOT be able to read the telegram:55 payload
    token = make_session_token({"id": "55", "name": "d", "platform": "discord"})
    resp = _run(app.handle_signin_status(_Req(token, {"payload_uuid": "u2"})))
    assert resp.status == 404
    app.signin_payloads.pop("u2", None)


def test_signin_signed_links_under_platform_no_legacy(monkeypatch):
    monkeypatch.setattr(app.config, "WEBAPP_DEV_MODE", False)
    legacy = {"called": False}
    linked = {}
    app.signin_payloads["u3"] = {
        "platform": "telegram",
        "user_id": "55",
        "name": "tg",
        "created_at": time.time(),
    }

    async def fake_status(uuid):
        return {"signed": True, "account": "rXRPL", "opened": True, "expired": False}

    monkeypatch.setattr(app.xumm_ops, "get_payload_status", fake_status)
    monkeypatch.setattr(app, "is_valid_classic_address", lambda w: True)

    def fake_reg(uid, name, w):
        legacy["called"] = True
        return True

    monkeypatch.setattr(app, "register_user", fake_reg)

    def fake_link(platform, uid, name, wallet):
        linked["args"] = (platform, uid, wallet)
        return True

    monkeypatch.setattr(app.identity_store, "link", fake_link)
    token = make_session_token({"id": "55", "name": "tg", "platform": "telegram"})
    resp = _run(app.handle_signin_status(_Req(token, {"payload_uuid": "u3"})))
    assert resp.status == 200
    assert linked["args"] == ("telegram", "55", "rXRPL")
    assert legacy["called"] is False  # non-discord: identities only


def test_signin_signed_discord_writes_legacy(monkeypatch):
    monkeypatch.setattr(app.config, "WEBAPP_DEV_MODE", False)
    legacy = {}
    app.signin_payloads["u4"] = {
        "platform": "discord",
        "user_id": "9",
        "name": "d",
        "created_at": time.time(),
    }

    async def fake_status(uuid):
        return {"signed": True, "account": "rDISCORD", "opened": True, "expired": False}

    monkeypatch.setattr(app.xumm_ops, "get_payload_status", fake_status)
    monkeypatch.setattr(app, "is_valid_classic_address", lambda w: True)
    monkeypatch.setattr(
        app, "register_user", lambda uid, name, w: legacy.update(args=(uid, name, w)) or True
    )
    monkeypatch.setattr(app.identity_store, "link", lambda *a: True)
    token = make_session_token({"id": "9", "name": "d", "platform": "discord"})
    resp = _run(app.handle_signin_status(_Req(token, {"payload_uuid": "u4"})))
    assert resp.status == 200
    assert legacy["args"] == ("9", "d", "rDISCORD")  # discord still writes legacy Users


def _signed_signin(monkeypatch, uuid, status_extra, link_ok=True):
    """Common harness: a signed sign-in for telegram:55 with `status_extra`
    merged into the payload-status dict. Returns the captured set_user_token
    calls list. `link_ok` controls the identity.link result."""
    monkeypatch.setattr(app.config, "WEBAPP_DEV_MODE", False)
    app.signin_payloads[uuid] = {
        "platform": "telegram",
        "user_id": "55",
        "name": "tg",
        "created_at": time.time(),
    }

    async def fake_status(_uuid):
        return {
            "signed": True,
            "account": "rXRPL",
            "opened": True,
            "expired": False,
            **status_extra,
        }

    monkeypatch.setattr(app.xumm_ops, "get_payload_status", fake_status)
    monkeypatch.setattr(app, "is_valid_classic_address", lambda w: True)
    monkeypatch.setattr(app.identity_store, "link", lambda *a: link_ok)
    captured = []
    monkeypatch.setattr(app.identity_store, "set_user_token", lambda *a: captured.append(a))
    token = make_session_token({"id": "55", "name": "tg", "platform": "telegram"})
    resp = _run(app.handle_signin_status(_Req(token, {"payload_uuid": uuid})))
    assert resp.status == 200
    return captured


def test_signin_captures_user_token(monkeypatch):
    # #135: a push token issued on sign-in is persisted against the identity.
    captured = _signed_signin(monkeypatch, "u5", {"user_token": "push-tok-xyz"})
    assert captured == [("telegram", "55", "push-tok-xyz")]
    app.signin_payloads.pop("u5", None)


def test_signin_captures_token_even_when_link_fails(monkeypatch):
    # A transient identity.link failure must NOT drop an issued push token
    # (Greptile #139) — set_user_token no-ops on a missing row anyway.
    captured = _signed_signin(monkeypatch, "u7", {"user_token": "push-tok-xyz"}, link_ok=False)
    assert captured == [("telegram", "55", "push-tok-xyz")]
    app.signin_payloads.pop("u7", None)


def test_signin_without_token_skips_capture(monkeypatch):
    # No issued token (user declined push / desktop) → no persistence attempt.
    captured = _signed_signin(monkeypatch, "u6", {"user_token": None})
    assert captured == []
    app.signin_payloads.pop("u6", None)
