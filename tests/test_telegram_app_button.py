# tests/test_telegram_app_button.py
# Telegram Mini App launch surfaces (#89, Part A): both gated on
# config.TELEGRAM_MINI_APP_URL. The /start menu gains a "🎮 Open App" WebApp
# inline button only when the URL is set; _post_init sets the BotFather chat
# menu button only when the URL is set. Feature dormant when unset.
import asyncio
import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import surfaces.telegram_bot.commands as cmds


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _reload_config(monkeypatch, url):
    for k, v in {
        "TELEGRAM_BOT_TOKEN": "t",
        "LFG_SERVICE_URL": "http://svc",
        "SERVICE_TOKEN_TELEGRAM": "s",
        "TELEGRAM_ANNOUNCE_CHAT_ID": "1",
    }.items():
        monkeypatch.setenv(k, v)
    if url is None:
        monkeypatch.delenv("TELEGRAM_MINI_APP_URL", raising=False)
    else:
        monkeypatch.setenv("TELEGRAM_MINI_APP_URL", url)
    import surfaces.telegram_bot.config as cfg

    importlib.reload(cfg)
    importlib.reload(cmds)
    return cfg


def _start_update():
    sent = {}

    async def reply_text(text, reply_markup=None):
        sent["text"] = text
        sent["markup"] = reply_markup

    update = SimpleNamespace(message=SimpleNamespace(reply_text=reply_text))
    return update, sent


def _webapp_buttons(markup):
    return [
        btn
        for row in markup.inline_keyboard
        for btn in row
        if getattr(btn, "web_app", None) is not None
    ]


def test_start_menu_includes_app_button_when_configured(monkeypatch):
    _reload_config(monkeypatch, "https://lfg.example.com")
    update, sent = _start_update()
    _run(cmds.start(update, SimpleNamespace()))
    webapps = _webapp_buttons(sent["markup"])
    assert len(webapps) == 1
    assert webapps[0].web_app.url == "https://lfg.example.com"


def test_start_menu_omits_app_button_when_unset(monkeypatch):
    _reload_config(monkeypatch, None)
    update, sent = _start_update()
    _run(cmds.start(update, SimpleNamespace()))
    assert _webapp_buttons(sent["markup"]) == []
    # The base menu (mint/swap/register) is unchanged.
    callbacks = {
        btn.callback_data
        for row in sent["markup"].inline_keyboard
        for btn in row
        if getattr(btn, "callback_data", None)
    }
    assert callbacks == {"mint", "swap", "register"}


def test_post_init_sets_menu_button_when_configured(monkeypatch):
    cfg = _reload_config(monkeypatch, "https://lfg.example.com")
    import surfaces.telegram_bot.bot as b

    importlib.reload(b)
    monkeypatch.setattr(b.svc, "__aenter__", AsyncMock(return_value=b.svc))
    monkeypatch.setattr(b, "run_event_loop", AsyncMock())

    set_menu = AsyncMock()
    bot = SimpleNamespace(set_chat_menu_button=set_menu)
    app = SimpleNamespace(bot=bot)

    async def scenario():
        await b._post_init(app)
        await b._post_shutdown(app)

    _run(scenario())
    set_menu.assert_awaited_once()
    menu_button = set_menu.await_args.kwargs["menu_button"]
    assert menu_button.web_app.url == cfg.TELEGRAM_MINI_APP_URL


def test_post_init_omits_menu_button_when_unset(monkeypatch):
    _reload_config(monkeypatch, None)
    import surfaces.telegram_bot.bot as b

    importlib.reload(b)
    monkeypatch.setattr(b.svc, "__aenter__", AsyncMock(return_value=b.svc))
    monkeypatch.setattr(b, "run_event_loop", AsyncMock())

    set_menu = AsyncMock()
    bot = SimpleNamespace(set_chat_menu_button=set_menu)
    app = SimpleNamespace(bot=bot)

    async def scenario():
        await b._post_init(app)
        await b._post_shutdown(app)

    _run(scenario())
    set_menu.assert_not_awaited()
