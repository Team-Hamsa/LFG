"""Tests for scripts/audit_layer_dimensions.py (the 1080x1080 layer guardrail)."""

import os
import subprocess
import sys

import pytest
from PIL import Image

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))

import audit_layer_dimensions as ald  # noqa: E402


def _write_png(path, size):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    Image.new("RGBA", size, (0, 0, 0, 0)).save(path)


def _write_gif(path, size):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    frames = [Image.new("RGBA", size, (0, 0, 0, 0)) for _ in range(2)]
    frames[0].save(path, save_all=True, append_images=frames[1:], duration=50, loop=0)


def test_clean_tree_passes(tmp_path):
    layers = tmp_path / "layers"
    _write_png(str(layers / "male" / "Body" / "Straight.png"), (1080, 1080))
    _write_gif(str(layers / "male" / "Body" / "Straight Diamond.gif"), (1080, 1080))
    assert ald.scan(str(layers)) == []


def test_undersized_gif_is_flagged(tmp_path):
    layers = tmp_path / "layers"
    _write_gif(str(layers / "female" / "Body" / "Curved Diamond.gif"), (600, 600))
    offenders = ald.scan(str(layers))
    assert len(offenders) == 1
    rel, problem = offenders[0]
    assert rel == os.path.join("female", "Body", "Curved Diamond.gif")
    assert "600x600" in problem


def test_undersized_png_flagged_unless_skipped(tmp_path):
    layers = tmp_path / "layers"
    _write_png(str(layers / "ape" / "Nose.png"), (500, 500))
    assert len(ald.scan(str(layers))) == 1
    assert ald.scan(str(layers), include_png=False) == []


def test_corrupt_file_is_flagged(tmp_path):
    layers = tmp_path / "layers"
    os.makedirs(layers / "male" / "Body")
    (layers / "male" / "Body" / "Broken.gif").write_bytes(b"not a gif")
    offenders = ald.scan(str(layers))
    assert len(offenders) == 1
    assert "unreadable" in offenders[0][1]


def test_truncated_but_identifiable_png_is_flagged(tmp_path):
    layers = tmp_path / "layers"
    good = layers / "male" / "Body" / "Whole.png"
    _write_png(str(good), (1080, 1080))
    data = good.read_bytes()
    good.unlink()
    # Keep the header (so Image.open succeeds and reports 1080x1080) but cut
    # the payload — only a full decode catches this.
    (layers / "male" / "Body" / "Truncated.png").write_bytes(data[: len(data) // 2])
    offenders = ald.scan(str(layers))
    assert len(offenders) == 1
    assert "unreadable" in offenders[0][1]


def test_cache_skips_unchanged_and_detects_changes(tmp_path, monkeypatch):
    layers = tmp_path / "layers"
    png = layers / "male" / "Body" / "Straight.png"
    _write_png(str(png), (1080, 1080))
    cache = str(tmp_path / "cache.json")
    assert ald.scan(str(layers), cache_path=cache) == []

    # A cached-clean file must not be re-probed...
    def boom(_path):
        raise AssertionError("probed a cached file")

    monkeypatch.setattr(ald, "read_dimensions", boom)
    assert ald.scan(str(layers), cache_path=cache) == []

    # ...but a modified file must be, and a cached FAILURE must persist.
    monkeypatch.undo()
    _write_png(str(png), (600, 600))
    os.utime(png, ns=(1, 1))  # force a distinct stamp either way
    assert len(ald.scan(str(layers), cache_path=cache)) == 1
    assert len(ald.scan(str(layers), cache_path=cache)) == 1


def test_non_layer_extensions_ignored(tmp_path):
    layers = tmp_path / "layers"
    os.makedirs(layers)
    (layers / "seasons.json").write_text("{}")
    assert ald.scan(str(layers)) == []


@pytest.mark.skipif(
    subprocess.run(["which", "ffprobe"], capture_output=True).returncode != 0
    or subprocess.run(["which", "ffmpeg"], capture_output=True).returncode != 0,
    reason="ffprobe or ffmpeg not on PATH",
)
def test_undersized_mp4_is_flagged(tmp_path):
    layers = tmp_path / "layers"
    os.makedirs(layers / "male" / "Body")
    mp4 = layers / "male" / "Body" / "Anim.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "quiet",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=600x600:d=0.2",
            "-pix_fmt",
            "yuv420p",
            str(mp4),
        ],
        check=True,
    )
    offenders = ald.scan(str(layers))
    assert len(offenders) == 1
    assert "600x600" in offenders[0][1]


def test_missing_layers_dir_exits_zero(tmp_path):
    proc = subprocess.run(
        [
            sys.executable,
            os.path.join(REPO_ROOT, "scripts", "audit_layer_dimensions.py"),
            "--layers-dir",
            str(tmp_path / "nope"),
        ],
        capture_output=True,
    )
    assert proc.returncode == 0


def test_cli_fails_on_offender(tmp_path):
    layers = tmp_path / "layers"
    _write_gif(str(layers / "male" / "Body" / "Small.gif"), (600, 600))
    proc = subprocess.run(
        [
            sys.executable,
            os.path.join(REPO_ROOT, "scripts", "audit_layer_dimensions.py"),
            "--layers-dir",
            str(layers),
        ],
        capture_output=True,
    )
    assert proc.returncode == 1
    assert b"Small.gif" in proc.stderr
