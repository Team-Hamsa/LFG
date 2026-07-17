# tests/test_market_pure_js.py
# Task 10 (#44): the marketplace panel's pure-function helpers live in
# webapp/client/market_pure.js as a plain ES module, kept separate from DOM
# code specifically so they can be unit-tested. Unlike the rest of the
# webapp client (guarded only by source-assertion tests — see
# test_app_js_boot.py / test_leaderboard_selector.py, no JS execution
# harness existed in this repo before), this file actually EXECUTES the
# module under Node (present on dev/CI hosts, v20+) and asserts on real
# outputs — required to genuinely cover the money-math discipline
# (.superpowers/sdd/global-constraints.md: integer drops, floats rejected)
# rather than just grepping for a function name.
#
# No lfg_core import at module top -> no env-guard preamble needed (that
# rule only applies to test files that import lfg_core, which freezes
# config constants at import time; this file never touches lfg_core).
import json
import os
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULE_REL = "./webapp/client/market_pure.js"

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed on this host")


def run_js(expr: str):
    """Run `expr` (a JS expression referencing the imported module as `M`)
    inside a small Node ES-module script, executed with cwd=ROOT so the
    relative import resolves; returns the JSON-decoded result of
    `console.log(JSON.stringify(expr))`. Raises AssertionError with stderr
    on a non-zero exit (syntax error / thrown exception)."""
    script = (
        f"import * as M from {json.dumps(MODULE_REL)};\n"
        f"const result = ({expr});\n"
        f"console.log(JSON.stringify(result === undefined ? null : result));\n"
    )
    proc = subprocess.run(
        [NODE, "--input-type=module"],
        input=script,
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=15,
    )
    assert proc.returncode == 0, f"node script failed:\n{script}\n--- stderr ---\n{proc.stderr}"
    return json.loads(proc.stdout)


def run_js_throws(expr: str) -> str:
    """Like run_js, but expects `expr` to throw; returns the error's .name
    (e.g. 'TypeError', 'RangeError') so callers can assert on error kind."""
    script = (
        f"import * as M from {json.dumps(MODULE_REL)};\n"
        "try {\n"
        f"  ({expr});\n"
        "  console.log(JSON.stringify({threw: false}));\n"
        "} catch (e) {\n"
        "  console.log(JSON.stringify({threw: true, name: e.constructor.name, message: e.message}));\n"
        "}\n"
    )
    proc = subprocess.run(
        [NODE, "--input-type=module"],
        input=script,
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=15,
    )
    assert proc.returncode == 0, f"node script crashed:\n{script}\n--- stderr ---\n{proc.stderr}"
    out = json.loads(proc.stdout)
    assert out["threw"], f"expected {expr} to throw, but it returned normally"
    return out["name"]


# --- xrpToDropsStr / dropsToXrpStr (mirrors lfg_core/market_ops.py) ---


@pytest.mark.parametrize(
    "xrp,drops",
    [
        ("5", "5000000"),
        ("1.5", "1500000"),
        ("0.000001", "1"),
        ("100", "100000000"),
        ("0.5", "500000"),
    ],
)
def test_xrp_to_drops(xrp, drops):
    assert run_js(f"M.xrpToDropsStr({json.dumps(xrp)})") == drops


@pytest.mark.parametrize(
    "drops,xrp",
    [
        ("5000000", "5"),
        ("1500000", "1.5"),
        ("1", "0.000001"),
        ("100000000", "100"),
        ("500000", "0.5"),
    ],
)
def test_drops_to_xrp(drops, xrp):
    assert run_js(f"M.dropsToXrpStr({json.dumps(drops)})") == xrp


def test_xrp_to_drops_rejects_non_string():
    assert run_js_throws("M.xrpToDropsStr(5)") == "TypeError"


def test_xrp_to_drops_rejects_zero_and_negative():
    assert run_js_throws("M.xrpToDropsStr('0')") == "RangeError"
    assert run_js_throws("M.xrpToDropsStr('-1')") == "RangeError"


def test_xrp_to_drops_rejects_too_many_decimals():
    assert run_js_throws("M.xrpToDropsStr('1.1234567')") == "RangeError"


def test_xrp_to_drops_rejects_garbage():
    assert run_js_throws("M.xrpToDropsStr('abc')") == "RangeError"


def test_xrp_to_drops_rejects_absurd_magnitude():
    # Mirrors lfg_core/market_ops.py's magnitude bound (#130): anything past
    # XRP's 100e9 total supply (1e17 drops) can never be a real price. The
    # regex already rejects scientific notation ('1e30'); plain-digit absurd
    # values must be rejected too.
    assert run_js_throws("M.xrpToDropsStr('1e30')") == "RangeError"
    assert run_js_throws("M.xrpToDropsStr('1000000000000000000000000000000')") == "RangeError"
    assert run_js_throws("M.xrpToDropsStr('100000000000.000001')") == "RangeError"


def test_xrp_to_drops_max_supply_boundary_accepted():
    assert run_js("M.xrpToDropsStr('100000000000')") == "100000000000000000"


def test_drops_to_xrp_rejects_non_digit_string():
    assert run_js_throws("M.dropsToXrpStr('1.5')") == "TypeError"
    assert run_js_throws("M.dropsToXrpStr(5000000)") == "TypeError"


def test_validate_price_ok_and_error_shapes():
    ok = run_js("M.validatePrice('5')")
    assert ok == {"ok": True, "drops": "5000000"}
    bad = run_js("M.validatePrice('abc')")
    assert bad["ok"] is False and "error" in bad


# --- computeRoyalty / royaltyDisclosure — the 93%/7% money math ---


@pytest.mark.parametrize(
    "price,total,fee,receive",
    [
        ("100", "100000000", "7000000", "93000000"),
        ("10", "10000000", "700000", "9300000"),
        ("1", "1000000", "70000", "930000"),
    ],
)
def test_compute_royalty_integer_math(price, total, fee, receive):
    r = run_js(f"M.computeRoyalty({json.dumps(price)})")
    assert r["totalDrops"] == total
    assert r["feeDrops"] == fee
    assert r["receiveDrops"] == receive
    # fee + receive must reconstruct the total exactly (no rounding drift).
    assert int(r["feeDrops"]) + int(r["receiveDrops"]) == int(r["totalDrops"])


def test_compute_royalty_fields_are_all_strings_never_floats():
    r = run_js("M.computeRoyalty('12.5')")
    assert all(isinstance(v, str) for v in r.values())


def test_royalty_disclosure_says_93_and_7_never_70_or_30():
    text = run_js("M.royaltyDisclosure('100')")
    assert text == "You receive 93 XRP (93% — 7% collection royalty)"
    assert "93%" in text and "7%" in text
    assert "70%" not in text and "30%" not in text


# --- row mapping / badges ---


def test_map_listing_row_character():
    row = {
        "nft_id": "N1",
        "kind": "character",
        "nft_number": 3537,
        "image": "https://cdn/x.png",
        "amount_xrp": "10",
        "amount_drops": 10000000,
        "seller": "rSeller",
        "offer_index": "OFF1",
    }
    vm = run_js(f"M.mapListingRow({json.dumps(row)})")
    assert vm["title"] == "#3537"
    assert vm["badge"] == "Character"
    assert vm["nftId"] == "N1"


def test_map_listing_row_trait():
    row = {
        "nft_id": "T1",
        "kind": "trait",
        "slot": "Head",
        "value": "Halo",
        "image": "/api/layer?body=male&trait=Head&value=Halo",
        "amount_xrp": "5",
        "amount_drops": 5000000,
        "seller": "rSeller",
        "offer_index": "OFF2",
    }
    vm = run_js(f"M.mapListingRow({json.dumps(row)})")
    assert vm["title"] == "Head: Halo"
    assert vm["badge"] == "Trait"


def test_badge_label():
    assert run_js('M.badgeLabel({kind: "trait"})') == "Trait"
    assert run_js('M.badgeLabel({kind: "character"})') == "Character"


# --- sortRows ---


def test_sort_rows_price_asc_and_desc():
    rows = [
        {"nft_id": "a", "amount_drops": 30},
        {"nft_id": "b", "amount_drops": 10},
        {"nft_id": "c", "amount_drops": 20},
    ]
    asc = run_js(f"M.sortRows({json.dumps(rows)}, 'price_asc')")
    assert [r["nft_id"] for r in asc] == ["b", "c", "a"]
    desc = run_js(f"M.sortRows({json.dumps(rows)}, 'price_desc')")
    assert [r["nft_id"] for r in desc] == ["a", "c", "b"]


def test_sort_rows_unknown_sort_is_noop():
    rows = [{"nft_id": "a", "amount_drops": 1}, {"nft_id": "b", "amount_drops": 2}]
    out = run_js(f"M.sortRows({json.dumps(rows)}, 'newest')")
    assert [r["nft_id"] for r in out] == ["a", "b"]


# --- query building ---


def test_build_listings_params_full():
    state = {
        "kind": "trait",
        "traits": ["Head:Halo", "Eyes:Shades"],
        "minXrp": "1",
        "maxXrp": "50",
        "sort": "price_desc",
        "limit": 12,
        "offset": 0,
    }
    pairs = run_js(f"M.buildListingsParams({json.dumps(state)})")
    assert pairs == [
        ["kind", "trait"],
        ["trait", "Head:Halo"],
        ["trait", "Eyes:Shades"],
        ["min_xrp", "1"],
        ["max_xrp", "50"],
        ["sort", "price_desc"],
        ["limit", "12"],
        ["offset", "0"],
    ]


def test_build_listings_params_omits_empty():
    pairs = run_js('M.buildListingsParams({kind: "character"})')
    assert pairs == [["kind", "character"]]


def test_trait_filter_token():
    assert run_js('M.traitFilterToken("Head", "Halo")') == "Head:Halo"


# --- trait-sell wizard step labels ---


def test_trait_wizard_step_labels():
    assert run_js("M.traitWizardStepLabel('extract_pending')") == "1 of 2: claim your trait token"
    assert run_js("M.traitWizardStepLabel('extract_done')") == "1 of 2: claim your trait token"
    assert run_js("M.traitWizardStepLabel('list_pending')") == "2 of 2: post your listing"
    assert run_js("M.traitWizardStepLabel('listed')") == "2 of 2: post your listing"
    assert run_js("M.traitWizardStepLabel('nonsense')") == ""


# --- marketFlow terminal-state check ---


@pytest.mark.parametrize("state", ["done", "failed", "unknown", "listed"])
def test_is_market_terminal_true(state):
    assert run_js(f"M.isMarketTerminal({json.dumps(state)})") is True


@pytest.mark.parametrize(
    "state", ["awaiting_signature", "pending", "extract_pending", "extract_done", "list_pending"]
)
def test_is_market_terminal_false(state):
    assert run_js(f"M.isMarketTerminal({json.dumps(state)})") is False


def test_closet_required_message_mentions_closet():
    msg = run_js("M.CLOSET_REQUIRED_MESSAGE")
    assert "Closet" in msg


# --- #133: bad-price errors must be catchable, and a non-throwing seam ---
# openBuyFlow renders server-provided amount_xrp through computeRoyalty; a
# malformed amount ("1E+1", "abc", "") must throw (callers handle it), and
# safeComputeRoyalty is the no-throw seam app.js uses to route the failure
# to showError instead of a dead click.


@pytest.mark.parametrize("bad", ["1E+1", "abc", ""])
def test_xrp_to_drops_throws_on_unparseable_amounts(bad):
    assert run_js_throws(f"M.xrpToDropsStr({json.dumps(bad)})") == "RangeError"


@pytest.mark.parametrize("bad", ["1E+1", "abc", ""])
def test_compute_royalty_throws_on_unparseable_amounts(bad):
    assert run_js_throws(f"M.computeRoyalty({json.dumps(bad)})") == "RangeError"


def test_safe_compute_royalty_ok_shape():
    r = run_js("M.safeComputeRoyalty('100')")
    assert r["ok"] is True
    assert r["royalty"]["receiveXrp"] == "93"
    assert r["royalty"]["feeXrp"] == "7"


@pytest.mark.parametrize("bad", ["1E+1", "abc", ""])
def test_safe_compute_royalty_error_shape(bad):
    r = run_js(f"M.safeComputeRoyalty({json.dumps(bad)})")
    assert r["ok"] is False
    assert isinstance(r["error"], str) and r["error"]


# --- #239: BRIX price helpers ---


@pytest.mark.parametrize(
    "brix,micro",
    [("5", "5000000"), ("10.5", "10500000"), ("0.000001", "1")],
)
def test_brix_to_micro(brix, micro):
    assert run_js(f"M.brixToMicroStr({json.dumps(brix)})") == micro


@pytest.mark.parametrize(
    "micro,brix",
    [("5000000", "5"), ("10500000", "10.5"), ("1", "0.000001")],
)
def test_micro_to_brix(micro, brix):
    assert run_js(f"M.microToBrixStr({json.dumps(micro)})") == brix


def test_brix_to_micro_rejects_bad_values():
    assert run_js_throws("M.brixToMicroStr('0')") == "RangeError"
    assert run_js_throws("M.brixToMicroStr('-1')") == "RangeError"
    assert run_js_throws("M.brixToMicroStr('1.1234567')") == "RangeError"
    assert run_js_throws("M.brixToMicroStr('1000000000000001')") == "RangeError"
    assert run_js_throws("M.brixToMicroStr(5)") == "TypeError"


def test_validate_brix_price_shapes():
    ok = run_js("M.validateBrixPrice('10.500000')")
    assert ok == {"ok": True, "value": "10.5"}
    bad = run_js("M.validateBrixPrice('abc')")
    assert bad["ok"] is False and bad["error"]


def test_brix_royalty_disclosure_says_93_and_7():
    text = run_js("M.brixRoyaltyDisclosure('100')")
    assert "93 BRIX" in text
    assert "93%" in text and "7%" in text


def test_price_label_per_kind():
    assert run_js("M.priceLabel({amount_brix: '10.5'})") == "10.5 BRIX"
    assert run_js("M.priceLabel({amount_xrp: '2'})") == "2 XRP"
    assert run_js("M.priceLabel({})") == ""


def test_map_listing_row_trait_carries_brix_price():
    row = {
        "kind": "trait",
        "nft_id": "T1",
        "slot": "Hat",
        "value": "Wizard Hat",
        "amount_brix": "10.5",
        "seller": "rS",
        "offer_index": "OFF1",
    }
    vm = run_js(f"M.mapListingRow({json.dumps(row)})")
    assert vm["amountBrix"] == "10.5"
    assert vm["priceLabel"] == "10.5 BRIX"
    assert vm["amountXrp"] is None


def test_build_listings_params_brix_bounds():
    pairs = run_js(
        "M.buildListingsParams({kind: 'trait', minBrix: '1', maxBrix: '50', sort: 'price_asc'})"
    )
    assert ["min_brix", "1"] in pairs
    assert ["max_brix", "50"] in pairs
    assert not any(k in ("min_xrp", "max_xrp") for k, _ in pairs)


def test_sort_rows_brix_decimal_not_lexicographic():
    rows = [
        {"amount_brix": "100", "kind": "trait"},
        {"amount_brix": "5", "kind": "trait"},
        {"amount_brix": "10.5", "kind": "trait"},
    ]
    out = run_js(f"M.sortRows({json.dumps(rows)}, 'price_asc')")
    assert [r["amount_brix"] for r in out] == ["5", "10.5", "100"]
