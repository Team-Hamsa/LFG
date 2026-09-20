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
TRAIT_TOKEN = {"nft_id": "0009FF", "slot": "Hat", "value": "Wizard Hat", "image_url": "/api/layer?a"}
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
