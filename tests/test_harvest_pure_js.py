# tests/test_harvest_pure_js.py
# Batch-harvest (#356) selection + summary decision logic, kept in the pure
# module webapp/client/harvest_pure.js and executed here under Node — same
# harness as tests/test_mint_pure_js.py / tests/test_build_pure_js.py.
#
# No lfg_core import at module top -> no env-guard preamble needed.
import json
import os
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULE_REL = "./webapp/client/harvest_pure.js"

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
# toggleSelected(selectedIds, nftId) -> new array (pure; never mutates input)
# ---------------------------------------------------------------------------


def test_toggle_adds_when_absent():
    assert run_js("M.toggleSelected(['A'], 'B')") == ["A", "B"]


def test_toggle_removes_when_present():
    assert run_js("M.toggleSelected(['A', 'B'], 'A')") == ["B"]


def test_toggle_from_empty():
    assert run_js("M.toggleSelected([], 'A')") == ["A"]


def test_toggle_does_not_mutate_input():
    out = run_js("(() => { const s = ['A']; M.toggleSelected(s, 'B'); return s; })()")
    assert out == ["A"]


def test_toggle_null_selected_treated_as_empty():
    assert run_js("M.toggleSelected(null, 'A')") == ["A"]


# ---------------------------------------------------------------------------
# harvestSelectable(char, harvestingIds) -> bool
# A tile is batch-selectable only when the single-harvest button would be:
# indexed (has body metadata), not already blank, not already being harvested.
# ---------------------------------------------------------------------------


def test_selectable_dressed_indexed_character():
    assert run_js("M.harvestSelectable({nft_id:'A', body:'male', blank:false}, [])") is True


def test_blank_is_not_selectable():
    assert run_js("M.harvestSelectable({nft_id:'A', body:'male', blank:true}, [])") is False


def test_unindexed_is_not_selectable():
    assert run_js("M.harvestSelectable({nft_id:'A', body:'', blank:false}, [])") is False


def test_inflight_harvest_is_not_selectable():
    assert run_js("M.harvestSelectable({nft_id:'A', body:'male', blank:false}, ['A'])") is False


def test_null_char_is_not_selectable():
    assert run_js("M.harvestSelectable(null, [])") is False


# ---------------------------------------------------------------------------
# batchSummary(characters, selectedIds) -> {count, mutable, legacy}
# legacy (non-mutable) units each cost one Xaman accept; mutable units are free.
# ---------------------------------------------------------------------------

CHARS = (
    "[{nft_id:'A', edition:1, mutable:true},"
    " {nft_id:'B', edition:2, mutable:false},"
    " {nft_id:'C', edition:3, mutable:true}]"
)


def test_summary_counts_mutable_and_legacy():
    out = run_js(f"M.batchSummary({CHARS}, ['A','B','C'])")
    assert out == {"count": 3, "mutable": 2, "legacy": 1}


def test_summary_ignores_ids_not_in_roster():
    out = run_js(f"M.batchSummary({CHARS}, ['A','ZZZ'])")
    assert out == {"count": 1, "mutable": 1, "legacy": 0}


def test_summary_empty_selection():
    assert run_js(f"M.batchSummary({CHARS}, [])") == {"count": 0, "mutable": 0, "legacy": 0}


# ---------------------------------------------------------------------------
# confirmText(summary) -> the one-confirm dialog copy
# ---------------------------------------------------------------------------


def test_confirm_text_all_mutable_mentions_no_taps():
    out = run_js("M.confirmText({count:3, mutable:3, legacy:0})")
    assert "3" in out
    assert "no Xaman" in out


def test_confirm_text_with_legacy_mentions_tap_count():
    out = run_js("M.confirmText({count:3, mutable:1, legacy:2})")
    assert "2" in out
    assert "Xaman accept" in out


def test_confirm_text_single_character_is_singular():
    out = run_js("M.confirmText({count:1, mutable:1, legacy:0})")
    assert "1 character" in out
    assert "characters" not in out


# ---------------------------------------------------------------------------
# splitBatchResults(results) -> {started, rejected}
# Per-character server results: a rejected unit carries its error; a started
# one carries a pollable session_id. A mid-batch rejection must not hide the
# started ones (issue #356: never strand the rest).
# ---------------------------------------------------------------------------

RESULTS = (
    "[{nft_id:'A', session_id:'s1', state:'running', error:null},"
    " {nft_id:'B', session_id:null, state:'failed', error:'cannot harvest: nope'},"
    " {nft_id:'C', session_id:'s2', state:'running', error:null}]"
)


def test_split_results_partitions_started_and_rejected():
    out = run_js(f"M.splitBatchResults({RESULTS})")
    assert [r["nft_id"] for r in out["started"]] == ["A", "C"]
    assert out["rejected"] == [{"nft_id": "B", "error": "cannot harvest: nope"}]


def test_split_results_empty():
    assert run_js("M.splitBatchResults([])") == {"started": [], "rejected": []}


def test_split_results_null_is_empty():
    assert run_js("M.splitBatchResults(null)") == {"started": [], "rejected": []}


def test_split_results_missing_session_id_is_rejected():
    out = run_js("M.splitBatchResults([{nft_id:'A', error:null}])")
    assert out["started"] == []
    assert out["rejected"][0]["nft_id"] == "A"


# ---------------------------------------------------------------------------
# wiring: app.js must import the module (with a cache-buster) and index.html
# must serve the bumped app.js (#356 ships client changes -> ?v=50)
# ---------------------------------------------------------------------------


def test_app_js_imports_harvest_pure():
    src = open(os.path.join(ROOT, "webapp", "client", "app.js")).read()
    assert "./harvest_pure.js" in src


def test_index_html_cache_buster_bumped():
    src = open(os.path.join(ROOT, "webapp", "client", "index.html")).read()
    assert "app.js?v=50" in src
