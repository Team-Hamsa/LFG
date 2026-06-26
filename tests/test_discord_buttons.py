# tests/test_discord_buttons.py
# MintView gains a Register button (#87) that delegates to the same surface-
# agnostic handle_register the /register slash command uses. Mirrors the
# MagicMock-interaction pattern from test_discord_mint.py.
import asyncio
import importlib
from unittest.mock import AsyncMock, MagicMock

import pytest


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture
def views_mod(monkeypatch):
    for k, v in {
        "DISCORD_BOT_TOKEN": "t",
        "ADMIN_LOG_CHANNEL_ID": "1",
        "LFG_SERVICE_URL": "http://svc",
        "SERVICE_TOKEN_DISCORD": "s",
        "SEED": "sEdSKaCy2JT7JaM7v95H9SxkhP9wS2r",
        "XUMM_API_KEY": "k",
        "XUMM_API_SECRET": "s",
        "TOKEN_ISSUER_ADDRESS": "rIssuer",
        "TOKEN_CURRENCY_HEX": "ABC",
    }.items():
        monkeypatch.setenv(k, v)
    import surfaces.discord_bot.config as cfg

    importlib.reload(cfg)
    # Import bot first so views' `from ...bot import svc` resolves without the
    # commands<->views circular import that a bare views import would trip.
    import surfaces.discord_bot.bot  # noqa: F401
    import surfaces.discord_bot.views as views

    return views


def test_register_button_delegates_to_handle_register(views_mod, monkeypatch):
    called = AsyncMock()
    monkeypatch.setattr(views_mod, "handle_register", called)

    view = views_mod.MintView()
    ix = MagicMock()
    # On an instance, the decorated button is a Button whose .callback is bound
    # to the view (self), so it takes just the interaction.
    _run(view.register_button.callback(ix))

    called.assert_awaited_once_with(views_mod.svc, ix)
