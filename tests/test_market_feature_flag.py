# MARKET_ENABLED feature flag: the in-app marketplace (#44) ships after the
# mainnet MVP (money-touching list/buy/cancel), so the service must be able to
# run with the whole market surface OFF — routes answer 403 feature-disabled
# and /api/config tells the client to hide the UI. Mirrors the ECONOMY_ENABLED
# flag (see test_economy_feature_flag.py).

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
    def __init__(self, token=None, body=None, query=None, match_info=None):
        self.headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._body = body or {}
        self._store: dict = {}
        self.query = query or {}
        self.match_info = match_info or {}

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


def _assert_disabled(resp):
    assert resp.status == 403
    body = json.loads(resp.body)
    assert body.get("code") == "market_disabled"


def test_config_default_is_enabled():
    assert app.config.MARKET_ENABLED is True


def test_api_config_reports_market_enabled(monkeypatch):
    monkeypatch.setattr(app.config, "MARKET_ENABLED", False)
    resp = _run(app.handle_config(_Req()))
    assert json.loads(resp.body)["market_enabled"] is False


def test_market_public_reads_disabled_return_403(monkeypatch):
    monkeypatch.setattr(app.config, "MARKET_ENABLED", False)
    monkeypatch.setattr(app.config, "WEBAPP_DEV_MODE", False)
    _assert_disabled(_run(app.handle_market_listings(_Req(query={"kind": "character"}))))
    _assert_disabled(_run(app.handle_market_history(_Req(query={"nft_id": "x"}))))


def test_market_wallet_routes_disabled_return_403(monkeypatch):
    monkeypatch.setattr(app.config, "MARKET_ENABLED", False)
    monkeypatch.setattr(app.config, "WEBAPP_DEV_MODE", False)
    for handler in (
        app.handle_market_mine,
        app.handle_market_list_start,
        app.handle_market_cancel_start,
        app.handle_market_buy_start,
        app.handle_market_trait_list_start,
    ):
        _assert_disabled(_run(handler(_wallet_req({}))))


def test_market_status_routes_disabled_return_403(monkeypatch):
    monkeypatch.setattr(app.config, "MARKET_ENABLED", False)
    monkeypatch.setattr(app.config, "WEBAPP_DEV_MODE", False)
    for handler in (
        app.handle_market_list_status,
        app.handle_market_cancel_status,
        app.handle_market_buy_status,
        app.handle_market_trait_list_status,
    ):
        _assert_disabled(_run(handler(_Req(match_info={"session_id": "nope"}))))


def test_client_hides_market_when_disabled():
    # No-build vanilla JS client: assert the source reads market_enabled from
    # /api/config and hides the Marketplace entry point when it is false.
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "webapp", "client", "app.js"), encoding="utf-8") as f:
        src = f.read()
    assert "market_enabled" in src
    assert "market-btn" in src  # the Marketplace button it must hide
