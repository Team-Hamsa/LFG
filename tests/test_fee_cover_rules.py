"""Pure fee-cover rules (spec §Promise eligibility, §Refund computation)."""

from __future__ import annotations

import copy
import json
import sqlite3
from pathlib import Path

import pytest

from lfg_core import fee_cover, market_store
from lfg_core.fee_cover_store import Campaign

FIXTURE = Path(__file__).parent / "fixtures" / "fee_cover" / "cafe_brokered_accept.json"
CAFE = "rpx9JThQ2y37FaGeeJP7PXDUVEXY3PHZSC"
BIDDS = "rpZqTPC8GvrSvEfFsUuHkmPCg29GdQuXhC"
ISSUER = "rLfgoMintj3KBcs4s2XKtquvDwEte2kYfJ"
SELLER = "rLfbT5Vkbigi4gFUi1QUGu5udijV79i3tF"
BUYER = "rHaMsAjoAN21s1XG5TCAM6ErAefzrggsHf"
NFT_ID = "00191B58D1AE1BC312BEF9C68233FB0C8CF6A338F7C227BECADA906904943EEE"
BUY_OFFER = "37B0856D51DF75221A9140AF8FCD89E1AC3CAC703D36070BA7E2FE85567C4B7D"


@pytest.fixture(autouse=True)
def _no_clearing_buffer(monkeypatch):
    monkeypatch.delenv("BROKER_CLEARING_BUFFER_DROPS", raising=False)
    monkeypatch.delenv("BROKER_ALLOWLIST_PATH", raising=False)


def _tx():
    return json.loads(FIXTURE.read_text())


def _promise(**over):
    base = {
        "offer_index": BUY_OFFER,
        "bidder": BUYER,
        "nft_id": NFT_ID,
        "promised_drops": 80_642,
        "coverage_bps": 10_000,
    }
    base.update(over)
    return base


def _rate(account):
    return {CAFE: 0.01589}.get(account)


def _refund(
    tx=None,
    *,
    promise=None,
    payer=ISSUER,
    rate=_rate,
    linked=lambda a, b: False,
):
    return fee_cover.compute_refund(
        tx if tx is not None else _tx(),
        promise=promise or _promise(),
        payer=payer,
        broker_rate_for=rate,
        linked=linked,
    )


def _campaign(**over):
    base = {
        "id": 1,
        "network": "mainnet",
        "status": "active",
        "coverage_bps": 10_000,
        "budget_drops": 50_000_000,
        "wallet_cap_drops": 5_000_000,
        "wallet_window_seconds": 2_592_000,
        "min_bid_drops": 1_000_000,
        "started_at": 1,
        "started_by": "x",
        "ends_at": None,
        "stopped_at": None,
        "stopped_by": None,
    }
    base.update(over)
    return Campaign(**base)


def _row(offer_index, *, seller=SELLER, drops=4_990_000, destination=CAFE):
    return {
        "offer_index": offer_index,
        "seller": seller,
        "amount_drops": drops,
        "destination": destination,
    }


# --- listing pick / promise ------------------------------------------------


def test_external_listing_for_picks_the_cheapest_measured_broker_row_by_the_owner():
    rows = [
        _row("PLAIN", destination=None),
        _row("OTHER_SELLER", seller="rSomeoneElse"),
        _row("BIDDS_UNMEASURED", drops=1_000_000, destination=BIDDS),
        _row("CAFE_DEAR", drops=6_000_000),
        _row("CAFE_CHEAP", drops=4_990_000),
        _row("UNKNOWN_DEST", drops=1, destination="rPrivatePeer"),
    ]
    listing = fee_cover.external_listing_for(rows, SELLER, NFT_ID)
    assert listing == fee_cover.ExternalListing("CAFE_CHEAP", SELLER, 4_990_000, CAFE, 0.01589)
    assert listing.clearing_drops == 5_070_572
    assert fee_cover.external_listing_for([_row("PLAIN", destination=None)], SELLER, NFT_ID) is None


def test_promise_drops_matches_the_verified_cafe_fee():
    assert fee_cover.promise_drops(5_075_000, 0.01589, 10_000) == 80_642
    assert fee_cover.promise_drops(5_075_000, 0.01589, 5_000) == 40_321
    assert fee_cover.promise_drops(5_075_000, 0.01589, 0) == 0


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"campaign": None}, "campaign_inactive"),
        ({"listing": None}, "not_external_listing"),
        ({"bidder": "rSystem"}, "system_wallet"),
        ({"bid_drops": 5_070_571}, "below_clearing"),
        ({"campaign": _campaign(min_bid_drops=6_000_000)}, "below_min_bid"),
        ({}, None),
    ],
)
def test_promise_decline_reason(kwargs, expected):
    args = {
        "campaign": _campaign(),
        "listing": fee_cover.ExternalListing("L", SELLER, 4_990_000, CAFE, 0.01589),
        "bid_drops": 5_070_572,
        "bidder": BUYER,
        "system_wallets": frozenset({"rSystem"}),
    }
    args.update(kwargs)
    assert fee_cover.promise_decline_reason(**args) == expected


def test_nft_issuer_decodes_the_token_id():
    assert fee_cover.nft_issuer(NFT_ID) == ISSUER


# --- refund computation ------------------------------------------------------


def test_real_cafe_fill_refunds_the_observed_fee():
    decision = _refund()
    assert decision.reason is None
    assert decision.refund_drops == 80_642
    assert decision.observed_fee_drops == 80_642
    assert decision.observed_royalty_drops == 349_605
    assert decision.seller == SELLER and decision.broker == CAFE
    assert decision.accept_tx_hash.startswith("FED6256D")


def test_coverage_is_taken_from_the_promise_snapshot():
    assert _refund(promise=_promise(coverage_bps=5_000)).refund_drops == 40_321


def test_refund_never_exceeds_the_promise():
    assert _refund(promise=_promise(promised_drops=10_000)).refund_drops == 10_000


def test_fake_broker_inflated_fee_is_capped_at_half_the_royalty():
    tx = _tx()
    tx["NFTokenBrokerFee"] = "1000000"
    decision = _refund(tx, promise=_promise(promised_drops=10_000_000))
    assert decision.refund_drops == 174_802  # 349_605 * 5000 // 10000


def test_unallowlisted_broker_is_not_broker_settled():
    assert _refund(rate=lambda account: None).reason == "not_broker_settled"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda tx: tx.pop("NFTokenBuyOffer"),
        lambda tx: tx.__setitem__("validated", False),
        lambda tx: tx["meta"].__setitem__("TransactionResult", "tecNO_PERMISSION"),
        lambda tx: tx.__setitem__("TransactionType", "Payment"),
        lambda tx: tx.__setitem__("NFTokenBuyOffer", "F" * 64),
    ],
)
def test_non_brokered_or_mismatched_accepts_are_declined(mutate):
    tx = _tx()
    mutate(tx)
    assert _refund(tx).reason == "not_broker_settled"


def test_wrong_bidder_is_not_broker_settled():
    assert _refund(promise=_promise(bidder="rNotTheBidder")).reason == "not_broker_settled"


def test_iou_broker_fee_is_unobserved():
    tx = _tx()
    tx["NFTokenBrokerFee"] = {"currency": "BRIX", "issuer": ISSUER, "value": "1"}
    assert _refund(tx).reason == "fee_unobserved"


def test_issuer_as_seller_is_declined():
    tx = _tx()
    for node in tx["meta"]["AffectedNodes"]:
        deleted = node.get("DeletedNode")
        if (
            deleted
            and deleted.get("LedgerEntryType") == "NFTokenOffer"
            and int(deleted["FinalFields"]["Flags"]) & 1
        ):
            deleted["FinalFields"]["Owner"] = ISSUER
    assert _refund(tx).reason == "issuer_party"


def test_missing_royalty_node_is_unobserved():
    tx = _tx()
    tx["meta"]["AffectedNodes"] = [
        n
        for n in tx["meta"]["AffectedNodes"]
        if (n.get("ModifiedNode") or {}).get("FinalFields", {}).get("Account") != ISSUER
    ]
    assert _refund(tx).reason == "royalty_unobserved"


def test_payer_that_is_not_the_issuer_is_unobserved():
    assert _refund(payer="rSomeOtherSigner").reason == "royalty_unobserved"


def test_linked_counterparties_are_declined():
    seen = []

    def linked(a, b):
        seen.append((a, b))
        return True

    assert _refund(linked=linked).reason == "linked_counterparty"
    assert seen == [(BUYER, SELLER)]


def test_zero_coverage_is_a_zero_refund():
    assert _refund(promise=_promise(coverage_bps=0)).reason == "zero_refund"


def test_declines_carry_zero_drops_and_do_not_mutate_input():
    tx = _tx()
    before = copy.deepcopy(tx)
    decision = _refund(tx, rate=lambda account: None)
    assert decision.refund_drops == 0
    assert tx == before


# --- market_store.live_listings_for_nft ------------------------------------


def test_live_listings_for_nft_returns_plain_and_destination_rows():
    conn = sqlite3.connect(":memory:")
    market_store.init_db(conn)
    for offer_index, destination, live in (("A", None, 1), ("B", CAFE, 1), ("C", CAFE, 0)):
        conn.execute(
            "INSERT INTO market_listings (offer_index, nft_id, kind, seller, amount_drops, destination, is_live)"
            " VALUES (?, ?, 'character', ?, 1000000, ?, ?)",
            (offer_index, NFT_ID, SELLER, destination, live),
        )
    rows = market_store.live_listings_for_nft(conn, NFT_ID)
    assert [r["offer_index"] for r in rows] == ["A", "B"]
