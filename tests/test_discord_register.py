import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from surfaces._client.errors import BadRequest


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture
def reg(monkeypatch):
    for k, v in {
        "DISCORD_BOT_TOKEN": "t",
        "ADMIN_LOG_CHANNEL_ID": "1",
        "LFG_SERVICE_URL": "http://svc",
        "SERVICE_TOKEN_DISCORD": "s",
        "XUMM_API_KEY": "k",
        "XUMM_API_SECRET": "s",
        "TOKEN_ISSUER_ADDRESS": "rI",
        "TOKEN_CURRENCY_HEX": "ABC",
    }.items():
        monkeypatch.setenv(k, v)
    import importlib

    import surfaces.discord_bot.config as cfg

    importlib.reload(cfg)
    import surfaces.discord_bot.commands as cmds

    # Do NOT reload cmds — @tree.command re-registration would fail.
    # _register_impl is the testable impl; the @tree.command shell just delegates.
    return cmds


def _fake_interaction():
    ix = MagicMock()
    ix.user.id = 42
    ix.user.__str__ = lambda self: "alice#0001"
    ix.response.send_message = AsyncMock()
    return ix


def test_register_calls_service_and_confirms(reg):
    fake_svc = MagicMock()
    fake_svc.register = AsyncMock(return_value={"ok": True})
    ix = _fake_interaction()
    _run(reg._register_impl(ix, "rWALLET", _svc=fake_svc))
    fake_svc.register.assert_awaited_once_with("42", "alice#0001", "rWALLET")
    ix.response.send_message.assert_awaited_once()
    assert "registered" in ix.response.send_message.call_args.args[0].lower()


def test_register_maps_service_error_to_failure(reg):
    fake_svc = MagicMock()
    fake_svc.register = AsyncMock(side_effect=BadRequest("bad wallet", status=400))
    ix = _fake_interaction()
    _run(reg._register_impl(ix, "nope", _svc=fake_svc))
    ix.response.send_message.assert_awaited_once()
