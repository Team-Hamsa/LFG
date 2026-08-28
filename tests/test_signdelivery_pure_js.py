# tests/test_signdelivery_pure_js.py
# #142 — Xaman sign-request delivery decisions, kept in the pure module
# webapp/client/signdelivery_pure.js and executed here under Node — same
# harness as tests/test_mint_pure_js.py / tests/test_build_pure_js.py.
#
# The truth table (spec 2026-07-24-xaman-deeplink-sign-requests-design.md):
#   push='sent' (any pointer)      -> QR collapsed, no auto-open (push already
#                                     delivered the request to Xaman)
#   push!='sent' + coarse pointer  -> deep link primary, QR collapsed,
#                                     auto-open once
#   push!='sent' + fine pointer    -> QR primary (today's desktop behavior)
#
# No lfg_core import at module top -> no env-guard preamble needed.
import json
import os
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULE_REL = "./webapp/client/signdelivery_pure.js"

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed on this host")


def run_js(expr: str):
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


# ---------------------------------------------------------------------------
# signDelivery({push, coarse, hasLink, hasQr}) -> {linkPrimary, qrCollapsed, autoOpen}
# ---------------------------------------------------------------------------


def test_push_sent_collapses_qr_regardless_of_pointer():
    for coarse in ("true", "false"):
        d = run_js(
            f"M.signDelivery({{push: 'sent', coarse: {coarse}, hasLink: true, hasQr: true}})"
        )
        assert d["qrCollapsed"] is True
        assert d["autoOpen"] is False  # never app-switch when push already delivered


def test_coarse_pointer_promotes_deeplink_and_collapses_qr():
    d = run_js("M.signDelivery({push: null, coarse: true, hasLink: true, hasQr: true})")
    assert d == {"linkPrimary": True, "qrCollapsed": True, "autoOpen": True}


def test_push_failed_on_coarse_still_deeplink_primary():
    d = run_js("M.signDelivery({push: 'failed', coarse: true, hasLink: true, hasQr: true})")
    assert d["linkPrimary"] is True and d["autoOpen"] is True


def test_fine_pointer_keeps_qr_primary_unchanged():
    d = run_js("M.signDelivery({push: null, coarse: false, hasLink: true, hasQr: true})")
    assert d == {"linkPrimary": False, "qrCollapsed": False, "autoOpen": False}


def test_no_link_never_promotes_or_auto_opens():
    d = run_js("M.signDelivery({push: null, coarse: true, hasLink: false, hasQr: true})")
    assert d["linkPrimary"] is False
    assert d["qrCollapsed"] is False  # never hide the only affordance left
    assert d["autoOpen"] is False


def test_no_qr_never_reports_collapsed():
    d = run_js("M.signDelivery({push: 'sent', coarse: true, hasLink: true, hasQr: false})")
    assert d["qrCollapsed"] is False


# ---------------------------------------------------------------------------
# shouldAutoOpen(seen, link) — at most one open per unique payload link
# ---------------------------------------------------------------------------


def test_should_auto_open_once_per_link():
    assert run_js("M.shouldAutoOpen([], 'https://xumm.app/sign/u')") is True
    assert (
        run_js("M.shouldAutoOpen(['https://xumm.app/sign/u'], 'https://xumm.app/sign/u')") is False
    )


def test_should_auto_open_rejects_missing_link():
    assert run_js("M.shouldAutoOpen([], null)") is False
    assert run_js("M.shouldAutoOpen([], '')") is False


# ---------------------------------------------------------------------------
# autoOpenOutcome(seen, link, launched) — a DETECTABLY blocked launch
# (window.open returning null) un-marks the link so a later render retries;
# success or an undetectable opener (Discord SDK promise) keeps the mark.
# ---------------------------------------------------------------------------


def test_blocked_launch_unmarks_for_retry():
    link = "https://xumm.app/sign/u"
    # optimistic mark, launch blocked -> un-marked -> shouldAutoOpen again
    assert run_js(f"M.autoOpenOutcome(['{link}'], '{link}', false)") == []
    assert (
        run_js(f"M.shouldAutoOpen(M.autoOpenOutcome(['{link}'], '{link}', false), '{link}')")
        is True
    )


def test_successful_or_undetectable_launch_keeps_mark():
    link = "https://xumm.app/sign/u"
    assert run_js(f"M.autoOpenOutcome(['{link}'], '{link}', true)") == [link]
    # undetectable opener result (e.g. a promise coerced to truthy/undefined)
    assert run_js(f"M.autoOpenOutcome(['{link}'], '{link}', undefined)") == [link]


def test_blocked_unmark_only_removes_that_link():
    assert run_js("M.autoOpenOutcome(['a', 'b'], 'a', false)") == ["b"]


# ---------------------------------------------------------------------------
# #447 WalletConnect (Joey Wallet) helpers
#
# A WalletConnect sign request reaches the client as the SAME `link` field
# every Xaman flow already carries, but with an `lfg-wc://<id>` scheme instead
# of a xumm.app URL — applySignDelivery branches on it and hands the id to
# wcSign() rather than rendering a QR.
# ---------------------------------------------------------------------------


def test_is_wc_link_only_matches_the_lfg_wc_scheme():
    assert run_js("M.isWcLink('lfg-wc://wc-abc')") is True
    assert run_js("M.isWcLink('https://xumm.app/sign/u')") is False
    assert run_js("M.isWcLink(null)") is False
    assert run_js("M.isWcLink('')") is False
    # Not a prefix match anywhere but the start.
    assert run_js("M.isWcLink('https://x/?u=lfg-wc://a')") is False


def test_wc_request_id_extracts_the_id_or_null():
    assert run_js("M.wcRequestId('lfg-wc://wc-abc')") == "wc-abc"
    assert run_js("M.wcRequestId('https://xumm.app/sign/u')") is None
    assert run_js("M.wcRequestId(null)") is None
    # An empty id is not an id.
    assert run_js("M.wcRequestId('lfg-wc://')") is None


# wcResultAction(resp): Joey returns {tx_json, hash?}. `hash` is present only
# when the wallet actually submitted; a submit failure inside the wallet comes
# back with no hash at all and must be reported as an error, never as success.
def test_wc_result_action_reports_the_hash_when_submitted():
    assert run_js("M.wcResultAction({tx_json: {}, hash: 'AB12'})") == {"hash": "AB12"}


def test_wc_result_action_reports_an_error_without_a_hash():
    no_hash = {"error": "no hash returned"}
    assert run_js("M.wcResultAction({tx_json: {}})") == no_hash
    assert run_js("M.wcResultAction({tx_json: {}, hash: ''})") == no_hash
    assert run_js("M.wcResultAction({tx_json: {}, hash: 123})") == no_hash
    assert run_js("M.wcResultAction(null)") == no_hash


# isWcRejection(err): a user declining in Joey surfaces as a WalletConnect
# JSON-RPC error, not a thrown transport failure — it must post
# {rejected:true}, never {error:…}.
def test_is_wc_rejection_matches_the_rejection_codes_and_message():
    assert run_js("M.isWcRejection({code: 5000, message: 'User rejected.'})") is True
    assert run_js("M.isWcRejection({code: 4001})") is True
    assert run_js("M.isWcRejection({message: 'User Rejected Request'})") is True
    assert run_js("M.isWcRejection({code: -32000, message: 'relay timeout'})") is False
    assert run_js("M.isWcRejection(null)") is False
