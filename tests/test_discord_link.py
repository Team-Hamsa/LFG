import asyncio
from types import SimpleNamespace

from surfaces._client.errors import ServiceError
from surfaces.discord_bot import link_view


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _FakeUser:
    id = 9

    def __str__(self) -> str:
        return "d#1"


def _interaction():
    sent = []

    async def defer(ephemeral=True):
        return None

    async def followup_send(embed=None, file=None, ephemeral=True):
        sent.append(embed)

    inter = SimpleNamespace(
        user=_FakeUser(),
        response=SimpleNamespace(defer=defer),
        followup=SimpleNamespace(send=followup_send),
    )
    return inter, sent


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
    inter, sent = _interaction()
    account = {
        "wallet": "rW",
        "identities": [
            {"platform": "discord", "platform_user_id": "9", "display_handle": "d#1"},
            {"platform": "telegram", "platform_user_id": "T", "display_handle": "alice_tg"},
        ],
    }
    svc = _Svc(
        start={"uuid": "u1", "signin_link": "https://xumm.app/sign/abc"},
        final={"state": "signed", "wallet": "rW", "account": account},
    )
    _run(link_view.handle_link(svc, inter))
    descs = [e.description or "" for e in sent if e is not None]
    assert any("Telegram" in d and "alice_tg" in d for d in descs)
    assert svc.link_start_username == "d#1"


def test_link_signed_different_wallet_only_self():
    inter, sent = _interaction()
    account = {
        "wallet": "rFRESH",
        "identities": [
            {"platform": "discord", "platform_user_id": "9", "display_handle": "d#1"},
        ],
    }
    svc = _Svc(
        start={"uuid": "u1", "signin_link": "https://xumm.app/sign/abc"},
        final={"state": "signed", "wallet": "rFRESH", "account": account},
    )
    _run(link_view.handle_link(svc, inter))
    descs = [e.description or "" for e in sent if e is not None]
    assert any("rFRESH" in d for d in descs)
    assert not any("Telegram" in d for d in descs)


def test_link_service_error_reports_friendly():
    inter, sent = _interaction()
    svc = _Svc(start=ServiceError("down", status=503), final={})
    _run(link_view.handle_link(svc, inter))
    assert sent
    assert any("down" in (e.description or "") for e in sent if e is not None)
