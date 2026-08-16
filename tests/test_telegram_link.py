import asyncio
from types import SimpleNamespace

from surfaces._client.errors import ServiceError
from surfaces.telegram_bot import link_view


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


def _update_ctx(bot, uid="T"):
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=uid, username="alice_tg", full_name="Alice"),
        effective_chat=SimpleNamespace(id=999),
    )
    return update, SimpleNamespace(bot=bot)


class _Svc:
    def __init__(self, start, final, qr=b"PNG"):
        self._start, self._final, self._qr = start, final, qr
        self.link_start_username = ""

    async def link_start(self, user_id, *, username=""):
        self.link_start_username = username
        if isinstance(self._start, Exception):
            raise self._start
        return self._start

    async def qr_png(self, data):
        return self._qr

    async def wait_for_link(self, user_id, uuid):
        return self._final


def test_link_shows_qr_then_confirms():
    bot = _Bot()
    update, ctx = _update_ctx(bot)
    account = {
        "wallet": "rW",
        "identities": [
            {"platform": "discord", "platform_user_id": "D", "display_handle": "alice"},
            {"platform": "telegram", "platform_user_id": "T", "display_handle": "alice_tg"},
        ],
    }
    svc = _Svc(
        start={"uuid": "u1", "signin_link": "https://xumm.app/sign/abc"},
        final={"state": "signed", "wallet": "rW", "account": account},
    )
    _run(link_view.handle_link(svc, update, ctx))
    assert bot.photos and bot.photos[0][0] == 999
    assert any("Discord" in m[1] and "alice" in m[1] for m in bot.messages)
    assert svc.link_start_username == "alice_tg"


def test_link_signed_different_wallet_only_self():
    bot = _Bot()
    update, ctx = _update_ctx(bot)
    account = {
        "wallet": "rFRESH",
        "identities": [
            {"platform": "telegram", "platform_user_id": "T", "display_handle": "alice_tg"},
        ],
    }
    svc = _Svc(
        start={"uuid": "u1", "signin_link": "https://xumm.app/sign/abc"},
        final={"state": "signed", "wallet": "rFRESH", "account": account},
    )
    _run(link_view.handle_link(svc, update, ctx))
    assert any("rFRESH" in m[1] for m in bot.messages)
    assert not any("Discord" in m[1] for m in bot.messages)


def test_link_service_error_reports_friendly():
    bot = _Bot()
    update, ctx = _update_ctx(bot)
    svc = _Svc(start=ServiceError("down", status=503), final={})
    _run(link_view.handle_link(svc, update, ctx))
    assert bot.messages and not bot.photos
    assert any("down" in m[1] for m in bot.messages)
