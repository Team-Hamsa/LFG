# /api/health reports in-flight (non-terminal) session counts so a deploy can
# drain before restarting instead of killing users mid-mint (#activity-drain).

import asyncio
import json
import os
import sys

os.environ.setdefault("XUMM_API_KEY", "test")
os.environ.setdefault("XUMM_API_SECRET", "test")
os.environ.setdefault("SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2")
os.environ.setdefault("TOKEN_ISSUER_ADDRESS", "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
os.environ.setdefault("TOKEN_CURRENCY_HEX", "4C46474F00000000000000000000000000000000")
os.environ.setdefault("BUNNY_CDN_ACCESS_KEY", "test")
os.environ.setdefault("BUNNY_CDN_STORAGE_ZONE", "test")
os.environ.setdefault("LAYER_SOURCE", "local")
os.environ.setdefault("BUNNY_PULL_ZONE", "nft.pullzone.example")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lfg_service.app as app  # noqa: E402
from lfg_core import mint_flow  # noqa: E402


class _Req:
    def __init__(self):
        self.headers = {}
        self.match_info = {}


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _S:
    def __init__(self, state):
        self.state = state


def _body(resp):
    return json.loads(resp.body)


def test_health_zero_when_idle(monkeypatch):
    monkeypatch.setattr(app, "mint_sessions", {}, raising=False)
    monkeypatch.setattr(app, "swap_sessions", {}, raising=False)
    monkeypatch.setattr(app, "economy_sessions", {}, raising=False)
    monkeypatch.setattr(app, "market_sessions", {}, raising=False)
    body = _body(_run(app.handle_health(_Req())))
    assert body["ok"] is True
    assert body["active_sessions"] == 0


def test_health_counts_only_non_terminal(monkeypatch):
    # one in-flight mint + one already-terminal mint -> count is 1.
    in_flight = "in_flight_state_not_terminal"
    assert in_flight not in mint_flow.TERMINAL_STATES
    terminal = next(iter(mint_flow.TERMINAL_STATES))
    monkeypatch.setattr(
        app, "mint_sessions", {"a": _S(in_flight), "b": _S(terminal)}, raising=False
    )
    monkeypatch.setattr(app, "swap_sessions", {}, raising=False)
    monkeypatch.setattr(app, "economy_sessions", {}, raising=False)
    monkeypatch.setattr(app, "market_sessions", {}, raising=False)
    body = _body(_run(app.handle_health(_Req())))
    assert body["detail"]["mint"] == 1
    assert body["active_sessions"] == 1
