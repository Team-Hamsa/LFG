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
    # ...and it runs BEFORE any attach is dispatched (the switch), not after.
    assert resume_body.index("invalidateFlowPolls()") < resume_body.index("switch (flow)")
    # The exact state changes, not mere identifier presence: every gen-guarded
    # poller's generation bumps (killing ticks already awaiting their fetch)
    # and every plain timer is cleared.
    inv = src.split("function invalidateFlowPolls", 1)[1].split("\n}\n", 1)[0]
    assert "clearTimeout(pollTimer)" in inv
    assert "pollGen++" in inv
    assert "clearTimeout(bulkPollTimer)" in inv
    assert "bulkPollGen++" in inv
    assert "clearTimeout(swapPollTimer)" in inv
    assert "swapPollGen++" in inv
    assert "clearTimeout(marketFlowTimer)" in inv
    assert "marketFlowGen++" in inv
    assert "clearTimeout(shopFlowTimer)" in inv
    assert "shopFlowGen++" in inv
    assert "flowRenderGen++" in inv


def test_app_js_economy_resume_result_is_ownership_guarded():
    """Greptile #376 rounds 4-5: the awaited pollEconomyOp promise can resolve
    after another flow took over the shared flow-panel; a visibility check
    alone can't tell 'my panel' from 'someone else's panel'. Ownership is
    flowRenderGen, bumped by EVERY showFlow() render — so a normal flow start
    (mint/market/shop/bulk/economy), not just a resume attach, supersedes the
    pending callback. attachEconomyResume captures it AFTER its own render and
    the callback bails when superseded, before the visibility check."""
    src = open(APP_JS).read()
    show_flow = src.split("function showFlow(", 1)[1].split("\n}\n", 1)[0]
    assert "flowRenderGen++;" in show_flow
    body = src.split("function attachEconomyResume", 1)[1].split("\n}\n", 1)[0]
    assert "const gen = flowRenderGen;" in body
    # captured after this attach's own showFlow render (which bumped it)
    assert body.index("showFlow({") < body.index("const gen = flowRenderGen;")
    assert "if (gen !== flowRenderGen) return;" in body
    # the ownership check comes before the visibility check in the callback
    assert body.index("gen !== flowRenderGen) return") < body.index("el('flow-panel').hidden")


def test_app_js_market_and_shop_polls_are_generation_guarded():
    """Greptile #376 round 3: clearTimeout only cancels the NEXT tick — a tick
    already awaiting its status fetch resumes afterwards, repaints flow-panel
    over the newly attached flow, and re-arms. The market/shop pollers need
    the same generation guard pollMint/pollSwap already carry: capture the gen
    at chain start and bail (before render AND before re-arming) when
    superseded."""
    src = open(APP_JS).read()
    for fn, gen in (
        ("function pollMarketFlow", "marketFlowGen"),
        ("function pollShopFlow", "shopFlowGen"),
    ):
        body = src.split(fn, 1)[1].split("\n}\n", 1)[0]
        assert f"const gen = ++{gen};" in body
        # positions, not counts: one guard before the await, one strictly
        # between the await and the render (a regression placing both guards
        # before the await must fail here).
        guard = f"if (gen !== {gen} || ownerGen !== flowRenderGen) return;"
        first_guard = body.index(guard)
        await_api = body.index("await api(")
        second_guard = body.index(guard, await_api)
        render = body.index("showFlow(")
        assert first_guard < await_api < second_guard < render
        # the transient-error re-arm after catch is guarded, and the guard
        # precedes that setTimeout
        catch_idx = body.index("} catch (e) {")
        rearm_guard = body.index(f"if (gen === {gen} && ownerGen === flowRenderGen)", catch_idx)
        rearm = body.index("setTimeout(tick, 3000)", rearm_guard)
        assert catch_idx < rearm_guard < rearm
        # shared-panel ownership (CodeRabbit #376): captured at chain start,
        # refreshed after this poller's own render
        assert "let ownerGen = flowRenderGen;" in body
        refresh = body.index("ownerGen = flowRenderGen;", render)
        assert render < refresh


def test_app_js_swap_cancel_consumes_armed_recheck():
    """Greptile #376 round 5: cancelling a resumed swap used to exit to
    openSwapper(), never consuming the armed re-check — the other live flow
    stayed hidden until another relaunch. Both swap-cancel exits (the cancel
    handler and pollSwap's cancelled-elsewhere branch) must route through
    exitSwapAfterCancel, which lands on showMintHome (consuming the re-check)
    when it is armed."""
    src = open(APP_JS).read()
    exit_body = src.split("function exitSwapAfterCancel", 1)[1].split("\n}\n", 1)[0]
    assert "resumeRecheckArmed" in exit_body
    assert "showMintHome()" in exit_body
    assert "openSwapper()" in exit_body
    cancel_body = src.split("async function cancelSwap", 1)[1].split("\n}\n", 1)[0]
    assert "exitSwapAfterCancel();" in cancel_body
    assert "openSwapper()" not in cancel_body
    poll_body = src.split("function pollSwap", 1)[1].split("\n}\n", 1)[0]
    assert "if (s.state === 'cancelled') { exitSwapAfterCancel(); return; }" in poll_body
    # Greptile #376 round 6: cancelSwap and pollSwap's cancelled branch can
    # race — both observing the same cancellation. Only the first exit may
    # act, or the second (finding the re-check consumed) opens the swap
    # picker over the freshly resumed flow. The dedupe flag guards the exit
    # and is re-armed by each new poll chain.
    assert exit_body.index("if (swapExitHandled) return;") < exit_body.index(
        "swapExitHandled = true;"
    )
    assert exit_body.index("swapExitHandled = true;") < exit_body.index("resumeRecheckArmed")
    # branch behavior: the armed path takes showMintHome AND returns without
    # reaching openSwapper; the unarmed fallthrough is openSwapper.
    assert "if (resumeRecheckArmed) { showMintHome(); return; }" in exit_body
    assert exit_body.index("showMintHome()") < exit_body.index("openSwapper()")
    # reset scope: the flag re-arms once per poll chain, BEFORE tick is
    # defined/scheduled — never inside tick (a mid-chain reset would let the
    # second racing exit act again).
    reset_idx = poll_body.index("swapExitHandled = false;")
    tick_idx = poll_body.index("const tick = async () =>")
    assert reset_idx < tick_idx
    assert "swapExitHandled" not in poll_body[tick_idx:]
