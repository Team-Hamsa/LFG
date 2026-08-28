import asyncio
import json

import lfg_service.app as app
from lfg_core import config


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_wc_surfaces_is_a_frozenset():
    assert isinstance(config.WC_SURFACES, frozenset)


def test_parse_wc_surfaces():
    assert config.parse_wc_surfaces("web, Telegram ,,") == frozenset({"web", "telegram"})
    assert config.parse_wc_surfaces("") == frozenset()


def test_config_reports_null_when_disabled(monkeypatch):
    monkeypatch.setattr(config, "REOWN_PROJECT_ID", "")
    body = json.loads(_run(app.handle_config(None)).text)
    assert body["walletconnect"] is None


def test_config_reports_block_when_enabled(monkeypatch):
    monkeypatch.setattr(config, "REOWN_PROJECT_ID", "pid123")
    monkeypatch.setattr(config, "WC_CHAIN", "xrpl:1")
    monkeypatch.setattr(config, "WC_SURFACES", frozenset({"web"}))
    body = json.loads(_run(app.handle_config(None)).text)
    assert body["walletconnect"] == {"project_id": "pid123", "chain": "xrpl:1", "surfaces": ["web"]}
