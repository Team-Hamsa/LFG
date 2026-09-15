"""Fee-cover client copy (executes market_pure.js under Node, like
tests/test_market_pure_js.py) plus app.js wiring assertions."""

import json
import os
import re
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULE_REL = "./webapp/client/market_pure.js"
NODE = shutil.which("node")


def run_js(expr):
    if NODE is None:
        pytest.skip("node is not installed on this host")
    script = (
        f"import * as M from {json.dumps(MODULE_REL)};\n"
        f"const result = ({expr});\n"
        "console.log(JSON.stringify(result === undefined ? null : result));\n"
    )
    proc = subprocess.run(
        [NODE, "--input-type=module"],
        input=script,
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=15,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


EXTERNAL_VM = '{external: true, clearingXrp: "5.070572", amountXrp: "4.99", marketplace: "xrp.cafe", feeCoverXrp: "0.080572"}'


def _fc(state, reason=None):
    return json.dumps(
        {
            "state": state,
            "drops": 80642,
            "xrp": "0.080642",
            "reason": reason,
            "payout_tx_hash": None,
        }
    )


def test_map_listing_row_carries_the_estimate():
    assert (
        run_js('M.mapListingRow({kind: "character", fee_cover_xrp: "0.080572"}).feeCoverXrp')
        == "0.080572"
    )
    assert run_js('M.mapListingRow({kind: "character"}).feeCoverXrp') is None


def test_fee_cover_note():
    assert run_js(f"M.feeCoverNote({EXTERNAL_VM})") == (
        "LFG refunds xrp.cafe's 0.080572 XRP fee after it settles, so you pay the 4.99 XRP ask (campaign limits apply)."
    )
    assert run_js('M.feeCoverNote({external: true, clearingXrp: "5", feeCoverXrp: null})') == ""
    assert run_js('M.feeCoverNote({external: false, clearingXrp: "5", feeCoverXrp: "1"})') == ""


@pytest.mark.parametrize(
    ("state", "reason", "expected"),
    [
        (
            "quoted",
            None,
            "LFG covers the marketplace fee on this purchase (campaign limits apply).",
        ),
        ("open", None, "Fee refund of 0.080642 XRP reserved."),
        ("owed", None, "Your 0.080642 XRP fee refund is on its way."),
        ("submitted", None, "Your 0.080642 XRP fee refund is on its way."),
        ("confirmed", None, "Refunded 0.080642 XRP."),
        (
            "declined",
            "wallet_cap",
            "This purchase isn't covered: you've reached this campaign's per-wallet limit.",
        ),
        ("declined", "something_new", "This purchase isn't covered: not eligible."),
        ("failed", None, "We couldn't send your fee refund. Contact support."),
        ("released", None, ""),
    ],
)
def test_fee_cover_line(state, reason, expected):
    assert run_js(f"M.feeCoverLine({_fc(state, reason)})") == expected


def test_fee_cover_line_and_pending_handle_null():
    assert run_js("M.feeCoverLine(null)") == ""
    assert run_js("M.feeCoverPending(null)") is False
    assert [
        run_js(f"M.feeCoverPending({_fc(s)})")
        for s in ("open", "owed", "submitted", "confirmed", "declined")
    ] == [
        True,
        True,
        True,
        False,
        False,
    ]


def test_external_fill_copy_appends_the_refund_line_and_is_unchanged_without_it():
    two_arg = run_js('M.externalFillCopy("xrp.cafe", "accepted")')
    three_arg_null = run_js('M.externalFillCopy("xrp.cafe", "accepted", null)')
    assert two_arg == three_arg_null
    with_refund = run_js(f'M.externalFillCopy("xrp.cafe", "accepted", {_fc("owed")})')
    assert with_refund["title"] == two_arg["title"] and with_refund["done"] is True
    assert with_refund["text"] == two_arg["text"] + " Your 0.080642 XRP fee refund is on its way."


def test_every_decline_reason_has_copy():
    reasons = run_js("Object.keys(M.FEE_COVER_DECLINE_COPY).sort()")
    assert reasons == sorted(
        [
            "campaign_inactive",
            "below_clearing",
            "below_min_bid",
            "system_wallet",
            "budget_exhausted",
            "wallet_cap",
            "not_broker_settled",
            "fee_unobserved",
            "issuer_party",
            "royalty_unobserved",
            "linked_counterparty",
            "zero_refund",
        ]
    )


def _app_js():
    with open(os.path.join(ROOT, "webapp", "client", "app.js"), encoding="utf-8") as f:
        return f.read()


def test_app_js_renders_refund_state_and_keeps_polling_for_it():
    src = _app_js()
    assert "marketPure.externalFillCopy(marketplace, s.fill, s.fee_cover)" in src  # the done render
    assert "marketPure.externalFillCopy(marketplace, fill, s.fee_cover)" in src  # the fill watcher
    assert src.count("marketPure.feeCoverNote(vm) || marketPure.externalFeeNote(vm)") == 2
    assert "FEE_COVER_REFUND_WAIT_MS" in src
    assert "marketPure.feeCoverPending(s.fee_cover)" in src
    assert "marketPure.feeCoverLine(s.fee_cover)" in src


def test_market_pure_import_and_app_js_cache_busters_move_together():
    src = _app_js()
    import_v = int(re.search(r"market_pure\.js\?v=(\d+)", src).group(1))
    with open(os.path.join(ROOT, "webapp", "client", "index.html"), encoding="utf-8") as f:
        app_v = int(re.search(r"app\.js\?v=(\d+)", f.read()).group(1))
    assert import_v >= 28 and app_v >= 89
