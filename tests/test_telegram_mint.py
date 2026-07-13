import asyncio
from types import SimpleNamespace

from surfaces._client.errors import BadRequest, ServiceError
from surfaces.telegram_bot import mint_view


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _Bot:
    def __init__(self):
        self.photos = []
        self.messages = []

    async def send_photo(self, chat_id, photo, caption=None):
        self.photos.append((chat_id, photo, caption))

    async def send_message(self, chat_id, text):
        self.messages.append((chat_id, text))


def _update_ctx(bot, uid="55"):
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=int(uid), username="tg", full_name="TG User"),
        effective_chat=SimpleNamespace(id=999),
    )
    ctx = SimpleNamespace(bot=bot, args=[])
    return update, ctx


class _Svc:
    def __init__(self, start, final, qr=b"PNG"):
        self._start = start
        self._final = final
        self._qr = qr

    async def start_mint(self, user_id, *, username=""):
        if isinstance(self._start, Exception):
            raise self._start
        return self._start

    async def qr_png(self, data):
        return self._qr

    async def wait_for_mint(self, user_id, session_id):
        return self._final


def test_happy_path_sends_payment_and_offer_qr():
    bot = _Bot()
    update, ctx = _update_ctx(bot)
    svc = _Svc(
        start={"id": "sid", "payment_link": "https://pay"},
        final={
            "state": "offer_ready",
            "nft_number": 3600,
            "image_url": "https://cdn/art.png",
            "accept_deeplink": "https://accept",
        },
    )
    _run(mint_view.handle_mint(svc, update, ctx))
    # three photos: payment QR + artwork + offer QR. caption (with the number)
    # is on the final offer photo (index 2).
    assert len(bot.photos) == 3
    # artwork photo carries the image_url and an artwork caption
    assert bot.photos[1][1] == "https://cdn/art.png"
    assert "3600" in bot.photos[1][2]
    assert "3600" in bot.photos[2][2]


def test_hosted_qr_url_used_directly():
    bot = _Bot()
    update, ctx = _update_ctx(bot)
    svc = _Svc(
        start={"id": "sid", "payment_link": "https://pay"},
        final={"state": "done", "nft_number": 7, "accept_qr_url": "https://cdn/qr.png"},
    )
    _run(mint_view.handle_mint(svc, update, ctx))
    # offer photo sent with the hosted URL as the photo arg
    assert bot.photos[1][0] == 999
    assert bot.photos[1][1] == "https://cdn/qr.png"


def test_free_mint_skips_payment_qr():
    bot = _Bot()
    update, ctx = _update_ctx(bot)
    svc = _Svc(
        start={"id": "sid", "free": True, "payment_link": ""},
        final={"state": "done", "nft_number": 7, "accept_qr_url": "https://cdn/qr.png"},
    )
    _run(mint_view.handle_mint(svc, update, ctx))
    # free path: no payment QR photo; only the offer photo goes out
    assert len(bot.photos) == 1
    assert bot.photos[0][1] == "https://cdn/qr.png"
    # the free-mint confirmation is sent as a plain message
    assert any("free mint" in m[1].lower() for m in bot.messages)


def test_no_wallet_sends_register_hint():
    bot = _Bot()
    update, ctx = _update_ctx(bot)
    svc = _Svc(start=BadRequest("no wallet registered", status=400), final={})
    _run(mint_view.handle_mint(svc, update, ctx))
    assert bot.messages and "register" in bot.messages[0][1].lower()


def test_bad_terminal_state_reports_failure():
    bot = _Bot()
    update, ctx = _update_ctx(bot)
    svc = _Svc(
        start={"id": "sid", "payment_link": "https://pay"},
        final={"state": "payment_timeout"},
    )
    _run(mint_view.handle_mint(svc, update, ctx))
    assert any("timed out" in m[1].lower() for m in bot.messages)


class _SvcQrFailsOnAccept(_Svc):
    """Payment QR renders fine; the accept-deeplink QR render fails."""

    def __init__(self, start, final):
        super().__init__(start, final)
        self._n = 0

    async def qr_png(self, data):
        self._n += 1
        if self._n >= 2:
            raise ServiceError("qr down")
        return b"PNG"


def test_offer_qr_render_failure_falls_back_to_message():
    bot = _Bot()
    update, ctx = _update_ctx(bot)
    svc = _SvcQrFailsOnAccept(
        start={"id": "sid", "payment_link": "https://pay"},
        final={"state": "offer_ready", "nft_number": 4242, "accept_deeplink": "https://accept"},
    )
    _run(mint_view.handle_mint(svc, update, ctx))
    # payment QR went out, but the offer QR render failed -> no second photo,
    # and the offer caption (carrying the nft_number) is surfaced as a message.
    assert len(bot.photos) == 1
    fallback_texts = [m[1] for m in bot.messages]
    assert any("4242" in t for t in fallback_texts)
    # fallback must NOT instruct scanning a QR (no QR was sent)
    for t in fallback_texts:
        assert "scan" not in t.lower()
        assert "qr" not in t.lower()
