# tests/test_swap_compose.py
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

from lfg_core import layer_store, swap_compose  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _png(path, color=(1, 2, 3, 255), size=(4, 4)):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    Image.new("RGBA", size, color).save(path)


def _mask_right_opaque(path, size=(4, 4)):
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    for y in range(size[1]):
        for x in range(size[0] // 2, size[0]):
            img.putpixel((x, y), (255, 255, 255, 255))
    img.save(path)


def _attrs(**kw):
    # minimal normalized-style attribute list
    return [{"trait_type": t, "value": v} for t, v in kw.items()]


def test_compose_nft_ape_inserts_nose_and_masks(tmp_path, monkeypatch):
    captured = {}

    def fake_run(files, output_path, is_video):
        captured["files"] = list(files)
        with open(output_path, "wb") as f:
            f.write(b"x")

    monkeypatch.setattr(swap_compose, "_run_ffmpeg", fake_run)

    base = tmp_path / "layers" / "ape"
    _png(str(base / "Body" / "Ape Melting XRay.png"))
    _png(str(base / "Eyes" / "Creepy.png"), color=(0, 255, 0, 255))
    _png(str(base / "Head" / "Cap.png"))
    _png(str(base / "Nose.png"), color=(0, 0, 0, 0))
    _mask_right_opaque(base / "Ape Mask.png")
    store = layer_store.LocalLayerStore(str(tmp_path / "layers"))

    attrs = _attrs(Body="Ape Melting XRay", Eyes="Creepy", Head="Cap")
    out_dir = str(tmp_path / "gen")
    path, is_video = _run(swap_compose.compose_nft(attrs, "ape", store, "out", out_dir=out_dir))

    files = captured["files"]
    names = [os.path.basename(f) for f in files]
    assert "Nose.masked.png" in names
    assert names.index("Nose.masked.png") == names.index("Creepy.masked.png") + 1  # nose above eyes
    assert is_video is False
    assert os.path.isfile(path)
    # masked temp cleaned up after compose
    assert not os.path.isfile(os.path.join(out_dir, "Creepy.masked.png"))


def test_compose_nft_non_ape_has_no_nose(tmp_path, monkeypatch):
    captured = {}
    monkeypatch.setattr(
        swap_compose,
        "_run_ffmpeg",
        lambda files, output_path, is_video: (
            captured.__setitem__("files", list(files)) or open(output_path, "wb").write(b"x")
        ),
    )
    base = tmp_path / "layers" / "male"
    _png(str(base / "Body" / "Straight Dark.png"))
    _png(str(base / "Eyes" / "Standard.png"))
    store = layer_store.LocalLayerStore(str(tmp_path / "layers"))

    attrs = _attrs(Body="Straight Dark", Eyes="Standard")
    _run(swap_compose.compose_nft(attrs, "male", store, "out", out_dir=str(tmp_path / "gen")))
    names = [os.path.basename(f) for f in captured["files"]]
    assert "Nose.png" not in names


def test_compose_nft_refuses_unnormalized_back_value_under_accessory(tmp_path):
    # #268 (NFT #4039): a Back-class value still sitting under Accessory means
    # normalize_attributes was never run — compose must refuse, not silently
    # z-sort the duplicate over Clothing.
    store = layer_store.LocalLayerStore(str(tmp_path / "layers"))
    attrs = [
        {"trait_type": "Back", "value": "Angel Wings Open"},
        {"trait_type": "Accessory", "value": "Angel Wings Open"},
    ]
    with pytest.raises(ValueError, match="#268"):
        _run(swap_compose.compose_nft(attrs, "male", store, "out", out_dir=str(tmp_path / "gen")))


def test_compose_nft_refuses_accessory_only_back_value(tmp_path):
    store = layer_store.LocalLayerStore(str(tmp_path / "layers"))
    attrs = _attrs(Body="Straight Dark", Accessory="Angel Wings")
    with pytest.raises(ValueError, match="#268"):
        _run(swap_compose.compose_nft(attrs, "male", store, "out", out_dir=str(tmp_path / "gen")))


def test_missing_layers_flags_ape_assets(tmp_path):
    base = tmp_path / "layers" / "ape"
    _png(str(base / "Body" / "Ape Melting.png"))
    _png(str(base / "Eyes" / "Creepy.png"))
    # No Nose.png and no Ape Mask.png present.
    store = layer_store.LocalLayerStore(str(tmp_path / "layers"))

    attrs = _attrs(Body="Ape Melting", Eyes="Creepy")
    missing = _run(swap_compose.missing_layers(attrs, "ape", store))
    assert "ape/Nose.png" in missing
    assert "ape/Ape Mask.png" in missing


def test_missing_layers_non_melt_ape_needs_nose_not_mask(tmp_path):
    base = tmp_path / "layers" / "ape"
    _png(str(base / "Body" / "Ape Gold.png"))
    _png(str(base / "Eyes" / "Creepy.png"))
    _png(str(base / "Nose.png"))
    store = layer_store.LocalLayerStore(str(tmp_path / "layers"))

    attrs = _attrs(Body="Ape Gold", Eyes="Creepy")
    missing = _run(swap_compose.missing_layers(attrs, "ape", store))
    assert missing == []  # nose present; mask not required for Ape Gold


def test_missing_layers_non_ape_ignores_ape_assets(tmp_path):
    base = tmp_path / "layers" / "male"
    _png(str(base / "Body" / "Straight Dark.png"))
    store = layer_store.LocalLayerStore(str(tmp_path / "layers"))
    attrs = _attrs(Body="Straight Dark")
    assert _run(swap_compose.missing_layers(attrs, "male", store)) == []


def _have_libvpx_encoder() -> bool:
    import subprocess

    try:
        out = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True
        ).stdout
    except OSError:
        return False
    return "libvpx-vp9" in out


def _webm_vp9_alpha(path, size=(8, 8)):
    """Encode a 1-frame VP9-alpha WebM: top half transparent, bottom half blue."""
    import subprocess

    frame = os.path.join(os.path.dirname(path), "_frame.png")
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    for y in range(size[1] // 2, size[1]):
        for x in range(size[0]):
            img.putpixel((x, y), (0, 0, 255, 255))
    img.save(frame)
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y",
            "-loop", "1", "-i", frame, "-frames:v", "2",
            "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p", path,
        ],
        check=True,
    )
    os.remove(frame)


@pytest.mark.skipif(not _have_libvpx_encoder(), reason="ffmpeg lacks libvpx-vp9")
def test_compose_nft_webm_layer_preserves_alpha(tmp_path):
    # A VP9-alpha WebM body over a red background: the transparent top half
    # must show the background through. Regression: ffmpeg's native VP9
    # decoder drops WebM alpha — _run_ffmpeg must force libvpx-vp9 on .webm
    # inputs or this composes opaque (black top half).
    base = tmp_path / "layers" / "male"
    _png(str(base / "Background" / "Red.png"), color=(255, 0, 0, 255), size=(8, 8))
    os.makedirs(base / "Body", exist_ok=True)
    _webm_vp9_alpha(str(base / "Body" / "Straight Diamond.webm"))
    store = layer_store.LocalLayerStore(str(tmp_path / "layers"))

    attrs = _attrs(Background="Red", Body="Straight Diamond")
    out_dir = str(tmp_path / "gen")
    path, is_video = _run(swap_compose.compose_nft(attrs, "male", store, "out", out_dir=out_dir))
    assert is_video is True and path.endswith(".mp4")

    still = os.path.join(out_dir, "still.png")
    swap_compose.extract_first_frame(path, still)
    img = Image.open(still).convert("RGB")
    top = img.getpixel((4, 1))
    bottom = img.getpixel((4, 6))
    assert top[0] > 180 and top[2] < 80, f"transparent region lost alpha: {top}"
    assert bottom[2] > 180 and bottom[0] < 80, f"opaque region wrong: {bottom}"
