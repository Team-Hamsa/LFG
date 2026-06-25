import asyncio
from types import SimpleNamespace

from surfaces._client.errors import BadRequest
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
        self.photos.append((chat_id, caption))

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
        final={"state": "offer_ready", "nft_number": 3600, "accept_deeplink": "https://accept"},
    )
    _run(mint_view.handle_mint(svc, update, ctx))
    # two photos: payment QR + offer QR
    assert len(bot.photos) == 2
    assert "3600" in bot.photos[1][1]


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
