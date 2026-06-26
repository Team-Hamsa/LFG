import asyncio
from types import SimpleNamespace

from surfaces._client.errors import ServiceError
from surfaces.telegram_bot import register_view


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
        effective_user=SimpleNamespace(id=int(uid), username="tg", full_name="TG"),
        effective_chat=SimpleNamespace(id=999),
    )
    return update, SimpleNamespace(bot=bot)


class _Svc:
    def __init__(self, start, final, qr=b"PNG"):
        self._start = start
        self._final = final
        self._qr = qr

    async def signin_start(self, user_id):
        if isinstance(self._start, Exception):
            raise self._start
        return self._start

    async def qr_png(self, data):
        return self._qr

    async def wait_for_signin(self, user_id, uuid):
        return self._final


def test_signed_registers_and_reports_wallet():
    bot = _Bot()
    update, ctx = _update_ctx(bot)
    svc = _Svc(
        start={"uuid": "u1", "signin_link": "https://xumm.app/sign/abc"},
        final={"state": "signed", "wallet": "rXRPL"},
    )
    _run(register_view.handle_register(svc, update, ctx))
    assert bot.photos and bot.photos[0][0] == 999  # QR sent
    assert any("rXRPL" in m[1] for m in bot.messages)  # verified wallet reported


def test_expired_reports_retry():
    bot = _Bot()
    update, ctx = _update_ctx(bot)
    svc = _Svc(
        start={"uuid": "u1", "signin_link": "https://xumm.app/sign/abc"},
        final={"state": "expired"},
    )
    _run(register_view.handle_register(svc, update, ctx))
    assert any("/register" in m[1] for m in bot.messages)


def test_service_error_at_start_reports_friendly():
    bot = _Bot()
    update, ctx = _update_ctx(bot)
    svc = _Svc(start=ServiceError("down", status=503), final={})
    _run(register_view.handle_register(svc, update, ctx))
    assert bot.messages and not bot.photos  # no QR; an error message instead
