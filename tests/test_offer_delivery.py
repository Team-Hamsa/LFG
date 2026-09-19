# #466: offer_delivery.ensure_offer is the shared mint-offer self-heal helper
# — adopt a live matching offer, detect an already-delivered token, or
# create a fresh one, retrying transient create failures with backoff.
# Extracted from bulk_mint_flow._ensure_offer (see tests/test_bulk_mint_flow.py
# and tests/test_bulk_mint_durability.py for that call site's own coverage,
# which must stay green unchanged against this same module).
from __future__ import annotations

import asyncio
from typing import Any

from lfg_core import offer_delivery


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _gift_offer(
    owner: str,
    destination: str,
    amount: str = "0",
    expiration: int | None = None,
    offer_index: str = "ADOPTME",
) -> dict[str, Any]:
    return {
        "offer_index": offer_index,
        "amount": amount,
        "destination": destination,
        "flags": 1,
        "owner": owner,
        "expiration": expiration,
    }


def _async_return(value: Any):
    async def _f(*args: Any, **kwargs: Any) -> Any:
        return value

    return _f


NFT_ID = "000800007D...EXAMPLE"
DESTINATION = "rUSER1111111111111111111111111"


def test_ensure_offer_retries_create_until_success(monkeypatch):
    """create_nft_offer fails once, then succeeds -- offered after exactly
    two calls, with no other RPC needed to reach that conclusion."""
    bot = offer_delivery.xrpl_ops.bot_wallet_address()
    calls = {"n": 0}

    async def _fail_then_succeed(nft_id, destination, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return "OFFER-IDX"

    monkeypatch.setattr(offer_delivery.xrpl_ops, "get_nft_sell_offers", _async_return([]))
    monkeypatch.setattr(offer_delivery.xrpl_ops, "nft_info", _async_return({"owner": bot}))
    monkeypatch.setattr(offer_delivery.xrpl_ops, "create_nft_offer", _fail_then_succeed)

    result = _run(offer_delivery.ensure_offer(NFT_ID, DESTINATION, attempts=3, base_delay=0))

    assert result.status == "offered"
    assert result.offer_index == "OFFER-IDX"
    assert result.reason is None
    assert calls["n"] == 2


def test_ensure_offer_adopts_existing_live_offer(monkeypatch):
    """A live offer already shaped exactly like our own gift offer is
    reused as-is; create_nft_offer must never be called."""
    bot = offer_delivery.xrpl_ops.bot_wallet_address()
    create_calls = {"n": 0}

    async def _count_create(*a, **kw):
        create_calls["n"] += 1
        return "SHOULD-NOT-BE-CALLED"

    monkeypatch.setattr(
        offer_delivery.xrpl_ops,
        "get_nft_sell_offers",
        _async_return([_gift_offer(bot, DESTINATION, offer_index="ADOPTME")]),
    )
    monkeypatch.setattr(offer_delivery.xrpl_ops, "create_nft_offer", _count_create)

    result = _run(offer_delivery.ensure_offer(NFT_ID, DESTINATION, attempts=3, base_delay=0))

    assert result.status == "adopted"
    assert result.offer_index == "ADOPTME"
    assert result.reason is None
    assert create_calls["n"] == 0


def test_ensure_offer_ignores_non_matching_offers_then_creates(monkeypatch):
    """Adoption requires the EXACT gift-offer shape: foreign owner, wrong
    destination, priced, or expiring offers must all fall through to a
    fresh create rather than being adopted."""
    bot = offer_delivery.xrpl_ops.bot_wallet_address()

    async def _foreign_offers(nft_id, **kw):
        return [
            _gift_offer("rSOMEONEELSE", DESTINATION, offer_index="F1"),  # foreign owner
            _gift_offer(bot, "rOTHER", offer_index="F2"),  # wrong destination
            _gift_offer(bot, DESTINATION, amount="5000000", offer_index="F3"),  # priced
            _gift_offer(bot, DESTINATION, expiration=777, offer_index="F4"),  # expires
        ]

    monkeypatch.setattr(offer_delivery.xrpl_ops, "get_nft_sell_offers", _foreign_offers)
    monkeypatch.setattr(offer_delivery.xrpl_ops, "nft_info", _async_return({"owner": bot}))
    monkeypatch.setattr(offer_delivery.xrpl_ops, "create_nft_offer", _async_return("FRESH"))

    result = _run(offer_delivery.ensure_offer(NFT_ID, DESTINATION, attempts=3, base_delay=0))

    assert result.status == "offered"
    assert result.offer_index == "FRESH"


def test_ensure_offer_detects_already_delivered(monkeypatch):
    """The token's on-ledger owner is no longer us (the issuer) -- already
    delivered. create_nft_offer must never be called, and offer_index stays
    None since there is nothing left to point at."""
    create_calls = {"n": 0}

    async def _count_create(*a, **kw):
        create_calls["n"] += 1
        return "SHOULD-NOT-BE-CALLED"

    monkeypatch.setattr(offer_delivery.xrpl_ops, "get_nft_sell_offers", _async_return([]))
    monkeypatch.setattr(
        offer_delivery.xrpl_ops, "nft_info", _async_return({"nft_id": NFT_ID, "owner": DESTINATION})
    )
    monkeypatch.setattr(offer_delivery.xrpl_ops, "create_nft_offer", _count_create)

    result = _run(offer_delivery.ensure_offer(NFT_ID, DESTINATION, attempts=3, base_delay=0))

    assert result.status == "delivered"
    assert result.offer_index is None
    assert result.reason is None
    assert create_calls["n"] == 0


def test_ensure_offer_fails_after_exhausting_attempts(monkeypatch):
    """create_nft_offer fails on every attempt -- failed, with a reason, and
    create_nft_offer was tried exactly `attempts` times."""
    bot = offer_delivery.xrpl_ops.bot_wallet_address()
    calls = {"n": 0}

    async def _always_fail(nft_id, destination, **kw):
        calls["n"] += 1
        return None

    monkeypatch.setattr(offer_delivery.xrpl_ops, "get_nft_sell_offers", _async_return([]))
    monkeypatch.setattr(offer_delivery.xrpl_ops, "nft_info", _async_return({"owner": bot}))
    monkeypatch.setattr(offer_delivery.xrpl_ops, "create_nft_offer", _always_fail)

    result = _run(offer_delivery.ensure_offer(NFT_ID, DESTINATION, attempts=3, base_delay=0))

    assert result.status == "failed"
    assert result.offer_index is None
    assert result.reason
    assert calls["n"] == 3


def test_ensure_offer_lookup_failure_is_retried_not_immediately_fatal(monkeypatch):
    """A transient get_nft_sell_offers failure on one attempt does not
    immediately give up -- the NEXT attempt gets a fresh chance (and, unlike
    a single-shot check, can still succeed within the same call)."""
    bot = offer_delivery.xrpl_ops.bot_wallet_address()
    lookup_calls = {"n": 0}

    async def _fail_once_then_ok(nft_id, **kw):
        lookup_calls["n"] += 1
        if lookup_calls["n"] == 1:
            raise RuntimeError("rpc down")
        return []

    monkeypatch.setattr(offer_delivery.xrpl_ops, "get_nft_sell_offers", _fail_once_then_ok)
    monkeypatch.setattr(offer_delivery.xrpl_ops, "nft_info", _async_return({"owner": bot}))
    monkeypatch.setattr(offer_delivery.xrpl_ops, "create_nft_offer", _async_return("OFFER-OK"))

    result = _run(offer_delivery.ensure_offer(NFT_ID, DESTINATION, attempts=3, base_delay=0))

    assert result.status == "offered"
    assert result.offer_index == "OFFER-OK"
    assert lookup_calls["n"] == 2


def test_ensure_offer_single_attempt_lookup_failure_fails_fast(monkeypatch):
    """attempts=1 (bulk_mint_flow's delegation) behaves exactly like the
    original single-pass _ensure_offer: a lookup failure fails immediately,
    with create_nft_offer never called."""
    create_calls = {"n": 0}

    async def _boom(nft_id, **kw):
        raise RuntimeError("rpc down")

    async def _count_create(*a, **kw):
        create_calls["n"] += 1
        return "SHOULD-NOT-BE-CALLED"

    monkeypatch.setattr(offer_delivery.xrpl_ops, "get_nft_sell_offers", _boom)
    monkeypatch.setattr(offer_delivery.xrpl_ops, "create_nft_offer", _count_create)

    result = _run(offer_delivery.ensure_offer(NFT_ID, DESTINATION, attempts=1, base_delay=0))

    assert result.status == "failed"
    assert "offer lookup failed" in (result.reason or "")
    assert create_calls["n"] == 0


def test_ensure_offer_threads_platform_to_create(monkeypatch):
    """The provenance `platform` kwarg must reach create_nft_offer so the
    on-chain memo (SourceTag + Memos are mandatory on every tx) is never
    silently omitted."""
    bot = offer_delivery.xrpl_ops.bot_wallet_address()
    seen: dict[str, Any] = {}

    async def _spy_create(nft_id, destination, **kw):
        seen.update(kw)
        return "OFFER-IDX"

    monkeypatch.setattr(offer_delivery.xrpl_ops, "get_nft_sell_offers", _async_return([]))
    monkeypatch.setattr(offer_delivery.xrpl_ops, "nft_info", _async_return({"owner": bot}))
    monkeypatch.setattr(offer_delivery.xrpl_ops, "create_nft_offer", _spy_create)

    result = _run(
        offer_delivery.ensure_offer(
            NFT_ID, DESTINATION, attempts=1, base_delay=0, platform="discord-activity"
        )
    )

    assert result.status == "offered"
    assert seen.get("platform") == "discord-activity"
