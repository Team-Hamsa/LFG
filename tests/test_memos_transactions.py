# Every XRPL transaction the app builds must carry the provenance Memos (#54)
# alongside the SourceTag (#61). These tests assert the Memos array is present
# and schema-conformant on each backend-signed builder (xrpl_ops) and each
# user-signed XUMM payload (xumm_ops).

import asyncio

from xrpl.utils import hex_to_str

import lfg_core.xrpl_ops as xrpl_ops
from lfg_core import config, memos, xumm_ops


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---- backend-signed builders (xrpl_ops) ------------------------------------


class _Resp:
    def __init__(self, result: dict) -> None:
        self.result = result


def _patch_client(monkeypatch, captured):
    def fake_submit(tx, client, wallet):
        captured["tx"] = tx
        return _Resp(
            {
                "hash": "HASH",
                "meta": {
                    "TransactionResult": "tesSUCCESS",
                    "nftoken_id": "NFTID",
                    "offer_id": "OFFERID",
                },
            }
        )

    def fake_request(self, req):
        return _Resp(
            {
                "meta": {
                    "TransactionResult": "tesSUCCESS",
                    "nftoken_id": "NFTID",
                    "offer_id": "OFFERID",
                }
            }
        )

    monkeypatch.setattr(xrpl_ops, "submit_and_wait", fake_submit)
    monkeypatch.setattr(xrpl_ops.JsonRpcClient, "request", fake_request)


def _decoded_memos(tx):
    return {hex_to_str(m.memo_type): hex_to_str(m.memo_data) for m in (tx.memos or [])}


def test_mint_carries_backend_memos(monkeypatch):
    captured: dict = {}
    _patch_client(monkeypatch, captured)
    _run(xrpl_ops.mint_nft("https://x/m.json", taxon=1, issuer=config.SWAP_ISSUER_ADDRESS))
    d = _decoded_memos(captured["tx"])
    assert d["initiator"] == memos.INITIATOR_BACKEND
    assert d["action"] == memos.ACTION_MINT
    assert d["platform"] in memos._PLATFORMS


def test_mint_threads_platform(monkeypatch):
    captured: dict = {}
    _patch_client(monkeypatch, captured)
    _run(
        xrpl_ops.mint_nft(
            "https://x/m.json",
            taxon=1,
            issuer=config.SWAP_ISSUER_ADDRESS,
            platform=memos.PLATFORM_TELEGRAM,
        )
    )
    assert _decoded_memos(captured["tx"])["platform"] == memos.PLATFORM_TELEGRAM


def test_create_offer_carries_memos(monkeypatch):
    captured: dict = {}
    _patch_client(monkeypatch, captured)
    _run(xrpl_ops.create_nft_offer("NFTID", "rDest"))
    assert _decoded_memos(captured["tx"])["action"] == memos.ACTION_CREATE_OFFER


def test_burn_carries_memos(monkeypatch):
    captured: dict = {}
    _patch_client(monkeypatch, captured)
    _run(xrpl_ops.burn_nft("NFTID"))
    assert _decoded_memos(captured["tx"])["action"] == memos.ACTION_BURN


def test_modify_carries_memos(monkeypatch):
    captured: dict = {}
    _patch_client(monkeypatch, captured)
    _run(xrpl_ops.modify_nft("NFTID", config.SIGNING_ACCOUNT, "https://x/m.json"))
    assert _decoded_memos(captured["tx"])["action"] == memos.ACTION_MODIFY


def test_buy_and_burn_carries_memos(monkeypatch):
    captured: dict = {}
    _patch_client(monkeypatch, captured)
    # Use a valid non-self issuer so buy_and_burn actually builds/submits a
    # Payment (a self-issuer currency would short-circuit to the no-op path).
    _run(xrpl_ops.buy_and_burn("USD", "rPT1Sjq2YGrBMTttX4GZHjKu9dyfzbpAYe", "10"))
    d = _decoded_memos(captured["tx"])
    assert d["action"] == memos.ACTION_BUY_AND_BURN
    assert d["initiator"] == memos.INITIATOR_BACKEND


# ---- user-signed XUMM payloads (xumm_ops) ----------------------------------


class _XResp:
    @staticmethod
    def json():
        return {"refs": {"qr_png": "q"}, "next": {"always": "n"}, "uuid": "u"}


def _capture_xumm(monkeypatch):
    captured: dict = {}

    def fake_post(url, json, headers, timeout):
        captured["payload"] = json
        return _XResp()

    monkeypatch.setattr(xumm_ops.requests, "post", fake_post)
    return captured


def _decoded_json_memos(payload):
    return {
        hex_to_str(e["Memo"]["MemoType"]): hex_to_str(e["Memo"]["MemoData"])
        for e in payload["txjson"].get("Memos", [])
    }


def test_payment_payload_carries_user_memos(monkeypatch):
    captured = _capture_xumm(monkeypatch)
    _run(xumm_ops.create_payment_payload("rDest", value="1", platform=memos.PLATFORM_WEBAPP))
    d = _decoded_json_memos(captured["payload"])
    assert d["initiator"] == memos.INITIATOR_USER
    assert d["platform"] == memos.PLATFORM_WEBAPP
    assert d["action"] == memos.ACTION_PAYMENT


def test_payment_payload_action_override(monkeypatch):
    captured = _capture_xumm(monkeypatch)
    _run(xumm_ops.create_payment_payload("rDest", value="1", action=memos.ACTION_TRAIT_SWAP_FEE))
    assert _decoded_json_memos(captured["payload"])["action"] == memos.ACTION_TRAIT_SWAP_FEE


def test_accept_offer_payload_carries_memos(monkeypatch):
    captured = _capture_xumm(monkeypatch)
    _run(xumm_ops.create_accept_offer_payload("OFFER1"))
    assert _decoded_json_memos(captured["payload"])["action"] == memos.ACTION_ACCEPT_OFFER


def test_sell_offer_payload_carries_memos(monkeypatch):
    captured = _capture_xumm(monkeypatch)
    _run(xumm_ops.create_sell_offer_payload("rAcct", "NFTID", "1000000"))
    assert _decoded_json_memos(captured["payload"])["action"] == memos.ACTION_LIST


def test_cancel_offer_payload_carries_memos(monkeypatch):
    captured = _capture_xumm(monkeypatch)
    _run(xumm_ops.create_cancel_offer_payload("rAcct", "OFFERIDX"))
    assert _decoded_json_memos(captured["payload"])["action"] == memos.ACTION_CANCEL_OFFER


def test_signin_payload_has_no_memos(monkeypatch):
    captured = _capture_xumm(monkeypatch)
    _run(xumm_ops.create_signin_payload())
    assert "Memos" not in captured["payload"]["txjson"]
