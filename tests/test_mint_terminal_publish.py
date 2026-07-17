# tests/test_mint_terminal_publish.py
# #41 fix wave: terminal mint.completed/mint.failed firehose events must be
# published SERVER-SIDE when the mint session task finishes — not only from
# handle_mint_status's client poll. A mobile user whose Activity is killed
# after signing in Xaman (the #216 scenario; push delivery lets them finish
# the whole flow inside Xaman) mints successfully but never polls again;
# before this fix the status poll was the ONLY publish site, so those mints
# silently never reached the X poster / Telegram announce.
#
# Env-guard preamble: importing lfg_core.config freezes its constants (e.g.
# IMG_PROXY_ALLOWED_BASES, LAYER_SOURCE) at import time; set the same defaults
# test_smoke.py uses so collection order can't strand them. (Copy the block
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

import asyncio  # noqa: E402
import json  # noqa: E402

import pytest  # noqa: E402

from lfg_core import config, mint_flow  # noqa: E402
from lfg_service import app as server  # noqa: E402
from lfg_service import identity as identity_store  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _FakeStatusRequest:
    """Minimal fake for handle_mint_status (same shape as
    tests/test_service_mint_enrich.py — dev-mode auth injects the user)."""

    def __init__(self, session_id):
        self.match_info = {"session_id": session_id}
        self.headers = {}
        self._store = {}

    def __setitem__(self, key, value):
        self._store[key] = value

    def __getitem__(self, key):
        return self._store[key]


class _FakeStartRequest(_FakeStatusRequest):
    """handle_mint_start additionally reads the JSON body (return_url ctx)."""

    def __init__(self):
        super().__init__("unused")

    async def json(self):
        return {}


def _record_publishes(monkeypatch):
    """Intercept server.publish_event so tests can count exact publishes."""
    events = []

    async def record(type_, identity_obj, wallet, data):
        events.append({"type": type_, "identity": identity_obj, "wallet": wallet, "data": data})

    monkeypatch.setattr(server, "publish_event", record)
    return events


def _fake_run_to(state):
    async def fake_run(session):
        session.state = state

    return fake_run


def _poll(monkeypatch, session):
    """One dev-mode status poll against a registered session."""
    monkeypatch.setattr(config, "WEBAPP_DEV_MODE", True)

    async def noop_scan(_session):
        return None

    monkeypatch.setattr(mint_flow, "update_scan_state", noop_scan)
    return _run(server.handle_mint_status(_FakeStatusRequest(session.id)))


def test_server_side_publish_fires_without_any_status_poll(monkeypatch):
    """The #216 scenario: the session reaches terminal with ZERO status polls
    — the wrapped session task itself must publish exactly one event."""
    events = _record_publishes(monkeypatch)
    monkeypatch.setattr(mint_flow, "run_mint_session", _fake_run_to(mint_flow.DONE))

    session = mint_flow.MintSession(discord_id="dev", wallet_address="rWALLET")
    _run(server._run_mint_session_and_publish(session))

    assert [e["type"] for e in events] == ["mint.completed"]
    assert events[0]["wallet"] == "rWALLET"
    assert events[0]["identity"]["platform"] == "discord"
    assert session._published is True


def test_status_poll_after_server_side_publish_does_not_double_publish(monkeypatch):
    events = _record_publishes(monkeypatch)
    monkeypatch.setattr(mint_flow, "run_mint_session", _fake_run_to(mint_flow.DONE))

    session = mint_flow.MintSession(discord_id="dev", wallet_address="rWALLET")
    _run(server._run_mint_session_and_publish(session))
    assert len(events) == 1

    server.mint_sessions[session.id] = session
    try:
        _poll(monkeypatch, session)
    finally:
        server.mint_sessions.pop(session.id, None)
    assert len(events) == 1  # the poll saw _published and stayed silent


def test_poll_first_path_still_publishes_exactly_once(monkeypatch):
    """The pre-existing path: a client polls a terminal session before (or
    without) the server-side task publish — one event, and only one across
    repeated polls."""
    events = _record_publishes(monkeypatch)

    session = mint_flow.MintSession(discord_id="dev", wallet_address="rWALLET")
    session.state = mint_flow.DONE
    server.mint_sessions[session.id] = session
    try:
        _poll(monkeypatch, session)
        _poll(monkeypatch, session)
    finally:
        server.mint_sessions.pop(session.id, None)

    assert [e["type"] for e in events] == ["mint.completed"]


@pytest.mark.parametrize("state", [mint_flow.FAILED, mint_flow.PAYMENT_TIMEOUT])
def test_failure_states_publish_mint_failed_server_side(monkeypatch, state):
    events = _record_publishes(monkeypatch)
    monkeypatch.setattr(mint_flow, "run_mint_session", _fake_run_to(state))

    session = mint_flow.MintSession(discord_id="dev", wallet_address="rWALLET")
    _run(server._run_mint_session_and_publish(session))

    assert [e["type"] for e in events] == ["mint.failed"]


def test_publish_failure_never_breaks_the_mint_task_and_stays_retryable(monkeypatch):
    """publish_terminal ORDERING: _published is set only AFTER publish_event
    awaits successfully — a failed publish is logged, never raises out of the
    session task, and leaves the session unpublished so a later status poll
    retries."""
    monkeypatch.setattr(mint_flow, "run_mint_session", _fake_run_to(mint_flow.DONE))

    async def boom(*_args, **_kwargs):
        raise RuntimeError("bus down")

    monkeypatch.setattr(server, "publish_event", boom)

    session = mint_flow.MintSession(discord_id="dev", wallet_address="rWALLET")
    _run(server._run_mint_session_and_publish(session))  # must not raise

    assert session._published is False


def test_cancelled_session_publishes_nothing(monkeypatch):
    """A deliberate user cancel is not a mint outcome (#141): cancelling the
    wrapped task must not fire any terminal event."""
    events = _record_publishes(monkeypatch)

    async def parked(_session):
        await asyncio.Event().wait()  # stands in for the payment wait

    monkeypatch.setattr(mint_flow, "run_mint_session", parked)

    async def scenario():
        session = mint_flow.MintSession(discord_id="dev", wallet_address="rWALLET")
        session.task = asyncio.get_event_loop().create_task(
            server._run_mint_session_and_publish(session)
        )
        await asyncio.sleep(0)  # let the task park
        assert session.cancel() is True
        session.mark_published()  # what handle_mint_cancel does
        await asyncio.sleep(0)  # let the cancellation land
        assert session.task.cancelled()

    _run(scenario())
    assert events == []


def test_handle_mint_start_wires_the_publishing_wrapper(monkeypatch):
    """End-to-end wiring: a real /api/mint start (dev mode, payment prep
    stubbed) whose session runs to terminal publishes exactly one
    mint.completed with no status poll ever issued."""
    events = _record_publishes(monkeypatch)
    monkeypatch.setattr(config, "WEBAPP_DEV_MODE", True)
    monkeypatch.setattr(identity_store, "user_token_for", lambda _p, _u: None)

    async def noop_prepare(self):
        return None

    monkeypatch.setattr(mint_flow.MintSession, "prepare_payment", noop_prepare)
    monkeypatch.setattr(mint_flow, "run_mint_session", _fake_run_to(mint_flow.DONE))

    async def scenario():
        resp = await server.handle_mint_start(_FakeStartRequest())
        assert resp.status == 200
        # Await the spawned session task (never poll the status endpoint).
        for session in list(server.mint_sessions.values()):
            if session.task is not None:
                await session.task
            server.mint_sessions.pop(session.id, None)

    _run(scenario())
    assert [e["type"] for e in events] == ["mint.completed"]


def test_status_poll_publish_failure_still_returns_terminal_status(monkeypatch):
    """Poll-path guard (the #41 review fix): handle_mint_status's call to
    _publish_mint_terminal must be wrapped like _run_mint_session_and_publish's
    — if publish_event raises during a status poll, the client still gets
    their terminal status back (200, not a 500), _published stays False
    (publish_terminal ordering) so a later poll can retry, and a subsequent
    poll with a healthy bus publishes exactly once."""

    async def boom(*_args, **_kwargs):
        raise RuntimeError("bus down")

    monkeypatch.setattr(server, "publish_event", boom)

    session = mint_flow.MintSession(discord_id="dev", wallet_address="rWALLET")
    session.state = mint_flow.DONE
    server.mint_sessions[session.id] = session
    try:
        resp = _poll(monkeypatch, session)
        assert resp.status == 200
        body = json.loads(resp.body)
        assert body["state"] == mint_flow.DONE
        assert session._published is False

        # A later poll with a healthy bus retries and publishes exactly once.
        events = _record_publishes(monkeypatch)
        resp2 = _poll(monkeypatch, session)
        assert resp2.status == 200
        assert [e["type"] for e in events] == ["mint.completed"]
        assert session._published is True
    finally:
        server.mint_sessions.pop(session.id, None)
