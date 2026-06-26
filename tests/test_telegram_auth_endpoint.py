# tests/test_telegram_auth_endpoint.py
# POST /api/telegram/auth (#89, Part A): validate Telegram initData and mint a
# platform="telegram" session token. 503 when no bot token is configured; never
# touches the wallet/identity store; preserves cross-surface isolation.
import asyncio
import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import lfg_service.app as app
from lfg_service.app import make_session_token, verify_session_token

DUMMY_TOKEN = "123456:TEST-FAKE-TOKEN"


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _sign(fields: dict, bot_token: str) -> str:
    dcs = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    h = hmac.new(secret_key, dcs.encode(), hashlib.sha256).hexdigest()
    return urlencode({**fields, "hash": h})


def _valid_init_data(token: str, uid: int = 55, username: str = "alice") -> str:
    return _sign(
        {
            "auth_date": str(int(time.time())),
            "query_id": "AAEUR",
            "user": json.dumps({"id": uid, "username": username}),
        },
        token,
    )


class _Req:
    def __init__(self, body):
        self._body = body
        self.headers = {}
        self.match_info: dict = {}
        self._store: dict = {}

    async def json(self):
        return self._body

    def __getitem__(self, k):
        return self._store[k]

    def __setitem__(self, k, v):
        self._store[k] = v


def test_auth_returns_telegram_session_token(monkeypatch):
    monkeypatch.setattr(app.config, "TELEGRAM_BOT_TOKEN", DUMMY_TOKEN)
    monkeypatch.setattr(app.config, "TELEGRAM_INITDATA_MAX_AGE", 3600)
    init_data = _valid_init_data(DUMMY_TOKEN)
    resp = _run(app.handle_telegram_auth(_Req({"init_data": init_data})))
    assert resp.status == 200
    payload = json.loads(resp.body)
    token = payload["session_token"]
    decoded = verify_session_token(token)
    assert decoded is not None
    assert decoded["platform"] == "telegram"
    assert decoded["id"] == "55"
    assert payload["user"]["id"] == "55"
    assert payload["user"]["username"] == "alice"


def test_minted_token_accepted_by_require_auth(monkeypatch):
    monkeypatch.setattr(app.config, "TELEGRAM_BOT_TOKEN", DUMMY_TOKEN)
    monkeypatch.setattr(app.config, "TELEGRAM_INITDATA_MAX_AGE", 3600)
    monkeypatch.setattr(app.config, "WEBAPP_DEV_MODE", False)
    init_data = _valid_init_data(DUMMY_TOKEN)
    resp = _run(app.handle_telegram_auth(_Req({"init_data": init_data})))
    token = json.loads(resp.body)["session_token"]

    # A trivial protected handler proves require_auth admits the minted token.
    captured = {}

    @app.require_auth
    async def protected(request):
        captured["user"] = request["user"]
        from aiohttp import web

        return web.json_response({"ok": True})

    class _AuthReq:
        def __init__(self, tok):
            self.headers = {"Authorization": f"Bearer {tok}"}
            self._store: dict = {}

        def __getitem__(self, k):
            return self._store[k]

        def __setitem__(self, k, v):
            self._store[k] = v

    r = _run(protected(_AuthReq(token)))
    assert r.status == 200
    assert captured["user"]["platform"] == "telegram"
    assert captured["user"]["id"] == "55"


def test_auth_rejects_bad_initdata(monkeypatch):
    monkeypatch.setattr(app.config, "TELEGRAM_BOT_TOKEN", DUMMY_TOKEN)
    monkeypatch.setattr(app.config, "TELEGRAM_INITDATA_MAX_AGE", 3600)
    resp = _run(app.handle_telegram_auth(_Req({"init_data": "garbage=1&hash=deadbeef"})))
    assert resp.status == 401


def test_auth_503_when_unconfigured(monkeypatch):
    monkeypatch.setattr(app.config, "TELEGRAM_BOT_TOKEN", "")
    init_data = _valid_init_data(DUMMY_TOKEN)
    resp = _run(app.handle_telegram_auth(_Req({"init_data": init_data})))
    assert resp.status == 503


def test_auth_does_not_register_or_link(monkeypatch):
    monkeypatch.setattr(app.config, "TELEGRAM_BOT_TOKEN", DUMMY_TOKEN)
    monkeypatch.setattr(app.config, "TELEGRAM_INITDATA_MAX_AGE", 3600)

    def boom_register(*a, **k):
        raise AssertionError("register_user must NOT be called by /api/telegram/auth")

    def boom_link(*a, **k):
        raise AssertionError("identity_store.link must NOT be called by /api/telegram/auth")

    monkeypatch.setattr(app, "register_user", boom_register)
    monkeypatch.setattr(app.identity_store, "link", boom_link)
    init_data = _valid_init_data(DUMMY_TOKEN)
    resp = _run(app.handle_telegram_auth(_Req({"init_data": init_data})))
    assert resp.status == 200


def test_telegram_token_cannot_read_discord_session(monkeypatch):
    # Cross-surface isolation (spec §8): a telegram:55 token minted via
    # /api/telegram/auth must NOT be able to read a discord:55-owned payload.
    monkeypatch.setattr(app.config, "TELEGRAM_BOT_TOKEN", DUMMY_TOKEN)
    monkeypatch.setattr(app.config, "TELEGRAM_INITDATA_MAX_AGE", 3600)
    monkeypatch.setattr(app.config, "WEBAPP_DEV_MODE", False)
    init_data = _valid_init_data(DUMMY_TOKEN, uid=55)
    resp = _run(app.handle_telegram_auth(_Req({"init_data": init_data})))
    tg_token = json.loads(resp.body)["session_token"]

    # Seed a discord:55-owned signin payload.
    app.signin_payloads["tgx"] = {
        "platform": "discord",
        "user_id": "55",
        "name": "d",
        "created_at": time.time(),
    }

    class _AuthReq:
        def __init__(self, tok, match_info):
            self.headers = {"Authorization": f"Bearer {tok}"}
            self.match_info = match_info
            self._store: dict = {}

        def __getitem__(self, k):
            return self._store[k]

        def __setitem__(self, k, v):
            self._store[k] = v

    r = _run(app.handle_signin_status(_AuthReq(tg_token, {"payload_uuid": "tgx"})))
    assert r.status == 404
    app.signin_payloads.pop("tgx", None)


def _make_session_token_default_platform():
    # Sanity: tokens minted without a platform default to discord (regression).
    tok = make_session_token({"id": "1", "name": "x"})
    assert verify_session_token(tok)["platform"] == "discord"
