# tests/test_build_pure_js.py
# Build (Dressing Room) panel decision logic, kept in the pure module
# webapp/client/build_pure.js and executed here under Node — same harness as
# tests/test_mint_pure_js.py / tests/test_market_pure_js.py.
#
# No lfg_core import at module top -> no env-guard preamble needed.
import json
import os
import re
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULE_REL = "./webapp/client/build_pure.js"

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


def _app_js() -> str:
    with open(os.path.join(ROOT, "webapp", "client", "app.js"), encoding="utf-8") as f:
        return f.read()


def _render_closet_src() -> str:
    """Only the body of renderCloset() — so a `compatible` computed elsewhere in
    the 5k-line file can never satisfy these assertions by accident."""
    src = _app_js()
    start = src.index("function renderCloset() {")
    end = src.index("\nfunction ", start + 1)
    return src[start:end]


# ---------------------------------------------------------------------------
# pickDefaultCharacter(characters) -> nft_id | null
# The Build panel must never default-land on an unindexed token ("#null ·
# still indexing…"): prefer the first character whose metadata has a body.
# ---------------------------------------------------------------------------


def test_default_skips_unindexed_leading_character():
    chars = "[{nft_id: 'A', body: ''}, {nft_id: 'B', body: 'male'}, {nft_id: 'C', body: 'ape'}]"
    assert run_js(f"M.pickDefaultCharacter({chars})") == "B"


def test_default_keeps_first_when_it_is_indexed():
    chars = "[{nft_id: 'A', body: 'milady'}, {nft_id: 'B', body: ''}]"
    assert run_js(f"M.pickDefaultCharacter({chars})") == "A"


def test_default_falls_back_to_first_when_none_indexed():
    chars = "[{nft_id: 'A', body: ''}, {nft_id: 'B', body: null}]"
    assert run_js(f"M.pickDefaultCharacter({chars})") == "A"


def test_default_empty_roster_is_null():
    assert run_js("M.pickDefaultCharacter([])") is None
    assert run_js("M.pickDefaultCharacter(null)") is None


def test_default_prefers_dressed_over_blank():
    # A leading indexed-but-blank character must yield to a later dressed one so
    # the canvas opens on real art, not a bare silhouette.
    chars = (
        "[{nft_id: 'A', body: 'male', blank: true}, {nft_id: 'B', body: 'female', blank: false}]"
    )
    assert run_js(f"M.pickDefaultCharacter({chars})") == "B"


def test_default_falls_back_to_blank_when_none_dressed():
    # Every character is a blank: pick the first indexed one anyway.
    chars = "[{nft_id: 'A', body: '', blank: true}, {nft_id: 'B', body: 'male', blank: true}]"
    assert run_js(f"M.pickDefaultCharacter({chars})") == "B"


def test_default_dressed_preference_ignores_unindexed():
    # An unindexed (no body) character is never dressed-preferred even if blank
    # is false — it would 400 on every layer fetch.
    chars = "[{nft_id: 'A', body: '', blank: false}, {nft_id: 'B', body: 'ape', blank: false}]"
    assert run_js(f"M.pickDefaultCharacter({chars})") == "B"


# ---------------------------------------------------------------------------
# goTileState(char, activeNftId) -> {label, sub, state}
# ---------------------------------------------------------------------------


def test_tile_active():
    out = run_js("M.goTileState({nft_id: 'A', edition: 3521, body: 'male'}, 'A')")
    assert out == {"label": "#3521", "sub": "male", "state": "active"}


def test_tile_selectable():
    out = run_js("M.goTileState({nft_id: 'B', edition: 398, body: 'ape'}, 'A')")
    assert out == {"label": "#398", "sub": "ape", "state": "selectable"}


def test_tile_unindexed_is_disabled_and_labeled():
    out = run_js("M.goTileState({nft_id: 'C', edition: null, body: ''}, 'A')")
    assert out == {"label": "#?", "sub": "indexing…", "state": "indexing"}


def test_tile_blank_is_selectable_despite_empty_body():
    # A harvested blank carries no Body metadata (its index `body` is ''), but
    # it is fully indexed and MUST stay selectable — renderCanvas draws its
    # silhouette image and fetches no layers, so the 'indexing' disable (which
    # exists only because a layer fetch would 400) does not apply to it.
    out = run_js("M.goTileState({nft_id: 'B', edition: 3543, body: '', blank: true}, 'A')")
    assert out == {"label": "#3543", "sub": "Blank \u2014 build me!", "state": "selectable"}


def test_tile_blank_can_be_the_active_character():
    out = run_js("M.goTileState({nft_id: 'A', edition: 65, body: '', blank: true}, 'A')")
    assert out["state"] == "active"


def test_tile_unindexed_active_stays_indexing():
    # An unindexed GO that also happens to be the active character must render
    # 'indexing' (disabled), not 'active' — the picker only disables 'indexing'
    # tiles, so an 'active' unindexed tile would be selectable and 400 on every
    # layer fetch (missing body metadata takes precedence over active state).
    out = run_js("M.goTileState({nft_id: 'A', edition: 3521, body: ''}, 'A')")
    assert out["state"] == "indexing"


# ---------------------------------------------------------------------------
# pickBuilderBlank(blanks, preselectNftId) -> the blank the builder opens on
# ---------------------------------------------------------------------------


def test_builder_blank_preselects_the_requested_one():
    blanks = "[{nft_id: 'A', edition: 1}, {nft_id: 'B', edition: 2}]"
    assert run_js(f"M.pickBuilderBlank({blanks}, 'B')") == {"nft_id": "B", "edition": 2}


def test_builder_blank_auto_selects_a_lone_blank():
    assert run_js("M.pickBuilderBlank([{nft_id: 'A', edition: 1}], null)")["nft_id"] == "A"


def test_builder_blank_is_null_when_several_and_none_requested():
    blanks = "[{nft_id: 'A', edition: 1}, {nft_id: 'B', edition: 2}]"
    assert run_js(f"M.pickBuilderBlank({blanks}, null)") is None


def test_builder_blank_unknown_preselect_falls_back_to_the_normal_rule():
    # A requested blank the server did not return (e.g. a non-mutable legacy
    # character) must not preselect anything — the user picks from step 1.
    blanks = "[{nft_id: 'A', edition: 1}, {nft_id: 'B', edition: 2}]"
    assert run_js(f"M.pickBuilderBlank({blanks}, 'ZZ')") is None
    assert run_js("M.pickBuilderBlank([{nft_id: 'A', edition: 1}], 'ZZ')")["nft_id"] == "A"


# ---------------------------------------------------------------------------
# applyPending(attributes, pending) -> attributes with staged values applied
# ---------------------------------------------------------------------------

ATTRS = (
    "[{trait_type: 'Body', value: 'Straight Blue'},"
    " {trait_type: 'Head', value: 'Crown'},"
    " {trait_type: 'Eyes', value: 'None'}]"
)
CHAR = f"{{nft_id: 'A', body: 'male', attributes: {ATTRS}}}"


def test_apply_pending_overrides_only_staged_slots():
    out = run_js(f"M.applyPending({ATTRS}, {{Head: 'Tiara'}})")
    assert out == [
        {"trait_type": "Body", "value": "Straight Blue"},
        {"trait_type": "Head", "value": "Tiara"},
        {"trait_type": "Eyes", "value": "None"},
    ]


def test_apply_pending_empty_is_identity():
    out = run_js(f"M.applyPending({ATTRS}, {{}})")
    assert out[1] == {"trait_type": "Head", "value": "Crown"}


def test_apply_pending_ignores_slots_the_character_lacks():
    out = run_js(f"M.applyPending({ATTRS}, {{Wings: 'Angel'}})")
    assert len(out) == 3 and all(a["trait_type"] != "Wings" for a in out)


# ---------------------------------------------------------------------------
# effectiveAssets(assets, character, pending) -> optimistic Closet counts
# ---------------------------------------------------------------------------


def test_effective_assets_decrements_the_staged_incoming():
    assets = "[{slot: 'Head', value: 'Tiara', count: 2}]"
    out = run_js(f"M.effectiveAssets({assets}, {CHAR}, {{Head: 'Tiara'}})")
    # Tiara -1; Crown (displaced off the character) appears
    assert {"slot": "Head", "value": "Tiara", "count": 1} in out
    assert {"slot": "Head", "value": "Crown", "count": 1} in out


def test_effective_assets_drops_entries_reaching_zero():
    assets = "[{slot: 'Head', value: 'Tiara', count: 1}]"
    out = run_js(f"M.effectiveAssets({assets}, {CHAR}, {{Head: 'Tiara'}})")
    assert all(a["value"] != "Tiara" for a in out)
    assert out == [{"slot": "Head", "value": "Crown", "count": 1}]


def test_effective_assets_never_materializes_none():
    # Eyes currently holds 'None'; staging Laser must not create an Eyes/None tile
    assets = "[{slot: 'Eyes', value: 'Laser', count: 1}]"
    out = run_js(f"M.effectiveAssets({assets}, {CHAR}, {{Eyes: 'Laser'}})")
    assert out == []


def test_effective_assets_merges_displaced_into_an_existing_stack():
    assets = "[{slot: 'Head', value: 'Tiara', count: 1}, {slot: 'Head', value: 'Crown', count: 2}]"
    out = run_js(f"M.effectiveAssets({assets}, {CHAR}, {{Head: 'Tiara'}})")
    assert {"slot": "Head", "value": "Crown", "count": 3} in out


def test_effective_assets_no_pending_is_identity():
    assets = "[{slot: 'Head', value: 'Tiara', count: 2}]"
    out = run_js(f"M.effectiveAssets({assets}, {CHAR}, {{}})")
    assert out == [{"slot": "Head", "value": "Tiara", "count": 2}]


def test_effective_assets_without_a_character_is_identity():
    assets = "[{slot: 'Head', value: 'Tiara', count: 2}]"
    out = run_js(f"M.effectiveAssets({assets}, null, {{Head: 'Tiara'}})")
    assert out == [{"slot": "Head", "value": "Tiara", "count": 2}]


# ---------------------------------------------------------------------------
# netChanges(character, pending) -> the POST payload
# ---------------------------------------------------------------------------


def test_net_changes_lists_staged_slots():
    out = run_js(f"M.netChanges({CHAR}, {{Head: 'Tiara', Eyes: 'Laser'}})")
    assert sorted(out, key=lambda c: c["slot"]) == [
        {"slot": "Eyes", "value": "Laser"},
        {"slot": "Head", "value": "Tiara"},
    ]


def test_net_changes_drops_a_slot_staged_back_to_its_current_value():
    # Re-clicking the character's own Crown undoes the stage -> empty batch
    out = run_js(f"M.netChanges({CHAR}, {{Head: 'Crown'}})")
    assert out == []


def test_net_changes_empty_pending_is_empty():
    assert run_js(f"M.netChanges({CHAR}, {{}})") == []


def test_net_changes_without_a_character_is_empty():
    assert run_js("M.netChanges(null, {Head: 'Tiara'})") == []


# A SELECTED BLANK and the equip path (#523). PR #409 assumed this module
# already stopped a blank from being dressed, on the premise that a blank has no
# body. It does not, and that premise is false: a blank's metadata parses fine,
# and swap_meta.detect_body() has no "no body" answer — Body="None" matches none
# of its branches and falls through to "skeleton" — so `body` is a truthy body
# CLASS, never ''. An empty `body` means UNREADABLE metadata, which is_blank
# rejects outright. These two tests pin what follows from that, and it is why
# the real guard has to sit at the staging gate in renderCloset() (asserted at
# the bottom of this file): the tiles render, and a staged trait IS a change.
BLANK = (
    "{nft_id: 'A', body: 'skeleton', blank: true, attributes: "
    "[{trait_type: 'Head', value: 'None'}, {trait_type: 'Eyes', value: 'None'}]}"
)


def test_blank_character_still_shows_real_closet_tiles():
    # And it must: the tile is the host for Extract, and the trait strip's chip
    # is the host for Deposit — hiding them would strand a blank-only owner with
    # no way to move traits at all.
    out = run_js(f"M.closetTileState({{slot: 'Head', value: 'Camp Hat'}}, {BLANK})")
    assert out == {"visible": True, "art": "layer", "label": ""}


def test_blank_character_staged_trait_is_a_real_net_change():
    # Every slot of a blank reads "None", so a staged trait always differs from
    # what it wears: the Save bar would show and POST /api/equip would fire.
    # Nothing downstream of staging stops this — only the renderCloset() gate.
    out = run_js(f"M.netChanges({BLANK}, {{Head: 'Camp Hat'}})")
    assert out == [{"slot": "Head", "value": "Camp Hat"}]


# ---------------------------------------------------------------------------
# closetTileState(asset, char) -> {visible, art, label}
# A harvested character deposits one asset per non-body slot, INCLUDING the
# literal "None" of an empty slot (trait_economy conserves it). Those tiles
# must stay visible with a GO selected — they were being dropped, so a
# harvested "None" Back simply vanished from the Closet.
# ---------------------------------------------------------------------------


def test_closet_tile_none_stays_visible_with_a_go_selected():
    out = run_js("M.closetTileState({slot: 'Back', value: 'None', count: 3}, {body: 'male'})")
    assert out["visible"] is True
    assert out["art"] == "blank"
    assert out["label"] == "None"


def test_closet_tile_none_visible_without_a_go():
    out = run_js("M.closetTileState({slot: 'Back', value: 'None', count: 1}, null)")
    assert out["visible"] is True
    assert out["art"] == "blank"


def test_closet_tile_real_asset_renders_layer_art():
    out = run_js("M.closetTileState({slot: 'Head', value: 'Camp Hat', count: 1}, {body: 'male'})")
    assert out == {"visible": True, "art": "layer", "label": ""}


def test_closet_tile_real_asset_without_a_go_is_a_blank_placeholder():
    out = run_js("M.closetTileState({slot: 'Head', value: 'Camp Hat', count: 1}, null)")
    assert out == {"visible": True, "art": "blank", "label": ""}


def test_closet_tile_hidden_for_unindexed_character():
    # No body metadata: a layer fetch would 400, so the tile is dropped
    # (unchanged behavior) — but a "None" asset is still never dropped.
    hidden = run_js("M.closetTileState({slot: 'Head', value: 'Camp Hat'}, {body: ''})")
    assert hidden["visible"] is False
    assert run_js("M.closetTileState({slot: 'Head', value: 'None'}, {body: ''})")["visible"] is True


# defaultChosen(slots, slotOptions) -> {slot: firstValue, ...}
# First legal value per slot from an options map — mirrors the server's old
# first-match prefill so one-tap assemble still works.
# ---------------------------------------------------------------------------


def test_default_chosen_picks_first_value_per_slot():
    slots = "['Hat', 'Eyes', 'Mouth']"
    slotOptions = (
        "{'Hat': ['Wizard Hat', 'Baseball Cap'], 'Eyes': ['Blue', 'Green'], 'Mouth': ['Smile']}"
    )
    result = run_js(f"M.defaultChosen({slots}, {slotOptions})")
    assert result == {"Hat": "Wizard Hat", "Eyes": "Blue", "Mouth": "Smile"}


def test_default_chosen_skips_empty_slots():
    slots = "['Hat', 'Eyes', 'Mouth']"
    slotOptions = "{'Hat': ['Wizard Hat'], 'Eyes': [], 'Mouth': []}"
    result = run_js(f"M.defaultChosen({slots}, {slotOptions})")
    assert result == {"Hat": "Wizard Hat"}


def test_default_chosen_empty_slots_list():
    slots = "[]"
    slotOptions = "{}"
    result = run_js(f"M.defaultChosen({slots}, {slotOptions})")
    assert result == {}


def test_default_chosen_all_empty_options():
    slots = "['Hat', 'Eyes']"
    slotOptions = "{'Hat': [], 'Eyes': []}"
    result = run_js(f"M.defaultChosen({slots}, {slotOptions})")
    assert result == {}


def test_default_chosen_missing_slot_in_options():
    slots = "['Hat', 'Eyes', 'Unknown']"
    slotOptions = "{'Hat': ['Wizard Hat'], 'Eyes': ['Blue']}"
    result = run_js(f"M.defaultChosen({slots}, {slotOptions})")
    assert result == {"Hat": "Wizard Hat", "Eyes": "Blue"}


# ---------------------------------------------------------------------------
# missingSlots(slots, slotOptions) -> [slot, ...]
# Slots with no legal closet asset for this body (blocks the commit button).
# ---------------------------------------------------------------------------


def test_missing_slots_returns_empty_when_all_have_options():
    slots = "['Hat', 'Eyes']"
    slotOptions = "{'Hat': ['Wizard Hat'], 'Eyes': ['Blue']}"
    result = run_js(f"M.missingSlots({slots}, {slotOptions})")
    assert result == []


def test_missing_slots_returns_empty_slots():
    slots = "['Hat', 'Eyes', 'Mouth']"
    slotOptions = "{'Hat': ['Wizard Hat'], 'Eyes': [], 'Mouth': []}"
    result = run_js(f"M.missingSlots({slots}, {slotOptions})")
    assert result == ["Eyes", "Mouth"]


def test_missing_slots_empty_slots_list():
    slots = "[]"
    slotOptions = "{}"
    result = run_js(f"M.missingSlots({slots}, {slotOptions})")
    assert result == []


def test_missing_slots_missing_slot_in_options():
    slots = "['Hat', 'Eyes', 'Unknown']"
    slotOptions = "{'Hat': ['Wizard Hat']}"
    result = run_js(f"M.missingSlots({slots}, {slotOptions})")
    assert result == ["Eyes", "Unknown"]


def test_missing_slots_all_empty_options():
    slots = "['Hat', 'Eyes']"
    slotOptions = "{'Hat': [], 'Eyes': []}"
    result = run_js(f"M.missingSlots({slots}, {slotOptions})")
    assert result == ["Hat", "Eyes"]


# ---------------------------------------------------------------------------
# saveOutcome(finalSession) -> "committed" | "reverted" | "uncertain"  (#316)
# ---------------------------------------------------------------------------


def test_save_outcome_uncertain():
    assert run_js('M.saveOutcome({state:"failed", resolution:"uncertain"})') == "uncertain"


def test_save_outcome_reverted_on_null_resolution():
    assert run_js('M.saveOutcome({state:"failed", resolution:null})') == "reverted"


def test_save_outcome_reverted_on_unknown_resolution():
    assert run_js('M.saveOutcome({state:"failed", resolution:"bogus"})') == "reverted"


def test_save_outcome_reverted_on_explicit_reverted():
    assert run_js('M.saveOutcome({state:"failed", resolution:"reverted"})') == "reverted"


def test_save_outcome_committed_on_done():
    assert run_js('M.saveOutcome({state:"done"})') == "committed"


def test_save_outcome_committed_on_resolution_committed():
    assert run_js('M.saveOutcome({state:"done", resolution:"committed"})') == "committed"


def test_save_outcome_missing_session_is_reverted():
    assert run_js("M.saveOutcome(null)") == "reverted"


# ---------------------------------------------------------------------------
# renderCloset() equip gating for a BLANK character (#523)
# The Closet grid is the ONLY door to stagePendingEquip(), so its `compatible`
# check is where a blank has to be stopped client-side. It must exclude blanks
# explicitly: a blank is NOT distinguishable by `body` (see the blank-tile
# tests above), so a slot-membership test alone lets a real trait be staged and
# saved onto a blank — the interaction behind the 6 mainnet "phantom None set"
# drift events of 2026-08-07..09-11.
# ---------------------------------------------------------------------------


def test_equip_compatible_allows_a_dressed_character_and_a_known_slot():
    assert run_js(f"M.equipCompatible({CHAR}, {{slot: 'Head', value: 'Tiara'}}, ['Head', 'Eyes'])")


def test_equip_compatible_refuses_a_blank_character():
    # The whole point (#523). An INVERTED guard (`char.blank` instead of
    # `!char.blank`) fails right here, which a source-text assertion could not
    # catch — it would still "mention char.blank".
    out = run_js(f"M.equipCompatible({BLANK}, {{slot: 'Head', value: 'Camp Hat'}}, ['Head'])")
    assert out is False


def test_equip_compatible_refuses_without_a_character():
    assert run_js("M.equipCompatible(null, {slot: 'Head'}, ['Head'])") is False


def test_equip_compatible_refuses_a_slot_the_character_has_no_room_for():
    assert run_js(f"M.equipCompatible({CHAR}, {{slot: 'Wings'}}, ['Head', 'Eyes'])") is False


def test_equip_compatible_refuses_a_missing_slot_list():
    assert run_js(f"M.equipCompatible({CHAR}, {{slot: 'Head'}}, null)") is False


def test_closet_tile_equip_delegates_to_equip_compatible():
    # renderCloset() must not re-derive the predicate inline — the Node tests
    # above are only worth something while the grid actually calls it.
    body = _render_closet_src()
    assert "buildPure.equipCompatible(char, asset, economyState.slots)" in body
    # the compatible arm is a block since #533 (click and keyboard activation
    # share it), so pin the wiring inside that branch rather than the one-liner
    wiring = body[body.index("if (compatible)") :]
    assert "item.onclick = () => stagePendingEquip(" in wiring


def test_app_js_build_pure_import_is_bumped_past_the_equip_compatible_export():
    """A module and its caller must move in lockstep, but browser caches evict
    them independently. equipCompatible was ADDED at v30: a browser holding the
    old build_pure.js?v=29 alongside a fresh app.js would get a module without
    that export, and renderCloset()'s call would TypeError and leave the Closet
    unrendered. A floor (not an equality) so later bumps don't fight this test.
    """
    m = re.search(r"build_pure\.js\?v=(\d+)", _app_js())
    assert m, "app.js no longer imports build_pure.js with a cache key"
    assert int(m.group(1)) >= 30, (
        "build_pure.js?v= regressed below the version that first exported "
        "equipCompatible — a cached older module breaks the Closet"
    )

# ---------------------------------------------------------------------------
# Closet tile keyboard activation.
#
# The tile is a <div role="button" tabindex="0"> — it deliberately cannot be a
# real <button> because it wraps the nested Extract <button>. A div with a
# button role gets NO free Enter/Space activation (only native <button>/<a>
# do), and app.js has no global Enter->click delegation: every keydown listener
# in the client closes an overlay on Escape. So the tile announced itself as a
# button that a keyboard or screen-reader user could focus but never activate.
#
# isActivationKey(key)          -> is this the keyboard equivalent of a click?
# closetTileA11y(compatible)    -> {tabIndex, ariaDisabled} for the tile
#
# The second one covers the state half of the same bug: an incompatible tile
# (no GO selected, or an asset whose slot this collection doesn't have) wires
# no handler at all, yet still claimed tabindex="0" and a bare button role —
# a focus stop that does nothing and announces nothing about why.
# ---------------------------------------------------------------------------


def test_activation_key_accepts_enter():
    assert run_js("M.isActivationKey('Enter')") is True


def test_activation_key_accepts_space():
    assert run_js("M.isActivationKey(' ')") is True


def test_activation_key_rejects_other_keys():
    for key in ("Escape", "Tab", "ArrowRight", "a", "Shift"):
        assert run_js(f"M.isActivationKey({json.dumps(key)})") is False, key


def test_activation_key_rejects_missing_key():
    assert run_js("M.isActivationKey(undefined)") is False


def test_closet_tile_a11y_compatible_is_tabbable_and_enabled():
    assert run_js("M.closetTileA11y(true)") == {"tabIndex": 0, "ariaDisabled": False}


def test_closet_tile_a11y_incompatible_is_disabled_and_skipped_by_tab():
    # Still reachable by a screen reader's virtual cursor (it keeps role=button
    # + aria-disabled), just not a Tab stop — and the nested Extract button,
    # which works regardless of equip compatibility, stays focusable.
    assert run_js("M.closetTileA11y(false)") == {"tabIndex": -1, "ariaDisabled": True}


def test_closet_tile_a11y_treats_a_missing_character_as_incompatible():
    # `compatible` is `char && slots.includes(slot)` — a falsy char makes it
    # null, not false. That is the live case: with no GO selected EVERY tile
    # renders incompatible.
    assert run_js("M.closetTileA11y(null)") == {"tabIndex": -1, "ariaDisabled": True}


# ---------------------------------------------------------------------------
# renderCloset() wiring in app.js (source assertions — the DOM code itself is
# not unit-testable here, so the decisions above are pure and this pins the
# call sites that consume them).
# ---------------------------------------------------------------------------


def _closet_keydown_src():
    src = _render_closet_src()
    return src[src.index("item.onkeydown") :][:600]


def test_closet_tile_activates_on_enter_or_space():
    handler = _closet_keydown_src()
    assert "buildPure.isActivationKey(e.key)" in handler
    assert "stagePendingEquip(asset.slot, asset.value)" in handler


def test_closet_tile_space_does_not_scroll_the_panel():
    # Space on a focused element scrolls by default; a real <button> suppresses
    # it, a div must do so itself.
    assert "e.preventDefault()" in _closet_keydown_src()


def test_closet_tile_keydown_ignores_the_nested_extract_button():
    # Enter on the focused Extract <button> fires its click AND bubbles a
    # keydown to the tile. The click is stopped by extractBtn's
    # stopPropagation, the keydown is not — without this guard one Enter would
    # both extract the trait and stage an equip.
    assert "e.target !== item" in _closet_keydown_src()


def test_closet_tile_keyboard_activation_is_wired_only_when_compatible():
    src = _render_closet_src()
    wiring = src[src.index("if (compatible)") :][:600]
    assert "item.onclick" in wiring
    assert "item.onkeydown" in wiring


def test_incompatible_closet_tile_is_announced_disabled():
    src = _render_closet_src()
    assert "buildPure.closetTileA11y(compatible)" in src
    assert "'aria-disabled', 'true'" in src
    # the unconditional tab stop is what made an inert tile focusable
    assert "item.tabIndex = 0;" not in src
