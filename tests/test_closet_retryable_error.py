# #386: a transient closet-mint failure (upstream rippled flakiness, #385)
# must surface to the client as a structured, retryable 503 — not a dead-end
# 502 — and a retry must succeed via the idempotent ensure_closet.

import asyncio
import json
import os
import sqlite3
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

import pytest  # noqa: E402

import lfg_service.app as app  # noqa: E402
from lfg_core import closet_token as ct  # noqa: E402
from lfg_core import economy_store as es  # noqa: E402
from lfg_service.app import make_session_token  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _Req:
    def __init__(self, token=None, body=None):
        self.headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._body = body or {}
        self._store: dict = {}
        self.match_info: dict = {}

    async def json(self):
        return self._body

    def __getitem__(self, k):
        return self._store[k]

    def __setitem__(self, k, v):
        self._store[k] = v


def _wallet_req():
    token = make_session_token({"id": "u1", "name": "u", "platform": "discord"})
    req = _Req(token)
    req["user"] = {"id": "u1", "name": "u"}
    req["wallet"] = "rUserWalletXXXXXXXXXXXXXXXXXXXXXXX"
    return req


def _patch_common(monkeypatch):
    monkeypatch.setattr(app.config, "ECONOMY_ENABLED", True)
    monkeypatch.setattr(app.config, "WEBAPP_DEV_MODE", False)

    async def no_token(user):
        return None

    monkeypatch.setattr(app, "_push_token", no_token)

    async def resolve_wallet(platform, user_id):
        return "rUserWalletXXXXXXXXXXXXXXXXXXXXXXX"

    monkeypatch.setattr(app, "_resolve_wallet", resolve_wallet)


def _closet_resp(monkeypatch, exc):
    _patch_common(monkeypatch)

    async def boom(discord_id, owner, user_token=None):
        raise exc

    monkeypatch.setattr(app.economy_api, "start_closet", boom)
    return _run(app.handle_closet(_wallet_req()))


def test_transient_closet_error_returns_retryable_503(monkeypatch):
    resp = _closet_resp(monkeypatch, ct.ClosetError("failed to mint Closet NFToken"))
    assert resp.status == 503
    body = json.loads(resp.body)
    assert body["error"] == "closet_mint_transient"
    assert body["retryable"] is True


def test_indeterminate_closet_error_is_not_advertised_retryable(monkeypatch):
    # Outcome-unknown must NOT invite a retry (duplicate-mint risk).
    resp = _closet_resp(monkeypatch, ct.ClosetIndeterminateError("outcome unknown"))
    assert resp.status == 502
    assert json.loads(resp.body)["error"] == "could not create or retrieve Closet"


def test_unexpected_error_keeps_opaque_502(monkeypatch):
    resp = _closet_resp(monkeypatch, RuntimeError("boom"))
    assert resp.status == 502
    body = json.loads(resp.body)
    assert body["error"] == "could not create or retrieve Closet"
    assert "retryable" not in body


def test_success_after_transient_failure(monkeypatch):
    # First call fails transient (503); a plain client retry then succeeds.
    _patch_common(monkeypatch)
    calls = {"n": 0}

    async def flaky(discord_id, owner, user_token=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ct.ClosetError("failed to mint Closet NFToken")
        return {"status": "pending_accept", "nft_id": "NFT1", "accept": "x", "accept_push": None}

    monkeypatch.setattr(app.economy_api, "start_closet", flaky)
    first = _run(app.handle_closet(_wallet_req()))
    assert first.status == 503
    second = _run(app.handle_closet(_wallet_req()))
    assert second.status == 200
    assert json.loads(second.body)["status"] == "pending_accept"


# --- ensure_closet idempotence under a failed mint (the property the 503 relies on)


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    es.init_economy_schema(c)
    return c


class _Fakes:
    def __init__(self, fail_first_mint: bool = False) -> None:
        self.mints = 0
        self.fail_first_mint = fail_first_mint

    async def up(self, meta: dict) -> str:
        return "https://cdn/closet/1.json"

    async def mint(self, url: str) -> str | None:
        self.mints += 1
        if self.fail_first_mint and self.mints == 1:
            return None  # definitive upstream failure (the #386 shape)
        return f"NFT{self.mints}"

    async def offer(self, nft_id: str, owner: str) -> str:
        return "OFFER1"

    async def accept(self, offer_id: str) -> dict:
        return {"xumm_url": "x"}


def test_ensure_closet_failed_mint_records_nothing_and_retry_mints_clean():
    c = _conn()
    f = _Fakes(fail_first_mint=True)

    def call():
        return _run(
            ct.ensure_closet(
                c,
                "rA",
                upload_fn=f.up,
                mint_fn=f.mint,
                offer_fn=f.offer,
                accept_payload_fn=f.accept,
            )
        )

    with pytest.raises(ct.ClosetError):
        call()
    # Nothing persisted — a retry sees a clean slate...
    assert es.get_closet_record(c, "rA") is None
    # ...and mints cleanly on the second attempt.
    ref = call()
    assert ref.status == ct.PENDING_ACCEPT
    assert ref.nft_id == "NFT2"
    assert es.get_closet_record(c, "rA") is not None
