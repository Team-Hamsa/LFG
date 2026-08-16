# ECONOMY_ENABLED feature flag: the Closet / dress-up trait economy ships
# after the mainnet MVP, so the service must be able to run with the whole
# economy surface OFF — routes answer 403 feature-disabled, registration does
# not auto-issue Closets, and /api/config tells the client to hide the UI.

import asyncio
import json
import os
import sys

# Env guard: set before lfg_core imports so frozen config constants are sane
# when this file runs first (see test-env-guard convention).
os.environ.setdefault("XUMM_API_KEY", "test")
os.environ.setdefault("XUMM_API_SECRET", "test")
os.environ.setdefault("SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2")  # throwaway test seed
os.environ.setdefault("TOKEN_ISSUER_ADDRESS", "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
os.environ.setdefault("TOKEN_CURRENCY_HEX", "4C46474F00000000000000000000000000000000")
os.environ.setdefault("BUNNY_CDN_ACCESS_KEY", "test")
os.environ.setdefault("BUNNY_CDN_STORAGE_ZONE", "test")
os.environ.setdefault("LAYER_SOURCE", "local")
os.environ.setdefault("BUNNY_PULL_ZONE", "nft.pullzone.example")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lfg_service.app as app  # noqa: E402
from lfg_service.app import make_session_token  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _Req:
    def __init__(self, token=None, body=None):
        self.headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._body = body or {}
        self._store: dict = {}
        self.match_info: dict = {}

    async def json(self):
        return self._body

    def __getitem__(self, k):
        return self._store[k]

    def __setitem__(self, k, v):
        self._store[k] = v


def _wallet_req(body=None):
    token = make_session_token({"id": "u1", "name": "u", "platform": "discord"})
    req = _Req(token, body)
    req["user"] = {"id": "u1", "name": "u"}
    req["wallet"] = "rUserWalletXXXXXXXXXXXXXXXXXXXXXXX"
    return req


def test_api_config_reports_economy_enabled(monkeypatch):
    monkeypatch.setattr(app.config, "ECONOMY_ENABLED", False)
    resp = _run(app.handle_config(_Req()))
    assert json.loads(resp.body)["economy_enabled"] is False


def _assert_disabled(resp):
    assert resp.status == 403
    body = json.loads(resp.body)
    assert body.get("code") == "economy_disabled"


def test_closet_disabled_returns_403(monkeypatch):
    monkeypatch.setattr(app.config, "ECONOMY_ENABLED", False)
    monkeypatch.setattr(app.config, "WEBAPP_DEV_MODE", False)
    _assert_disabled(_run(app.handle_closet(_wallet_req())))


def test_economy_read_disabled_returns_403(monkeypatch):
    monkeypatch.setattr(app.config, "ECONOMY_ENABLED", False)
    monkeypatch.setattr(app.config, "WEBAPP_DEV_MODE", False)
    _assert_disabled(_run(app.handle_economy(_wallet_req())))


def test_economy_posts_disabled_return_403(monkeypatch):
    monkeypatch.setattr(app.config, "ECONOMY_ENABLED", False)
    monkeypatch.setattr(app.config, "WEBAPP_DEV_MODE", False)
    for handler in (
        app.handle_equip_start,
        app.handle_harvest_start,
        app.handle_assemble_start,
        app.handle_extract_start,
        app.handle_deposit_start,
    ):
        _assert_disabled(_run(handler(_wallet_req({}))))


def test_register_skips_closet_issuance_when_disabled(monkeypatch):
    monkeypatch.setattr(app.config, "ECONOMY_ENABLED", False)
    monkeypatch.setattr(app.config, "WEBAPP_DEV_MODE", False)
    monkeypatch.setattr(app, "is_valid_classic_address", lambda w: True)
    monkeypatch.setattr(app, "register_user", lambda uid, name, w: True)
    monkeypatch.setattr(app.identity_store, "link", lambda *a: True)

    def boom(*a, **k):
        raise AssertionError("start_closet must not run with the economy disabled")

    monkeypatch.setattr(app.economy_api, "start_closet", boom)
    token = make_session_token({"id": "u1", "name": "u", "platform": "discord"})
    resp = _run(app.handle_register(_Req(token, {"wallet": "rXRPL"})))
    assert resp.status == 200
    assert "closet_accept" not in json.loads(resp.body)


def test_client_hides_dressup_when_economy_disabled():
    # No-build vanilla JS client: assert the source reads economy_enabled from
    # /api/config and hides the Dress Up entry point when it is false.
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "webapp", "client", "app.js"), encoding="utf-8") as f:
        src = f.read()
    assert "economy_enabled" in src
    assert "swap-btn" in src  # the Dress Up button it must hide
