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
