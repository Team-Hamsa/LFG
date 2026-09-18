import json
import os
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULE_REL = "./webapp/client/closet_market_pure.js"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed on this host")


def run_js(expr: str):
    script = (
        f"import * as M from {json.dumps(MODULE_REL)};\n"
        f"const result = ({expr});\n"
        "console.log(JSON.stringify(result === undefined ? null : result, "
        "(k, v) => typeof v === 'bigint' ? v.toString() : v));\n"
    )
    proc = subprocess.run(
        [NODE, "--input-type=module"], input=script, capture_output=True, text=True, cwd=ROOT
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.mark.parametrize("raw,micro", [("1", "1000000"), ("12.5", "12500000"), ("0.000001", "1")])
def test_to_micro(raw, micro):
    assert run_js(f"M.toMicro({json.dumps(raw)})") == micro


@pytest.mark.parametrize(
    "price,bps,net",
    [("10", 700, "9.3"), ("0.000001", 700, "0.000001"), ("12.345678", 250, "12.037037")],
)
def test_seller_net_matches_server_round_down(price, bps, net):
    assert run_js(f"M.sellerNet({json.dumps(price)}, {bps})") == net


def test_disclosures_mention_money_and_terms():
    assert "9.3 BRIX" in run_js('M.askDisclosure("10", 700)') and "7%" in run_js(
        'M.askDisclosure("10", 700)'
    )
    assert "fee" not in run_js('M.askDisclosure("10", 0)')
    bid = run_js('M.bidDisclosure("10", 7)')
    assert "10 BRIX" in bid and "7 days" in bid and "escrow" in bid
    assert "BRIX" in run_js("M.bidDisclosure(null, 7)")
    assert "deposit" in run_js('M.fillDisclosure("10", 0, true)').lower()
    assert "deposit" not in run_js('M.fillDisclosure("10", 0, false)').lower()


def test_book_row_and_badges():
    row = {
        "slot": "Head",
        "value": "Crown",
        "best_ask_brix": "12",
        "ask_count": 2,
        "best_bid_brix": None,
        "bid_count": 0,
    }
    vm = run_js(f"M.mapBookRow({json.dumps(row)})")
    assert (
        vm["title"] == "Head: Crown"
        and vm["askLabel"] == "Ask 12 BRIX (2)"
        and vm["bidLabel"] == "No bids"
    )
    assert run_js("M.bookBadge(null)") is None
    assert run_js(f"M.bookBadge({json.dumps({**row, 'best_bid_brix': '9'})})") == "Bid 9"
    assert (
        run_js(f"M.bookLine({json.dumps({**row, 'best_bid_brix': '9', 'bid_count': 1})})")
        == "Closet market: best bid 9 BRIX · best ask 12 BRIX"
    )


def test_keys_group_by_slot():
    keys = [
        {"slot": "Head", "value": "Crown"},
        {"slot": "Eyes", "value": "Laser"},
        {"slot": "Head", "value": "Tiara"},
    ]
    assert run_js(f"M.keySlots({json.dumps(keys)})") == ["Eyes", "Head"]
    assert run_js(f"M.keyValues({json.dumps(keys)}, 'Head')") == ["Crown", "Tiara"]


def test_chip_labels():
    order = {
        "side": "bid",
        "slot": "Head",
        "value": "Crown",
        "price_brix": "10",
        "state": "pending_escrow",
    }
    assert (
        run_js(f"M.orderChipLabel({json.dumps(order)})")
        == "Bid · Head: Crown · 10 BRIX (awaiting signature)"
    )
    fill = {
        "buyer": "rMe",
        "seller": "rYou",
        "slot": "Head",
        "value": "Crown",
        "price_brix": "10",
        "state": "refunded",
    }
    assert (
        run_js(f"M.fillChipLabel({json.dumps(fill)}, 'rMe')")
        == "Bought · Head: Crown · 10 BRIX — Refunded"
    )
    token = {
        "source": "token",
        "nft_id": "N1",
        "slot": "Head",
        "value": "Crown",
        "price_brix": "10",
    }
    assert run_js(f"M.holdingFillAction({json.dumps(token)})") == {
        "label": "Deposit & fill",
        "needsDeposit": True,
        "nftId": "N1",
    }
    assert (
        run_js(f"M.holdingLabel({json.dumps(token)})") == "Head: Crown — 10 BRIX (in your wallet)"
    )
    assert (
        run_js("M.closetAssetLabel({slot: 'Head', value: 'Crown', count: 3, listed: 1})")
        == "Head: Crown ×3 (1 listed)"
    )
    assert (
        run_js("M.closetAssetLabel({slot: 'Head', value: 'Crown', count: 3})") == "Head: Crown ×3"
    )


def test_buy_disclosure_names_the_price_and_the_xrp_fallback():
    assert run_js("M.buyDisclosure('4')") == (
        "4 BRIX — it goes straight into your Closet. Not enough BRIX? You'll pay in XRP instead."
    )
