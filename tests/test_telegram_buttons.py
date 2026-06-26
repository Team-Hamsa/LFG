# tests/test_telegram_buttons.py
# Inline-keyboard button UX for the Telegram surface (#87): the /start menu
# carries Mint + Register buttons, and the callback dispatchers reuse the SAME
# surface-agnostic handlers as the /mint and /register commands. Fakes mirror the
# CallbackQuery shape: update.message is None, update.callback_query.message is
# the live message, and effective_chat/effective_user are populated.
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from surfaces.telegram_bot import commands as cmds


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _callback_update():
    # A CallbackQuery update: message is None on the update itself; the live
    # message hangs off callback_query.message. effective_* ARE populated.
    query = SimpleNamespace(answer=AsyncMock(), message=SimpleNamespace(chat_id=999))
    return SimpleNamespace(
        message=None,
        callback_query=query,
        effective_user=SimpleNamespace(id=55, username="tg", full_name="TG User"),
        effective_chat=SimpleNamespace(id=999),
    )


def test_start_shows_mint_and_register_buttons():
    sent = {}

    async def reply_text(text, reply_markup=None):
        sent["text"] = text
        sent["markup"] = reply_markup

    update = SimpleNamespace(message=SimpleNamespace(reply_text=reply_text))
    _run(cmds.start(update, SimpleNamespace()))

    markup = sent["markup"]
    assert markup is not None  # an InlineKeyboardMarkup was attached
    callbacks = {btn.callback_data for row in markup.inline_keyboard for btn in row}
    assert callbacks == {"mint", "swap", "register"}


def test_mint_button_answers_query_and_runs_handler(monkeypatch):
    handler = AsyncMock()
    # patch the lazily-imported handler at its source
    import surfaces.telegram_bot.mint_view as mv

    monkeypatch.setattr(mv, "handle_mint", handler)

    update = _callback_update()
    ctx = SimpleNamespace(bot=object())
    _run(cmds.mint_button(update, ctx))

    update.callback_query.answer.assert_awaited_once()
    handler.assert_awaited_once()
    # the SAME svc the /mint command uses is forwarded (positional arg 0)
    from surfaces.telegram_bot.bot import svc as bot_svc

    assert handler.await_args.args[0] is bot_svc


def test_register_button_answers_query_and_runs_handler(monkeypatch):
    handler = AsyncMock()
    import surfaces.telegram_bot.register_view as rv

    monkeypatch.setattr(rv, "handle_register", handler)

    update = _callback_update()
    ctx = SimpleNamespace(bot=object())
    _run(cmds.register_button(update, ctx))

    update.callback_query.answer.assert_awaited_once()
    handler.assert_awaited_once()
    from surfaces.telegram_bot.bot import svc as bot_svc

    assert handler.await_args.args[0] is bot_svc


def test_build_application_registers_callback_handlers(monkeypatch):
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
    from telegram.ext import CallbackQueryHandler

    app = b.build_application()
    cb_handlers = [
        h for group in app.handlers.values() for h in group if isinstance(h, CallbackQueryHandler)
    ]
    # mint + register + swap menu button + 5 swap_* conversation patterns (#88)
    assert len(cb_handlers) == 8
