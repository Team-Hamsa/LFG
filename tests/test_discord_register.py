import asyncio
from types import SimpleNamespace

from surfaces._client.errors import ServiceError
from surfaces.discord_bot import register_view


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _interaction():
    sent = []

    async def defer(ephemeral=True):
        return None

    async def followup_send(embed=None, file=None, ephemeral=True):
        sent.append(embed)

    inter = SimpleNamespace(
        user=SimpleNamespace(id=9, __str__=lambda self: "d#1"),
        response=SimpleNamespace(defer=defer),
        followup=SimpleNamespace(send=followup_send),
    )
    return inter, sent


class _Svc:
    def __init__(self, start, final, qr=b"PNG"):
        self._start, self._final, self._qr = start, final, qr

    async def signin_start(self, user_id):
        if isinstance(self._start, Exception):
            raise self._start
        return self._start

    async def qr_png(self, data):
        return self._qr

    async def wait_for_signin(self, user_id, uuid):
        return self._final


def test_signed_reports_wallet():
    inter, sent = _interaction()
    svc = _Svc(
        start={"uuid": "u1", "signin_link": "https://xumm.app/sign/abc"},
        final={"state": "signed", "wallet": "rXRPL"},
    )
    _run(register_view.handle_register(svc, inter))
    # at least the QR embed + a success embed mentioning the wallet
    assert any("rXRPL" in (e.description or "") for e in sent if e is not None)


def test_service_error_reports_friendly():
    inter, sent = _interaction()
    svc = _Svc(start=ServiceError("down", status=503), final={})
    _run(register_view.handle_register(svc, inter))
    assert sent  # an error embed was sent
    assert any("down" in (e.description or "") for e in sent if e is not None)
