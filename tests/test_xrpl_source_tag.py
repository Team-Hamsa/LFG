# Every XRPL transaction the bot builds must carry the Make Waves source tag
# (config.SOURCE_TAG) or its volume does not count toward the hackathon.

import asyncio

import lfg_core.xrpl_ops as xrpl_ops
from lfg_core import config


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


def test_mint_sets_source_tag(monkeypatch):
    captured: dict = {}
    _patch_client(monkeypatch, captured)
    _run(xrpl_ops.mint_nft("https://x/m.json", taxon=1, issuer=config.SWAP_ISSUER_ADDRESS))
    assert captured["tx"].source_tag == config.SOURCE_TAG


def test_mint_flags_override(monkeypatch):
    captured: dict = {}
    _patch_client(monkeypatch, captured)
    _run(
        xrpl_ops.mint_nft("https://x/m.json", taxon=1, issuer=config.SWAP_ISSUER_ADDRESS, flags=25)
    )
    assert captured["tx"].flags == 25


def test_mint_omits_transfer_fee_when_not_transferable(monkeypatch):
    # A soulbound/non-transferable NFToken (no tfTransferable flag, e.g. the
    # Bucket flags=16) must NOT carry a TransferFee — XRPL rejects that as
    # temMALFORMED. The fee is only valid on transferable tokens.
    captured: dict = {}
    _patch_client(monkeypatch, captured)
    _run(
        xrpl_ops.mint_nft("https://x/m.json", taxon=1, issuer=config.SWAP_ISSUER_ADDRESS, flags=16)
    )
    assert captured["tx"].transfer_fee is None


def test_mint_sets_transfer_fee_when_transferable(monkeypatch):
    # Transferable economy characters (flags=25 include tfTransferable) keep the
    # configured transfer fee.
    captured: dict = {}
    _patch_client(monkeypatch, captured)
    _run(
        xrpl_ops.mint_nft("https://x/m.json", taxon=1, issuer=config.SWAP_ISSUER_ADDRESS, flags=25)
    )
    assert captured["tx"].transfer_fee == config.NFT_TRANSFER_FEE


def test_create_offer_sets_source_tag(monkeypatch):
    captured: dict = {}
    _patch_client(monkeypatch, captured)
    _run(xrpl_ops.create_nft_offer("NFTID", "rDest", amount="0"))
    assert captured["tx"].source_tag == config.SOURCE_TAG


def test_burn_sets_source_tag(monkeypatch):
    captured: dict = {}
    _patch_client(monkeypatch, captured)
    _run(xrpl_ops.burn_nft("NFTID", owner="rOwner"))
    assert captured["tx"].source_tag == config.SOURCE_TAG


def test_modify_sets_source_tag(monkeypatch):
    captured: dict = {}
    _patch_client(monkeypatch, captured)
    _run(xrpl_ops.modify_nft("NFTID", "rOwner", "https://x/new.json"))
    assert captured["tx"].source_tag == config.SOURCE_TAG


def test_buy_and_burn_sets_source_tag(monkeypatch):
    captured: dict = {}
    _patch_client(monkeypatch, captured)
    # max_xrp set so the Payment carries send_max (on testnet the SEED account
    # is itself the BRIX issuer, so a same-currency burn would be a circular
    # self-payment the model rejects — unrelated to the source tag under test).
    _run(
        xrpl_ops.buy_and_burn(
            config.SWAP_OFFER_CURRENCY_HEX, config.SWAP_OFFER_ISSUER, "10", max_xrp="10"
        )
    )
    assert captured["tx"].source_tag == config.SOURCE_TAG
