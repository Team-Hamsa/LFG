# tests/test_market_payloads.py
# Env-guard preamble: importing lfg_core.config freezes its constants (e.g.
# IMG_PROXY_ALLOWED_BASES, LAYER_SOURCE) at import time; set the same defaults
# test_smoke.py uses so collection order can't strand them. (Copy the block
# verbatim from tests/test_server_identity_wiring.py — same keys/values.)
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


# XUMM payload builders for the in-app marketplace: listing (sell offer) and
# cancel. Both are thin wrappers over `_create_xumm_payload`, which centrally
# stamps SourceTag on every txjson (lfg_core/xumm_ops.py:142-149) — these
# tests assert that central mechanism covers the new builders rather than
# setting SourceTag themselves.

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


def test_sell_offer_payload_shape(monkeypatch):
    captured = _capture(monkeypatch)
    _run(xumm_ops.create_sell_offer_payload("rSeller", "NFT123", "1000000"))
    txjson = captured["payload"]["txjson"]

    assert txjson["TransactionType"] == "NFTokenCreateOffer"
    assert txjson["Account"] == "rSeller"
    assert txjson["NFTokenID"] == "NFT123"
    assert txjson["Amount"] == "1000000"
    assert isinstance(txjson["Amount"], str)
    assert txjson["Flags"] == 1
    assert "Owner" not in txjson
    assert "Destination" not in txjson


def test_sell_offer_payload_has_source_tag(monkeypatch):
    captured = _capture(monkeypatch)
    _run(xumm_ops.create_sell_offer_payload("rSeller", "NFT123", "1000000"))
    assert captured["payload"]["txjson"]["SourceTag"] == config.SOURCE_TAG


def test_cancel_offer_payload_shape(monkeypatch):
    captured = _capture(monkeypatch)
    _run(xumm_ops.create_cancel_offer_payload("rSeller", "OFFERINDEX1"))
    txjson = captured["payload"]["txjson"]

    assert txjson["TransactionType"] == "NFTokenCancelOffer"
    assert txjson["Account"] == "rSeller"
    assert txjson["NFTokenOffers"] == ["OFFERINDEX1"]


def test_cancel_offer_payload_has_source_tag(monkeypatch):
    captured = _capture(monkeypatch)
    _run(xumm_ops.create_cancel_offer_payload("rSeller", "OFFERINDEX1"))
    assert captured["payload"]["txjson"]["SourceTag"] == config.SOURCE_TAG


# --- #239: BRIX sell offers + XRP→BRIX on-ramp self-Payment ---


def _brix_dict(value="10"):
    return {
        "currency": config.TOKEN_CURRENCY_HEX,
        "issuer": config.TOKEN_ISSUER_ADDRESS,
        "value": value,
    }


def test_sell_offer_payload_brix_dict_amount(monkeypatch):
    captured = _capture(monkeypatch)
    _run(xumm_ops.create_sell_offer_payload("rSeller", "NFT123", _brix_dict("10.5")))
    txjson = captured["payload"]["txjson"]
    assert txjson["Amount"] == _brix_dict("10.5")
    assert txjson["Flags"] == 1
    assert txjson["SourceTag"] == config.SOURCE_TAG


def test_onramp_payment_payload_shape(monkeypatch):
    captured = _capture(monkeypatch)
    _run(xumm_ops.create_onramp_payment_payload("rBuyer", _brix_dict(), "2500000"))
    txjson = captured["payload"]["txjson"]
    assert txjson["TransactionType"] == "Payment"
    # Self-payment: the buyer buys BRIX out of the AMM into their own wallet.
    assert txjson["Account"] == "rBuyer"
    assert txjson["Destination"] == "rBuyer"
    assert txjson["Amount"] == _brix_dict()
    assert txjson["SendMax"] == "2500000"
    assert txjson["SourceTag"] == config.SOURCE_TAG


def test_onramp_payment_payload_memo_action_payment(monkeypatch):
    import json as _json

    captured = _capture(monkeypatch)
    _run(xumm_ops.create_onramp_payment_payload("rBuyer", _brix_dict(), "2500000"))
    memos_field = captured["payload"]["txjson"]["Memos"]
    decoded = {
        bytes.fromhex(m["Memo"]["MemoType"]).decode(): bytes.fromhex(
            m["Memo"]["MemoData"]
        ).decode()
        for m in memos_field
    }
    assert decoded["action"] == "payment"
    assert decoded["initiator"] == "user"


def test_onramp_payment_payload_sends_user_token(monkeypatch):
    captured = _capture(monkeypatch)
    _run(
        xumm_ops.create_onramp_payment_payload(
            "rBuyer", _brix_dict(), "2500000", user_token="tok-1"
        )
    )
    assert captured["payload"]["user_token"] == "tok-1"
