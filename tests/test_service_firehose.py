import os

# Set env vars before any lfg_core.config import (mirrors test_service_mint_enrich).
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

from lfg_core import config, swap_flow
from lfg_service import app as server
from lfg_service import identity as identity_store


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _Session:
    """A minimal session stand-in for publish_terminal."""

    def __init__(self, state, data):
        self.state = state
        self._data = data

    def to_dict(self):
        return dict(self._data)


def _next_event(coro):
    async def body():
        async with server.BUS.subscribe(lambda e: True) as stream:
            await coro()
            return await asyncio.wait_for(stream.__anext__(), timeout=1)

    return _run(body())


def test_publish_terminal_completed_normalizes_image(monkeypatch):
    monkeypatch.setattr(identity_store, "identities_for_wallet", lambda w: [])
    s = _Session("done", {"id": "x"})

    evt = _next_event(
        lambda: server.publish_terminal(
            s,
            "swap",
            wallet="rWALLET",
            user_id="42",
            platform="discord",
            image_url="https://cdn/a.png",
            success_states={"done"},
            fail_states={"failed"},
        )
    )
    assert evt.type == "swap.completed"
    assert evt.wallet == "rWALLET"
    assert evt.identity["platform"] == "discord"
    assert evt.identity["platform_user_id"] == "42"
    assert evt.data["image_url"] == "https://cdn/a.png"
    assert s._published is True


def test_publish_terminal_keeps_existing_image(monkeypatch):
    monkeypatch.setattr(identity_store, "identities_for_wallet", lambda w: [])
    s = _Session("done", {"image_url": "https://cdn/existing.png"})

    evt = _next_event(
        lambda: server.publish_terminal(
            s,
            "assemble",
            wallet="rW",
            user_id="1",
            platform="discord",
            image_url="https://cdn/override.png",
            success_states={"done"},
            fail_states={"failed"},
        )
    )
    assert evt.data["image_url"] == "https://cdn/existing.png"


def test_publish_terminal_failed_type(monkeypatch):
    monkeypatch.setattr(identity_store, "identities_for_wallet", lambda w: [])
    s = _Session("failed", {})

    evt = _next_event(
        lambda: server.publish_terminal(
            s,
            "harvest",
            wallet="rW",
            user_id="1",
            platform="telegram",
            image_url=None,
            success_states={"done"},
            fail_states={"failed"},
        )
    )
    assert evt.type == "harvest.failed"
    assert "image_url" not in evt.data


def test_publish_terminal_guard_prevents_double_publish(monkeypatch):
    monkeypatch.setattr(identity_store, "identities_for_wallet", lambda w: [])
    s = _Session("done", {})
    s._published = True
    published = []

    async def fake_publish(*a, **k):
        published.append(a)

    monkeypatch.setattr(server, "publish_event", fake_publish)

    async def body():
        await server.publish_terminal(
            s,
            "swap",
            wallet="rW",
            user_id="1",
            platform="discord",
            image_url=None,
            success_states={"done"},
            fail_states={"failed"},
        )

    _run(body())
    assert published == []


def test_publish_terminal_non_terminal_does_not_publish(monkeypatch):
    monkeypatch.setattr(identity_store, "identities_for_wallet", lambda w: [])
    s = _Session("composing", {})
    published = []

    async def fake_publish(*a, **k):
        published.append(a)

    monkeypatch.setattr(server, "publish_event", fake_publish)

    async def body():
        await server.publish_terminal(
            s,
            "swap",
            wallet="rW",
            user_id="1",
            platform="discord",
            image_url=None,
            success_states={"done"},
            fail_states={"failed"},
        )

    _run(body())
    assert published == []
    assert getattr(s, "_published", False) is False


def _swap_session(state, results):
    s = swap_flow.SwapSession(
        discord_id="dev",
        wallet_address="rWALLET",
        nft1={"name": "A", "image": "ia"},
        nft2={"name": "B", "image": "ib"},
        traits_to_swap=["Hat"],
        platform="discord",
    )
    s.state = state
    s.results = results
    return s


def test_swap_status_publishes_completed_with_image(monkeypatch):
    monkeypatch.setattr(config, "WEBAPP_DEV_MODE", True)
    monkeypatch.setattr(identity_store, "identities_for_wallet", lambda w: [])
    s = _swap_session(swap_flow.OFFERS_READY, [{"image_url": "https://cdn/swap.png"}])
    server.swap_sessions[s.id] = s

    class _Req:
        match_info = {"session_id": s.id}
        headers: dict = {}
        _store = {"user": {"id": "42", "name": "n", "platform": "discord"}}

        def __getitem__(self, k):
            return self._store[k]

        def __setitem__(self, k, v):
            self._store[k] = v

    try:
        evt = _next_event(lambda: server.handle_swap_status(_Req()))
    finally:
        server.swap_sessions.pop(s.id, None)

    assert evt.type == "swap.completed"
    assert evt.data["image_url"] == "https://cdn/swap.png"
    assert evt.wallet == "rWALLET"


def test_swap_status_publishes_failed(monkeypatch):
    monkeypatch.setattr(config, "WEBAPP_DEV_MODE", True)
    monkeypatch.setattr(identity_store, "identities_for_wallet", lambda w: [])
    s = _swap_session(swap_flow.FAILED, [])
    server.swap_sessions[s.id] = s

    class _Req:
        match_info = {"session_id": s.id}
        headers: dict = {}
        _store = {"user": {"id": "42", "name": "n", "platform": "discord"}}

        def __getitem__(self, k):
            return self._store[k]

        def __setitem__(self, k, v):
            self._store[k] = v

    try:
        evt = _next_event(lambda: server.handle_swap_status(_Req()))
    finally:
        server.swap_sessions.pop(s.id, None)

    assert evt.type == "swap.failed"


def test_assemble_status_publishes_with_wallet_and_image(monkeypatch):
    monkeypatch.setattr(config, "WEBAPP_DEV_MODE", True)
    monkeypatch.setattr(identity_store, "identities_for_wallet", lambda w: [])
    from webapp import economy_api

    class _Inner:
        id = "ie"
        state = "done"
        error = None
        owner = "rOWNER"
        results = [{"accept": None, "image_url": "https://cdn/assemble.png", "nft_id": "N"}]

    sess = economy_api.EconomyWebSession(discord_id="dev", kind="assemble", inner=_Inner())
    server.economy_sessions[sess.id] = sess

    class _Req:
        match_info = {"session_id": sess.id}
        headers: dict = {}
        _store = {"user": {"id": "42", "name": "n", "platform": "discord"}}

        def __getitem__(self, k):
            return self._store[k]

        def __setitem__(self, k, v):
            self._store[k] = v

    try:
        evt = _next_event(lambda: server.handle_assemble_status(_Req()))
    finally:
        server.economy_sessions.pop(sess.id, None)

    assert evt.type == "assemble.completed"
    assert evt.wallet == "rOWNER"
    assert evt.data["image_url"] == "https://cdn/assemble.png"


def test_publish_terminal_does_not_mark_published_when_publish_raises(monkeypatch):
    """If publish_event raises/cancels mid-await, the session must stay
    unpublished so a later poll retries (the idempotency guard is set only
    after the await succeeds)."""
    monkeypatch.setattr(identity_store, "identities_for_wallet", lambda w: [])
    s = _Session("done", {})

    async def boom(*a, **k):
        raise asyncio.CancelledError

    monkeypatch.setattr(server, "publish_event", boom)

    async def body():
        await server.publish_terminal(
            s,
            "swap",
            wallet="rW",
            user_id="1",
            platform="discord",
            image_url=None,
            success_states={"done"},
            fail_states={"failed"},
        )

    import pytest

    with pytest.raises(asyncio.CancelledError):
        _run(body())
    assert getattr(s, "_published", False) is False


def _economy_session(kind, inner_state="done"):
    from webapp import economy_api

    class _Inner:
        id = "inner-id"
        state = inner_state
        error = None
        owner = "rOWNER"
        results = [{"image_url": "https://cdn/x.png", "nft_id": "N", "accept": None}]
        moved_assets: list = []
        displaced_value = None

    return economy_api.EconomyWebSession(discord_id="dev", kind=kind, inner=_Inner())


class _EconReq:
    headers: dict = {}

    def __init__(self, session_id):
        self.match_info = {"session_id": session_id}
        self._store = {"user": {"id": "dev", "name": "n", "platform": "discord"}}

    def __getitem__(self, k):
        return self._store[k]

    def __setitem__(self, k, v):
        self._store[k] = v


def test_economy_status_404_on_kind_mismatch(monkeypatch):
    """Polling assemble/{harvest_id}/status must not publish assemble.* for a
    harvest session; it returns 404 and never burns the _published slot."""
    monkeypatch.setattr(config, "WEBAPP_DEV_MODE", True)
    monkeypatch.setattr(identity_store, "identities_for_wallet", lambda w: [])
    sess = _economy_session("harvest")
    server.economy_sessions[sess.id] = sess
    published = []

    async def fake_publish(*a, **k):
        published.append(a)

    monkeypatch.setattr(server, "publish_event", fake_publish)
    try:
        resp = _run(server.handle_assemble_status(_EconReq(sess.id)))
    finally:
        server.economy_sessions.pop(sess.id, None)

    assert resp.status == 404
    assert published == []
    assert getattr(sess, "_published", False) is False


def test_economy_status_publishes_on_kind_match(monkeypatch):
    """A matching prefix still publishes the terminal event (200)."""
    monkeypatch.setattr(config, "WEBAPP_DEV_MODE", True)
    monkeypatch.setattr(identity_store, "identities_for_wallet", lambda w: [])
    sess = _economy_session("harvest")
    server.economy_sessions[sess.id] = sess
    try:
        evt = _next_event(lambda: server.handle_harvest_status(_EconReq(sess.id)))
    finally:
        server.economy_sessions.pop(sess.id, None)

    assert evt.type == "harvest.completed"
    assert evt.wallet == "rOWNER"
