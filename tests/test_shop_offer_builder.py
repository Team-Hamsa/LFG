"""tests/test_shop_offer_builder.py — create_nft_offer expiration + action params.

Env-guard preamble (copy from test_market_flow.py): importing lfg_core.config
freezes its constants at import time; set the same defaults test_smoke.py uses
so collection order can't strand them.
"""

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

import asyncio

from lfg_core import memos, xrpl_ops


def _run(coro):
    # new_event_loop (not asyncio.run) so the policy's current loop is not
    # poisoned for later tests that rely on asyncio.get_event_loop().
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _Resp:
    def __init__(self, result: dict) -> None:
        self.result = result


def _patch_client(monkeypatch, captured):
    """Capture the tx model passed to submit_and_wait; stub Tx status checks."""

    def fake_submit(tx, client, wallet, **kwargs):
        captured["tx"] = tx
        return _Resp(
            {
                "hash": "HASH",
                "meta": {
                    "TransactionResult": "tesSUCCESS",
                    "offer_id": "OFFERID",
                },
            }
        )

    def fake_request(self, req):
        return _Resp(
            {
                "meta": {
                    "TransactionResult": "tesSUCCESS",
                    "offer_id": "OFFERID",
                }
            }
        )

    monkeypatch.setattr(xrpl_ops, "submit_and_wait", fake_submit)
    monkeypatch.setattr(xrpl_ops.JsonRpcClient, "request", fake_request)


def test_create_nft_offer_carries_expiration_and_issued_amount(monkeypatch):
    captured: dict = {}
    _patch_client(monkeypatch, captured)
    amount = {"currency": "BRX", "issuer": "rIssuer", "value": "42"}

    offer_id = _run(
        xrpl_ops.create_nft_offer(
            "F" * 64,
            "rBuyer",
            amount=amount,
            expiration=772000000,
            action=memos.ACTION_SHOP_BUY,
        )
    )

    tx = captured["tx"]
    assert tx.expiration == 772000000
    assert tx.amount == amount
    assert offer_id == "OFFERID"

    action_memo = tx.memos[2]
    assert bytes.fromhex(action_memo.memo_type).decode() == "action"
    assert bytes.fromhex(action_memo.memo_data).decode() == memos.ACTION_SHOP_BUY


def test_create_nft_offer_default_expiration_omitted(monkeypatch):
    # Existing callers (no expiration/action passed) must be unaffected.
    captured: dict = {}
    _patch_client(monkeypatch, captured)
    _run(xrpl_ops.create_nft_offer("NFTID", "rDest", amount="0"))
    assert captured["tx"].expiration is None
