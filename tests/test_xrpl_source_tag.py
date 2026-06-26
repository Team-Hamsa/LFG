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
    # Use a DIFFERENT issuer than the bot wallet so the self-issuer no-op guard
    # (which short-circuits before building the Payment) does not fire here.
    _run(
        xrpl_ops.buy_and_burn(
            config.SWAP_OFFER_CURRENCY_HEX,
            "rDifferentIssuerXXXXXXXXXXXXXXXXXXX",
            "10",
            max_xrp="10",
        )
    )
    assert captured["tx"].source_tag == config.SOURCE_TAG


def test_buy_and_burn_self_issuer_is_noop(monkeypatch):
    # When the bot wallet IS the issuer of the IOU (the testnet case, where the
    # SEED account issues BRIX), paying the IOU to its own issuer would redeem
    # it on receipt — there is nothing to burn and you cannot send your own IOU
    # to yourself. buy_and_burn must short-circuit: return a TRUTHY sentinel
    # (so `if not await buy_and_burn(...)` callers don't log a spurious error),
    # never call submit_and_wait, and never raise.
    from xrpl.wallet import Wallet

    issuer = Wallet.from_seed(config.SEED).classic_address
    called = {"submit": False}

    def boom(*a, **k):
        called["submit"] = True
        raise AssertionError("submit_and_wait must not be called in the self-issuer no-op")

    monkeypatch.setattr(xrpl_ops, "submit_and_wait", boom)

    result = _run(xrpl_ops.buy_and_burn(config.SWAP_OFFER_CURRENCY_HEX, issuer, "10"))
    assert result  # truthy sentinel, not None
    assert called["submit"] is False


def test_buy_and_burn_different_issuer_submits(monkeypatch):
    # Regression: the real-burn path (mainnet, where the issuer is a separate
    # account) must still build and submit a Payment.
    captured: dict = {}
    _patch_client(monkeypatch, captured)
    result = _run(
        xrpl_ops.buy_and_burn(
            config.SWAP_OFFER_CURRENCY_HEX,
            "rDifferentIssuerXXXXXXXXXXXXXXXXXXX",
            "10",
            max_xrp="10",
        )
    )
    assert result == "HASH"  # the fake tesSUCCESS response's hash
    assert "tx" in captured  # submit_and_wait was called
