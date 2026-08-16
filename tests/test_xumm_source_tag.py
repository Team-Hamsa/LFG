# XUMM sign-request payloads must carry the Make Waves source tag (except the
# SignIn pseudo-transaction).

import asyncio

from lfg_core import config, xumm_ops


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


def _capture(monkeypatch):
    captured = {}

    def fake_post(url, json, headers, timeout):
        captured["payload"] = json
        return _Resp()

    monkeypatch.setattr(xumm_ops.requests, "post", fake_post)
    return captured


def test_accept_offer_payload_has_source_tag(monkeypatch):
    captured = _capture(monkeypatch)
    _run(xumm_ops.create_accept_offer_payload("OFFER1"))
    assert captured["payload"]["txjson"]["SourceTag"] == config.SOURCE_TAG


def test_payment_payload_has_source_tag(monkeypatch):
    captured = _capture(monkeypatch)
    _run(xumm_ops.create_payment_payload("rDest", value="1"))
    assert captured["payload"]["txjson"]["SourceTag"] == config.SOURCE_TAG


def test_signin_payload_has_no_source_tag(monkeypatch):
    captured = _capture(monkeypatch)
    _run(xumm_ops.create_signin_payload())
    assert "SourceTag" not in captured["payload"]["txjson"]
