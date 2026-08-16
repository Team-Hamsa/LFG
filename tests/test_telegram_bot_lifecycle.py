# tests/test_telegram_bot_lifecycle.py
import asyncio

import pytest


@pytest.fixture
def bot_mod(monkeypatch):
    for k, v in {
        "TELEGRAM_BOT_TOKEN": "t",
        "LFG_SERVICE_URL": "http://svc",
        "SERVICE_TOKEN_TELEGRAM": "s",
        "TELEGRAM_ANNOUNCE_CHAT_ID": "1",
    }.items():
        monkeypatch.setenv(k, v)
    import importlib

    import surfaces.telegram_bot.config as cfg

    importlib.reload(cfg)
    import surfaces.telegram_bot.bot as b

    importlib.reload(b)
    return b


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_svc_configured_for_telegram(bot_mod):
    assert bot_mod.svc._surface == "telegram"
    assert bot_mod.svc._service_token == "s"


def test_post_shutdown_cancels_events_and_closes_svc(bot_mod, monkeypatch):
    closed = {"svc": False}

    async def fake_aenter():
        return bot_mod.svc

    async def fake_close():
        closed["svc"] = True

    monkeypatch.setattr(bot_mod.svc, "__aenter__", fake_aenter)
    monkeypatch.setattr(bot_mod.svc, "close", fake_close)

    # Fake an Application whose bot has an async send_message
    class _Bot:
        async def send_message(self, **kw):
            pass

    app = type("App", (), {"bot": _Bot()})()

    # post_init should enter svc and start a (cancellable) events task
    async def never_ending(svc, announce, dm):
        ev = asyncio.Event()
        await ev.wait()

    monkeypatch.setattr(bot_mod, "run_event_loop", never_ending)

    async def scenario():
        await bot_mod._post_init(app)
        assert bot_mod._events_task is not None
        await bot_mod._post_shutdown(app)
        assert bot_mod._events_task is None
        assert closed["svc"] is True

    _run(scenario())
