# tests/test_rebuild_cdn_images.py
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

import sys  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))

import rebuild_cdn_images as rci  # noqa: E402

# ---------------------------------------------------------------- classify


def test_classify_image_dir():
    kind, base = rci.classify_files(["242_1.json", "242_1.mp4", "242_1.png"])
    assert kind == "image"
    assert base == "242_1"


def test_classify_json_only_dir():
    kind, base = rci.classify_files(["1_3.json"])
    assert kind == "json_only"
    assert base == "1_3"


def test_classify_missing_dir():
    assert rci.classify_files(None) == ("missing", None)
    assert rci.classify_files([]) == ("missing", None)


def test_classify_ignores_stray_non_media_files():
    # A dir holding only unknown junk is as good as missing an image.
    kind, base = rci.classify_files(["notes.txt"])
    assert kind == "missing"
    assert base is None


# ----------------------------------------------------------- target name


def test_target_basename_pairs_with_existing_json():
    # A json-only dir already names the burn revision — the rebuilt image
    # must land beside it so `<stem>.png` pairs with `<stem>.json`.
    assert rci.target_basename(1, ["1_3.json"], burn_count=0) == "1_3"


def test_target_basename_uses_burn_count_when_no_json():
    assert rci.target_basename(8, [], burn_count=2) == "8_2"
    assert rci.target_basename(8, None, burn_count=0) == "8_0"


# ----------------------------------------------------- archive candidates


def test_pick_archive_source_prefers_png_and_takes_video():
    img, vid = rci.pick_archive_source(["242_1.json", "242_1.mp4", "242_1.png"])
    assert img == "242_1.png"
    assert vid == "242_1.mp4"


def test_pick_archive_source_falls_back_to_any_image_ext():
    img, vid = rci.pick_archive_source(["5_0.gif"])
    assert img == "5_0.gif"
    assert vid is None


def test_pick_archive_source_none_when_no_image():
    assert rci.pick_archive_source(["1_3.json"]) == (None, None)


# ------------------------------------------------------- metadata patching


def test_patched_metadata_fills_null_image():
    meta = {"name": "Let's Effing Go! #1", "image": None, "video": "ipfs://cid"}
    out = rci.patched_metadata(meta, "https://cdn/LFGO/1/1_3.png", "https://cdn/LFGO/1/1_3.mp4")
    assert out is not None
    assert out["image"] == "https://cdn/LFGO/1/1_3.png"
    assert out["video"] == "https://cdn/LFGO/1/1_3.mp4"
    assert out["name"] == "Let's Effing Go! #1"


def test_patched_metadata_replaces_ipfs_image():
    meta = {"image": "ipfs://bafyimg"}
    out = rci.patched_metadata(meta, "https://cdn/LFGO/8/8_0.png", None)
    assert out is not None
    assert out["image"] == "https://cdn/LFGO/8/8_0.png"
    assert "video" not in out


def test_patched_metadata_no_change_when_already_cdn():
    meta = {"image": "https://lfgo.b-cdn.net/LFGO/242/242_1.png"}
    assert rci.patched_metadata(meta, "https://cdn/other.png", None) is None


def test_patched_metadata_does_not_mutate_input():
    meta = {"image": None}
    rci.patched_metadata(meta, "https://cdn/x.png", None)
    assert meta["image"] is None
