# tests/test_web_cors.py
# Standalone web surface (spec 2026-07-16): CORS middleware is dark by default
# (no WEB_ALLOWED_ORIGINS → responses byte-identical to today); an allowlisted
# Origin gets ACAO + Vary and an OPTIONS preflight short-circuits to 204.
import asyncio
import os

os.environ.setdefault("BUNNY_PULL_ZONE", "nft.pullzone.example")
os.environ.setdefault("LAYER_SOURCE", "local")

from aiohttp import web
from aiohttp.test_utils import make_mocked_request

import lfg_service.app as app

ALLOWED = "https://build.letseffinggo.com"


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _ok(request):
    return web.json_response({"ok": True})


def _req(method="GET", origin=None):
    headers = {"Origin": origin} if origin else {}
    return make_mocked_request(method, "/api/config", headers=headers)


def test_no_allowlist_no_cors_headers(monkeypatch):
    monkeypatch.setattr(app.config, "WEB_ALLOWED_ORIGINS", ())
    resp = _run(app.cors_mw(_req(origin=ALLOWED), _ok))
    assert "Access-Control-Allow-Origin" not in resp.headers


def test_no_origin_header_untouched(monkeypatch):
    monkeypatch.setattr(app.config, "WEB_ALLOWED_ORIGINS", (ALLOWED,))
    resp = _run(app.cors_mw(_req(), _ok))
    assert "Access-Control-Allow-Origin" not in resp.headers


def test_allowed_origin_gets_acao_and_vary(monkeypatch):
    monkeypatch.setattr(app.config, "WEB_ALLOWED_ORIGINS", (ALLOWED,))
    resp = _run(app.cors_mw(_req(origin=ALLOWED), _ok))
    assert resp.headers["Access-Control-Allow-Origin"] == ALLOWED
    assert "Origin" in resp.headers.getall("Vary")
    assert resp.status == 200


def test_foreign_origin_gets_nothing(monkeypatch):
    monkeypatch.setattr(app.config, "WEB_ALLOWED_ORIGINS", (ALLOWED,))
    resp = _run(app.cors_mw(_req(origin="https://evil.example"), _ok))
    assert "Access-Control-Allow-Origin" not in resp.headers


def test_allowed_preflight_short_circuits(monkeypatch):
    monkeypatch.setattr(app.config, "WEB_ALLOWED_ORIGINS", (ALLOWED,))

    async def _boom(request):
        raise AssertionError("preflight must not reach the handler")

    resp = _run(app.cors_mw(_req("OPTIONS", origin=ALLOWED), _boom))
    assert resp.status == 204
    assert resp.headers["Access-Control-Allow-Origin"] == ALLOWED
    assert "Authorization" in resp.headers["Access-Control-Allow-Headers"]
    assert "POST" in resp.headers["Access-Control-Allow-Methods"]
    assert resp.headers["Access-Control-Max-Age"] == "3600"


def test_foreign_preflight_reaches_handler(monkeypatch):
    # A non-allowlisted OPTIONS is not ours to answer — pass it through so
    # aiohttp's normal 405 handling (or a real OPTIONS route) still applies.
    monkeypatch.setattr(app.config, "WEB_ALLOWED_ORIGINS", (ALLOWED,))
    resp = _run(app.cors_mw(_req("OPTIONS", origin="https://evil.example"), _ok))
    assert resp.status == 200
    assert "Access-Control-Allow-Origin" not in resp.headers
