import asyncio

import pytest


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _Resp:
    @staticmethod
    def json():
        return {"refs": {"qr_png": "q"}, "next": {"always": "n"}, "uuid": "u"}


@pytest.fixture
def trustline(monkeypatch):
    for k, v in {
        "DISCORD_BOT_TOKEN": "t",
        "ADMIN_LOG_CHANNEL_ID": "1",
        "LFG_SERVICE_URL": "http://svc",
        "SERVICE_TOKEN_DISCORD": "s",
        "XUMM_API_KEY": "k",
        "XUMM_API_SECRET": "s",
        "TOKEN_ISSUER_ADDRESS": "rIssuer",
        "TOKEN_CURRENCY_HEX": "ABC",
        "SEED": "sEdSKaCy2JT7JaM7v95H9SxkhP9wS2r",
    }.items():
        monkeypatch.setenv(k, v)
    import importlib

    import surfaces.discord_bot.config as cfg

    importlib.reload(cfg)
    import surfaces.discord_bot.trustline as tl

    importlib.reload(tl)
    return tl


def test_trustline_payload_has_source_tag(trustline, monkeypatch):
    captured = {}

    def fake_post(url, json, headers, timeout=None):
        captured["payload"] = json
        return _Resp()

    monkeypatch.setattr(trustline.requests, "post", fake_post)
    _run(trustline.create_trustline_request())
    assert captured["payload"]["txjson"]["SourceTag"] == 2606160021


def test_trustline_payload_has_provenance_memos(trustline, monkeypatch):
    from xrpl.utils import hex_to_str

    captured = {}

    def fake_post(url, json, headers, timeout=None):
        captured["payload"] = json
        return _Resp()

    monkeypatch.setattr(trustline.requests, "post", fake_post)
    _run(trustline.create_trustline_request())
    decoded = {
        hex_to_str(e["Memo"]["MemoType"]): hex_to_str(e["Memo"]["MemoData"])
        for e in captured["payload"]["txjson"]["Memos"]
    }
    assert decoded == {
        "initiator": "user",
        "platform": "discord-bot",
        "action": "trustset",
    }
