import os

# Set env vars before any lfg_core.config import so module-level constants
# (e.g. IMG_PROXY_ALLOWED_BASES) are frozen with the correct values even when
# this file is collected before webapp/test_smoke.py.
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

import lfg_service.identity as identity
from lfg_service import app as server
from lfg_service.events import Event


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _FakeRequest:
    def __init__(self, query=None, headers=None):
        self.query = query or {}
        self.headers = headers or {}
        self._store = {}

    def __setitem__(self, key, value):
        self._store[key] = value

    def __getitem__(self, key):
        return self._store[key]


def _evt(type_, wallet, n=0):
    return Event(type=type_, ts=1, identity=None, wallet=wallet, data={"n": n})


def test_events_requires_service_token():
    resp = _run(server.handle_events(_FakeRequest()))
    assert resp.status == 401


def test_events_me_rejects_bad_session():
    resp = _run(server.handle_events_me(_FakeRequest(query={"token": "garbage"})))
    assert resp.status == 401


def test_events_me_no_wallet_returns_403(tmp_path, monkeypatch):
    monkeypatch.setattr(identity, "DATABASE", str(tmp_path / "t.db"))
    identity.ensure_identities_table()  # user has no linked wallet
    token = server.make_session_token({"id": "999", "name": "ghost"})
    resp = _run(server.handle_events_me(_FakeRequest(query={"token": token})))
    assert resp.status == 403


def test_events_me_filters_to_caller_wallet(tmp_path, monkeypatch):
    monkeypatch.setattr(identity, "DATABASE", str(tmp_path / "t.db"))
    identity.ensure_identities_table()
    identity.link("discord", "42", "me", "rME")
    captured = {}

    async def fake_ws_stream(request, predicate):
        captured["predicate"] = predicate
        return "WS_OK"

    monkeypatch.setattr(server, "_ws_stream", fake_ws_stream)
    token = server.make_session_token({"id": "42", "name": "me"})
    result = _run(server.handle_events_me(_FakeRequest(query={"token": token})))
    assert result == "WS_OK"
    predicate = captured["predicate"]
    assert predicate(_evt("mint.completed", "rME", 1)) is True
    assert predicate(_evt("mint.completed", "rOTHER", 0)) is False


def test_events_firehose_type_filter(monkeypatch):
    monkeypatch.setenv("SERVICE_TOKEN_DISCORD", "tok-d")
    captured = {}

    async def fake_ws_stream(request, predicate):
        captured["predicate"] = predicate
        return "WS_OK"

    monkeypatch.setattr(server, "_ws_stream", fake_ws_stream)
    result = _run(
        server.handle_events(_FakeRequest(query={"token": "tok-d", "types": "swap.completed"}))
    )
    assert result == "WS_OK"
    predicate = captured["predicate"]
    assert predicate(_evt("swap.completed", "rA")) is True
    assert predicate(_evt("mint.completed", "rA")) is False


def test_publish_event_reaches_bus():
    async def body():
        async with server.BUS.subscribe(lambda e: True) as stream:
            await server.publish_event(
                "mint.completed", {"platform": "discord", "platform_user_id": "1"}, "rME", {"n": 7}
            )
            return await asyncio.wait_for(stream.__anext__(), timeout=1)

    evt = _run(body())
    assert evt.type == "mint.completed"
    assert evt.wallet == "rME"
    assert evt.data["n"] == 7


def test_event_routes_registered(tmp_path, monkeypatch):
    monkeypatch.setattr(identity, "DATABASE", str(tmp_path / "t.db"))
    app = server.create_app()
    paths = {r.resource.canonical for r in app.router.routes() if r.resource is not None}
    assert "/events" in paths
    assert "/events/me" in paths


def test_ws_subscriber_cleaned_up_on_disconnect(monkeypatch):
    """FIX 1: subscriber entry must be removed promptly when the WS client
    disconnects, even if no events arrive (i.e. the queue is idle)."""

    async def body():
        from aiohttp.test_utils import TestClient, TestServer

        from lfg_service import app as server  # noqa: F811 (already imported above)

        app = (
            server.create_app.__wrapped__()
            if hasattr(server.create_app, "__wrapped__")
            else server.create_app()
        )  # type: ignore[attr-defined]

        async with TestClient(TestServer(app)) as client:
            monkeypatch.setenv("SERVICE_TOKEN_DISCORD", "svc-tok")
            # Open a /events WS (service-token auth)
            ws = await client.ws_connect("/events?token=svc-tok")
            # Give the server a tick to register the subscriber
            await asyncio.sleep(0.05)
            assert len(server.BUS._subscribers) == 1, "subscriber should be registered"
            # Close the client side
            await ws.close()
            # Give the event loop a couple of ticks for the disconnect to propagate
            await asyncio.sleep(0.1)
            assert len(server.BUS._subscribers) == 0, (
                "subscriber should be cleaned up after disconnect"
            )

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(body())
    finally:
        loop.close()


def test_events_me_falls_back_to_legacy_users(tmp_path, monkeypatch):
    """FIX 2b: a user present in Users but NOT in identities must still get events
    (no 403) — the legacy get_user fallback must supply the wallet."""

    import user_db

    # Point both identity and user_db to the same fresh DB
    db_path = str(tmp_path / "t.db")
    monkeypatch.setattr(identity, "DATABASE", db_path)
    monkeypatch.setattr(user_db, "DATABASE", db_path)
    # Also patch the module-level import inside app (it imported get_user from user_db at load time)
    monkeypatch.setattr(server, "get_user", user_db.get_user)

    # Create tables
    identity.ensure_identities_table()
    user_db.create_users_table()

    # Seed user into legacy Users ONLY (not identities)
    user_db.register_user("u-legacyonly", "legacyuser", "rLEGACY1")
    # Confirm identity resolve returns None
    assert identity.resolve("discord", "u-legacyonly") is None

    captured: dict = {}

    async def fake_ws_stream(request, predicate):
        captured["predicate"] = predicate
        return "WS_OK"

    monkeypatch.setattr(server, "_ws_stream", fake_ws_stream)

    token = server.make_session_token({"id": "u-legacyonly", "name": "legacyuser"})
    result = _run(server.handle_events_me(_FakeRequest(query={"token": token})))

    # Must NOT return a 403 — the fake_ws_stream returns "WS_OK"
    assert result == "WS_OK", f"Expected WS_OK (legacy fallback), got {result!r}"
    # The predicate must filter to the legacy wallet
    predicate = captured["predicate"]
    assert predicate(_evt("mint.completed", "rLEGACY1")) is True
    assert predicate(_evt("mint.completed", "rOTHER")) is False
