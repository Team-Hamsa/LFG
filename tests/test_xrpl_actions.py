import hashlib
import os

import pytest
from xrpl.core.addresscodec import decode_classic_address
from xrpl.models.requests import AccountObjects, Feature

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
