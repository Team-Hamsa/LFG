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


def test_app_js_unscanned_cancel_passes_false_not_click_event():
    """Greptile #148: `cancel: cancelMint` would receive the click event as
    `maybeSigned` (truthy), showing the already-approved-in-Xaman warning on
    the unscanned pay screen. The unscanned variant must pass false."""
    src = open(APP_JS).read()
    assert "cancel: () => cancelMint(false)" in src
    assert "cancel: cancelMint," not in src


def test_app_js_poll_generation_guard():
    """Greptile #148: a refused cancel resumes pollMint while an old tick may
    still be awaiting the API — a generation token must invalidate the stale
    tick or two poll chains run for the same session."""
    src = open(APP_JS).read()
    assert "let pollGen = 0" in src
    assert "const gen = ++pollGen" in src
    assert "if (gen !== pollGen) return" in src


# ---------------------------------------------------------------------------
# activeMintSessionId(activeResult) -> session id | null
#   activeResult: the GET /api/mint/active response body ({"session": ...}),
#   or null when the call failed. Resume only a LIVE session — the server
#   filters terminal states already, but the client stays defensive.
#   (Mint session resume: Discord mobile reloads the Activity when the user
#   app-switches to Xaman; the relaunched client re-attaches via this call.)
# ---------------------------------------------------------------------------


def test_active_none_response_goes_home():
    assert run_js("M.activeMintSessionId(null)") is None


def test_active_no_session_goes_home():
    assert run_js("M.activeMintSessionId({session: null})") is None


def test_active_live_session_resumes():
    assert (
        run_js("M.activeMintSessionId({session: {id: 'abc', state: 'awaiting_payment'}})") == "abc"
    )


def test_active_mid_pipeline_session_resumes():
    assert run_js("M.activeMintSessionId({session: {id: 'abc', state: 'minting'}})") == "abc"


def test_active_terminal_session_goes_home():
    for state in ["offer_ready", "done", "failed", "payment_timeout", "cancelled"]:
        assert (
            run_js(f"M.activeMintSessionId({{session: {{id: 'abc', state: '{state}'}}}})") is None
        )


def test_active_session_without_id_goes_home():
    assert run_js("M.activeMintSessionId({session: {state: 'awaiting_payment'}})") is None


# --- app.js wiring: boot must re-attach to a live mint session -------------


def test_app_js_boot_resumes_active_mint():
    src = open(APP_JS).read()
    assert "/api/mint/active" in src
    assert "activeMintSessionId" in src
    assert "function resumeMint" in src


# --- app.js wiring: the swap fee-QR screen must offer a way out ------------
# (User report: a stale fee QR left the Trait Swapper with no regenerate and
# no back button — closing and reopening the whole Activity was the only
# escape. Mirror the mint pay screen's regen + cancel affordances.)


def test_app_js_swap_payment_screen_has_regen_and_cancel():
    src = open(APP_JS).read()
    body = src.split("function renderSwapPayment", 1)[1].split("\nfunction ", 1)[0]
    assert "regenerate" in body
    assert "cancelSwap" in body


def test_app_js_swap_regenerated_qr_rerenders():
    """renderSwapPayment skips re-rendering for the same session id; after a
    regenerate the payment_link changes but the id doesn't, so the guard must
    key on the link too or the fresh QR never appears."""
    src = open(APP_JS).read()
    body = src.split("function renderSwapPayment", 1)[1].split("\nfunction ", 1)[0]
    assert "payment_link" in body.split("return;", 1)[0]


def test_app_js_swap_poll_handles_cancelled():
    src = open(APP_JS).read()
    body = src.split("function pollSwap", 1)[1].split("\nfunction ", 1)[0]
    assert "'cancelled'" in body


def test_app_js_swap_cancel_reuses_cancel_outcome():
    """cancelSwap must reuse the shared cancel decision (mint issue #141):
    a refused cancel (fee already paid) resumes polling, never strands or
    dumps the user."""
    src = open(APP_JS).read()
    assert "async function cancelSwap" in src
    body = src.split("async function cancelSwap", 1)[1].split("\n}\n", 1)[0]
    assert "cancelMintOutcome" in body
    assert "pollSwap(" in body


def test_app_js_main_wallet_branch_awaits_resume_before_home():
    """CodeRabbit #216: global substring checks pass even if resumeMint goes
    dead — assert the exact boot wiring: the wallet branch awaits resumeMint
    and only falls back to showMintHome when nothing resumed."""
    src = open(APP_JS).read()
    main_body = src.split("async function main", 1)[1]
    assert "if (!(await resumeBulkMint()) && !(await resumeMint())) showMintHome();" in main_body


def test_app_js_resume_cancel_warns_only_when_scanned():
    """Greptile #216 P2: a resumed unscanned awaiting_payment session provably
    has nothing signed in Xaman — the cancel warning must key on the session's
    qr_scanned flag, not fire unconditionally."""
    src = open(APP_JS).read()
    body = src.split("async function resumeMint", 1)[1].split("\n}\n", 1)[0]
    assert "cancelMint(true)" not in body
    # The exact conditional wiring, not mere symbol presence: the warning flag
    # IS the session's scan state.
    assert "cancelMint(!!active.session.qr_scanned)" in body


def test_app_js_swap_cancel_invalidates_inflight_poll():
    """CodeRabbit #216: clearTimeout can't stop a tick already awaiting the
    status API — it could repaint the fee screen after openSwapper(). The
    generation token must be bumped on the way out."""
    src = open(APP_JS).read()
    body = src.split("async function cancelSwap", 1)[1].split("\n}\n", 1)[0]
    # On the exit path specifically: after the go-home decision, before the
    # panel switch — not merely somewhere in the function.
    outcome_idx = body.index("cancelMintOutcome")
    bump_idx = body.index("++swapPollGen")
    open_idx = body.index("openSwapper()", outcome_idx)
    assert outcome_idx < bump_idx < open_idx


# ---------------------------------------------------------------------------
# Bulk-mint pay-page quantity helpers (#215 UX revision)
#   clampQty(q, max)                 -> int in [1, max]
#   qtyStale(selectedQty, liveQty)   -> bool (shown QR no longer matches qty)
#   qtyMintTarget(selectedQty)       -> 'single' | 'bulk'
# ---------------------------------------------------------------------------


def test_clamp_qty_bounds():
    assert run_js("M.clampQty(1, 10)") == 1
    assert run_js("M.clampQty(0, 10)") == 1
    assert run_js("M.clampQty(-5, 10)") == 1
    assert run_js("M.clampQty(10, 10)") == 10
    assert run_js("M.clampQty(11, 10)") == 10
    assert run_js("M.clampQty(3, 10)") == 3


def test_clamp_qty_non_finite_is_one():
    assert run_js("M.clampQty(NaN, 10)") == 1
    assert run_js("M.clampQty(undefined, 10)") == 1


def test_qty_stale_no_live_session_is_stale():
    # liveQty null == no live payload backs the shown QR
    assert run_js("M.qtyStale(1, null)") is True
    assert run_js("M.qtyStale(3, null)") is True


def test_qty_stale_matching_qty_is_fresh():
    assert run_js("M.qtyStale(1, 1)") is False
    assert run_js("M.qtyStale(3, 3)") is False


def test_qty_stale_changed_qty_is_stale():
    assert run_js("M.qtyStale(3, 1)") is True
    assert run_js("M.qtyStale(1, 3)") is True


def test_qty_mint_target():
    assert run_js("M.qtyMintTarget(1)") == "single"
    assert run_js("M.qtyMintTarget(2)") == "bulk"
    assert run_js("M.qtyMintTarget(10)") == "bulk"


# ---------------------------------------------------------------------------
# #215 UX revision: home stepper removed + buttons relabelled; the quantity
# stepper markup now lives in the flow (pay) panel.
# ---------------------------------------------------------------------------

INDEX_HTML = os.path.join(ROOT, "webapp", "client", "index.html")


def test_home_has_no_qty_stepper():
    html = open(INDEX_HTML).read()
    mint_panel = html.split('id="mint-panel"', 1)[1].split("</section>", 1)[0]
    assert 'id="mint-qty"' not in mint_panel  # stepper moved off the home screen


def test_home_buttons_relabelled():
    html = open(INDEX_HTML).read()
    assert 'id="mint-btn" class="primary big">⛏️ Mint<' in html
    assert 'id="swap-btn" class="secondary">🏗️ Build<' in html
    assert 'id="swapper-btn" class="secondary">🔁 Swap<' in html
    assert 'id="market-btn" class="secondary">🛒 Trade<' in html


def test_flow_panel_has_qty_stepper():
    html = open(INDEX_HTML).read()
    flow_panel = html.split('id="flow-panel"', 1)[1].split("</section>", 1)[0]
    assert 'id="flow-qty"' in flow_panel
    assert 'id="flow-qty-minus"' in flow_panel
    assert 'id="flow-qty-value"' in flow_panel
    assert 'id="flow-qty-plus"' in flow_panel


# ---------------------------------------------------------------------------
# #215 UX revision: app.js wiring for the pay-page quantity stepper.
# Source-assertion style (same as the boot/cancel wiring tests above).
# ---------------------------------------------------------------------------


def test_app_js_mint_btn_always_single():
    """Home Mint button no longer branches on qty — it always starts a single
    mint; quantity is chosen on the pay page now."""
    src = open(APP_JS).read()
    assert "mintQty > 1 ? startBulkMint" not in src
    assert "el('mint-btn').onclick = () => startMint();" in src


def test_app_js_regen_uses_qty_aware_handler():
    src = open(APP_JS).read()
    assert "el('flow-regen-btn').onclick = onFlowRegen;" in src


def test_app_js_regen_routes_by_qty_target():
    """onFlowRegen commits the selected quantity to the right endpoint."""
    src = open(APP_JS).read()
    body = src.split("async function onFlowRegen", 1)[1].split("\n}\n", 1)[0]
    assert "qtyMintTarget" in body
    assert "startBulkMint" in body
    assert "regeneratePaymentQr" in body  # same-qty expired refresh keeps the session


def test_app_js_qty_change_cancels_live_payload():
    """Changing quantity must cancel the live session server-side immediately."""
    src = open(APP_JS).read()
    body = src.split("function onQtyChange", 1)[1].split("\n}\n", 1)[0]
    assert "cancelLiveMintSilently" in body
    assert "needs-regen" in body  # highlights regenerate
    assert "qr-stale" in body  # dims the QR


def test_app_js_cancel_silent_hits_both_endpoints_and_stops_polls():
    src = open(APP_JS).read()
    body = src.split("async function cancelLiveMintSilently", 1)[1].split("\n}\n", 1)[0]
    assert "/api/mint/" in body and "/cancel" in body
    assert "/api/mint/bulk/" in body
    assert "++pollGen" in body  # stop the single-mint poll chain
    assert "++bulkPollGen" in body  # stop the bulk poll chain
    assert "liveQty = null" in body


def test_app_js_showflow_renders_qty_stepper_flag():
    src = open(APP_JS).read()
    body = src.split("function showFlow(", 1)[1].split("\n}\n", 1)[0]
    assert "qtyStepper" in body
    assert "el('flow-qty').hidden" in body
    # a fresh render clears any leftover stale visuals
    assert "classList.remove('qr-stale')" in body
    assert "classList.remove('needs-regen')" in body


def test_app_js_pay_views_request_stepper():
    """Both the single and bulk pay views opt into the stepper."""
    src = open(APP_JS).read()
    mint_body = src.split("function mintPayView", 1)[1].split("\n}\n", 1)[0]
    bulk_body = src.split("function bulkPayView", 1)[1].split("\n}\n", 1)[0]
    assert "qtyStepper: true" in mint_body
    assert "qtyStepper: true" in bulk_body


def test_app_js_start_paths_set_live_qty():
    src = open(APP_JS).read()
    single = src.split("async function startMint", 1)[1].split("\n}\n", 1)[0]
    bulk = src.split("async function startBulkMint", 1)[1].split("\n}\n", 1)[0]
    assert "liveQty = 1" in single
    assert "liveQty = quantity" in bulk


def test_setup_bulk_stepper_noops_when_flag_off():
    """Flag off: setupBulkStepper returns before wiring any stepper handler, so
    the pay page is exactly today's single-mint page."""
    src = open(APP_JS).read()
    body = src.split("function setupBulkStepper", 1)[1].split("\n}\n", 1)[0]
    # The early return guards all stepper wiring.
    ret_idx = body.index("if (!bulkCfg.enabled) return;")
    wire_idx = body.index("flow-qty-minus")
    assert ret_idx < wire_idx


def test_app_js_regen_cancels_live_before_fresh_start():
    """A regenerate that isn't the same-session expired-QR refresh must cancel
    whatever payload/job is live before starting a fresh one, or a repeated
    bulk Regenerate tap orphans the old bulk job's XUMM payload + headroom
    reservation (#226)."""
    src = open(APP_JS).read()
    assert "async function onFlowRegen" in src
    body = src.split("async function onFlowRegen", 1)[1].split("\n}\n", 1)[0]
    assert "regeneratePaymentQr()" in body  # same-session refresh path preserved
    cancel_idx = body.index("cancelLiveMintSilently")
    start_idx = body.index("startBulkMint")
    assert cancel_idx < start_idx  # cancel the old payload before starting fresh


def test_app_js_regen_has_single_flight_guard():
    """A double-tap on Regenerate must not be able to race a second
    await cancelLiveMintSilently() into launching a second concurrent bulk
    job (PR #290 review): the button must be disabled for the whole
    cancel+start round trip and re-enabled in a finally block."""
    src = open(APP_JS).read()
    body = src.split("async function onFlowRegen", 1)[1].split("\n}\n", 1)[0]
    assert "regeneratePaymentQr()" in body  # same-session early-return preserved
    assert "btn.disabled = true" in body
    assert "finally" in body
    finally_body = body.split("finally", 1)[1]
    assert "btn.disabled = false" in finally_body


def test_app_js_bulk_payview_hides_regen():
    """A fresh bulk job has no same-session QR refresh, so bulkPayView must
    not opt into the Regenerate button (onQtyChange reveals it when a qty
    change actually invalidates the shown QR)."""
    src = open(APP_JS).read()
    body = src.split("function bulkPayView", 1)[1].split("\n}\n", 1)[0]
    assert "qtyStepper: true" in body
    assert "regen: true" not in body
