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
    def __init__(self, token, match_info=None):
        self.headers = {"Authorization": f"Bearer {token}"}
        self.match_info = match_info or {}
        self._store: dict = {}

    def __getitem__(self, k):
        return self._store[k]

    def __setitem__(self, k, v):
        self._store[k] = v


# ---- Task 3: /api/me opportunistically refreshes the display handle ----


def test_handle_me_refreshes_handle(monkeypatch):
    monkeypatch.setattr(app.config, "WEBAPP_DEV_MODE", False)
    captured = {}

    def fake_touch(platform, uid, handle):
        captured["args"] = (platform, uid, handle)

    monkeypatch.setattr(app.identity_store, "touch_handle", fake_touch)

    async def fake_resolve(platform, uid):
        return "rWALLET"

    monkeypatch.setattr(app, "_resolve_wallet", fake_resolve)

    token = make_session_token({"id": "55", "name": "alice_now", "platform": "telegram"})
    resp = _run(app.handle_me(_Req(token)))
    assert resp.status == 200
    import json

    body = json.loads(resp.body.decode())
    assert body == {"id": "55", "username": "alice_now", "wallet": "rWALLET"}
    # refreshed with the token's current name, under the right platform
    assert captured["args"] == ("telegram", "55", "alice_now")


# ---- Task 4: GET /api/account ----


def test_account_returns_caller_identities(monkeypatch):
    monkeypatch.setattr(app.config, "WEBAPP_DEV_MODE", False)

    async def fake_resolve(platform, uid):
        return "rRESOLVED"

    monkeypatch.setattr(app, "_resolve_wallet", fake_resolve)

    seen = {}

    def fake_for_wallet(wallet):
        seen["wallet"] = wallet
        return [
            {"platform": "discord", "platform_user_id": "D", "display_handle": "alice"},
            {"platform": "telegram", "platform_user_id": "T", "display_handle": "alice_tg"},
        ]

    monkeypatch.setattr(app.identity_store, "identities_for_wallet", fake_for_wallet)

    token = make_session_token({"id": "D", "name": "alice", "platform": "discord"})
    resp = _run(app.handle_account(_Req(token)))
    assert resp.status == 200
    import json

    body = json.loads(resp.body.decode())
    assert body["wallet"] == "rRESOLVED"
    assert {i["platform"] for i in body["identities"]} == {"discord", "telegram"}
    # used the resolved wallet, never a client-supplied one
    assert seen["wallet"] == "rRESOLVED"


def test_account_no_wallet_400(monkeypatch):
    monkeypatch.setattr(app.config, "WEBAPP_DEV_MODE", False)

    async def fake_resolve(platform, uid):
        return None

    monkeypatch.setattr(app, "_resolve_wallet", fake_resolve)
    token = make_session_token({"id": "D", "name": "alice", "platform": "discord"})
    resp = _run(app.handle_account(_Req(token)))
    assert resp.status == 400
