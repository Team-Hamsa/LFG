# tests/test_xumm_push.py
# XUMM push delivery via user_token (issue #135): payload builders must forward
# a stored user_token as a top-level payload field so XUMM push-delivers the
# sign request to the user's Xaman app; the create response's `pushed` flag and
# the payload-status `issued_user_token` must be surfaced to callers. A missing
# token must never appear in the request body (so an empty string can't be
# misread as a token) and never block the flow.
#
# Env-guard preamble: importing lfg_core.config freezes its constants at import
# time; set the same defaults test_smoke.py uses so collection order can't
# strand them. (Copy the block verbatim from tests/test_market_ops.py.)
import os

os.environ.setdefault("XUMM_API_KEY", "test")
os.environ.setdefault("XUMM_API_SECRET", "test")
os.environ.setdefault("SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2")
os.environ.setdefault("TOKEN_ISSUER_ADDRESS", "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
os.environ.setdefault("TOKEN_CURRENCY_HEX", "4C46474F00000000000000000000000000000000")
os.environ.setdefault("BUNNY_CDN_ACCESS_KEY", "test")
os.environ.setdefault("BUNNY_CDN_STORAGE_ZONE", "test")
os.environ.setdefault("LAYER_SOURCE", "local")
os.environ.setdefault("BUNNY_PULL_ZONE", "nft.pullzone.example")

import asyncio  # noqa: E402

import pytest  # noqa: E402

from lfg_core import xumm_ops  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _CreateResp:
    def __init__(self, pushed=False):
        self._pushed = pushed

    def json(self):
        return {
            "refs": {"qr_png": "q"},
            "next": {"always": "n"},
            "uuid": "u",
            "pushed": self._pushed,
        }


def _capture_post(monkeypatch, pushed=False):
    captured = {}

    def fake_post(url, json, headers, timeout):
        captured["payload"] = json
        return _CreateResp(pushed=pushed)

    monkeypatch.setattr(xumm_ops.requests, "post", fake_post)
    return captured


# --- create-side: user_token is sent, and only when present ---


def test_user_token_sent_as_top_level_field(monkeypatch):
    captured = _capture_post(monkeypatch)
    _run(xumm_ops.create_accept_offer_payload("OFFER1", user_token="tok-123"))
    # user_token is a top-level payload field, NOT nested under options/txjson.
    assert captured["payload"]["user_token"] == "tok-123"
    assert "user_token" not in captured["payload"]["txjson"]


def test_no_user_token_omits_the_field(monkeypatch):
    captured = _capture_post(monkeypatch)
    _run(xumm_ops.create_accept_offer_payload("OFFER1"))
    assert "user_token" not in captured["payload"]


def test_empty_user_token_omits_the_field(monkeypatch):
    # An empty string must never be sent — XUMM would reject/ignore it and it
    # signals "no token" just like None.
    captured = _capture_post(monkeypatch)
    _run(xumm_ops.create_accept_offer_payload("OFFER1", user_token=""))
    assert "user_token" not in captured["payload"]


@pytest.mark.parametrize(
    "call",
    [
        lambda: xumm_ops.create_payment_payload("rDest", value="1", user_token="tok"),
        lambda: xumm_ops.create_accept_offer_payload("OFFER1", user_token="tok"),
        lambda: xumm_ops.create_sell_offer_payload("rAcct", "NFT1", "1000000", user_token="tok"),
        lambda: xumm_ops.create_cancel_offer_payload("rAcct", "OFFERIDX", user_token="tok"),
    ],
)
def test_every_builder_forwards_user_token(monkeypatch, call):
    captured = _capture_post(monkeypatch)
    _run(call())
    assert captured["payload"]["user_token"] == "tok"
    # SourceTag discipline is unchanged by push delivery.
    assert captured["payload"]["txjson"]["SourceTag"] == xumm_ops.config.SOURCE_TAG


def test_create_returns_pushed_true(monkeypatch):
    _capture_post(monkeypatch, pushed=True)
    result = _run(xumm_ops.create_accept_offer_payload("OFFER1", user_token="tok"))
    assert result["pushed"] is True
    # QR/deep link are always present as the universal fallback.
    assert result["qr_url"] == "q"
    assert result["xumm_url"] == "n"


def test_create_returns_pushed_false_when_absent(monkeypatch):
    # A stale token yields pushed:false (or no key at all) → caller falls back.
    _capture_post(monkeypatch, pushed=False)
    result = _run(xumm_ops.create_accept_offer_payload("OFFER1", user_token="stale"))
    assert result["pushed"] is False


# --- status-side: issued_user_token is captured ---


class _StatusResp:
    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


_UUID = "11111111-2222-3333-4444-555555555555"


def _capture_status(monkeypatch, body):
    monkeypatch.setattr(xumm_ops.requests, "get", lambda url, headers, timeout: _StatusResp(body))


def test_status_surfaces_issued_user_token(monkeypatch):
    _capture_status(
        monkeypatch,
        {
            "meta": {"signed": True, "opened": True, "expired": False},
            "response": {"account": "rSIGNER", "txid": "HASH"},
            "application": {"issued_user_token": "issued-tok-abc"},
        },
    )
    s = _run(xumm_ops.get_payload_status(_UUID))
    assert s["user_token"] == "issued-tok-abc"
    assert s["signed"] is True
    assert s["account"] == "rSIGNER"


def test_status_user_token_none_when_no_application_block(monkeypatch):
    _capture_status(
        monkeypatch,
        {"meta": {"signed": False}, "response": {}},
    )
    s = _run(xumm_ops.get_payload_status(_UUID))
    assert s["user_token"] is None
