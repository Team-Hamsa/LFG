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
