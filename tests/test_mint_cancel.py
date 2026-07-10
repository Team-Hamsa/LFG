# tests/test_mint_cancel.py
# Issue #141: the mint pay/QR screen had no way to back out — a stuck
# awaiting_payment session held the per-user mint lock for the full
# PAYMENT_TIMEOUT_SECONDS. These tests drive the server-side cancel:
# POST /api/mint/{session_id}/cancel marks the session terminal (CANCELLED),
# stops the background task, and releases the one-active-session lock so a
# new mint can start immediately. Cancel of a completed session is a safe
# no-op; a mid-pipeline (post-payment) session cannot be cancelled.
#
# Env-guard preamble (verbatim from tests/test_swap_cross_body_api.py):
# importing lfg_core.config freezes its constants (e.g. LAYER_SOURCE,
# BUNNY_PULL_ZONE) at import time; set the same defaults test_smoke.py uses
# so collection order can't strand them.
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

import lfg_core.mint_flow as mint_flow  # noqa: E402
import lfg_service.app as app  # noqa: E402
from lfg_service.app import make_session_token  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _MockRequest:
    """Minimal fake request object (same shape as test_service_ownership_platform)."""

    def __init__(self, session_id: str, token: str):
        self.headers = {"Authorization": f"Bearer {token}"}
        self.match_info = {"session_id": session_id}
        self._store: dict = {}

    def __getitem__(self, k):
        return self._store[k]

    def __setitem__(self, k, v):
        self._store[k] = v


def _token(user_id: str = "55", platform: str = "discord") -> str:
    return make_session_token({"id": user_id, "name": "d", "platform": platform})


# ---------------------------------------------------------------------------
# State machine: CANCELLED is a terminal state; MintSession.cancel()
# ---------------------------------------------------------------------------


def test_cancelled_is_terminal_state():
    assert mint_flow.CANCELLED == "cancelled"
    assert mint_flow.CANCELLED in mint_flow.TERMINAL_STATES


def test_cancel_awaiting_payment_marks_terminal_and_stops_task():
    async def scenario():
        session = mint_flow.MintSession("55", "rA")
        assert session.state == mint_flow.AWAITING_PAYMENT
        # Stand in for run_mint_session's wait_for_payment poll
        session.task = asyncio.get_event_loop().create_task(asyncio.sleep(300))
        assert session.cancel() is True
        assert session.state == mint_flow.CANCELLED
        await asyncio.sleep(0)  # let the cancellation land
        assert session.task.cancelled()

    _run(scenario())


def test_cancel_is_refused_past_payment():
    session = mint_flow.MintSession("55", "rA")
    session.state = mint_flow.GENERATING
    assert session.cancel() is False
    assert session.state == mint_flow.GENERATING


def test_cancel_terminal_session_is_noop():
    session = mint_flow.MintSession("55", "rA")
    session.state = mint_flow.OFFER_READY
    assert session.cancel() is False
    assert session.state == mint_flow.OFFER_READY


def test_cancel_refused_once_payment_confirmed_mid_buy_and_burn(monkeypatch):
    """The moment wait_for_payment returns True the user's money is
    irrevocably taken, so cancel() must be refused from that instant —
    including during the multi-second buy_and_burn await that runs before
    any further pipeline stage. Regression test for the paid-but-still-
    cancellable window."""

    async def scenario():
        entered_buy_and_burn = asyncio.Event()
        release_buy_and_burn = asyncio.Event()

        async def fake_wait_for_payment(**kwargs):
            return True

        async def fake_buy_and_burn(*args, **kwargs):
            entered_buy_and_burn.set()
            await release_buy_and_burn.wait()
            return True

        monkeypatch.setattr(mint_flow.xrpl_ops, "wait_for_payment", fake_wait_for_payment)
        monkeypatch.setattr(mint_flow.xrpl_ops, "buy_and_burn", fake_buy_and_burn)

        session = mint_flow.MintSession("55", "rA")
        session.pay_with, session.pay_amount = "XRP", 1
        session.task = asyncio.get_event_loop().create_task(mint_flow.run_mint_session(session))
        await asyncio.wait_for(entered_buy_and_burn.wait(), timeout=5)

        # Payment is confirmed and the pipeline is mid-buy_and_burn: the
        # session must already have left AWAITING_PAYMENT, so cancel() is
        # refused and the task keeps running.
        assert session.cancel() is False
        assert session.state != mint_flow.CANCELLED
        assert session.state != mint_flow.AWAITING_PAYMENT
        assert not session.task.cancelled()

        # Unblock and tear down without running the rest of the pipeline.
        session.task.cancel()
        release_buy_and_burn.set()
        try:
            await session.task
        except asyncio.CancelledError:
            pass

    _run(scenario())


# ---------------------------------------------------------------------------
# Endpoint: POST /api/mint/{session_id}/cancel
# ---------------------------------------------------------------------------


def test_mint_cancel_releases_lock_for_new_mint(monkeypatch):
    """Cancel an awaiting_payment session → terminal + one-active-session
    lock released, so a new mint is no longer blocked."""
    monkeypatch.setattr(app.config, "WEBAPP_DEV_MODE", False)
    s = mint_flow.MintSession("55", "rA", platform="discord")
    app.mint_sessions[s.id] = s
    try:
        # the session holds the per-user lock while awaiting payment
        assert app._active_session(app.mint_sessions, mint_flow.TERMINAL_STATES, "55", "discord")
        resp = _run(app.handle_mint_cancel(_MockRequest(s.id, _token())))
        assert resp.status == 200
        body = json.loads(resp.text)
        assert body["state"] == mint_flow.CANCELLED
        assert s.state == mint_flow.CANCELLED
        # lock released: a new mint for this user is allowed immediately
        assert (
            app._active_session(app.mint_sessions, mint_flow.TERMINAL_STATES, "55", "discord")
            is None
        )
    finally:
        app.mint_sessions.pop(s.id, None)


def test_mint_cancel_stops_background_task(monkeypatch):
    monkeypatch.setattr(app.config, "WEBAPP_DEV_MODE", False)

    async def scenario():
        s = mint_flow.MintSession("55", "rA", platform="discord")
        s.task = asyncio.get_event_loop().create_task(asyncio.sleep(300))
        app.mint_sessions[s.id] = s
        try:
            resp = await app.handle_mint_cancel(_MockRequest(s.id, _token()))
            assert resp.status == 200
            await asyncio.sleep(0)
            assert s.task.cancelled()
        finally:
            app.mint_sessions.pop(s.id, None)

    _run(scenario())


def test_mint_cancel_unknown_session_404(monkeypatch):
    monkeypatch.setattr(app.config, "WEBAPP_DEV_MODE", False)
    resp = _run(app.handle_mint_cancel(_MockRequest("nope", _token())))
    assert resp.status == 404


def test_mint_cancel_rejects_cross_platform(monkeypatch):
    """A discord token must NOT be able to cancel a telegram:55 session."""
    monkeypatch.setattr(app.config, "WEBAPP_DEV_MODE", False)
    s = mint_flow.MintSession("55", "rA", platform="telegram")
    app.mint_sessions[s.id] = s
    try:
        resp = _run(app.handle_mint_cancel(_MockRequest(s.id, _token(platform="discord"))))
        assert resp.status == 404
        assert s.state == mint_flow.AWAITING_PAYMENT
    finally:
        app.mint_sessions.pop(s.id, None)


def test_mint_cancel_rejects_other_user(monkeypatch):
    monkeypatch.setattr(app.config, "WEBAPP_DEV_MODE", False)
    s = mint_flow.MintSession("55", "rA", platform="discord")
    app.mint_sessions[s.id] = s
    try:
        resp = _run(app.handle_mint_cancel(_MockRequest(s.id, _token(user_id="66"))))
        assert resp.status == 404
        assert s.state == mint_flow.AWAITING_PAYMENT
    finally:
        app.mint_sessions.pop(s.id, None)


def test_mint_cancel_completed_session_is_safe_noop(monkeypatch):
    """Cancelling an already-terminal session doesn't change its state."""
    monkeypatch.setattr(app.config, "WEBAPP_DEV_MODE", False)
    s = mint_flow.MintSession("55", "rA", platform="discord")
    s.state = mint_flow.OFFER_READY
    app.mint_sessions[s.id] = s
    try:
        resp = _run(app.handle_mint_cancel(_MockRequest(s.id, _token())))
        assert resp.status == 200
        assert s.state == mint_flow.OFFER_READY
    finally:
        app.mint_sessions.pop(s.id, None)


def test_mint_cancel_mid_pipeline_409(monkeypatch):
    """A session past payment (money taken, pipeline running) can't be
    cancelled — the mint must complete or fail on its own."""
    monkeypatch.setattr(app.config, "WEBAPP_DEV_MODE", False)
    s = mint_flow.MintSession("55", "rA", platform="discord")
    s.state = mint_flow.MINTING
    app.mint_sessions[s.id] = s
    try:
        resp = _run(app.handle_mint_cancel(_MockRequest(s.id, _token())))
        assert resp.status == 409
        assert s.state == mint_flow.MINTING
    finally:
        app.mint_sessions.pop(s.id, None)


def test_mint_cancel_suppresses_terminal_event(monkeypatch):
    """A deliberate user cancel must not publish mint.completed/mint.failed
    to the event firehose via a late status poll."""
    monkeypatch.setattr(app.config, "WEBAPP_DEV_MODE", False)
    published: list = []

    async def fake_publish(*args, **kwargs):
        published.append(args)

    monkeypatch.setattr(app, "publish_event", fake_publish)
    s = mint_flow.MintSession("55", "rA", platform="discord")
    app.mint_sessions[s.id] = s
    try:
        resp = _run(app.handle_mint_cancel(_MockRequest(s.id, _token())))
        assert resp.status == 200
        resp = _run(app.handle_mint_status(_MockRequest(s.id, _token())))
        assert resp.status == 200
        assert published == []
    finally:
        app.mint_sessions.pop(s.id, None)
