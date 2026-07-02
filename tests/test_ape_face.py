# tests/test_ape_face.py
# Env-guard preamble: importing lfg_core.config freezes its constants (e.g.
# IMG_PROXY_ALLOWED_BASES, LAYER_SOURCE) at import time; set the same defaults
# test_smoke.py uses so collection order can't strand them. (Copy the block
# verbatim from tests/test_server_identity_wiring.py — same keys/values.)
import os

os.environ.setdefault("XUMM_API_KEY", "test")
os.environ.setdefault("XUMM_API_SECRET", "test")
os.environ.setdefault("SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2")
os.environ.setdefault("TOKEN_ISSUER_ADDRESS", "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
os.environ.setdefault("TOKEN_CURRENCY_HEX", "4C46474F00000000000000000000000000000000")
os.environ.setdefault("BUNNY_CDN_ACCESS_KEY", "test")
os.environ.setdefault("BUNNY_CDN_STORAGE_ZONE", "test")
os.environ.setdefault("LAYER_SOURCE", "local")
os.environ.setdefault("BUNNY_PULL_ZONE", "nft.pullzone.example")

import asyncio  # noqa: E402

import pytest  # noqa: E402
from PIL import Image  # noqa: E402

from lfg_core import ape_face, layer_store  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


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


def _ape_store_with_assets(tmp_path):
    ape = tmp_path / "layers" / "ape"
    ape.mkdir(parents=True)
    Image.new("RGBA", (4, 4), (0, 0, 0, 0)).save(ape / "Nose.png")
    _mask_right_opaque(ape / "Ape Mask.png")
    return layer_store.LocalLayerStore(str(tmp_path / "layers")), ape


def test_inject_and_mask_non_ape_unchanged(tmp_path):
    store, _ = _ape_store_with_assets(tmp_path)
    layers = [("Body", "Straight Dark", "/x/body.png"), ("Eyes", "Standard", "/x/eyes.png")]
    out = _run(
        ape_face.inject_and_mask(layers, "male", "Straight Dark", store, str(tmp_path / "gen"))
    )
    assert out == layers


def test_inject_and_mask_inserts_nose_above_eyes(tmp_path):
    store, _ = _ape_store_with_assets(tmp_path)
    layers = [
        ("Body", "Ape Gold", "/x/body.png"),
        ("Eyes", "Standard", "/x/eyes.png"),
        ("Head", "Cap", "/x/head.png"),
    ]
    out = _run(ape_face.inject_and_mask(layers, "ape", "Ape Gold", store, str(tmp_path / "gen")))
    types = [t for t, _v, _p in out]
    assert types == ["Body", "Eyes", "Nose", "Head"]
    # Ape Gold is not a melt body -> nose injected, no masking.
    assert all(not p.endswith(".masked.png") for _t, _v, p in out)


def test_inject_and_mask_clips_face_features_on_melt_body(tmp_path):
    store, ape = _ape_store_with_assets(tmp_path)
    eyes = ape / "Eyes"
    eyes.mkdir()
    Image.new("RGBA", (4, 4), (0, 255, 0, 255)).save(eyes / "Creepy.png")
    layers = [
        ("Body", "Ape Melting XRay", "/x/body.png"),
        ("Eyes", "Creepy", str(eyes / "Creepy.png")),
    ]
    out = _run(
        ape_face.inject_and_mask(layers, "ape", "Ape Melting XRay", store, str(tmp_path / "gen"))
    )
    eyes_path = next(p for t, _v, p in out if t == "Eyes")
    assert eyes_path.endswith(".masked.png")
    res = Image.open(eyes_path).convert("RGBA")
    assert res.getpixel((0, 0))[3] == 255  # left kept
    assert res.getpixel((3, 0))[3] == 0  # right cleared
    # Melt/xray apes are still apes: the nose must also be injected, above Eyes.
    types = [t for t, _v, _p in out]
    assert "Nose" in types
    assert types.index("Eyes") < types.index("Nose")


def test_inject_and_mask_nose_fallback_when_no_eyes(tmp_path):
    store, _ = _ape_store_with_assets(tmp_path)
    layers = [("Eyebrows", "Flat", "/x/brow.png"), ("Head", "Cap", "/x/head.png")]
    out = _run(ape_face.inject_and_mask(layers, "ape", "Ape Gold", store, str(tmp_path / "gen")))
    types = [t for t, _v, _p in out]
    assert types == ["Eyebrows", "Nose", "Head"]  # nose at canonical Eyes slot
