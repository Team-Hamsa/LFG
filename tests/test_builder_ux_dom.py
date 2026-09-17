# tests/test_builder_ux_dom.py
# T31 — Builder (Assemble) UX: layered preview, body switch keeps traits,
# pickers collapse after a choice. The webapp client has no JS execution
# harness for DOM code (see test_market_panel_dom.py / test_closet_market_dom.py),
# so this guards the wiring with source assertions on webapp/client/app.js,
# index.html and the stylesheet; the decision logic itself lives in
# build_pure.js and is executed under Node by tests/test_build_pure_js.py.
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLIENT = os.path.join(ROOT, "webapp", "client")


def _read(name: str) -> str:
    with open(os.path.join(CLIENT, name), encoding="utf-8") as f:
        return f.read()


def _stylesheet() -> str:
    html = _read("index.html")
    m = re.search(r'<link rel="stylesheet" href="(style[^"]*\.css)[^"]*"', html)
    assert m, "index.html has no app stylesheet <link>"
    return _read(m.group(1))


def _fn_body(js: str, start_sig: str, *end_sigs: str) -> str:
    """The source text of one top-level function, from its signature up to
    whichever of `end_sigs` appears next (the following function/section)."""
    start = js.index(start_sig)
    end = len(js)
    for sig in end_sigs:
        idx = js.find(sig, start + len(start_sig))
        if idx != -1:
            end = min(end, idx)
    return js[start:end]


# ---------------------------------------------------------------------------
# Defect 1 — the choose-traits preview must use the SAME layer stacker as
# the Dressing Room canvas, not a second, divergent one.
# ---------------------------------------------------------------------------


def test_refresh_builder_preview_uses_the_shared_stacker():
    js = _read("app.js")
    body = _fn_body(js, "function refreshBuilderPreview()", "\nfunction renderBuilder()")
    assert "buildPure.orderedLayers(" in body
    # The bug: Body force-prepended ahead of the canonical trait_order.
    assert "[layerMediaEl(layerSrc(cls, 'Body', body), body)]" not in body


def test_render_canvas_uses_the_same_shared_stacker():
    js = _read("app.js")
    body = _fn_body(js, "function renderCanvas(char)", "\n// --- GO picker")
    assert "buildPure.orderedLayers(" in body


def test_builder_preview_orders_by_economy_state_trait_order():
    js = _read("app.js")
    body = _fn_body(js, "function refreshBuilderPreview()", "\nfunction renderBuilder()")
    assert "economyState.trait_order" in body
    assert "Body: body" in body  # Body's value threaded into the shared map


def test_builder_preview_reads_trait_order_unguarded_like_render_canvas():
    """Task review finding on #532: a `(economyState && economyState.trait_order)
    || ['Body', ...opts.slots]` fallback would, if ever exercised, silently
    reintroduce the exact defect-1 bug (Body forced near the front). Every path
    into the builder already awaits /api/economy first, so refreshBuilderPreview
    must read economyState.trait_order unguarded -- the same way renderCanvas
    does -- rather than defend against a state that cannot occur."""
    js = _read("app.js")
    preview_body = _fn_body(js, "function refreshBuilderPreview()", "\nfunction renderBuilder()")
    assert "const order = economyState.trait_order;" in preview_body
    assert "['Body', ...opts.slots]" not in preview_body
    assert "economyState &&" not in preview_body

    canvas_body = _fn_body(js, "function renderCanvas(char)", "\n// --- GO picker")
    assert "const order = economyState.trait_order;" in canvas_body


# ---------------------------------------------------------------------------
# Defect 2 — switching bodies must keep still-valid selections, using the
# server's own per-body legality list (the shared cross-body matrix), and
# tell the user what was dropped.
# ---------------------------------------------------------------------------


def test_body_step_carries_over_selections_instead_of_resetting():
    js = _read("app.js")
    body = _fn_body(js, "function builderBodyStep()", "\nfunction builderTraitStep()")
    assert "buildPure.carryOverChosen(" in body
    # The old bug: an unconditional reset to the body's bare defaults.
    assert (
        "builderState.chosen = buildPure.defaultChosen(opts.slots, opts.options[b] || {});"
        not in body
    )


def test_body_step_reuses_the_servers_body_affinity_options_not_a_new_rule():
    js = _read("app.js")
    body = _fn_body(js, "function builderBodyStep()", "\nfunction builderTraitStep()")
    # opts.options[b] is /api/assemble/options' swap_compose.resolve_layer
    # filtered list — the same cross-body matrix swap/equip enforce.
    assert "opts.options[b] || {}" in body
    assert "buildPure.carryOverChosen(opts.slots, slotOptions, builderState.chosen)" in body


def test_body_step_reports_dropped_selections_via_the_pure_helper():
    js = _read("app.js")
    body = _fn_body(js, "function builderBodyStep()", "\nfunction builderTraitStep()")
    assert "buildPure.dropNoticeText(dropped)" in body
    assert "builderState.dropNotice" in body


def test_stylesheet_has_drop_notice_styling():
    assert ".builder-drop-notice" in _stylesheet()


# ---------------------------------------------------------------------------
# Defect 3 — the blank and body pickers collapse after a choice, and stay
# re-openable with keyboard support / aria-expanded state.
# ---------------------------------------------------------------------------


def test_builder_state_initializes_collapse_and_drop_notice_fields():
    js = _read("app.js")
    body = _fn_body(js, "function openBuilder(opts, preselectNftId)", "\nfunction closeBuilder()")
    assert "dropNotice: null" in body
    assert "blankForceOpen: false" in body
    assert "bodyForceOpen: false" in body


def test_step_header_is_a_real_button_with_aria_expanded():
    js = _read("app.js")
    body = _fn_body(
        js,
        "function builderStepHeader(",
        "\nfunction builderBlankStep()",
    )
    assert "document.createElement('button')" in body
    assert "btn.type = 'button';" in body
    assert "setAttribute('aria-expanded', String(expanded))" in body
    assert "setAttribute('aria-controls', controlsId)" in body


def test_blank_step_collapses_on_choice_and_is_reopenable():
    js = _read("app.js")
    body = _fn_body(js, "function builderBlankStep()", "\nfunction builderBodyStep()")
    assert "buildPure.stepExpanded(blank, blankForceOpen)" in body
    assert "builderState.blankForceOpen = false;" in body  # collapses on pick
    assert "builderState.blankForceOpen = !expanded;" in body  # reopen toggle
    assert "grid.hidden = !expanded;" in body


def test_body_step_collapses_on_choice_and_is_reopenable():
    js = _read("app.js")
    body = _fn_body(js, "function builderBodyStep()", "\nfunction builderTraitStep()")
    assert "buildPure.stepExpanded(body, bodyForceOpen)" in body
    assert "builderState.bodyForceOpen = false;" in body  # collapses on pick
    assert "builderState.bodyForceOpen = !expanded;" in body  # reopen toggle
    assert "grid.hidden = !expanded;" in body


def test_blank_and_body_grids_carry_stable_ids_for_aria_controls():
    js = _read("app.js")
    assert "grid.id = 'builder-blank-grid';" in js
    assert "grid.id = 'builder-body-grid';" in js
    assert "'builder-blank-grid'" in _fn_body(
        js, "function builderBlankStep()", "\nfunction builderBodyStep()"
    )
    assert "'builder-body-grid'" in _fn_body(
        js, "function builderBodyStep()", "\nfunction builderTraitStep()"
    )


def test_stylesheet_has_step_toggle_styling():
    assert ".builder-step-toggle" in _stylesheet()
