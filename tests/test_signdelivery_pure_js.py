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
