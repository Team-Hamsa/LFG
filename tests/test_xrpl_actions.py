import hashlib
import os
from dataclasses import replace

import pytest
from xrpl.core.addresscodec import decode_classic_address
from xrpl.core.binarycodec import encode_for_signing_batch
from xrpl.core.keypairs import is_valid_message
from xrpl.models import IssuedCurrencyAmount
from xrpl.models.requests import AccountObjects, Feature
from xrpl.models.transactions import (
    Batch,
    BatchFlag,
    NFTokenAcceptOffer,
    NFTokenMint,
    Payment,
    TransactionFlag,
)
from xrpl.wallet import Wallet

os.environ.setdefault("XUMM_API_KEY", "test")
os.environ.setdefault("XUMM_API_SECRET", "test")
os.environ.setdefault("SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2")
os.environ.setdefault("TOKEN_ISSUER_ADDRESS", "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
os.environ.setdefault(
    "TOKEN_CURRENCY_HEX", "4C46474F00000000000000000000000000000000"
)
os.environ.setdefault("BUNNY_CDN_ACCESS_KEY", "test")
os.environ.setdefault("BUNNY_CDN_STORAGE_ZONE", "test")

from lfg_core import xrpl_actions  # noqa: E402

ACCOUNT = "rHb9CJAWyB4rj91VRWn96DkukG4bwdtyTh"
ISSUER_ACCOUNT = "rLs1MzkFWCxTbuAHgjeTZK4fcCDDnf2KRv"
NFT_ISSUER = "rLfgoMintj3KBcs4s2XKtquvDwEte2kYfJ"
METADATA_URL = "https://cdn.example/7.json"
SOURCE_TAG = 2606160021


def _reference_offer_id(account: str, sequence: int) -> str:
    payload = b"\x00q" + decode_classic_address(account) + sequence.to_bytes(4, "big")
    return hashlib.sha512(payload).digest()[:32].hex().upper()


def test_nft_offer_id_matches_protocol_keylet():
    assert xrpl_actions.nft_offer_id(ACCOUNT, 349) == _reference_offer_id(ACCOUNT, 349)


@pytest.mark.parametrize("sequence", [-1, 2**32, True, "7"])
def test_nft_offer_id_rejects_non_uint32(sequence):
    with pytest.raises(ValueError):
        xrpl_actions.nft_offer_id(ACCOUNT, sequence)


@pytest.mark.parametrize(
    ("batch", "mint_offer", "enabled", "reason"),
    [
        (True, True, True, None),
        (False, True, False, "batch_unavailable"),
        (True, False, False, "mint_offer_unavailable"),
    ],
)
def test_capability_requires_both_exact_amendments(
    batch, mint_offer, enabled, reason
):
    rows = {
        xrpl_actions.BATCH_V1_1_ID: {"supported": True, "enabled": batch},
        xrpl_actions.NFTOKEN_MINT_OFFER_ID: {
            "supported": True,
            "enabled": mint_offer,
        },
    }
    got = xrpl_actions.evaluate_capabilities(rows, configured=True)
    assert (got.enabled, got.reason) == (enabled, reason)


def test_obsolete_batch_never_substitutes_for_v1_1():
    rows = {
        xrpl_actions.OBSOLETE_BATCH_ID: {"supported": True, "enabled": True},
        xrpl_actions.NFTOKEN_MINT_OFFER_ID: {"supported": True, "enabled": True},
    }
    got = xrpl_actions.evaluate_capabilities(rows, configured=True)
    assert got.enabled is False
    assert got.reason == "obsolete_batch_enabled"


def test_obsolete_batch_enabled_hard_closes_even_if_v1_1_is_enabled():
    rows = {
        xrpl_actions.BATCH_V1_1_ID: {"supported": True, "enabled": True},
        xrpl_actions.NFTOKEN_MINT_OFFER_ID: {"supported": True, "enabled": True},
        xrpl_actions.OBSOLETE_BATCH_ID: {"supported": True, "enabled": True},
    }
    got = xrpl_actions.evaluate_capabilities(rows, configured=True)
    assert got == xrpl_actions.BatchCapability(False, "obsolete_batch_enabled")


def test_config_switch_can_only_close_gate():
    rows = {
        xrpl_actions.BATCH_V1_1_ID: {"supported": True, "enabled": True},
        xrpl_actions.NFTOKEN_MINT_OFFER_ID: {"supported": True, "enabled": True},
    }
    assert (
        xrpl_actions.evaluate_capabilities(rows, configured=False).reason
        == "action_disabled"
    )


@pytest.mark.asyncio
async def test_feature_rpc_reads_row_keyed_by_amendment_id():
    class FakeClient:
        def request(self, request):
            assert isinstance(request, Feature)
            amendment_id = request.feature
            return type(
                "Response",
                (),
                {
                    "result": {
                        amendment_id: {
                            "supported": True,
                            "enabled": amendment_id != xrpl_actions.OBSOLETE_BATCH_ID,
                        }
                    }
                },
            )()

    got = await xrpl_actions.fetch_batch_capability(FakeClient(), configured=True)
    assert got == xrpl_actions.BatchCapability(True, None)


@pytest.mark.asyncio
async def test_feature_rpc_fails_closed_on_malformed_response():
    class FakeClient:
        def request(self, request):
            return type("Response", (), {"result": {"enabled": True}})()

    got = await xrpl_actions.fetch_batch_capability(FakeClient(), configured=True)
    assert got == xrpl_actions.BatchCapability(False, "batch_unavailable")


@pytest.mark.asyncio
async def test_feature_rpc_fails_closed_on_transport_error():
    class FakeClient:
        def request(self, request):
            raise RuntimeError("offline")

    got = await xrpl_actions.fetch_batch_capability(FakeClient(), configured=True)
    assert got == xrpl_actions.BatchCapability(False, "batch_unavailable")


@pytest.mark.asyncio
async def test_list_ticket_sequences_filters_and_sorts_validated_ticket_objects():
    class FakeClient:
        def request(self, request):
            assert isinstance(request, AccountObjects)
            assert request.account == ACCOUNT
            assert request.ledger_index == "validated"
            return type(
                "Response",
                (),
                {
                    "result": {
                        "account_objects": [
                            {"LedgerEntryType": "Ticket", "TicketSequence": 9},
                            {"LedgerEntryType": "Offer", "TicketSequence": 2},
                            {"LedgerEntryType": "Ticket", "TicketSequence": 3},
                            {"LedgerEntryType": "Ticket", "TicketSequence": "4"},
                        ]
                    }
                },
            )()

    assert await xrpl_actions.list_ticket_sequences(FakeClient(), ACCOUNT) == [3, 9]


def _payment(pay_with="LFGO"):
    if pay_with == "XRP":
        return xrpl_actions.MintPayment("XRP", "10", ISSUER_ACCOUNT, "10000000")
    amount = IssuedCurrencyAmount(
        currency="4C46474F00000000000000000000000000000000",
        issuer=ISSUER_ACCOUNT,
        value="1",
    )
    return xrpl_actions.MintPayment("LFGO", "1", ISSUER_ACCOUNT, amount)


def _build_test_batch(**overrides):
    values = {
        "buyer": ACCOUNT,
        "issuer_account": ISSUER_ACCOUNT,
        "nft_issuer": NFT_ISSUER,
        "issuer_ticket": 9001,
        "metadata_url": METADATA_URL,
        "payment": _payment(),
        "platform": "webapp",
        "campaign": "x-mint-link",
        "nft_flags": 9,
        "nft_taxon": 0,
        "transfer_fee": 7000,
        "source_tag": SOURCE_TAG,
    }
    values.update(overrides)
    return xrpl_actions.build_atomic_mint_batch(**values)


def _filled_test_batch(**overrides):
    batch = _build_test_batch(**overrides)
    payment, mint, accept = batch.raw_transactions
    return replace(
        batch,
        sequence=100,
        fee="40",
        last_ledger_sequence=500,
        raw_transactions=[
            replace(payment, sequence=101, fee="0", signing_pub_key=""),
            replace(mint, sequence=0, fee="0", signing_pub_key=""),
            replace(accept, sequence=102, fee="0", signing_pub_key=""),
        ],
    )


def _validate_test_batch(batch):
    xrpl_actions.validate_atomic_mint_batch(
        batch,
        buyer=ACCOUNT,
        issuer_account=ISSUER_ACCOUNT,
        nft_issuer=NFT_ISSUER,
        issuer_ticket=9001,
        payment=_payment(),
        metadata_url=METADATA_URL,
        platform="webapp",
        campaign="x-mint-link",
        nft_flags=9,
        nft_taxon=0,
        transfer_fee=7000,
        source_tag=SOURCE_TAG,
    )


def test_builder_orders_payment_mint_accept_and_charges_once():
    batch = _build_test_batch()
    assert batch.flags == BatchFlag.TF_ALL_OR_NOTHING
    assert [tx.transaction_type.value for tx in batch.raw_transactions] == [
        "Payment",
        "NFTokenMint",
        "NFTokenAcceptOffer",
    ]
    payment, mint, accept = batch.raw_transactions
    assert payment.account == ACCOUNT
    assert payment.flags == TransactionFlag.TF_INNER_BATCH_TXN
    assert mint.sequence == 0 and mint.ticket_sequence == 9001
    assert mint.issuer == NFT_ISSUER
    assert mint.amount == "0" and mint.destination == ACCOUNT
    assert mint.transfer_fee == 7000
    assert accept.nftoken_sell_offer == xrpl_actions.nft_offer_id(
        ISSUER_ACCOUNT, 9001
    )
    assert accept.flags == TransactionFlag.TF_INNER_BATCH_TXN
    assert all(tx.fee is None or tx.fee == "0" for tx in batch.raw_transactions)
    assert mint.has_flag(TransactionFlag.TF_INNER_BATCH_TXN)


def test_xrp_builder_keeps_payment_first_and_offer_free():
    batch = _build_test_batch(payment=_payment("XRP"))
    assert batch.raw_transactions[0].amount == "10000000"
    assert batch.raw_transactions[1].amount == "0"


def test_nontransferable_builder_omits_transfer_fee():
    batch = _build_test_batch(nft_flags=1)
    assert batch.raw_transactions[1].transfer_fee is None


def test_validator_accepts_exact_autofilled_shape():
    _validate_test_batch(_filled_test_batch())


def test_validator_rejects_non_payment_first():
    batch = _filled_test_batch()
    txs = list(batch.raw_transactions)
    txs[0], txs[1] = txs[1], txs[0]
    with pytest.raises(xrpl_actions.AtomicMintInvariantError):
        _validate_test_batch(replace(batch, raw_transactions=txs))


@pytest.mark.parametrize(
    "mutation",
    [
        lambda batch: replace(batch, flags=BatchFlag.TF_ONLY_ONE),
        lambda batch: replace(
            batch,
            raw_transactions=[
                replace(batch.raw_transactions[0], amount="2"),
                *batch.raw_transactions[1:],
            ],
        ),
        lambda batch: replace(
            batch,
            raw_transactions=[
                batch.raw_transactions[0],
                replace(batch.raw_transactions[1], amount="1"),
                batch.raw_transactions[2],
            ],
        ),
        lambda batch: replace(
            batch,
            raw_transactions=[
                batch.raw_transactions[0],
                batch.raw_transactions[1],
                replace(batch.raw_transactions[2], sequence=103),
            ],
        ),
    ],
)
def test_validator_rejects_security_invariant_mutations(mutation):
    with pytest.raises(xrpl_actions.AtomicMintInvariantError):
        _validate_test_batch(mutation(_filled_test_batch()))


def test_regular_key_signature_names_authorizing_issuer_not_seed_address():
    wallet = Wallet.from_seed("sEdTM1uX8pu2do5XvTnutH6HsouMaM2")
    batch = _filled_test_batch()
    signed = xrpl_actions.sign_issuer_batch(
        batch, wallet=wallet, issuer_account=ISSUER_ACCOUNT
    )
    signer = signed.batch_signers[0]
    message = encode_for_signing_batch(
        {
            "flags": int(signed.flags),
            "transaction_ids": [tx.get_hash() for tx in signed.raw_transactions],
        }
    )
    assert wallet.address != ISSUER_ACCOUNT
    assert signer.account == ISSUER_ACCOUNT
    assert is_valid_message(
        bytes.fromhex(message),
        bytes.fromhex(signer.txn_signature),
        signer.signing_pub_key,
    )


@pytest.mark.asyncio
async def test_prepare_autofills_validates_and_adds_one_issuer_signer(monkeypatch):
    filled = _filled_test_batch()

    async def fake_autofill(batch, client, signers_count):
        assert batch.raw_transactions[0].transaction_type.value == "Payment"
        assert signers_count == 1
        return filled

    monkeypatch.setattr(xrpl_actions, "autofill", fake_autofill)
    prepared = await xrpl_actions.prepare_atomic_mint_batch(
        client=object(),
        wallet=Wallet.from_seed("sEdTM1uX8pu2do5XvTnutH6HsouMaM2"),
        buyer=ACCOUNT,
        issuer_account=ISSUER_ACCOUNT,
        nft_issuer=NFT_ISSUER,
        issuer_ticket=9001,
        metadata_url=METADATA_URL,
        payment=_payment(),
        platform="webapp",
        campaign="x-mint-link",
        nft_flags=9,
        nft_taxon=0,
        transfer_fee=7000,
        source_tag=SOURCE_TAG,
    )
    assert prepared.offer_id == xrpl_actions.nft_offer_id(ISSUER_ACCOUNT, 9001)
    assert prepared.last_ledger_sequence == 500
    assert len(prepared.inner_hashes) == 3
    assert [signer.account for signer in prepared.transaction.batch_signers] == [
        ISSUER_ACCOUNT
    ]
