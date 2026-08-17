# tests/test_telegram_deeplink.py
# #142 — Telegram captions must carry the tappable Xaman deep link alongside
# the QR photo. Telegram auto-links plain URLs in captions, so including the
# raw xumm.app sign URL is the mobile tap-to-open path; previously the swap
# fee and swap claim captions were QR-only. Pure caption builders — no SDK,
# plus one flow regression: a failed QR render must not abandon the session
# when the deep link is still perfectly signable.
import asyncio
from types import SimpleNamespace

from surfaces._client.errors import ServiceError
from surfaces.telegram_bot import render, swap_render, swap_view

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


# ---------------------------------------------------------------------------
# Flow regression: a failed fee-QR render must fall back to a plain-text
# caption carrying the deep link and keep waiting for the payment — not
# abandon a session the user can still complete via the link.
# ---------------------------------------------------------------------------


class _Bot:
    def __init__(self):
        self.messages = []
        self.photos = []

    async def send_message(self, chat_id, text):
        self.messages.append(text)

    async def send_photo(self, chat_id, photo, caption=None):
        self.photos.append(caption)


class _Query:
    def __init__(self):
        self.data = "swap_confirm"
        self.message = SimpleNamespace(chat_id=999)

    async def answer(self, *a, **k):
        pass

    async def edit_message_text(self, text, reply_markup=None):
        pass


class _Svc:
    """start_swap lands in awaiting_payment; qr_png always fails."""

    def __init__(self):
        self.waited = False

    async def start_swap(self, user_id, nft1_id, nft2_id, traits, *, username=""):
        return {
            "id": "s1",
            "state": "awaiting_payment",
            "payment_link": LINK,
            "fee_amount": "10",
            "pay_with": "BRIX",
        }

    async def qr_png(self, data):
        raise ServiceError("qr backend down", status=503)

    async def wait_for_swap(self, user_id, session_id):
        self.waited = True
        return {"state": "done", "results": []}


def test_swap_confirm_qr_failure_sends_deeplink_and_keeps_waiting():
    bot = _Bot()
    svc = _Svc()
    update = SimpleNamespace(
        callback_query=_Query(),
        effective_user=SimpleNamespace(id=55, username="tg", full_name="TG User"),
        effective_chat=SimpleNamespace(id=999),
    )
    ctx = SimpleNamespace(
        bot=bot,
        user_data={
            "swap_session": {
                "nft1_id": "A",
                "nft2_id": "B",
                "traits": {"Head": True},
                "page": 0,
            }
        },
    )
    asyncio.new_event_loop().run_until_complete(swap_view.handle_swap_confirm(svc, update, ctx))
    # No QR photo, but the caption went out as text with the tappable link…
    assert bot.photos == []
    assert any(LINK in m for m in bot.messages)
    # …and the flow kept waiting for the payment instead of returning early.
    assert svc.waited is True
