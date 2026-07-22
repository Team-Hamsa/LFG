# tests/test_build_pure_js.py
# Build (Dressing Room) panel decision logic, kept in the pure module
# webapp/client/build_pure.js and executed here under Node — same harness as
# tests/test_mint_pure_js.py / tests/test_market_pure_js.py.
#
# No lfg_core import at module top -> no env-guard preamble needed.
import json
import os
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


def test_tile_unindexed_active_stays_indexing():
    # An unindexed GO that also happens to be the active character must render
    # 'indexing' (disabled), not 'active' — the picker only disables 'indexing'
    # tiles, so an 'active' unindexed tile would be selectable and 400 on every
    # layer fetch (missing body metadata takes precedence over active state).
    out = run_js("M.goTileState({nft_id: 'A', edition: 3521, body: ''}, 'A')")
    assert out["state"] == "indexing"


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
