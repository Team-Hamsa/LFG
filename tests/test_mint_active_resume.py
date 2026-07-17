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


# --- #262: fail fast when the payment payload was never created --------------


def _post_start_request():
    """POST /api/mint stand-in (mirrors tests/test_bulk_mint_service.py's
    _post_request): stub request.json() for _request_return_url."""
    req = make_mocked_request("POST", "/api/mint", app=web.Application())

    async def _json():
        return {}

    req.json = _json  # type: ignore[method-assign]
    return req


async def _prepare_static_link_only(self):
    """prepare_payment 'succeeded' but XUMM never created the sign request:
    the static detect link is set (it always is — do NOT gate on it), while
    payment_uuid stays None. Exactly the prod-incident shape (#262)."""
    self.pay_with, self.pay_amount = "XRP", server.config.MINT_PRICE_XRP
    self.payment_link = "https://xaman.app/detect/request:rBot"


def _record_publishes(monkeypatch):
    events = []

    async def fake_publish(type_, identity_obj, wallet, data):
        events.append(type_)

    monkeypatch.setattr(server, "publish_event", fake_publish)
    return events


def test_mint_start_fails_fast_without_payment_payload(dev_auth, monkeypatch):
    """#262 (prod incident 2026-07-17): XUMM 429'd during payload creation and
    the user sat 300s on a dead pay screen. The start handler must model the
    bulk fail-closed pattern (handle_bulk_mint_start): mark the session
    terminal FAILED (frees the one-active-session slot), spawn NO background
    task, and answer via _xumm_unavailable_response — 503 + rate_limited while
    XUMM is rate limiting us."""
    monkeypatch.setattr(mint_flow.MintSession, "prepare_payment", _prepare_static_link_only)
    monkeypatch.setattr(server.xumm_ops, "rate_limited", lambda: True)
    events = _record_publishes(monkeypatch)

    resp = _run(server.handle_mint_start(_post_start_request()))
    assert resp.status == 503
    assert resp.headers["Retry-After"] == "30"
    body = _run(_read_json(resp))
    assert body["code"] == "rate_limited"

    (session,) = dev_auth.values()
    assert session.state == mint_flow.FAILED
    assert session.error
    assert session.task is None  # run_mint_session never launched
    # FAILED is terminal, so the user's one-active-session slot is free.
    assert server._active_session(dev_auth, mint_flow.TERMINAL_STATES, "dev", "discord") is None
    # The admin-log firehose still sees the blocked attempt (the 503'd client
    # never polls, so the guard's publish is the only site).
    assert events == ["mint.failed"]


def test_mint_start_fails_fast_502_when_not_rate_limited(dev_auth, monkeypatch):
    """Same guard outside a 429 window: plain 'could not reach Xaman' 502."""
    monkeypatch.setattr(mint_flow.MintSession, "prepare_payment", _prepare_static_link_only)
    monkeypatch.setattr(server.xumm_ops, "rate_limited", lambda: False)

    resp = _run(server.handle_mint_start(_post_start_request()))
    assert resp.status == 502
    (session,) = dev_auth.values()
    assert session.state == mint_flow.FAILED
    assert session.task is None


def test_mint_start_fail_fast_preserves_concurrent_cancel(dev_auth, monkeypatch):
    """#262 guard's cancel-preservation branch: the session is discoverable
    via /api/mint/active during the (up to 8s) awaited prepare_payment, so a
    second tab can cancel it mid-prepare. The guard must not overwrite that
    terminal CANCELLED with FAILED — and mark_published (set by
    handle_mint_cancel) keeps the deliberate cancel out of the firehose."""

    async def cancelled_mid_prepare(self):
        await _prepare_static_link_only(self)
        # Simulate handle_mint_cancel landing during the prepare window
        # (its exact two steps: cancel() then mark_published()).
        assert self.cancel()
        self.mark_published()

    monkeypatch.setattr(mint_flow.MintSession, "prepare_payment", cancelled_mid_prepare)
    monkeypatch.setattr(server.xumm_ops, "rate_limited", lambda: True)
    events = _record_publishes(monkeypatch)

    resp = _run(server.handle_mint_start(_post_start_request()))
    assert resp.status == 503  # response shape is unchanged either way
    (session,) = dev_auth.values()
    assert session.state == mint_flow.CANCELLED  # survives, not FAILED
    assert not session.error
    assert session.task is None
    assert events == []  # deliberate cancels announce nothing (#141)
