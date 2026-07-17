# tests/test_mint_rate_limit_fail_fast.py
# #262: when XUMM payload creation fails during the rate-limit cooldown,
# /api/mint must fail fast with 503 + Retry-After (the same shape the signin
# guard uses) instead of spawning the 300s payment wait with no signable
# payload — the 2026-07-17 incident left a user staring at a dead pay screen
# for 5 minutes before a PAYMENT_TIMEOUT.
#
# Env-guard preamble: importing lfg_core.config freezes its constants at
# import time; set the same defaults test_smoke.py uses so collection order
# can't strand them. (Copied from tests/test_mint_terminal_publish.py.)
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
import time  # noqa: E402

from lfg_core import config, mint_flow, xumm_ops  # noqa: E402
from lfg_service import app as server  # noqa: E402
from lfg_service import identity as identity_store  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _FakeStartRequest:
    """Minimal fake for handle_mint_start (dev-mode auth injects the user)."""

    def __init__(self):
        self.match_info = {}
        self.headers = {}
        self._store = {}

    def __setitem__(self, key, value):
        self._store[key] = value

    def __getitem__(self, key):
        return self._store[key]

    async def json(self):
        return {}


def _start(monkeypatch):
    """Drive handle_mint_start and settle any spawned session task inside the
    same loop, so loop.close() never destroys a pending task (test leakage)."""
    monkeypatch.setattr(config, "WEBAPP_DEV_MODE", True)
    monkeypatch.setattr(identity_store, "user_token_for", lambda _p, _u: None)
    server.mint_sessions.clear()

    async def scenario():
        resp = await server.handle_mint_start(_FakeStartRequest())
        # Let any created task start, then await it to completion.
        await asyncio.sleep(0)
        for session in list(server.mint_sessions.values()):
            if session.task is not None:
                await session.task
        return resp

    return _run(scenario())


def _prepare_without_payload(monkeypatch):
    """prepare_payment that 'ran' but produced no XUMM payload (the create
    was skipped/rejected), as during the rate-limit cooldown."""

    async def prepare(self):
        self.pay_with, self.pay_amount = "XRP", config.MINT_PRICE_XRP
        self.payment_link = "https://xumm.app/detect/..."

    monkeypatch.setattr(mint_flow.MintSession, "prepare_payment", prepare)


def test_mint_start_503s_when_payload_create_fails_rate_limited(monkeypatch):
    _prepare_without_payload(monkeypatch)
    monkeypatch.setattr(xumm_ops, "_rate_limited_until", time.monotonic() + 30)
    # Record any spawned payment-wait: the fail-fast contract is that NO
    # session task ever starts, not just that the session dict is cleaned.
    started = []

    async def record(session):
        started.append(session)
        session.state = mint_flow.CANCELLED

    monkeypatch.setattr(mint_flow, "run_mint_session", record)

    resp = _start(monkeypatch)

    assert started == []  # the payment wait was never entered

    assert resp.status == 503
    body = json.loads(resp.body.decode())
    assert body["code"] == "rate_limited"
    assert resp.headers["Retry-After"] == "30"
    # No session left behind (would 409 the user's retry) and no payment-wait
    # task ever spawned.
    assert server.mint_sessions == {}


def test_mint_start_still_falls_back_when_not_rate_limited(monkeypatch):
    """A payload-less prepare WITHOUT the cooldown (XUMM outage/timeout) keeps
    the existing static-link fallback behavior: session starts normally."""
    _prepare_without_payload(monkeypatch)
    monkeypatch.setattr(xumm_ops, "_rate_limited_until", 0.0)

    async def parked(session):
        session.state = mint_flow.CANCELLED  # terminal, so cleanup is easy

    monkeypatch.setattr(mint_flow, "run_mint_session", parked)

    resp = _start(monkeypatch)

    assert resp.status == 200
    assert len(server.mint_sessions) == 1
    server.mint_sessions.clear()


def test_mint_start_ok_when_payload_created_during_cooldown_tail(monkeypatch):
    """If a payload WAS created (payment_uuid set), a later-armed cooldown
    must not block the mint — the user has a signable QR."""

    async def prepare(self):
        self.pay_with, self.pay_amount = "XRP", config.MINT_PRICE_XRP
        self.payment_link = "https://xumm.app/sign/uuid"
        self.payment_uuid = "11111111-2222-3333-4444-555555555555"

    monkeypatch.setattr(mint_flow.MintSession, "prepare_payment", prepare)
    monkeypatch.setattr(xumm_ops, "_rate_limited_until", time.monotonic() + 30)
    monkeypatch.setattr(mint_flow, "run_mint_session", _terminal_run)

    resp = _start(monkeypatch)

    assert resp.status == 200
    server.mint_sessions.clear()


async def _terminal_run(session):
    session.state = mint_flow.CANCELLED


# --- swap: same fail-fast for the modify-fee wait (#262) ---------------------


def test_swap_fee_collection_fails_fast_during_cooldown(monkeypatch):
    """_collect_modify_fee must not enter the payment wait when no payload
    could be built AND the rate-limit cooldown is armed — the static detect
    link is unusable in Xaman (#8), so the wait can only time out."""
    from lfg_core import swap_flow

    waited = []

    async def fake_wait(**kwargs):
        waited.append(kwargs)
        return False

    monkeypatch.setattr(swap_flow.xrpl_ops, "wait_for_payment", fake_wait)
    monkeypatch.setattr(swap_flow.xrpl_ops, "bot_wallet_address", lambda: "rBOT")

    async def no_payload(self):
        return False

    monkeypatch.setattr(swap_flow.SwapSession, "regenerate_payment", no_payload)
    monkeypatch.setattr(xumm_ops, "_rate_limited_until", time.monotonic() + 30)

    session = swap_flow.SwapSession(
        discord_id="dev", wallet_address="rWALLET", nft1={}, nft2={}, traits_to_swap=[]
    )
    session.pay_with = "BRIX"
    ok = _run(swap_flow._collect_modify_fee(session, 1))

    assert ok is False
    assert waited == []  # never entered the payment wait
    assert session.error and "rate limit" in session.error.lower()


def test_swap_fee_collection_still_waits_when_not_rate_limited(monkeypatch):
    """Payload-less prepare WITHOUT the cooldown keeps the static-link wait."""
    from lfg_core import swap_flow

    waited = []

    async def fake_wait(**kwargs):
        waited.append(kwargs)
        return False

    monkeypatch.setattr(swap_flow.xrpl_ops, "wait_for_payment", fake_wait)
    monkeypatch.setattr(swap_flow.xrpl_ops, "bot_wallet_address", lambda: "rBOT")

    async def no_payload(self):
        return False

    monkeypatch.setattr(swap_flow.SwapSession, "regenerate_payment", no_payload)
    monkeypatch.setattr(xumm_ops, "_rate_limited_until", 0.0)

    session = swap_flow.SwapSession(
        discord_id="dev", wallet_address="rWALLET", nft1={}, nft2={}, traits_to_swap=[]
    )
    session.pay_with = "BRIX"
    ok = _run(swap_flow._collect_modify_fee(session, 1))

    assert ok is False  # fake wait returns False (timeout)
    assert len(waited) == 1  # but the wait WAS entered
