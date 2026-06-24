# Env-guard preamble: importing lfg_service.app freezes lfg_core.config constants
# at import time; set the same defaults test_smoke.py / test_server_identity_wiring.py
# use so collection order can't strand IMG_PROXY_ALLOWED_BASES. (Copy the block
# verbatim from tests/test_server_identity_wiring.py — same keys/values.)
import os

os.environ.setdefault("XUMM_API_KEY", "test")
os.environ.setdefault("XUMM_API_SECRET", "test")
os.environ.setdefault("SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2")
os.environ.setdefault("TOKEN_ISSUER_ADDRESS", "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
os.environ.setdefault("TOKEN_CURRENCY_HEX", "4C46474F00000000000000000000000000000000")
os.environ.setdefault("BUNNY_CDN_ACCESS_KEY", "test")
os.environ.setdefault("BUNNY_CDN_STORAGE_ZONE", "test")
os.environ.setdefault("LAYER_SOURCE", "local")
os.environ.setdefault("BUNNY_PULL_ZONE", "nft.pullzone.example")

import asyncio
import json

import lfg_service.identity as identity
from lfg_service import app as server


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _FakeRequest:
    def __init__(self, headers, body):
        self.headers = headers
        self._body = body
        self._store = {}

    def __setitem__(self, key, value):
        self._store[key] = value

    def __getitem__(self, key):
        return self._store[key]

    async def json(self):
        return self._body


def test_session_requires_service_token():
    resp = _run(
        server.handle_session(_FakeRequest({}, {"platform_user_id": "5", "platform_username": "x"}))
    )
    assert resp.status == 401


def test_session_issues_token(monkeypatch):
    monkeypatch.setenv("SERVICE_TOKEN_TELEGRAM", "tok-t")
    resp = _run(
        server.handle_session(
            _FakeRequest(
                {"Authorization": "Bearer tok-t"},
                {"platform_user_id": "5", "platform_username": "neo"},
            )
        )
    )
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body["user"] == {"id": "5", "username": "neo"}
    assert body["session_token"]


def test_session_missing_pid_returns_400(monkeypatch):
    monkeypatch.setenv("SERVICE_TOKEN_TELEGRAM", "tok-t")
    resp = _run(
        server.handle_session(
            _FakeRequest(
                {"Authorization": "Bearer tok-t"},
                {"platform_username": "neo"},  # no platform_user_id
            )
        )
    )
    assert resp.status == 400


def test_session_route_registered(tmp_path, monkeypatch):
    monkeypatch.setattr(identity, "DATABASE", str(tmp_path / "t.db"))
    app = server.create_app()
    paths = {
        route.resource.canonical for route in app.router.routes() if route.resource is not None
    }
    assert "/api/session" in paths
