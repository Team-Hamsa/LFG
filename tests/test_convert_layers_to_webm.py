# tests/test_convert_layers_to_webm.py
# Source-discovery tests for scripts/convert_layers_to_webm.py. Pure path
# logic — no ffmpeg/ffprobe is invoked, so these run anywhere the suite does.

import importlib.util
import os

SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "scripts",
    "convert_layers_to_webm.py",
)
_spec = importlib.util.spec_from_file_location("convert_layers_to_webm", SCRIPT)
assert _spec is not None and _spec.loader is not None
converter = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(converter)


def _touch(path: str) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(b"")
    return path


def test_find_sources_picks_up_animated_layers(tmp_path):
    root = str(tmp_path)
    _touch(os.path.join(root, "male", "Accessory", "Smoke.gif"))
    _touch(os.path.join(root, "shared", "Background", "Glitch.mp4"))
    _touch(os.path.join(root, "male", "Body", "Plain.png"))

    found = {os.path.relpath(p, root) for p in converter.find_sources(root, None)}

    assert found == {
        os.path.join("male", "Accessory", "Smoke.gif"),
        os.path.join("shared", "Background", "Glitch.mp4"),
    }


def test_find_sources_skips_hidden_dirs(tmp_path):
    """The .thumbs/ tier is a derived 512px GIF-only mirror (layer_thumbs).

    Converting it would upscale previews to the 1080 layer canvas and write
    WebM into a tier that is deliberately GIF-only so it renders in <img>.
    It regenerates from the real sources via make_layer_thumbs.py.
    """
    root = str(tmp_path)
    _touch(os.path.join(root, "male", "Accessory", "Smoke.gif"))
    _touch(os.path.join(root, ".thumbs", "male", "Accessory", "Smoke.gif"))
    _touch(os.path.join(root, ".thumbs", "shared", "Background", "Glitch.mp4"))

    found = {os.path.relpath(p, root) for p in converter.find_sources(root, None)}

    assert found == {os.path.join("male", "Accessory", "Smoke.gif")}


def test_only_filter_matches_filename_substring(tmp_path):
    root = str(tmp_path)
    _touch(os.path.join(root, "male", "Accessory", "Smoke.gif"))
    _touch(os.path.join(root, "male", "Accessory", "Halo Shine.gif"))

    found = {os.path.relpath(p, root) for p in converter.find_sources(root, "halo")}

    assert found == {os.path.join("male", "Accessory", "Halo Shine.gif")}
