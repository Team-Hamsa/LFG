# tests/test_swap_offer_recovery.py
# Covers _create_offer_and_accept's self-offer guard (#136): the reminted
# replacement offer must be SKIPPED when the recipient is the issuer/signing
# account itself — mint_nft always mints Account=SIGNING_ACCOUNT, so the token
# is already in that wallet and a self-directed sell offer is invalid
# (temREDUNDANT). A genuine offer-creation failure still surfaces the admin
# error with no partial result.
#
# Env-guard preamble: importing lfg_core.config freezes its constants at import
# time; set the same defaults test_smoke.py uses so collection order can't
# strand them. (Copy the block verbatim from tests/test_market_ops.py.)
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
from decimal import Decimal  # noqa: E402

from lfg_core import config, swap_flow, xrpl_ops, xumm_ops  # noqa: E402

_NEW_NFT_ID = "00191B58B6161690B012F6916ADBBF17A24C4BB687348E21EF54A3CE00459893"


def _run(coro):
    # Repo convention (see tests/test_signing_account.py): a fresh loop that is
    # never set as the thread's current loop, so it doesn't strand loop state
    # for later tests the way asyncio.run() does (asyncio.run sets the current
    # loop to None on exit, breaking webapp/test_smoke's get_event_loop()).
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_session(wallet: str) -> swap_flow.SwapSession:
    s = swap_flow.SwapSession(
        discord_id="d",
        wallet_address=wallet,
        nft1={"name": "n1", "image": "i1"},
        nft2={"name": "n2", "image": "i2"},
        traits_to_swap=["Accessory"],
    )
    s.pay_with = "XRP"
    s.fee_per_nft = Decimal("1")
    return s


def _item() -> dict:
    return {
        "nft": {"name": "Let's Effing Go! #3536"},
        "new_nft_id": _NEW_NFT_ID,
        "image_url": "img",
        "video_url": None,
        "metadata_url": "meta",
    }


def test_self_issuer_recipient_skips_offer_and_marks_delivered(monkeypatch):
    """Recipient == issuer: never attempt a (self-)offer; record it delivered."""
    calls = []

    async def fake_offer(*a, **k):
        calls.append(1)
        return None

    monkeypatch.setattr(xrpl_ops, "create_nft_offer", fake_offer)
    monkeypatch.setattr(config, "SIGNING_ACCOUNT", "rISSUERxxxxxxxxxxxxxxxxxxxxxxxx")

    s = _make_session("rISSUERxxxxxxxxxxxxxxxxxxxxxxxx")
    item = _item()
    ok = _run(swap_flow._create_offer_and_accept(s, item))

    assert ok is True
    assert calls == []  # the offer was never even attempted
    assert s.error is None
    assert item["offer_id"] is None
    assert len(s.results) == 1
    r = s.results[0]
    assert r["modified"] is True  # drives every surface's "no action needed"
    assert r["nft_id"] == _NEW_NFT_ID
    assert "accept_deeplink" not in r


def test_non_issuer_recipient_creates_priced_offer(monkeypatch):
    """Recipient != issuer: create the sell offer and append the accept link."""

    async def fake_offer(nft_id, dest, amount=None):
        assert dest == "rUSERyyyyyyyyyyyyyyyyyyyyyyyyyy"
        return "OFFER123"

    async def fake_accept(offer_id, return_url=None):
        return {"qr_url": "q", "xumm_url": "x"}

    monkeypatch.setattr(xrpl_ops, "create_nft_offer", fake_offer)
    monkeypatch.setattr(xumm_ops, "create_accept_offer_payload", fake_accept)
    monkeypatch.setattr(config, "SIGNING_ACCOUNT", "rISSUERxxxxxxxxxxxxxxxxxxxxxxxx")

    s = _make_session("rUSERyyyyyyyyyyyyyyyyyyyyyyyyyy")
    item = _item()
    ok = _run(swap_flow._create_offer_and_accept(s, item))

    assert ok is True
    assert item["offer_id"] == "OFFER123"
    assert s.error is None
    assert len(s.results) == 1
    assert s.results[0]["accept_deeplink"] == "x"
    assert s.results[0]["modified"] is False


def test_offer_creation_failure_surfaces_admin_error(monkeypatch):
    """create_nft_offer returning None fails with the admin message, no result."""

    async def fake_offer(*a, **k):
        return None

    monkeypatch.setattr(xrpl_ops, "create_nft_offer", fake_offer)
    monkeypatch.setattr(config, "SIGNING_ACCOUNT", "rISSUERxxxxxxxxxxxxxxxxxxxxxxxx")

    s = _make_session("rUSERyyyyyyyyyyyyyyyyyyyyyyyyyy")
    item = _item()
    ok = _run(swap_flow._create_offer_and_accept(s, item))

    assert ok is False
    assert "offer failed" in (s.error or "")
    assert s.results == []
