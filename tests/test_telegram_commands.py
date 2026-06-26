import asyncio
from types import SimpleNamespace

import pytest

from surfaces._client.errors import ServiceError


@pytest.fixture
def cmds(monkeypatch):
    for k, v in {
        "TELEGRAM_BOT_TOKEN": "t",
        "LFG_SERVICE_URL": "http://svc",
        "SERVICE_TOKEN_TELEGRAM": "s",
        "TELEGRAM_ANNOUNCE_CHAT_ID": "1",
    }.items():
        monkeypatch.setenv(k, v)
    import surfaces.telegram_bot.commands as c

    return c


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _update():
    sent = []

    async def reply_text(msg):
        sent.append(msg)

    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=55, username="tg", full_name="TG"),
        message=SimpleNamespace(reply_text=reply_text),
    )
    return update, sent


class _OkSvc:
    def __init__(self):
        self.calls = []

    async def register(self, uid, name, wallet):
        self.calls.append((uid, name, wallet))
        return {"ok": True}


def test_register_happy_path(cmds):
    update, sent = _update()
    ctx = SimpleNamespace(args=["rWALLET"])
    svc = _OkSvc()
    _run(cmds._register_impl(update, ctx, _svc=svc))
    assert svc.calls == [("55", "tg", "rWALLET")]
    assert "registered" in sent[0].lower()


def test_register_missing_arg_shows_usage(cmds):
    update, sent = _update()
    ctx = SimpleNamespace(args=[])
    _run(cmds._register_impl(update, ctx, _svc=_OkSvc()))
    assert "/register" in sent[0]


def test_register_service_error_surfaced(cmds):
    update, sent = _update()
    ctx = SimpleNamespace(args=["rW"])

    class _ErrSvc:
        async def register(self, *a):
            raise ServiceError("nope")

    _run(cmds._register_impl(update, ctx, _svc=_ErrSvc()))
    assert "nope" in sent[0]
