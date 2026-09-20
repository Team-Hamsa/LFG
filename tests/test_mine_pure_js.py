import json
import os
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULE_REL = "./webapp/client/mine_pure.js"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed on this host")


def run_js(expr: str):
    script = (
        f"import * as M from {json.dumps(MODULE_REL)};\n"
        f"const result = ({expr});\n"
        "console.log(JSON.stringify(result === undefined ? null : result));\n"
    )
    proc = subprocess.run(
        [NODE, "--input-type=module"], input=script, capture_output=True, text=True, cwd=ROOT
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def build(mine=None, bids=None, closet=None, wallet="rMe", enabled=True):
    args = json.dumps(
        {
            "mine": mine or {},
            "bids": bids or {},
            "closet": closet or {},
            "wallet": wallet,
            "closetMarketEnabled": enabled,
        }
    )
    return run_js(f"M.buildMine({args})")


CHAR = {"nft_id": "0008AB", "nft_number": 1035, "image": "https://cdn/1035.png"}
TRAIT_TOKEN = {
    "nft_id": "0009FF",
    "slot": "Hat",
    "value": "Wizard Hat",
    "image_url": "/api/layer?a",
}
CLOSET_ASSET = {
    "slot": "Hat",
    "value": "Wizard Hat",
    "count": 3,
    "listed": 2,
    "image_url": "/api/layer?a",
}


def test_empty_payload_owns_nothing():
    out = build()
    assert out["ownsNothing"] is True
    assert out["nothingActive"] is True
    assert out["stuff"]["characters"] == []
    assert out["stuff"]["traits"] == []


def test_unlisted_character_becomes_a_card_with_a_list_action():
    out = build(mine={"unlisted_characters": [CHAR]})
    (item,) = out["stuff"]["characters"]
    assert item["unit"] == "card"
    assert item["title"] == "#1035"
    assert item["image"] == "https://cdn/1035.png"
    assert item["imageUrl"] is None
    assert item["price"] is None
    assert item["action"] == {
        "label": "List",
        "kind": "list",
        "payload": {"nftId": "0008AB", "label": "#1035", "wizard": False},
    }
    assert out["ownsNothing"] is False


def test_character_without_a_number_falls_back_to_its_nft_id():
    out = build(
        mine={"unlisted_characters": [{"nft_id": "0008CD", "nft_number": None, "image": None}]}
    )
    (item,) = out["stuff"]["characters"]
    assert item["title"] == "0008CD"
    assert item["image"] is None


def test_closet_and_wallet_copies_of_one_trait_are_separate_tiles_by_custody():
    out = build(mine={"closet_assets": [CLOSET_ASSET], "unlisted_trait_tokens": [TRAIT_TOKEN]})
    traits = out["stuff"]["traits"]
    assert len(traits) == 2
    closet = next(t for t in traits if "in your wallet" not in t["badges"])
    wallet = next(t for t in traits if "in your wallet" in t["badges"])
    assert closet["title"] == "Hat: Wizard Hat"
    assert closet["badges"] == ["×3 · 2 listed"]
    assert closet["action"]["kind"] == "sell"
    assert wallet["badges"] == ["in your wallet", "×1"]
    assert wallet["action"]["kind"] == "sell"
    assert closet["key"] != wallet["key"]


def test_several_wallet_tokens_of_one_key_roll_up_into_one_tile():
    tokens = [
        {"nft_id": "a", "slot": "Hat", "value": "Wizard Hat", "image_url": "/api/layer?a"},
        {"nft_id": "b", "slot": "Hat", "value": "Wizard Hat", "image_url": "/api/layer?a"},
    ]
    out = build(mine={"unlisted_trait_tokens": tokens})
    (item,) = out["stuff"]["traits"]
    assert item["badges"] == ["in your wallet", "×2"]
    assert item["action"]["payload"]["nftId"] == "a"


def test_a_fully_unencumbered_closet_tile_shows_only_its_count():
    out = build(mine={"closet_assets": [{**CLOSET_ASSET, "listed": 0}]})
    (item,) = out["stuff"]["traits"]
    assert item["badges"] == ["×3"]


def test_none_value_is_dimmed_actionless_and_last():
    assets = [
        {"slot": "Hat", "value": "None", "count": 1, "listed": 0, "image_url": None},
        CLOSET_ASSET,
    ]
    out = build(mine={"closet_assets": assets})
    titles = [t["title"] for t in out["stuff"]["traits"]]
    assert titles == ["Hat: Wizard Hat", "Hat: None"]
    none_tile = out["stuff"]["traits"][-1]
    assert none_tile["dim"] is True
    assert none_tile["action"] is None


def test_sell_payload_uses_the_closet_ask_path_only_when_the_market_is_on():
    on = build(mine={"closet_assets": [CLOSET_ASSET]}, enabled=True)
    off = build(mine={"closet_assets": [CLOSET_ASSET]}, enabled=False)
    assert on["stuff"]["traits"][0]["action"]["payload"] == {
        "slot": "Hat",
        "value": "Wizard Hat",
        "label": "Hat: Wizard Hat",
        "wizard": False,
        "closetAsk": True,
    }
    assert off["stuff"]["traits"][0]["action"]["payload"]["wizard"] is True
    assert off["stuff"]["traits"][0]["action"]["payload"]["closetAsk"] is False


def test_cap_group_returns_the_head_and_the_remainder_count():
    assert run_js("M.capGroup([1,2,3,4,5], 2)") == {"items": [1, 2], "total": 5, "hidden": 3}
    assert run_js("M.capGroup([1,2], 5)") == {"items": [1, 2], "total": 2, "hidden": 0}


CHAR_LISTING = {
    "kind": "character",
    "nft_id": "0008AB",
    "nft_number": 1035,
    "image": "https://cdn/1035.png",
    "amount_xrp": "12",
    "amount_brix": None,
    "offer_index": "E3F",
    "seller": "rMe",
}
TRAIT_LISTING = {
    "kind": "trait",
    "nft_id": "0009CD",
    "slot": "Hat",
    "value": "Wizard Hat",
    "image": "/api/layer?a",
    "amount_xrp": None,
    "amount_brix": "25",
    "offer_index": "A11",
    "seller": "rMe",
}
ASK = {
    "id": "o1",
    "side": "ask",
    "slot": "Hat",
    "value": "Wizard Hat",
    "price_brix": "25",
    "state": "open",
    "created_ts": "2026-09-19T10:00:00Z",
    "image_url": "/api/layer?a",
}
CLOSET_BID = {**ASK, "id": "o2", "side": "bid", "state": "open", "price_brix": "9"}
MY_BID = {
    "offer_index": "B1",
    "nft_id": "0008AB",
    "nft_number": 1035,
    "image": "https://cdn/1035.png",
    "bidder": "rMe",
    "amount_xrp": "9",
}


def test_selling_holds_both_currencies_as_cards_in_one_group():
    out = build(mine={"listings": [CHAR_LISTING, TRAIT_LISTING]})
    assert [i["unit"] for i in out["selling"]] == ["card", "card"]
    char, trait = out["selling"]
    assert char["title"] == "#1035"
    assert char["price"] == {"amount": "12", "currency": "XRP"}
    assert char["image"] == "https://cdn/1035.png" and char["imageUrl"] is None
    assert trait["title"] == "Hat: Wizard Hat"
    assert trait["price"] == {"amount": "25", "currency": "BRIX"}
    assert trait["imageUrl"] == "/api/layer?a" and trait["image"] is None
    assert char["action"]["kind"] == "cancelListing"
    assert char["action"]["payload"]["offer_index"] == "E3F"


def test_a_closet_ask_sells_beside_the_nft_listings():
    out = build(closet={"orders": [ASK]})
    (item,) = out["selling"]
    assert item["unit"] == "card"
    assert item["title"] == "Hat: Wizard Hat"
    assert item["price"] == {"amount": "25", "currency": "BRIX"}
    assert item["action"] == {
        "label": "Cancel",
        "kind": "cancelClosetOrder",
        "payload": {"id": "o1", "side": "ask"},
    }


def test_closet_orders_split_by_side():
    out = build(closet={"orders": [ASK, CLOSET_BID]})
    assert [i["title"] for i in out["selling"]] == ["Hat: Wizard Hat"]
    assert [i["title"] for i in out["buying"]] == ["Hat: Wizard Hat"]
    assert out["buying"][0]["unit"] == "row"
    assert out["buying"][0]["action"]["payload"] == {"id": "o2", "side": "bid"}


def test_matched_and_cancelling_orders_stay_put_with_a_state_chip_and_no_action():
    out = build(
        closet={"orders": [{**ASK, "state": "matched"}, {**CLOSET_BID, "state": "cancelling"}]}
    )
    assert out["selling"][0]["state"] == {"label": "filling"}
    assert out["selling"][0]["action"] is None
    assert out["buying"][0]["state"] == {"label": "cancelling"}
    assert out["buying"][0]["action"] is None
    assert out["needsYou"] == []


def test_my_native_bids_are_buying_rows():
    out = build(bids={"my_bids": [MY_BID]})
    (item,) = out["buying"]
    assert item["unit"] == "row"
    assert item["title"] == "#1035"
    assert item["price"] == {"amount": "9", "currency": "XRP"}
    assert item["action"]["kind"] == "cancelBid"
    assert item["action"]["payload"]["offer_index"] == "B1"
    assert out["nothingActive"] is False


INCOMING_NFT_BID = {
    "offer_index": "B2",
    "nft_id": "0008EF",
    "nft_number": 2048,
    "image": "https://cdn/2048.png",
    "bidder": "rOther",
    "amount_xrp": "14",
}
INCOMING_TRAIT_BID = {
    "id": "o9",
    "slot": "Eyes",
    "value": "Laser",
    "price_brix": "40",
    "source": "closet",
    "nft_id": None,
    "image_url": "/api/layer?e",
}
FILL_DONE = {
    "id": "f1",
    "seller": "rOther",
    "buyer": "rMe",
    "slot": "Hat",
    "value": "Wizard Hat",
    "price_brix": "25",
    "state": "mirrored",
    "created_ts": "2026-09-18T10:00:00Z",
    "image_url": "/api/layer?a",
}


def test_incoming_nft_bids_lead_needs_you_highest_first():
    high = {**INCOMING_NFT_BID, "offer_index": "B3", "amount_xrp": "30"}
    out = build(bids={"bids_on_my_nfts": [INCOMING_NFT_BID, high]})
    assert [i["price"]["amount"] for i in out["needsYou"]] == ["30", "14"]
    assert out["needsYou"][0]["unit"] == "row"
    assert out["needsYou"][0]["action"]["kind"] == "acceptBid"


def test_character_bids_group_before_trait_bids_never_sorted_across_currencies():
    out = build(
        bids={"bids_on_my_nfts": [INCOMING_NFT_BID]},
        closet={"bids_on_my_traits": [INCOMING_TRAIT_BID]},
    )
    assert [i["price"]["currency"] for i in out["needsYou"]] == ["XRP", "BRIX"]


def test_a_wallet_held_trait_bid_discloses_the_deposit_at_action_time():
    token_bid = {**INCOMING_TRAIT_BID, "source": "token", "nft_id": "0009FF"}
    out = build(closet={"bids_on_my_traits": [token_bid]})
    (item,) = out["needsYou"]
    assert item["action"]["label"] == "Deposit & fill"
    assert item["badges"] == ["in your wallet"]
    plain = build(closet={"bids_on_my_traits": [INCOMING_TRAIT_BID]})["needsYou"][0]
    assert plain["action"]["label"] == "Fill"
    assert plain["badges"] == []


def test_a_half_signed_bid_is_something_only_the_user_can_finish():
    # Only a BID is ever pending_escrow — an ask is off-ledger and opens
    # instantly, so it has no signature to finish.
    out = build(closet={"orders": [{**CLOSET_BID, "state": "pending_escrow"}]})
    (item,) = out["needsYou"]
    assert item["action"] == {
        "label": "Finish signing",
        "kind": "resumeClosetOrder",
        "payload": {"id": "o2", "side": "bid"},
    }
    assert out["buying"] == []


def test_a_fill_waiting_on_my_money_or_stuck_needs_me_everything_else_is_history():
    fills = [
        {**FILL_DONE, "id": "f2", "state": "funds_pending", "buyer": "rMe"},
        {**FILL_DONE, "id": "f3", "state": "funds_pending", "buyer": "rOther", "seller": "rMe"},
        {**FILL_DONE, "id": "f4", "state": "failed"},
        FILL_DONE,
    ]
    out = build(closet={"fills": fills})
    assert [i["key"] for i in out["needsYou"]] == ["fill:f2", "fill:f4"]
    assert [i["action"]["kind"] for i in out["needsYou"]] == ["resumeFill", "viewClosetFill"]
    assert sorted(i["key"] for i in out["history"]) == ["fill:f1", "fill:f3"]
    assert all(i["unit"] == "row" for i in out["history"])


def test_history_rows_say_which_side_i_was_on():
    out = build(closet={"fills": [FILL_DONE]}, wallet="rMe")
    (item,) = out["history"]
    assert item["subtitle"] == "Bought · Complete"
    sold = build(
        closet={"fills": [{**FILL_DONE, "buyer": "rOther", "seller": "rMe"}]}, wallet="rMe"
    )
    assert sold["history"][0]["subtitle"] == "Sold · Complete"


def test_the_partition_is_total_and_disjoint():
    out = build(
        mine={
            "listings": [CHAR_LISTING, TRAIT_LISTING],
            "unlisted_characters": [CHAR],
            "unlisted_trait_tokens": [TRAIT_TOKEN],
            "closet_assets": [CLOSET_ASSET],
        },
        bids={"my_bids": [MY_BID], "bids_on_my_nfts": [INCOMING_NFT_BID]},
        closet={
            "orders": [ASK, CLOSET_BID, {**CLOSET_BID, "id": "o3", "state": "pending_escrow"}],
            "bids_on_my_traits": [INCOMING_TRAIT_BID],
            "fills": [FILL_DONE, {**FILL_DONE, "id": "f4", "state": "failed"}],
        },
    )
    groups = (
        out["needsYou"]
        + out["selling"]
        + out["buying"]
        + out["history"]
        + out["stuff"]["characters"]
        + out["stuff"]["traits"]
    )
    keys = [i["key"] for i in groups]
    assert len(keys) == len(set(keys)), "a row landed in two groups"
    # every input row produced exactly one item: 2 listings + 1 char + 1 token
    # + 1 closet asset + 1 my_bid + 1 incoming bid + 3 orders + 1 trait bid
    # + 2 fills = 13
    assert len(keys) == 13


def test_nothing_active_is_true_while_holdings_exist():
    out = build(mine={"unlisted_characters": [CHAR]})
    assert out["ownsNothing"] is False
    assert out["nothingActive"] is True


def test_the_closet_market_being_off_simply_yields_empty_groups():
    out = build(mine={"unlisted_characters": [CHAR]}, closet={}, enabled=False)
    assert out["selling"] == [] and out["buying"] == [] and out["needsYou"] == []
    assert out["history"] == []
    assert len(out["stuff"]["characters"]) == 1


def test_stuck_actions_are_oldest_first_however_the_server_ordered_them():
    """The server returns orders and fills newest-first; the thing that has
    been stuck longest is the one most worth finishing."""
    orders = [
        {
            **CLOSET_BID,
            "id": "new",
            "state": "pending_escrow",
            "created_ts": "2026-09-19T10:00:00Z",
        },
        {
            **CLOSET_BID,
            "id": "old",
            "state": "pending_escrow",
            "created_ts": "2026-09-01T10:00:00Z",
        },
    ]
    fills = [
        {**FILL_DONE, "id": "fnew", "state": "failed", "created_ts": "2026-09-19T10:00:00Z"},
        {**FILL_DONE, "id": "fold", "state": "failed", "created_ts": "2026-09-01T10:00:00Z"},
    ]
    out = build(closet={"orders": orders, "fills": fills})
    assert [i["key"] for i in out["needsYou"]] == [
        "order:old",
        "order:new",
        "fill:fold",
        "fill:fnew",
    ]
