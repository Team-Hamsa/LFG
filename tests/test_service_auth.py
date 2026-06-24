import asyncio

import lfg_service.auth as auth


def test_surface_for_token(monkeypatch):
    monkeypatch.setenv("SERVICE_TOKEN_DISCORD", "tok-d")
    monkeypatch.setenv("SERVICE_TOKEN_TELEGRAM", "tok-t")
    assert auth.surface_for_token("tok-d") == "discord"
    assert auth.surface_for_token("tok-t") == "telegram"
    assert auth.surface_for_token("nope") is None
    assert auth.surface_for_token(None) is None


class _FakeRequest:
    def __init__(self, headers):
        self.headers = headers
        self._store = {}

    def __setitem__(self, key, value):
        self._store[key] = value

    def __getitem__(self, key):
        return self._store[key]


def _fake_request(headers):
    return _FakeRequest(headers)


def test_require_service_token_rejects_missing(monkeypatch):
    monkeypatch.setenv("SERVICE_TOKEN_DISCORD", "tok-d")

    @auth.require_service_token
    async def handler(request):
        return "ok"

    resp = asyncio.run(handler(_fake_request({})))
    assert resp.status == 401


def test_require_service_token_accepts_valid_and_tags_surface(monkeypatch):
    monkeypatch.setenv("SERVICE_TOKEN_DISCORD", "tok-d")
    seen = {}

    @auth.require_service_token
    async def handler(request):
        seen["surface"] = request["surface"]
        return "ok"

    result = asyncio.run(handler(_fake_request({"Authorization": "Bearer tok-d"})))
    assert result == "ok"
    assert seen["surface"] == "discord"
