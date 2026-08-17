# tests/test_media_pure_js.py
# Animated-art grid/detail decision logic (#298), kept in the pure module
# webapp/client/media_pure.js and executed here under Node — same harness as
# tests/test_build_pure_js.py / tests/test_market_pure_js.py.
#
# No lfg_core import at module top -> no env-guard preamble needed.
import json
import os
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULE_REL = "./webapp/client/media_pure.js"

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


# --- isAnimated: does this row's art play as video? -------------------------


def test_is_animated_true_on_video():
    assert run_js('M.isAnimated({image: "a.png", video: "a.mp4"})') is True


def test_is_animated_accepts_video_url_alias():
    # Economy characters carry video_url (economy_api), market rows carry video.
    assert run_js('M.isAnimated({image_url: "a.png", video_url: "a.mp4"})') is True


def test_is_animated_false_without_video():
    assert run_js('M.isAnimated({image: "a.png"})') is False
    assert run_js('M.isAnimated({image: "a.png", video: null})') is False
    assert run_js('M.isAnimated({image: "a.png", video: ""})') is False
    assert run_js("M.isAnimated(null)") is False


# --- gridMedia: dense grid tiles are ALWAYS the static image ----------------


def test_grid_media_is_static_even_when_animated():
    out = run_js('M.gridMedia({image: "a.png", video: "a.mp4"})')
    assert out == {"image": "a.png", "animated": True}


def test_grid_media_static_row():
    out = run_js('M.gridMedia({image: "a.png"})')
    assert out == {"image": "a.png", "animated": False}


def test_grid_media_alias_fields():
    out = run_js('M.gridMedia({image_url: "b.png", video_url: "b.mp4"})')
    assert out == {"image": "b.png", "animated": True}


def test_grid_media_null_row():
    out = run_js("M.gridMedia(null)")
    assert out == {"image": None, "animated": False}


# --- detailMedia: the focused/detail view upgrades to video lazily ----------


def test_detail_media_upgrades_to_video():
    out = run_js('M.detailMedia({image: "a.png", video: "a.mp4"})')
    assert out == {"image": "a.png", "video": "a.mp4"}


def test_detail_media_static_stays_image_only():
    out = run_js('M.detailMedia({image: "a.png"})')
    assert out == {"image": "a.png", "video": None}


def test_detail_media_respects_can_play_false():
    # A webview that can't decode the container never gets a dead <video>.
    out = run_js('M.detailMedia({image: "a.png", video: "a.mp4"}, false)')
    assert out == {"image": "a.png", "video": None}


def test_detail_media_alias_fields():
    out = run_js('M.detailMedia({image_url: "b.png", video_url: "b.mp4"})')
    assert out == {"image": "b.png", "video": "b.mp4"}


# --- videoFallback: what a failed <video> degrades to -----------------------
# The inputs are the element's CURRENT poster/label — setMedia reuses one
# fixed-id element across renders, so the fallback must reflect the render
# in effect at error time, never the creation-time closure values.


def test_video_fallback_uses_current_poster():
    # Two renders on one slot, then an error: the SECOND render's poster wins.
    out = run_js(
        "(() => {"
        "  let el = { poster: 'first.png', label: 'first' };"  # render 1
        "  el = { poster: 'second.png', label: 'second' };"  # render 2 (same slot)
        "  return M.videoFallback(el.poster, el.label);"  # error fires now
        "})()"
    )
    assert out == {"src": "second.png", "alt": "second"}


def test_video_fallback_without_poster_is_null():
    # No still to degrade to: keep the video element rather than a broken img.
    assert run_js("M.videoFallback('', 'x')") is None
    assert run_js("M.videoFallback(null, 'x')") is None


def test_video_fallback_missing_label_defaults_empty():
    assert run_js("M.videoFallback('a.png', null)") == {"src": "a.png", "alt": ""}
