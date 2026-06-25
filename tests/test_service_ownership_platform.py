"""
BV2: ownership / one-active-session checks compare (platform, id).

Tests verify that:
- _active_session distinguishes platform so a discord:55 session does NOT
  block a telegram:55 user from starting a new session.
- handle_mint_status rejects a cross-platform read (telegram session ≠
  discord token with the same user id) with 404.
- Same-platform regression: a discord token CAN still read its own discord
  session (backward compat).
"""

import asyncio
import types

import lfg_core.mint_flow as mint_flow
import lfg_service.app as app
from lfg_service.app import make_session_token


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# _active_session distinguishes platform
# ---------------------------------------------------------------------------


def test_active_session_distinguishes_platform():
    sessions = {}
    s = mint_flow.MintSession("55", "rA", platform="discord")
    sessions[s.id] = s
    # a discord:55 active session must NOT count as active for telegram:55
    assert app._active_session(sessions, mint_flow.TERMINAL_STATES, "55", "telegram") is None
    # ...but DOES for discord:55
    assert app._active_session(sessions, mint_flow.TERMINAL_STATES, "55", "discord") is s


def test_active_session_no_platform_still_matches():
    """When platform arg is omitted (None), match on id only — backward compat."""
    sessions = {}
    s = mint_flow.MintSession("77", "rB", platform="discord")
    sessions[s.id] = s
    assert app._active_session(sessions, mint_flow.TERMINAL_STATES, "77") is s


# ---------------------------------------------------------------------------
# handle_mint_status ownership — cross-platform rejection
# ---------------------------------------------------------------------------


class _MockRequest:
    """Minimal fake request object for handle_mint_status."""

    def __init__(self, session_id: str, token: str):
        self.headers = {"Authorization": f"Bearer {token}"}
        self.match_info = {"session_id": session_id}
        self._store: dict = {}

    def __getitem__(self, k):
        return self._store[k]

    def __setitem__(self, k, v):
        self._store[k] = v


def test_mint_status_rejects_cross_platform(monkeypatch):
    """A discord token with id=55 must NOT be able to read a telegram:55 session."""
    monkeypatch.setattr(app.config, "WEBAPP_DEV_MODE", False)
    s = mint_flow.MintSession("55", "rA", platform="telegram")
    app.mint_sessions[s.id] = s
    try:
        discord_token = make_session_token({"id": "55", "name": "d", "platform": "discord"})
        resp = _run(app.handle_mint_status(_MockRequest(s.id, discord_token)))
        assert resp.status == 404
    finally:
        app.mint_sessions.pop(s.id, None)


def test_mint_status_same_platform_regression(monkeypatch):
    """A discord token with id=55 MUST still be able to read its own discord:55 session."""
    monkeypatch.setattr(app.config, "WEBAPP_DEV_MODE", False)
    s = mint_flow.MintSession("55", "rA", platform="discord")
    app.mint_sessions[s.id] = s
    try:
        discord_token = make_session_token({"id": "55", "name": "d", "platform": "discord"})
        resp = _run(app.handle_mint_status(_MockRequest(s.id, discord_token)))
        # Fresh session is AWAITING_PAYMENT with no payment_uuid, so
        # update_scan_state returns early (no I/O) and the handler returns 200.
        assert resp.status == 200
    finally:
        app.mint_sessions.pop(s.id, None)


# ---------------------------------------------------------------------------
# handle_mint_regenerate ownership — cross-platform rejection (state-mutating)
# ---------------------------------------------------------------------------


def test_mint_regenerate_rejects_cross_platform(monkeypatch):
    """A discord token must NOT be able to regenerate a telegram:55 session's QR."""
    monkeypatch.setattr(app.config, "WEBAPP_DEV_MODE", False)
    s = mint_flow.MintSession("55", "rA", platform="telegram")
    app.mint_sessions[s.id] = s
    try:
        discord_token = make_session_token({"id": "55", "name": "d", "platform": "discord"})
        resp = _run(app.handle_mint_regenerate(_MockRequest(s.id, discord_token)))
        assert resp.status == 404
    finally:
        app.mint_sessions.pop(s.id, None)


# ---------------------------------------------------------------------------
# make_status_handler (generic — swap/economy status path) cross-platform
# ---------------------------------------------------------------------------


def _make_session_like(discord_id: str, platform: str):
    return types.SimpleNamespace(
        discord_id=discord_id,
        platform=platform,
        to_dict=lambda: {"id": "x", "state": "ok"},
    )


def test_make_status_handler_rejects_cross_platform(monkeypatch):
    """The generic status handler (swap/economy) rejects a cross-platform read."""
    monkeypatch.setattr(app.config, "WEBAPP_DEV_MODE", False)
    sessions = {"sid1": _make_session_like("55", "telegram")}
    handler = app.make_status_handler(sessions)
    discord_token = make_session_token({"id": "55", "name": "d", "platform": "discord"})
    resp = _run(handler(_MockRequest("sid1", discord_token)))
    assert resp.status == 404


def test_make_status_handler_same_platform(monkeypatch):
    """The generic status handler lets a discord token read its own discord session."""
    monkeypatch.setattr(app.config, "WEBAPP_DEV_MODE", False)
    sessions = {"sid1": _make_session_like("55", "discord")}
    handler = app.make_status_handler(sessions)
    discord_token = make_session_token({"id": "55", "name": "d", "platform": "discord"})
    resp = _run(handler(_MockRequest("sid1", discord_token)))
    assert resp.status == 200
