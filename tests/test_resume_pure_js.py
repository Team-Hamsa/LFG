# tests/test_resume_pure_js.py
# Issue #221: the cold-boot session-resume decision (which live flow to
# re-attach to after an Activity webview relaunch) is a pure function in
# webapp/client/resume_pure.js, executed here under Node (same harness as
# tests/test_mint_pure_js.py / test_market_pure_js.py).
#
# No lfg_core import at module top -> no env-guard preamble needed.
import json
import os
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULE_REL = "./webapp/client/resume_pure.js"

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


def pick(sessions):
    return run_js(f"M.pickActiveFlow({json.dumps(sessions)})")


EMPTY = {"mint": None, "bulk": None, "swap": None, "market": None, "economy": None, "shop": None}


def _with(**kw):
    d = dict(EMPTY)
    d.update(kw)
    return d


def test_null_or_empty_payload():
    assert pick(None) is None
    assert pick({}) is None
    assert pick(EMPTY) is None


def test_single_live_flow_is_picked():
    got = pick(_with(swap={"id": "s1", "state": "awaiting_payment"}))
    assert got == {"flow": "swap", "session": {"id": "s1", "state": "awaiting_payment"}}


def test_priority_mint_over_market():
    got = pick(
        _with(
            mint={"id": "m1", "state": "awaiting_payment"},
            market={"id": "k1", "state": "awaiting_signature", "kind": "buy"},
        )
    )
    assert got["flow"] == "mint"


def test_priority_bulk_over_swap():
    got = pick(
        _with(
            bulk={"id": "b1", "state": "fulfilling"},
            swap={"id": "s1", "state": "composing"},
        )
    )
    assert got["flow"] == "bulk"


def test_terminal_states_skipped_per_flow():
    # A (stale-race) terminal session must never be resumed; the next live
    # flow down the priority order wins instead.
    got = pick(
        _with(
            mint={"id": "m1", "state": "offer_ready"},  # terminal for mint
            shop={"id": "h1", "state": "awaiting_accept"},
        )
    )
    assert got["flow"] == "shop"
    # swap's offers_ready is terminal (results screen already reachable via
    # Xaman); economy done/failed likewise.
    assert pick(_with(swap={"id": "s1", "state": "offers_ready"})) is None
    assert pick(_with(economy={"id": "e1", "state": "failed"})) is None
    assert pick(_with(market={"id": "k1", "state": "listed"})) is None
    assert pick(_with(market={"id": "k1", "state": "unknown"})) is None
    assert pick(_with(bulk={"id": "b1", "state": "payment_timeout"})) is None
    assert pick(_with(shop={"id": "h1", "state": "done"})) is None


def test_sessions_without_id_ignored():
    assert pick(_with(swap={"state": "awaiting_payment"})) is None


def test_market_session_returned_intact_for_routing():
    s = {"id": "k1", "state": "awaiting_signature", "kind": "trait_list", "nft_id": "00A"}
    got = pick(_with(market=s))
    assert got == {"flow": "market", "session": s}


def test_flow_order_exported():
    assert run_js("M.FLOW_ORDER") == ["mint", "bulk", "swap", "market", "economy", "shop"]


# --- Greptile #376 P1: chained resume when two flows were live ---------------
# Single-winner attach is deliberate (one panel), but once the attached flow
# finishes, the OTHER still-live session must surface on the next home
# landing instead of staying hidden until a full relaunch.


def test_terminal_flow_yields_to_next_live_flow():
    # Envelope re-fetched after flow A (mint) went terminal: picker must now
    # return flow B (swap) — the chained resumeAnyFlow re-check relies on this.
    got = pick(
        _with(
            mint={"id": "m1", "state": "done"},
            swap={"id": "s1", "state": "awaiting_payment"},
        )
    )
    assert got["flow"] == "swap"


def has_other(sessions, flow):
    return run_js(f"M.hasOtherActiveFlow({json.dumps(sessions)}, {json.dumps(flow)})")


def test_has_other_active_flow():
    two = _with(
        mint={"id": "m1", "state": "awaiting_payment"},
        swap={"id": "s1", "state": "awaiting_payment"},
    )
    assert has_other(two, "mint") is True
    assert has_other(_with(mint={"id": "m1", "state": "awaiting_payment"}), "mint") is False
    # a terminal or id-less other flow doesn't count
    assert (
        has_other(
            _with(
                mint={"id": "m1", "state": "awaiting_payment"},
                swap={"id": "s1", "state": "done"},
            ),
            "mint",
        )
        is False
    )
    assert has_other(None, "mint") is False


APP_JS = os.path.join(ROOT, "webapp", "client", "app.js")


def test_app_js_arms_recheck_and_home_rechecks():
    """Wiring: resumeAnyFlow arms a one-shot re-check when another flow was
    live besides the attached winner, and showMintHome consumes it — re-running
    resumeAnyFlow (falling through to the real home render when nothing is
    left) so the second flow can never be stranded invisible."""
    src = open(APP_JS).read()
    resume_body = src.split("async function resumeAnyFlow", 1)[1].split("\n}\n", 1)[0]
    assert "hasOtherActiveFlow(sessions, flow)" in resume_body
    home_body = src.split("function showMintHome", 1)[1].split("\n}\n", 1)[0]
    assert "resumeRecheckArmed" in home_body
    assert "resumeAnyFlow()" in home_body
    # one-shot: the flag is cleared BEFORE the re-check so a no-op recheck
    # (nothing live) lands home without looping.
    assert home_body.index("resumeRecheckArmed = false") < home_body.index("resumeAnyFlow()")


def test_app_js_resume_invalidates_stale_flow_polls():
    """Greptile #376 round 2: pollMint keeps watching through offer_ready and
    only stops when flow-panel hides — a chained market/economy/shop resume
    keeps that panel visible, so the attach must invalidate every flow-panel
    poller (generation bumps for the gen-guarded ones) or the stale mint tick
    repaints over the resumed flow."""
    src = open(APP_JS).read()
    resume_body = src.split("async function resumeAnyFlow", 1)[1].split("\n}\n", 1)[0]
    assert "invalidateFlowPolls()" in resume_body
    inv = src.split("function invalidateFlowPolls", 1)[1].split("\n}\n", 1)[0]
    assert "pollGen++" in inv
    assert "bulkPollGen++" in inv
    assert "swapPollGen" in inv
    assert "marketFlowTimer" in inv
    assert "shopFlowTimer" in inv
