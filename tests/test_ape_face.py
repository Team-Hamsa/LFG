# tests/test_ape_face.py
import pytest
from PIL import Image

from lfg_core import ape_face


def test_should_mask_normal_face_feature_on_melt_body():
    assert ape_face.should_mask("Eyes", "Creepy", "Ape Melting XRay") is True
    assert ape_face.should_mask("Mouth", "Serious", "Ape Xray") is True
    assert ape_face.should_mask("Eyebrows", "Flat", "Ape Melting") is True


def test_should_mask_exempts_top_effect():
    assert ape_face.should_mask("Mouth", "Rainbow Puke", "Ape Xray") is False
    assert ape_face.should_mask("Eyes", "Laser Eyes", "Ape Xray") is False


def test_should_mask_only_melt_bodies():
    assert ape_face.should_mask("Eyes", "Creepy", "Ape Gold") is False
    assert ape_face.should_mask("Eyes", "Creepy", "Straight Dark") is False


def test_should_mask_only_face_trait_types():
    assert ape_face.should_mask("Clothing", "Wonder", "Ape Xray") is False
    assert ape_face.should_mask("Background", "Wave", "Ape Xray") is False


def test_should_mask_respects_skip_list(monkeypatch):
    monkeypatch.setattr(ape_face, "NO_MASK_VALUES", [{"trait_type": "Mouth", "value": "Cigar"}])
    assert ape_face.should_mask("Mouth", "Cigar", "Ape Xray") is False
    assert ape_face.should_mask("Mouth", "Smile", "Ape Xray") is True


def _mask_right_opaque(path, size=(4, 4)):
    """Mask: left half fully transparent, right half fully opaque white."""
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    for y in range(size[1]):
        for x in range(size[0] // 2, size[0]):
            img.putpixel((x, y), (255, 255, 255, 255))
    img.save(path)


def test_apply_alpha_mask_clips_right_keeps_left(tmp_path):
    layer = tmp_path / "Creepy.png"
    Image.new("RGBA", (4, 4), (0, 255, 0, 255)).save(layer)  # opaque green
    mask = tmp_path / "Ape Mask.png"
    _mask_right_opaque(mask)

    out = ape_face.apply_alpha_mask(str(layer), str(mask), str(tmp_path / "gen"))
    res = Image.open(out).convert("RGBA")

    assert res.getpixel((0, 0))[3] == 255  # left half kept
    assert res.getpixel((3, 0))[3] == 0  # right half cleared
    assert res.getpixel((0, 0))[:3] == (0, 255, 0)  # RGB preserved where kept


def test_apply_alpha_mask_resizes_mismatched_mask(tmp_path):
    layer = tmp_path / "Creepy.png"
    Image.new("RGBA", (8, 8), (0, 255, 0, 255)).save(layer)
    mask = tmp_path / "Ape Mask.png"
    _mask_right_opaque(mask, size=(4, 4))  # smaller than layer

    out = ape_face.apply_alpha_mask(str(layer), str(mask), str(tmp_path / "gen"))
    res = Image.open(out).convert("RGBA")
    assert res.size == (8, 8)
    assert res.getpixel((1, 1))[3] == 255  # left kept
    assert res.getpixel((6, 1))[3] == 0  # right cleared


def test_apply_alpha_mask_rejects_non_png(tmp_path):
    with pytest.raises(ValueError):
        ape_face.apply_alpha_mask(
            str(tmp_path / "x.mp4"), str(tmp_path / "m.png"), str(tmp_path / "gen")
        )
