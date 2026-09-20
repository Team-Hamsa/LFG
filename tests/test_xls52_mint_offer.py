# tests/test_xls52_mint_offer.py
# XLS-52 (NFTokenMintOffer, enabled on mainnet + testnet): a paid mint folds
# its destination-locked delivery offer into the NFTokenMint itself, so the
# minted-but-never-offered state cannot arise. These pin the three things that
# make that safe: the tx carries the offer fields, the offer id is read back
# out of the validated meta, and a folded mint whose offer is unreadable still
# falls back to a separate NFTokenCreateOffer instead of stranding the token.
import asyncio
from typing import Any

import pytest

from lfg_core import config, mint_flow, offer_delivery, xrpl_ops


def _run(coro):
    # get_event_loop (not asyncio.run) on purpose: asyncio.run closes + unsets
    # the loop, breaking the suite-order tests that reuse it (the same reason
    # tests/test_local_roster.py:78 does this).
    return asyncio.get_event_loop().run_until_complete(coro)


WALLET = "rBuyerWalletxxxxxxxxxxxxxxxxxxxxxx"
NFT_ID = "000800001234567890ABCDEF000000000000000000000000000000000000000A"
OFFER_ID = "AAAA1111BBBB2222CCCC3333DDDD4444EEEE5555FFFF6666AAAA7777BBBB8888"


def _meta_with_offer(nft_id: str = NFT_ID, offer_index: str = OFFER_ID) -> dict[str, Any]:
    """A validated folded-mint meta WITHOUT the convenience offer_id field,
    so the CreatedNode fallback is what's under test."""
    return {
        "nftoken_id": nft_id,
        "AffectedNodes": [
            {"ModifiedNode": {"LedgerEntryType": "AccountRoot"}},
            {
                "CreatedNode": {
                    "LedgerEntryType": "NFTokenOffer",
                    "LedgerIndex": offer_index,
                    "NewFields": {"NFTokenID": nft_id, "Amount": "0", "Flags": 1},
                }
            },
        ],
    }


def test_created_sell_offer_id_prefers_meta_offer_id():
    meta = _meta_with_offer()
    meta["offer_id"] = "F" * 64
    assert xrpl_ops.created_sell_offer_id(meta, NFT_ID) == "F" * 64


def test_created_sell_offer_id_falls_back_to_created_node():
    assert xrpl_ops.created_sell_offer_id(_meta_with_offer(), NFT_ID) == OFFER_ID


def test_created_sell_offer_id_ignores_another_tokens_offer():
    assert xrpl_ops.created_sell_offer_id(_meta_with_offer(), "0" * 64) is None


@pytest.mark.parametrize("meta", [None, {}, {"AffectedNodes": "nope"}])
def test_created_sell_offer_id_handles_malformed_meta(meta):
    assert xrpl_ops.created_sell_offer_id(meta, NFT_ID) is None


def test_mint_nft_folds_destination_amount_into_the_mint(monkeypatch):
    """A destination= mint must put Destination + Amount on the NFTokenMint
    itself -- Destination without Amount is temMALFORMED -- and hand back the
    offer the same transaction created."""

    async def _go():
        captured: dict[str, Any] = {}

        async def fake_submit(tx, wallet, client, label):
            captured["tx"] = tx
            captured["label"] = label
            return {"meta": _meta_with_offer(), "hash": "D" * 64}

        monkeypatch.setattr(xrpl_ops, "_submit_and_confirm", fake_submit)
        monkeypatch.setattr(xrpl_ops, "rpc_client", lambda *a, **k: object())

        result = await xrpl_ops.mint_nft(
            metadata_cdn_url="https://cdn.example/m.json",
            taxon=config.NFT_TAXON,
            issuer=config.SWAP_ISSUER_ADDRESS,
            destination=WALLET,
            return_details=True,
        )

        assert captured["label"] == "NFTokenMint"
        tx = captured["tx"]
        assert tx.destination == WALLET
        assert tx.amount == "0"
        assert tx.source_tag == config.SOURCE_TAG  # hackathon invariant survives
        assert isinstance(result, xrpl_ops.MintNFTResult)
        assert result.nft_id == NFT_ID
        assert result.offer_id == OFFER_ID

    _run(_go())


def test_plain_mint_sends_no_offer_fields(monkeypatch):
    """Every non-folded caller (sponsored blobs, swap remint, economy) must be
    byte-for-byte unaffected: no Destination/Amount/Expiration, no offer id."""

    async def _go():
        captured: dict[str, Any] = {}

        async def fake_submit(tx, wallet, client, label):
            captured["tx"] = tx
            return {"meta": {"nftoken_id": NFT_ID}, "hash": "D" * 64}

        monkeypatch.setattr(xrpl_ops, "_submit_and_confirm", fake_submit)
        monkeypatch.setattr(xrpl_ops, "rpc_client", lambda *a, **k: object())

        result = await xrpl_ops.mint_nft(
            metadata_cdn_url="https://cdn.example/m.json",
            taxon=config.NFT_TAXON,
            issuer=config.SWAP_ISSUER_ADDRESS,
            return_details=True,
        )

        tx = captured["tx"]
        assert tx.destination is None
        assert tx.amount is None
        assert tx.expiration is None
        assert isinstance(result, xrpl_ops.MintNFTResult)
        assert result.offer_id is None

    _run(_go())


def test_folded_mint_without_readable_offer_is_not_a_failed_mint(monkeypatch):
    """The token is on-ledger; an unreadable folded offer must degrade to
    offer_id=None (caller re-offers separately), never to a None mint."""

    async def _go():

        async def fake_submit(tx, wallet, client, label):
            return {"meta": {"nftoken_id": NFT_ID, "AffectedNodes": []}, "hash": "D" * 64}

        monkeypatch.setattr(xrpl_ops, "_submit_and_confirm", fake_submit)
        monkeypatch.setattr(xrpl_ops, "rpc_client", lambda *a, **k: object())

        result = await xrpl_ops.mint_nft(
            metadata_cdn_url="https://cdn.example/m.json",
            taxon=config.NFT_TAXON,
            issuer=config.SWAP_ISSUER_ADDRESS,
            destination=WALLET,
            return_details=True,
        )
        assert isinstance(result, xrpl_ops.MintNFTResult)
        assert result.nft_id == NFT_ID
        assert result.offer_id is None

    _run(_go())


def test_finalize_skips_ensure_offer_when_the_mint_already_offered(monkeypatch):
    """The point of the whole change: with a folded offer there is no second
    transaction, so ensure_offer -- and its minted_no_offer failure mode --
    is never reached."""

    async def _go():
        called = False

        async def fake_ensure(*a, **k):
            nonlocal called
            called = True
            raise AssertionError("ensure_offer must not run for a folded mint")

        monkeypatch.setattr(offer_delivery, "ensure_offer", fake_ensure)
        monkeypatch.setattr(mint_flow, "record_nft_mint", lambda **kw: True)

        async def fake_accept(*a, **k):
            return {"uuid": "x", "next": {"always": "https://xumm.example/x"}}

        monkeypatch.setattr(mint_flow.xumm_ops, "create_accept_offer_payload", fake_accept)

        result = await mint_flow._finalize_minted_unit(
            nft_number=4242,
            nft_id=NFT_ID,
            discord_id="42",
            wallet_address=WALLET,
            metadata_url="https://cdn.example/m.json",
            image_url="https://cdn.example/i.png",
            video_url=None,
            traits_dict={"Body": "Straight"},
            body="male",
            attributes=[{"trait_type": "Body", "value": "Straight"}],
            mint_tx_hash="D" * 64,
            platform="discord-activity",
            push_user_token=None,
            return_url=None,
            on_state=None,
            on_offer_created=None,
            offer_id=OFFER_ID,
        )

        assert called is False
        assert result.offer_id == OFFER_ID
        assert result.error is None

    _run(_go())


def test_finalize_still_creates_an_offer_when_none_was_folded(monkeypatch):
    """Non-folded mints (sponsored) keep the existing #466 ensure_offer path."""

    async def _go():
        seen: dict[str, Any] = {}

        async def fake_ensure(nft_id, wallet, **kw):
            seen["nft_id"] = nft_id
            return offer_delivery.OfferResult(status="offered", offer_index=OFFER_ID, reason=None)

        monkeypatch.setattr(offer_delivery, "ensure_offer", fake_ensure)
        monkeypatch.setattr(mint_flow, "record_nft_mint", lambda **kw: True)

        async def fake_accept(*a, **k):
            return {"uuid": "x", "next": {"always": "https://xumm.example/x"}}

        monkeypatch.setattr(mint_flow.xumm_ops, "create_accept_offer_payload", fake_accept)

        result = await mint_flow._finalize_minted_unit(
            nft_number=4243,
            nft_id=NFT_ID,
            discord_id="42",
            wallet_address=WALLET,
            metadata_url="https://cdn.example/m.json",
            image_url="https://cdn.example/i.png",
            video_url=None,
            traits_dict={"Body": "Straight"},
            body="male",
            attributes=[{"trait_type": "Body", "value": "Straight"}],
            mint_tx_hash="D" * 64,
            platform="discord-activity",
            push_user_token=None,
            return_url=None,
            on_state=None,
            on_offer_created=None,
        )

        assert seen["nft_id"] == NFT_ID
        assert result.offer_id == OFFER_ID

    _run(_go())
