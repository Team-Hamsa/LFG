import os

# Set env vars before any lfg_core.config import (mirrors test_event_endpoints).
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

from lfg_core import config, mint_flow
from lfg_service import app as server
from lfg_service import identity as identity_store


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _FakeRequest:
    def __init__(self, session_id):
        self.match_info = {"session_id": session_id}
        self.headers = {}
        self._store = {}

    def __setitem__(self, key, value):
        self._store[key] = value

    def __getitem__(self, key):
        return self._store[key]


def test_handle_mint_status_publishes_enriched_identity(monkeypatch):
    monkeypatch.setattr(config, "WEBAPP_DEV_MODE", True)

    async def noop_scan(_session):
        return None

    monkeypatch.setattr(mint_flow, "update_scan_state", noop_scan)

    rows = [
        {
            "platform": "discord",
            "platform_user_id": "dev",
            "display_handle": "alice",
            "platform_username": "alice_u",
            "created_at": "t1",
            "updated_at": "t2",
        }
    ]
    monkeypatch.setattr(identity_store, "identities_for_wallet", lambda w: rows)

    session = mint_flow.MintSession(discord_id="dev", wallet_address="rWALLET")
    session.state = mint_flow.DONE
    server.mint_sessions[session.id] = session

    async def body():
        async with server.BUS.subscribe(lambda e: True) as stream:
            req = _FakeRequest(session.id)
            await server.handle_mint_status(req)
            return await asyncio.wait_for(stream.__anext__(), timeout=1)

    try:
        evt = _run(body())
    finally:
        server.mint_sessions.pop(session.id, None)

    assert evt.type == "mint.completed"
    assert evt.identity["platform"] == "discord"
    assert evt.identity["platform_user_id"] == "dev"
    assert evt.identity["display_handle"] == "alice"
    assert evt.identity["linked"] == [
        {"platform": "discord", "platform_user_id": "dev", "display_handle": "alice"}
    ]


def test_enrich_attaches_minter_handle_and_linked(monkeypatch):
    rows = [
        {
            "platform": "discord",
            "platform_user_id": "42",
            "display_handle": "alice",
            "platform_username": "alice_u",
            "created_at": "t1",
            "updated_at": "t2",
        },
        {
            "platform": "telegram",
            "platform_user_id": "55",
            "display_handle": "alice_tg",
            "platform_username": "alice_tg_u",
            "created_at": "t3",
            "updated_at": "t4",
        },
    ]
    monkeypatch.setattr(identity_store, "identities_for_wallet", lambda w: rows)

    out = server.enrich_minter_identity("discord", "42", "rWALLET")

    assert out["platform"] == "discord"
    assert out["platform_user_id"] == "42"
    assert out["display_handle"] == "alice"  # the minter's own handle
    assert out["linked"] == [
        {"platform": "discord", "platform_user_id": "42", "display_handle": "alice"},
        {"platform": "telegram", "platform_user_id": "55", "display_handle": "alice_tg"},
    ]


def test_enrich_minter_unknown_handle_is_none(monkeypatch):
    # wallet has linked identities but none matches the minter -> handle None
    rows = [
        {
            "platform": "telegram",
            "platform_user_id": "55",
            "display_handle": "bob_tg",
            "platform_username": "bob",
            "created_at": "t1",
            "updated_at": "t2",
        }
    ]
    monkeypatch.setattr(identity_store, "identities_for_wallet", lambda w: rows)

    out = server.enrich_minter_identity("discord", "42", "rWALLET")
    assert out["display_handle"] is None
    assert out["linked"] == [
        {"platform": "telegram", "platform_user_id": "55", "display_handle": "bob_tg"}
    ]


def test_enrich_none_wallet_returns_bare_identity(monkeypatch):
    def boom(_w):
        raise AssertionError("identities_for_wallet must not be called for None wallet")

    monkeypatch.setattr(identity_store, "identities_for_wallet", boom)
    out = server.enrich_minter_identity("discord", "42", None)
    assert out == {
        "platform": "discord",
        "platform_user_id": "42",
        "display_handle": None,
        "linked": [],
    }


def test_enrich_lookup_raises_returns_bare_identity(monkeypatch):
    def boom(_w):
        raise RuntimeError("db down")

    monkeypatch.setattr(identity_store, "identities_for_wallet", boom)
    out = server.enrich_minter_identity("telegram", "55", "rWALLET")
    assert out == {
        "platform": "telegram",
        "platform_user_id": "55",
        "display_handle": None,
        "linked": [],
    }


def test_enrich_never_mutates_wallet(monkeypatch):
    seen = {}

    def capture(w):
        seen["wallet"] = w
        return []

    monkeypatch.setattr(identity_store, "identities_for_wallet", capture)
    # Mixed-case classic address must be passed verbatim.
    server.enrich_minter_identity("discord", "42", "rAbCdEf123")
    assert seen["wallet"] == "rAbCdEf123"
