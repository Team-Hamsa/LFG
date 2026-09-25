# tests/test_make_layer_thumbs.py
# The PNG thumb converter must keep transparency. Palette PNGs carry it in a
# tRNS chunk, which ffmpeg's scale filter dropped: 35 of prod's 36 palette
# layers (the ape Mask, 28 ape Eyebrows, 4 ape Eyes...) got OPAQUE thumbs, so
# every client preview drew them as black squares over the character.
import importlib.util
import os
import shutil

import pytest
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def _script():
    spec = importlib.util.spec_from_file_location(
        "make_layer_thumbs", os.path.join(ROOT, "scripts", "make_layer_thumbs.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _alpha_range(path):
    with Image.open(path) as im:
        return im.convert("RGBA").getchannel("A").getextrema()


def test_palette_png_with_trns_keeps_its_transparency(tmp_path):
    src = str(tmp_path / "Curious.png")
    im = Image.new("P", (8, 8), 0)
    im.putpalette([0, 0, 0, 255, 255, 255] + [0] * 762)
    for x in range(4, 8):
        for y in range(8):
            im.putpixel((x, y), 1)
    im.save(src, transparency=0)  # left half transparent, right half white
    assert _alpha_range(src) == (0, 255)

    dest = str(tmp_path / "thumb" / "Curious.png")
    _script().build_png_thumb(src, dest, 4)

    assert _alpha_range(dest) == (0, 255)
    with Image.open(dest) as th:
        assert th.convert("RGBA").getpixel((0, 2))[3] == 0  # still see-through


def test_rgba_png_stays_rgba(tmp_path):
    src = str(tmp_path / "Blue.png")
    im = Image.new("RGBA", (8, 8), (0, 0, 0, 0))
    im.putpixel((5, 5), (10, 20, 30, 255))
    im.save(src)
    dest = str(tmp_path / "thumb" / "Blue.png")
    _script().build_png_thumb(src, dest, 8)
    assert _alpha_range(dest) == (0, 255)
