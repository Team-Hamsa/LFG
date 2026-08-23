"""#424: abandoned pre-money sessions expire so the deployer drain can finish.

A market Buy left at ``awaiting_signature`` (user closed the Activity without
signing) never reaches a terminal state on its own, so ``/api/health`` kept
counting it and prod's drain timed out at 900s. Once the XUMM payload's own
15-min expire has passed, such a session is safe to expire client-side; a
mint past ``paid`` (money in flight) must never be touched.
"""

import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lfg_service.app as app  # noqa: E402
from lfg_core import market_flow, mint_flow, swap_flow  # noqa: E402


class _Req:
    def __init__(self):
        self.headers = {}
        self.match_info = {}


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _health(monkeypatch, **stores):
    for name in ("mint_sessions", "swap_sessions", "economy_sessions", "market_sessions"):
        monkeypatch.setattr(app, name, stores.get(name, {}), raising=False)
    return json.loads(_run(app.handle_health(_Req())).body)


def _buy(state, age):
    s = market_flow.BuySession(
        discord_id="u1",
        wallet_address="rBUYER",
        offer_index="OFF",
        nft_id="N1",
        listing_kind="character",
        network="testnet",
    )
    s.state = state
    s.created_at = time.time() - age
    s.payload_uuid = "payload-uuid-1"
    return s


def _capture_cancels(monkeypatch):
    seen = []
    monkeypatch.setattr(app, "_spawn_payload_cancel", lambda uuid: seen.append(uuid))
    return seen


def test_abandoned_buy_awaiting_signature_is_expired(monkeypatch):
    cancels = _capture_cancels(monkeypatch)
    s = _buy(market_flow.AWAITING_SIGNATURE, app.SESSION_ABANDON_TTL + 60)
    body = _health(monkeypatch, market_sessions={s.id: s})
    assert body["detail"]["market"] == 0
    assert body["active_sessions"] == 0
    assert s.state in market_flow.TERMINAL_STATES
    assert s.reason == "abandoned"
    assert cancels == ["payload-uuid-1"]


def test_fresh_buy_awaiting_signature_untouched(monkeypatch):
    cancels = _capture_cancels(monkeypatch)
    s = _buy(market_flow.AWAITING_SIGNATURE, 30)
    body = _health(monkeypatch, market_sessions={s.id: s})
    assert body["detail"]["market"] == 1
    assert s.state == market_flow.AWAITING_SIGNATURE
    assert cancels == []


def test_buy_past_signature_never_expired(monkeypatch):
    # PENDING = signed, tx not yet validated: money may be moving.
    _capture_cancels(monkeypatch)
    s = _buy(market_flow.PENDING, app.SESSION_ABANDON_TTL * 10)
    body = _health(monkeypatch, market_sessions={s.id: s})
    assert body["detail"]["market"] == 1
    assert s.state == market_flow.PENDING


def test_abandoned_onramp_buy_is_expired_and_onramp_payload_cancelled(monkeypatch):
    cancels = _capture_cancels(monkeypatch)
    s = _buy(market_flow.AWAITING_ONRAMP, app.SESSION_ABANDON_TTL + 1)
    s.payload_uuid = None
    s.onramp_payload_uuid = "onramp-uuid"
    body = _health(monkeypatch, market_sessions={s.id: s})
    assert body["detail"]["market"] == 0
    assert s.state in market_flow.TERMINAL_STATES
    assert cancels == ["onramp-uuid"]


class _Mint:
    """Minimal MintSession stand-in: a real one needs a running event loop
    and XUMM to build a payload; only state/age/cancel() matter here."""

    def __init__(self, state, age):
        self.id = "m1"
        self.discord_id = "u1"
        self.state = state
        self.created_at = time.time() - age
        self.payment_uuid = "pay-uuid"
        self.stale_payment_uuids = ["old-uuid"]
        self.cancel_calls = 0
        self.published = False

    def cancel(self):
        if self.state != mint_flow.AWAITING_PAYMENT:
            return False
        self.cancel_calls += 1
        self.state = mint_flow.CANCELLED
        return True

    def mark_published(self):
        self.published = True


def test_paid_mint_session_never_expired(monkeypatch):
    cancels = _capture_cancels(monkeypatch)
    for state in (
        mint_flow.GENERATING,
        mint_flow.MINTING,
        mint_flow.CREATING_OFFER,
    ):
        assert state not in mint_flow.TERMINAL_STATES
        m = _Mint(state, app.SESSION_ABANDON_TTL * 100)
        body = _health(monkeypatch, mint_sessions={"m": m})
        assert body["detail"]["mint"] == 1
        assert m.state == state
        assert m.cancel_calls == 0
    assert cancels == []


def test_abandoned_mint_awaiting_payment_is_cancelled_via_session_cancel(monkeypatch):
    cancels = _capture_cancels(monkeypatch)
    m = _Mint(mint_flow.AWAITING_PAYMENT, app.SESSION_ABANDON_TTL + 5)
    body = _health(monkeypatch, mint_sessions={"m": m})
    assert body["detail"]["mint"] == 0
    assert m.state == mint_flow.CANCELLED
    assert m.cancel_calls == 1
    assert m.published is True
    # the live payload AND the superseded-QR payloads are all cancelled
    assert sorted(cancels) == ["old-uuid", "pay-uuid"]
    assert m.stale_payment_uuids == []


def test_expire_abandoned_helper_respects_ttl_and_states():
    now = time.time()
    old = _buy(market_flow.AWAITING_SIGNATURE, app.SESSION_ABANDON_TTL + 1)
    fresh = _buy(market_flow.AWAITING_SIGNATURE, 1)
    pending_old = _buy(market_flow.PENDING, app.SESSION_ABANDON_TTL + 1)
    sessions = {"old": old, "fresh": fresh, "pending": pending_old}
    expired = app._expire_abandoned(
        sessions,
        {market_flow.AWAITING_SIGNATURE},
        market_flow.FAILED,
        now=now,
        spawn_cancel=lambda uuid: None,
    )
    assert expired == 1
    assert old.state == market_flow.FAILED
    assert old.error == "abandoned"
    assert fresh.state == market_flow.AWAITING_SIGNATURE
    assert pending_old.state == market_flow.PENDING


def test_swap_awaiting_payment_expired_but_paid_untouched(monkeypatch):
    _capture_cancels(monkeypatch)

    class _Swap(_Mint):
        def cancel(self):
            if self.state != swap_flow.AWAITING_PAYMENT:
                return False
            self.cancel_calls += 1
            self.state = swap_flow.CANCELLED
            return True

    old = _Swap(swap_flow.AWAITING_PAYMENT, app.SESSION_ABANDON_TTL + 1)
    paid = _Swap(swap_flow.MODIFYING, app.SESSION_ABANDON_TTL + 1)
    body = _health(monkeypatch, swap_sessions={"a": old, "b": paid})
    assert body["detail"]["swap"] == 1
    assert old.state == swap_flow.CANCELLED
    assert paid.state == swap_flow.MODIFYING


def test_abandon_ttl_env_override(monkeypatch):
    monkeypatch.setenv("SESSION_ABANDON_TTL_SECONDS", "42")
    assert app._abandon_ttl_from_env() == 42
    monkeypatch.setenv("SESSION_ABANDON_TTL_SECONDS", "garbage")
    assert app._abandon_ttl_from_env() == app.SESSION_ABANDON_TTL_DEFAULT
    monkeypatch.delenv("SESSION_ABANDON_TTL_SECONDS")
    assert app._abandon_ttl_from_env() == app.SESSION_ABANDON_TTL_DEFAULT
    # must outlast the XUMM payload expire (#260: 15 min) so a still-signable
    # payload is never pulled out from under a slow user
    assert app.SESSION_ABANDON_TTL_DEFAULT > 15 * 60
