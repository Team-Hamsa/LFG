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


def test_inject_and_mask_clips_nose_on_melt_body(tmp_path):
    store, ape = _ape_store_with_assets(tmp_path)
    # Opaque nose so the clip is observable.
    Image.new("RGBA", (4, 4), (255, 0, 0, 255)).save(ape / "Nose.png")
    layers = [("Body", "Ape Melting XRay", "/x/body.png")]
    out = _run(
        ape_face.inject_and_mask(layers, "ape", "Ape Melting XRay", store, str(tmp_path / "gen"))
    )
    nose_path = next(p for t, _v, p in out if t == "Nose")
    assert nose_path.endswith(".masked.png")
    res = Image.open(nose_path).convert("RGBA")
    assert res.getpixel((0, 0))[3] == 255  # left kept
    assert res.getpixel((3, 0))[3] == 0  # right cleared


def test_inject_and_mask_nose_unmasked_on_solid_body(tmp_path):
    store, _ = _ape_store_with_assets(tmp_path)
    layers = [("Body", "Ape Gold", "/x/body.png")]
    out = _run(ape_face.inject_and_mask(layers, "ape", "Ape Gold", store, str(tmp_path / "gen")))
    nose_path = next(p for t, _v, p in out if t == "Nose")
    assert not nose_path.endswith(".masked.png")


def test_inject_and_mask_nose_below_full_face_eyes(tmp_path):
    store, _ = _ape_store_with_assets(tmp_path)
    for value in sorted(ape_face.NOSE_BELOW_EYES_VALUES):
        layers = [
            ("Body", "Ape Gold", "/x/body.png"),
            ("Eyes", value, "/x/eyes.png"),
            ("Head", "Cap", "/x/head.png"),
        ]
        out = _run(
            ape_face.inject_and_mask(layers, "ape", "Ape Gold", store, str(tmp_path / "gen"))
        )
        types = [t for t, _v, _p in out]
        assert types == ["Body", "Nose", "Eyes", "Head"], value


def test_inject_and_mask_nose_fallback_when_no_eyes(tmp_path):
    store, _ = _ape_store_with_assets(tmp_path)
    layers = [("Eyebrows", "Flat", "/x/brow.png"), ("Head", "Cap", "/x/head.png")]
    out = _run(ape_face.inject_and_mask(layers, "ape", "Ape Gold", store, str(tmp_path / "gen")))
    types = [t for t, _v, _p in out]
    assert types == ["Eyebrows", "Nose", "Head"]  # nose at canonical Eyes slot


def test_compose_nose_stays_below_head_with_effect_eyes(tmp_path, monkeypatch):
    """Regression: compose z-sorts TOP_TRAITS Eyes (e.g. Laser Eyes, z 95) to
    the very end of the layer list BEFORE nose injection runs. _nose_index
    must not anchor on that floated effect tuple, or the nose lands at the
    top of the stack (above Head/Accessory/the effect) instead of its face
    slot below Head."""
    from lfg_core import swap_compose, trait_config

    captured = {}

    def fake_run(files, output_path, is_video):
        captured["files"] = list(files)
        with open(output_path, "wb") as f:
            f.write(b"x")

    monkeypatch.setattr(swap_compose, "_run_ffmpeg", fake_run)

    store, ape = _ape_store_with_assets(tmp_path)
    for trait_type, value in [("Body", "Ape Gold"), ("Eyes", "Laser Eyes"), ("Head", "Cap")]:
        d = ape / trait_type
        d.mkdir(exist_ok=True)
        Image.new("RGBA", (4, 4), (0, 0, 0, 255)).save(d / f"{value}.png")

    attrs = [
        {"trait_type": "Body", "value": "Ape Gold"},
        {"trait_type": "Eyes", "value": "Laser Eyes"},
        {"trait_type": "Head", "value": "Cap"},
    ]
    trait_config.reset_config()
    try:
        _run(swap_compose.compose_nft(attrs, "ape", store, "out", out_dir=str(tmp_path / "gen")))
    finally:
        trait_config.reset_config()

    names = [os.path.basename(f) for f in captured["files"]]
    # Final stack: nose below Head AND below the floated effect Eyes.
    assert names.index("Nose.png") < names.index("Cap.png")
    assert names.index("Nose.png") < names.index("Laser Eyes.png")
    # Byte-identical to the pre-config-sort output for this combo.
    assert names == ["Ape Gold.png", "Nose.png", "Cap.png", "Laser Eyes.png"]


def test_compose_nose_below_effect_without_head_accessory(tmp_path, monkeypatch):
    """Regression: when an ape has no Head and no Accessory but has TOP_TRAITS
    Eyes (e.g. Laser Eyes), the fallback path of _nose_index must detect and
    stop before the TOP_TRAIT, ensuring nose stays below the effect layer."""
    from lfg_core import swap_compose, trait_config

    captured = {}

    def fake_run(files, output_path, is_video):
        captured["files"] = list(files)
        with open(output_path, "wb") as f:
            f.write(b"x")

    monkeypatch.setattr(swap_compose, "_run_ffmpeg", fake_run)

    store, ape = _ape_store_with_assets(tmp_path)
    for trait_type, value in [("Body", "Ape Gold"), ("Eyes", "Laser Eyes")]:
        d = ape / trait_type
        d.mkdir(exist_ok=True)
        Image.new("RGBA", (4, 4), (0, 0, 0, 255)).save(d / f"{value}.png")

    attrs = [
        {"trait_type": "Body", "value": "Ape Gold"},
        {"trait_type": "Eyes", "value": "Laser Eyes"},
    ]
    trait_config.reset_config()
    try:
        _run(swap_compose.compose_nft(attrs, "ape", store, "out", out_dir=str(tmp_path / "gen")))
    finally:
        trait_config.reset_config()

    names = [os.path.basename(f) for f in captured["files"]]
    # Nose must be below the floated effect Eyes even without Head/Accessory.
    assert names.index("Nose.png") < names.index("Laser Eyes.png")
    assert names == ["Ape Gold.png", "Nose.png", "Laser Eyes.png"]
