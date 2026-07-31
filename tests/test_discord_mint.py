# Drives the inverted mint handler (surfaces/discord_bot/mint_view.handle_mint)
# with a fake svc + fake interaction. Field names mirror the REAL service
# contract (lfg_core.mint_flow.MintSession.to_dict): session key is "id",
# accept link is "accept_qr_url"/"accept_deeplink", terminal states are
# offer_ready/done/failed/payment_timeout. No live service or XRPL.
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from surfaces._client.errors import BadRequest, ServiceError


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture
def mint_mod(monkeypatch):
    for k, v in {
        "DISCORD_BOT_TOKEN": "t",
        "ADMIN_LOG_CHANNEL_ID": "1",
        "LFG_SERVICE_URL": "http://svc",
        "SERVICE_TOKEN_DISCORD": "s",
        "SEED": "sEdSKaCy2JT7JaM7v95H9SxkhP9wS2r",
        "XUMM_API_KEY": "k",
        "XUMM_API_SECRET": "s",
        "TOKEN_ISSUER_ADDRESS": "rIssuer",
        "TOKEN_CURRENCY_HEX": "ABC",
    }.items():
        monkeypatch.setenv(k, v)
    import importlib

    import surfaces.discord_bot.config as cfg

    importlib.reload(cfg)
    import surfaces.discord_bot.render as render

    importlib.reload(render)
    import surfaces.discord_bot.mint_view as mv

    importlib.reload(mv)
    return mv


def _ix():
    ix = MagicMock()
    ix.user.id = 7
    ix.user.__str__ = lambda self: "bob#0002"
    ix.response.defer = AsyncMock()
    ix.followup.send = AsyncMock()
    return ix


def _svc():
    svc = MagicMock()
    svc.start_mint = AsyncMock(
        return_value={"id": "sid", "payment_link": "L", "state": "awaiting_payment"}
    )
    svc.qr_png = AsyncMock(return_value=b"\x89PNG")
    svc.wait_for_mint = AsyncMock(
        return_value={
            "id": "sid",
            "state": "offer_ready",
            "nft_number": 3600,
            "image_url": "https://cdn/x.png",
            "accept_qr_url": "https://cdn/qr.png",
            "accept_deeplink": "https://xumm/accept",
        }
    )
    return svc


def test_mint_happy_path(mint_mod):
    svc, ix = _svc(), _ix()
    _run(mint_mod.handle_mint(svc, ix))
    svc.start_mint.assert_awaited_once_with("7", username="bob#0002")
    svc.wait_for_mint.assert_awaited_once_with("7", "sid")
    # payment QR is rendered from payment_link; offer uses hosted accept_qr_url
    # (no second qr_png round-trip).
    svc.qr_png.assert_awaited_once_with("L")
    assert ix.followup.send.await_count == 2
    embeds = ix.followup.send.await_args_list[1].kwargs["embeds"]
    offer_embed = embeds[0]
    assert "Minted Successfully" in offer_embed.title
    # the large artwork embed is included alongside the offer embed
    art = [e for e in embeds if "Your NFT" in (e.title or "")]
    assert art and art[0].image.url == "https://cdn/x.png"


def test_mint_offer_falls_back_to_qr_png(mint_mod):
    svc, ix = _svc(), _ix()
    svc.wait_for_mint = AsyncMock(
        return_value={
            "id": "sid",
            "state": "done",
            "nft_number": 3601,
            "accept_qr_url": None,
            "accept_deeplink": "https://xumm/accept2",
        }
    )
    _run(mint_mod.handle_mint(svc, ix))
    # no hosted QR -> the handler renders the accept deeplink itself
    assert svc.qr_png.await_count == 2
    svc.qr_png.assert_awaited_with("https://xumm/accept2")
    assert ix.followup.send.await_count == 2


def test_mint_no_wallet_maps_to_register(mint_mod):
    svc, ix = _svc(), _ix()
    svc.start_mint = AsyncMock(side_effect=BadRequest("no wallet registered", status=400))
    _run(mint_mod.handle_mint(svc, ix))
    assert ix.followup.send.await_count == 1
    embed = ix.followup.send.await_args.kwargs["embed"]
    assert "register" in embed.description.lower()


def test_mint_payment_timeout(mint_mod):
    svc, ix = _svc(), _ix()
    svc.wait_for_mint = AsyncMock(return_value={"id": "sid", "state": "payment_timeout"})
    _run(mint_mod.handle_mint(svc, ix))
    embed = ix.followup.send.await_args.kwargs["embed"]
    assert "timed out" in embed.description.lower()


def test_mint_failed_state(mint_mod):
    svc, ix = _svc(), _ix()
    svc.wait_for_mint = AsyncMock(return_value={"id": "sid", "state": "failed"})
    _run(mint_mod.handle_mint(svc, ix))
    embed = ix.followup.send.await_args.kwargs["embed"]
    assert "failed" in embed.description.lower()


def test_mint_already_in_progress(mint_mod):
    svc, ix = _svc(), _ix()
    svc.start_mint = AsyncMock(side_effect=ServiceError("mint already in progress", status=409))
    _run(mint_mod.handle_mint(svc, ix))
    embed = ix.followup.send.await_args.kwargs["embed"]
    assert "in progress" in embed.description.lower()


def test_sponsored_session_skips_the_payment_qr_and_still_delivers_the_offer(mint_mod):
    """Regression: sponsored admission happens in the shared service handler, so
    ANY surface can be admitted — but only the Activity client had a sponsored
    branch. A sponsored session carries payment_link=None; qr_png(None) raises
    TypeError, which is not a ServiceError and so escaped the handler's except.
    The service kept minting in the background, so the user's one free mint was
    consumed and 1 LFGO burned while this surface died before ever showing the
    accept QR."""
    svc, ix = _svc(), _ix()
    svc.start_mint = AsyncMock(
        return_value={
            "id": "sid",
            "payment_link": None,
            "state": "awaiting_payment",
            "sponsored": True,
            "pay_with": "SPONSORED",
            "pay_amount": "0",
        }
    )

    _run(mint_mod.handle_mint(svc, ix))

    # No payment QR was requested for the (nonexistent) payment link...
    assert svc.qr_png.await_args_list == []
    # ...and the flow still ran to the offer step.
    svc.wait_for_mint.assert_awaited_once_with("7", "sid")
    assert ix.followup.send.await_count == 2
    first = ix.followup.send.await_args_list[0].kwargs["embed"]
    assert "Sponsored" in first.title
    assert "No XRP or LFGO" in first.description
    # Honesty: the accept is still an on-ledger tx the user signs.
    assert "reserve" in first.description.lower()
