# tests/test_mint_active_resume.py
# Mint session resume: Discord mobile kills/reloads the Activity webview when
# the user app-switches to Xaman to sign the payment, and the relaunched client
# loses its in-memory currentMintId — the user is dumped to the home screen
# mid-mint ("keeps kicking me out before it mints"). The server still holds the
# live session, so GET /api/mint/active lets the relaunched client re-attach.
#
# Env-guard preamble: importing lfg_service.app freezes lfg_core.config
# constants at import time; set the same defaults test_smoke.py /
# test_server_identity_wiring.py use so collection order can't strand them.
# (Copy the block verbatim from tests/test_server_identity_wiring.py.)
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

import asyncio  # noqa: E402
import json  # noqa: E402

import pytest  # noqa: E402
from aiohttp import web  # noqa: E402
from aiohttp.test_utils import make_mocked_request  # noqa: E402

from lfg_core import mint_flow  # noqa: E402
from lfg_service import app as server  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _request():
    return make_mocked_request("GET", "/api/mint/active", app=web.Application())


async def _read_json(resp):
    return json.loads(resp.body.decode())


@pytest.fixture
def dev_auth(monkeypatch):
    """require_auth in dev mode injects user {'id': 'dev'} (platform discord)."""
    monkeypatch.setattr(server.config, "WEBAPP_DEV_MODE", True)
    monkeypatch.setattr(server, "mint_sessions", {})
    return server.mint_sessions


def _session(discord_id="dev", platform="discord", state=mint_flow.AWAITING_PAYMENT):
    s = mint_flow.MintSession(discord_id=discord_id, wallet_address="rTest", platform=platform)
    s.state = state
    return s


def test_active_route_resolves_to_active_handler(dev_auth):
    """/api/mint/active must NOT be swallowed by /api/mint/{session_id}
    (aiohttp dispatches in registration order): a GET with no live session
    answers 200 {"session": null}, not the status handler's 404."""
    app = server.create_app()
    match = _run(app.router.resolve(_request()))
    assert getattr(match, "http_exception", None) is None
    # Assert the actual dispatch target: a registration-order regression would
    # route here through handle_mint_status with session_id='active'.
    assert match.handler is server.handle_mint_active
    resp = _run(server.handle_mint_active(_request()))
    assert resp.status == 200
    assert _run(_read_json(resp)) == {"session": None}


def test_active_returns_live_session(dev_auth):
    s = _session()
    dev_auth[s.id] = s
    resp = _run(server.handle_mint_active(_request()))
    assert resp.status == 200
    body = _run(_read_json(resp))
    assert body["session"]["id"] == s.id
    assert body["session"]["state"] == mint_flow.AWAITING_PAYMENT


def test_active_ignores_terminal_sessions(dev_auth):
    for state in mint_flow.TERMINAL_STATES:
        s = _session(state=state)
        dev_auth[s.id] = s
    resp = _run(server.handle_mint_active(_request()))
    assert _run(_read_json(resp)) == {"session": None}


def test_active_ignores_other_users_sessions(dev_auth):
    s = _session(discord_id="someone-else")
    dev_auth[s.id] = s
    resp = _run(server.handle_mint_active(_request()))
    assert _run(_read_json(resp)) == {"session": None}


def test_active_ignores_other_platform_sessions(dev_auth):
    s = _session(platform="telegram")
    dev_auth[s.id] = s
    resp = _run(server.handle_mint_active(_request()))
    assert _run(_read_json(resp)) == {"session": None}
