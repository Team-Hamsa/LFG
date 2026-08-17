# tests/test_telegram_deeplink.py
# #142 — Telegram captions must carry the tappable Xaman deep link alongside
# the QR photo. Telegram auto-links plain URLs in captions, so including the
# raw xumm.app sign URL is the mobile tap-to-open path; previously the swap
# fee and swap claim captions were QR-only. Pure caption builders — no SDK.
from surfaces.telegram_bot import render, swap_render

LINK = "https://xumm.app/sign/abc"


def test_mint_payment_caption_carries_deeplink():
    assert LINK in render.payment_caption(LINK)


def test_mint_offer_caption_carries_deeplink():
    assert LINK in render.offer_caption({"nft_number": 1, "accept_deeplink": LINK})


def test_swap_payment_caption_carries_deeplink():
    cap = swap_render.swap_payment_caption("10", "BRIX", payment_link=LINK)
    assert LINK in cap


def test_swap_result_caption_carries_accept_deeplink():
    cap = swap_render.swap_result_caption(
        {"name": "LFGO #2", "modified": False, "accept_deeplink": LINK}
    )
    assert LINK in cap


def test_swap_result_caption_modified_needs_no_link():
    cap = swap_render.swap_result_caption({"name": "LFGO #1", "modified": True})
    assert "no action" in cap.lower()
