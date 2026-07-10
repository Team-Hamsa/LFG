# tests/test_mint_pure_js.py
# Issue #141 follow-up: cancelMint must never dump a PAID user back to the
# mint start screen. When the server refuses the cancel (409 'session is
# past payment' — money already taken) the client has to stay on the flow
# panel and resume polling so the user still reaches the offer_ready accept
# QR for the NFT they just paid for. The go-home-vs-resume decision is a
# pure function in webapp/client/mint_pure.js, executed here under Node
# (same harness as tests/test_market_pure_js.py).
#
# No lfg_core import at module top -> no env-guard preamble needed.
import json
import os
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULE_REL = "./webapp/client/mint_pure.js"

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed on this host")


def run_js(expr: str):
    """Run `expr` (a JS expression referencing the imported module as `M`)
    inside a small Node ES-module script, executed with cwd=ROOT so the
    relative import resolves; returns the JSON-decoded result."""
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
# cancelMintOutcome(cancelResult, refetchResult) -> 'home' | 'resume'
#   cancelResult:  session dict from POST /cancel when it returned 2xx, else null
#   refetchResult: session dict from the GET refetch after a failed cancel,
#                  else null (refetch failed too / never attempted)
# ---------------------------------------------------------------------------


def test_cancel_succeeded_goes_home():
    assert run_js("M.cancelMintOutcome({state: 'cancelled'}, null)") == "home"


def test_cancel_refused_paid_session_resumes_polling():
    """The critical case: 409 (payment confirmed, pipeline running) -> the
    refetch shows the real pipeline state -> stay and poll, never go home."""
    for state in ("generating", "minting", "creating_offer", "awaiting_payment"):
        assert run_js(f"M.cancelMintOutcome(null, {{state: '{state}'}})") == "resume", state


def test_cancel_refused_terminal_states_resume_so_poll_renders_them():
    """A refetch landing on offer_ready must resume so the accept QR shows;
    failed/payment_timeout resume so the poll renders the real error."""
    for state in ("offer_ready", "failed", "payment_timeout"):
        assert run_js(f"M.cancelMintOutcome(null, {{state: '{state}'}})") == "resume", state


def test_cancel_noop_on_finished_session_resumes():
    """POST /cancel on an already-terminal session is a 200 no-op returning
    that terminal state — e.g. the mint finished as the user clicked cancel.
    offer_ready must resume (accept QR), not go home."""
    assert run_js("M.cancelMintOutcome({state: 'offer_ready'}, null)") == "resume"
    assert run_js("M.cancelMintOutcome({state: 'failed'}, null)") == "resume"


def test_session_gone_on_both_calls_goes_home():
    assert run_js("M.cancelMintOutcome(null, null)") == "home"


def test_refetch_shows_cancelled_goes_home():
    """Cancel POST failed transiently but the session is in fact cancelled
    (e.g. a concurrent cancel from another tab landed)."""
    assert run_js("M.cancelMintOutcome(null, {state: 'cancelled'})") == "home"


# ---------------------------------------------------------------------------
# Wiring: app.js must actually route cancelMint through the pure function
# (source assertions, same style as test_app_js_boot.py).
# ---------------------------------------------------------------------------

APP_JS = os.path.join(ROOT, "webapp", "client", "app.js")


def test_app_js_imports_mint_pure():
    src = open(APP_JS).read()
    assert "./mint_pure.js" in src


def test_app_js_cancel_uses_outcome_and_resumes_poll():
    src = open(APP_JS).read()
    body = src.split("async function cancelMint", 1)[1]
    body = body.split("\n}\n", 1)[0]
    assert "cancelMintOutcome" in body
    assert "pollMint(" in body  # the resume path exists inside cancelMint
