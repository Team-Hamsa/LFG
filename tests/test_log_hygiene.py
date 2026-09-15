# tests/test_log_hygiene.py — httpx request lines must never reach process logs.
# python-telegram-bot puts the bot token in every Bot API URL, and httpx logs
# each request URL at INFO, so the Telegram surface leaked its token to the pm2
# error log on every getUpdates poll.
import ast
import importlib
import logging
import pathlib

import httpx
import pytest

from lfg_core.log_hygiene import HTTP_CLIENT_LOGGERS, quiet_http_client_loggers

_SECRET = "AAFakeTokenValueForTheLeakTest0123456789"
_URL = f"https://api.telegram.org/bot123456:{_SECRET}/getUpdates"


@pytest.fixture
def reset_http_loggers():
    saved = {n: logging.getLogger(n).level for n in HTTP_CLIENT_LOGGERS}
    for n in HTTP_CLIENT_LOGGERS:
        logging.getLogger(n).setLevel(logging.NOTSET)
    yield
    for n, level in saved.items():
        logging.getLogger(n).setLevel(level)


def _request_and_capture(caplog) -> str:
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json={"ok": True}))
    with caplog.at_level(logging.INFO), httpx.Client(transport=transport) as client:
        client.get(_URL)
    return caplog.text


def test_httpx_logs_request_urls_at_info_by_default(reset_http_loggers, caplog):
    # Control: proves the leak is real, so the next test can't pass vacuously.
    assert _SECRET in _request_and_capture(caplog)


def test_quieted_httpx_does_not_log_request_urls(reset_http_loggers, caplog):
    quiet_http_client_loggers()
    for name in HTTP_CLIENT_LOGGERS:
        assert logging.getLogger(name).level == logging.WARNING
    assert _SECRET not in _request_and_capture(caplog)


@pytest.mark.parametrize(
    ("module", "env"),
    [
        (
            "surfaces.telegram_bot.config",
            {
                "TELEGRAM_BOT_TOKEN": "tg-tok",
                "LFG_SERVICE_URL": "http://svc",
                "SERVICE_TOKEN_TELEGRAM": "s",
                "TELEGRAM_ANNOUNCE_CHAT_ID": "12345",
            },
        ),
        (
            "surfaces.discord_bot.config",
            {
                "DISCORD_BOT_TOKEN": "tok",
                "ADMIN_LOG_CHANNEL_ID": "123",
                "LFG_SERVICE_URL": "http://svc",
                "SERVICE_TOKEN_DISCORD": "stk",
            },
        ),
        ("surfaces.x_bot.config", {}),
    ],
)
def test_surface_config_quiets_http_client_loggers(reset_http_loggers, monkeypatch, module, env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    importlib.reload(importlib.import_module(module))
    for name in HTTP_CLIENT_LOGGERS:
        assert logging.getLogger(name).level == logging.WARNING


def test_service_app_quiets_httpx_at_module_level():
    # lfg_service.app can't be safely re-imported per test, and another test may
    # already have set the level, so check the module-level call directly.
    tree = ast.parse(pathlib.Path(lfg_service_app_path()).read_text())
    calls = [
        node.value.func.id
        for node in tree.body
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
    ]
    assert "quiet_http_client_loggers" in calls


def lfg_service_app_path() -> str:
    return str(pathlib.Path(__file__).resolve().parent.parent / "lfg_service" / "app.py")
