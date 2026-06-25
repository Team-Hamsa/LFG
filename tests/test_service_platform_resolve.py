import asyncio

import lfg_service.app as app


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_resolve_uses_identity_platform(monkeypatch):
    calls = {}

    def fake_resolve(platform, uid):
        calls["args"] = (platform, uid)
        return "rWalletTELEGRAM" if platform == "telegram" else None

    monkeypatch.setattr(app.identity_store, "resolve", fake_resolve)
    # get_user must NOT be consulted for a non-discord platform
    monkeypatch.setattr(app, "get_user", lambda uid: {"address": "rLEGACY"})
    wallet = _run(app._resolve_wallet("telegram", "55"))
    assert wallet == "rWalletTELEGRAM"
    assert calls["args"] == ("telegram", "55")


def test_resolve_falls_back_to_legacy_for_discord(monkeypatch):
    monkeypatch.setattr(app.identity_store, "resolve", lambda platform, uid: None)
    monkeypatch.setattr(app, "get_user", lambda uid: {"address": "rLEGACY"})
    assert _run(app._resolve_wallet("discord", "9")) == "rLEGACY"


def test_resolve_no_legacy_fallback_for_non_discord(monkeypatch):
    monkeypatch.setattr(app.identity_store, "resolve", lambda platform, uid: None)
    monkeypatch.setattr(app, "get_user", lambda uid: {"address": "rLEGACY"})
    assert _run(app._resolve_wallet("telegram", "55")) is None
