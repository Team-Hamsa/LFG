# tests/test_layer_thumbs.py
# The layer thumbnail tier: path mapping + scan logic (lfg_core/layer_thumbs.py)
# and the /api/layer?thumb=1 serving path (thumb preferred, full-asset
# fallback). No ffmpeg/gifski involved — conversion is exercised by
# scripts/make_layer_thumbs.py in ops, not here.
#
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

from aiohttp.test_utils import make_mocked_request  # noqa: E402

from lfg_core import layer_store, layer_thumbs  # noqa: E402
from lfg_service import app as service_app  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _touch(path, mtime=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"x")
    if mtime is not None:
        os.utime(path, (mtime, mtime))


# --- thumb_path_for ---


def test_thumb_path_mapping(tmp_path):
    base = str(tmp_path)
    cases = {
        "male/Body/Straight Diamond.webm": ".thumbs/male/Body/Straight Diamond.gif",
        "female/Body/Curved Irridescent.gif": ".thumbs/female/Body/Curved Irridescent.gif",
        "shared/Background/Claw Diamond.mp4": ".thumbs/shared/Background/Claw Diamond.gif",
        "male/Eyes/Laser.png": ".thumbs/male/Eyes/Laser.png",
        "ape/Nose.png": ".thumbs/ape/Nose.png",
    }
    for src, thumb in cases.items():
        assert layer_thumbs.thumb_path_for(os.path.join(base, src), base) == os.path.join(
            base, thumb
        )


def test_thumb_path_rejects_non_layer_and_outside(tmp_path):
    base = str(tmp_path)
    # non-layer extension
    assert layer_thumbs.thumb_path_for(os.path.join(base, "male/Body/notes.txt"), base) is None
    # already inside .thumbs (never thumb a thumb)
    assert layer_thumbs.thumb_path_for(os.path.join(base, ".thumbs/male/Body/X.gif"), base) is None
    # outside the base dir
    assert layer_thumbs.thumb_path_for("/elsewhere/male/Body/X.png", base) is None


# --- scan ---


def test_scan_reports_missing_stale_fresh_and_orphans(tmp_path):
    base = str(tmp_path)
    _touch(os.path.join(base, "male/Body/Fresh.png"), mtime=100)
    _touch(os.path.join(base, ".thumbs/male/Body/Fresh.png"), mtime=200)
    _touch(os.path.join(base, "male/Body/Stale.webm"), mtime=300)
    _touch(os.path.join(base, ".thumbs/male/Body/Stale.gif"), mtime=100)
    _touch(os.path.join(base, "male/Body/Missing.gif"), mtime=100)
    _touch(os.path.join(base, ".thumbs/male/Body/Orphan.gif"), mtime=100)
    # a .webm source keeps its .gif thumb alive (reverse mapping)
    _touch(os.path.join(base, "female/Body/Kept.webm"), mtime=100)
    _touch(os.path.join(base, ".thumbs/female/Body/Kept.gif"), mtime=200)

    stale, orphans = layer_thumbs.scan(base)
    stale_srcs = {os.path.relpath(s, base) for s, _ in stale}
    assert stale_srcs == {"male/Body/Stale.webm", "male/Body/Missing.gif"}
    for src, thumb in stale:
        assert thumb == layer_thumbs.thumb_path_for(src, base)
    assert [os.path.relpath(t, base) for t in orphans] == [".thumbs/male/Body/Orphan.gif"]


def test_scan_same_stem_winner_follows_resolve_priority(tmp_path):
    # X.gif + X.webm share one thumb; resolve() serves the .gif, so the .gif
    # must drive the thumb — even when the .webm is newer than the thumb.
    base = str(tmp_path)
    _touch(os.path.join(base, "male/Body/X.gif"), mtime=100)
    _touch(os.path.join(base, "male/Body/X.webm"), mtime=300)
    _touch(os.path.join(base, ".thumbs/male/Body/X.gif"), mtime=200)
    stale, orphans = layer_thumbs.scan(base)
    assert stale == [] and orphans == []
    # and when the thumb is stale, only the winning source is scheduled
    _touch(os.path.join(base, "male/Body/X.gif"), mtime=400)
    stale, _ = layer_thumbs.scan(base)
    assert [os.path.relpath(s, base) for s, _ in stale] == ["male/Body/X.gif"]


def test_scan_ignores_hidden_dirs_as_sources(tmp_path):
    base = str(tmp_path)
    _touch(os.path.join(base, ".thumbs/male/Body/X.png"))
    _touch(os.path.join(base, ".git/objects/foo.png"))
    stale, orphans = layer_thumbs.scan(base)
    assert stale == []
    # the .thumbs png has no source -> orphan; .git is never a source
    assert [os.path.relpath(t, base) for t in orphans] == [".thumbs/male/Body/X.png"]


# --- /api/layer?thumb=1 serving ---


def _layer_request(query):
    return make_mocked_request("GET", f"/api/layer?{query}")


def _serve(monkeypatch, base, query):
    monkeypatch.setattr(layer_store, "_store", layer_store.LocalLayerStore(base_dir=base))
    return _run(service_app.handle_layer(_layer_request(query)))


def test_layer_thumb_served_when_present(tmp_path, monkeypatch):
    base = str(tmp_path)
    _touch(os.path.join(base, "male/Body/Straight Diamond.webm"))
    _touch(os.path.join(base, ".thumbs/male/Body/Straight Diamond.gif"))
    resp = _serve(monkeypatch, base, "body=male&trait=Body&value=Straight%20Diamond&thumb=1")
    assert resp.status == 200
    assert str(resp._path).endswith(".thumbs/male/Body/Straight Diamond.gif")
    assert resp.headers["Content-Type"] == "image/gif"


def test_layer_thumb_missing_falls_back_to_full(tmp_path, monkeypatch):
    base = str(tmp_path)
    _touch(os.path.join(base, "male/Body/Straight Diamond.webm"))
    resp = _serve(monkeypatch, base, "body=male&trait=Body&value=Straight%20Diamond&thumb=1")
    assert resp.status == 200
    assert str(resp._path).endswith("male/Body/Straight Diamond.webm")


def test_layer_without_thumb_param_serves_full(tmp_path, monkeypatch):
    base = str(tmp_path)
    _touch(os.path.join(base, "male/Body/Straight Diamond.webm"))
    _touch(os.path.join(base, ".thumbs/male/Body/Straight Diamond.gif"))
    resp = _serve(monkeypatch, base, "body=male&trait=Body&value=Straight%20Diamond")
    assert resp.status == 200
    assert str(resp._path).endswith("male/Body/Straight Diamond.webm")


def test_trait_image_url_carries_thumb_flag():
    # every server-built trait preview URL must opt into the thumb tier
    from lfg_core import trait_config

    cfg = trait_config.get_config()
    url = service_app._trait_image_url(cfg, "Body", "Straight Diamond")
    assert url.startswith("/api/layer?")
    assert "thumb=1" in url
