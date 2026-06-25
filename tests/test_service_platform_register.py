import asyncio

import lfg_service.app as app
from lfg_service.app import make_session_token


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _Req:
    def __init__(self, token, body):
        self.headers = {"Authorization": f"Bearer {token}"}
        self._body = body
        self._store: dict = {}
        self.match_info: dict = {}

    async def json(self):
        return self._body

    def __getitem__(self, k):
        return self._store[k]

    def __setitem__(self, k, v):
        self._store[k] = v


def test_register_links_under_token_platform(monkeypatch):
    linked = {}
    monkeypatch.setattr(app, "is_valid_classic_address", lambda w: True)
    monkeypatch.setattr(app, "register_user", lambda uid, name, w: True)

    def fake_link(platform, uid, name, wallet):
        linked["args"] = (platform, uid, wallet)
        return True

    monkeypatch.setattr(app.identity_store, "link", fake_link)
    monkeypatch.setattr(app.config, "WEBAPP_DEV_MODE", False)

    token = make_session_token({"id": "55", "name": "tg", "platform": "telegram"})
    resp = _run(app.handle_register(_Req(token, {"wallet": "rXRPL"})))
    assert resp.status == 200
    assert linked["args"] == ("telegram", "55", "rXRPL")


def test_register_telegram_skips_legacy_users(monkeypatch):
    """A non-discord (telegram) registration must NOT touch the legacy Users
    table (keyed by discord_id) — only identities. Prevents a colliding numeric
    id from overwriting a discord user's wallet (and being mismigrated into
    identities as a discord row on the next startup)."""
    legacy = {"called": False}
    linked = {}
    monkeypatch.setattr(app, "is_valid_classic_address", lambda w: True)

    def fake_register_user(uid, name, w):
        legacy["called"] = True
        return True

    def fake_link(platform, uid, name, wallet):
        linked["args"] = (platform, uid, wallet)
        return True

    monkeypatch.setattr(app, "register_user", fake_register_user)
    monkeypatch.setattr(app.identity_store, "link", fake_link)
    monkeypatch.setattr(app.config, "WEBAPP_DEV_MODE", False)

    token = make_session_token({"id": "55", "name": "tg", "platform": "telegram"})
    resp = _run(app.handle_register(_Req(token, {"wallet": "rXRPL"})))
    assert resp.status == 200
    assert legacy["called"] is False
    assert linked["args"] == ("telegram", "55", "rXRPL")


def test_register_discord_writes_legacy_users(monkeypatch):
    """A discord registration still writes the legacy Users table (regression)."""
    legacy = {}
    monkeypatch.setattr(app, "is_valid_classic_address", lambda w: True)

    def fake_register_user(uid, name, w):
        legacy["args"] = (uid, name, w)
        return True

    monkeypatch.setattr(app, "register_user", fake_register_user)
    monkeypatch.setattr(app.identity_store, "link", lambda *a: True)
    monkeypatch.setattr(app.config, "WEBAPP_DEV_MODE", False)

    token = make_session_token({"id": "9", "name": "d", "platform": "discord"})
    resp = _run(app.handle_register(_Req(token, {"wallet": "rDISCORD"})))
    assert resp.status == 200
    assert legacy["args"] == ("9", "d", "rDISCORD")


def test_signin_status_telegram_skips_legacy_users(monkeypatch):
    """Mirror of the register gate for the second P1 call site: a telegram
    signed-payload status check must NOT write the legacy Users table — only
    identities. Guards against a future revert of the handle_signin_status gate."""
    legacy = {"called": False}
    linked = {}
    monkeypatch.setattr(app, "is_valid_classic_address", lambda w: True)
    monkeypatch.setattr(app.config, "WEBAPP_DEV_MODE", False)
    monkeypatch.setitem(app.signin_payloads, "u1", {"discord_id": "55", "name": "tg"})

    async def fake_status(uuid):
        return {"opened": True, "signed": True, "expired": False, "account": "rXRPL"}

    monkeypatch.setattr(app.xumm_ops, "get_payload_status", fake_status)

    def fake_register_user(uid, name, w):
        legacy["called"] = True
        return True

    monkeypatch.setattr(app, "register_user", fake_register_user)

    def fake_link(platform, uid, name, wallet):
        linked["args"] = (platform, uid, wallet)
        return True

    monkeypatch.setattr(app.identity_store, "link", fake_link)

    token = make_session_token({"id": "55", "name": "tg", "platform": "telegram"})
    req = _Req(token, {})
    req.match_info = {"payload_uuid": "u1"}
    resp = _run(app.handle_signin_status(req))
    assert resp.status == 200
    assert legacy["called"] is False
    assert linked["args"] == ("telegram", "55", "rXRPL")
