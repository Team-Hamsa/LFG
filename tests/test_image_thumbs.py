# tests/test_image_thumbs.py
# Thumbnails for the local image archive: the Activity's roster/grid tiles
# render at ~120px but were downloading the full 1080px stills (~634 KB each —
# ~195 MB for a 300-NFT wallet). scripts/generate_thumbnails.py pre-renders
# 256px WebP thumbs into images_<network>/thumbs/, and /api/img?w=256 serves
# them, falling back to the full still (then the CDN/IPFS proxy) on a miss.
#
# Env-guard preamble (verbatim from tests/test_seasons.py lines 1-18): importing
# lfg_core.config freezes its constants (e.g. IMG_PROXY_ALLOWED_BASES,
# LAYER_SOURCE) at import time; set the same defaults test_smoke.py uses so
# collection order can't strand them.
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
from urllib.parse import quote  # noqa: E402

from PIL import Image  # noqa: E402

from lfg_core import image_archive, nft_index  # noqa: E402
from lfg_service import app as server  # noqa: E402
from scripts import generate_thumbnails as gt  # noqa: E402

_IPFS_URL = "ipfs://bafythumbed/5.png"


def _write_png(path, size=(64, 64), color=(200, 40, 40)):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    Image.new("RGB", size, color).save(path, "PNG")


# ------------------------------------------------------------- local_thumb


def test_local_thumb_finds_webp(monkeypatch, tmp_path):
    monkeypatch.setenv("IMAGES_DIR", str(tmp_path))
    thumb = tmp_path / "thumbs" / "5.webp"
    thumb.parent.mkdir()
    thumb.write_bytes(b"RIFFwebp")
    assert image_archive.local_thumb("mainnet", 5) == (str(thumb), "image/webp")


def test_local_thumb_none_on_miss(monkeypatch, tmp_path):
    monkeypatch.setenv("IMAGES_DIR", str(tmp_path))
    assert image_archive.local_thumb("mainnet", 5) is None


# --------------------------------------------- generate_thumbnails helpers


def test_iter_editions_numeric_stills_only(tmp_path):
    _write_png(str(tmp_path / "5.png"))
    _write_png(str(tmp_path / "12.png"))
    (tmp_path / "5.mp4").write_bytes(b"vid")  # animated companion: skip
    (tmp_path / "manifest.json").write_text("{}")  # not an edition
    (tmp_path / "history").mkdir()  # evolution archive: skip
    _write_png(str(tmp_path / "history" / "3.png"))
    (tmp_path / "thumbs").mkdir()  # output dir: never an input
    (tmp_path / "thumbs" / "5.webp").write_bytes(b"RIFF")
    got = gt.iter_editions(str(tmp_path))
    assert [(e, os.path.basename(p)) for e, p in got] == [(5, "5.png"), (12, "12.png")]


def test_thumb_stale_missing_or_older(tmp_path):
    src = tmp_path / "5.png"
    dest = tmp_path / "thumbs" / "5.webp"
    _write_png(str(src))
    assert gt.thumb_stale(str(src), str(dest))  # missing
    dest.parent.mkdir()
    dest.write_bytes(b"RIFF")
    os.utime(dest, (1, 1))  # older than src
    assert gt.thumb_stale(str(src), str(dest))
    os.utime(src, (1, 1))
    os.utime(dest, (2, 2))  # newer than src
    assert not gt.thumb_stale(str(src), str(dest))


def test_make_thumb_writes_bounded_webp(tmp_path):
    src = tmp_path / "5.png"
    dest = tmp_path / "thumbs" / "5.webp"
    _write_png(str(src), size=(1080, 1080))
    gt.make_thumb(str(src), str(dest), size=256, quality=80)
    with Image.open(dest) as im:
        assert im.format == "WEBP"
        assert max(im.size) == 256
    assert os.path.getsize(dest) < os.path.getsize(src)


def test_make_thumb_never_upscales(tmp_path):
    src = tmp_path / "5.png"
    dest = tmp_path / "thumbs" / "5.webp"
    _write_png(str(src), size=(100, 100))
    gt.make_thumb(str(src), str(dest), size=256, quality=80)
    with Image.open(dest) as im:
        assert im.size == (100, 100)


def test_run_sweeps_and_is_idempotent(monkeypatch, tmp_path):
    monkeypatch.setenv("IMAGES_DIR", str(tmp_path))
    _write_png(str(tmp_path / "5.png"), size=(512, 512))
    _write_png(str(tmp_path / "9.png"), size=(512, 512))
    stats = gt.run(network="mainnet", size=256, quality=80)
    assert stats == {"built": 2, "skipped": 0, "failed": 0}
    assert os.path.exists(tmp_path / "thumbs" / "5.webp")
    stats = gt.run(network="mainnet", size=256, quality=80)
    assert stats == {"built": 0, "skipped": 2, "failed": 0}


def test_run_counts_failures_and_continues(monkeypatch, tmp_path):
    monkeypatch.setenv("IMAGES_DIR", str(tmp_path))
    (tmp_path / "5.png").write_bytes(b"not a png")
    _write_png(str(tmp_path / "9.png"))
    stats = gt.run(network="mainnet", size=256, quality=80)
    assert stats == {"built": 1, "skipped": 0, "failed": 1}
    assert os.path.exists(tmp_path / "thumbs" / "9.webp")


# ------------------------------------------------------ /api/img?w= serving


def _img_request(url: str, w: str | None = None):
    from aiohttp.test_utils import make_mocked_request

    qs = "u=" + quote(url, safe="")
    if w is not None:
        qs += f"&w={w}"
    return make_mocked_request("GET", "/api/img?" + qs)


def _seed_env(monkeypatch, tmp_path, *, with_thumb: bool) -> None:
    db = tmp_path / "onchain.db"
    monkeypatch.setenv("ONCHAIN_DB_PATH", str(db))
    monkeypatch.setenv("IMAGES_DIR", str(tmp_path / "images"))
    os.makedirs(tmp_path / "images", exist_ok=True)
    conn = nft_index.init_db(str(db))
    conn.execute(
        "INSERT INTO onchain_nfts (nft_id, nft_number, image, is_burned) VALUES (?, ?, ?, 0)",
        ("A" * 64, 5, _IPFS_URL),
    )
    conn.commit()
    conn.close()
    (tmp_path / "images" / "5.png").write_bytes(b"\x89PNG full-size bytes")
    if with_thumb:
        os.makedirs(tmp_path / "images" / "thumbs", exist_ok=True)
        (tmp_path / "images" / "thumbs" / "5.webp").write_bytes(b"RIFF thumb bytes")


def _get(monkeypatch, url, w):
    async def boom(u):  # pragma: no cover - archive hits must not proxy
        raise AssertionError("hit the network")

    monkeypatch.setattr(server, "_fetch_cdn", boom)
    return asyncio.get_event_loop().run_until_complete(server.handle_img(_img_request(url, w)))


def test_img_w_serves_thumb(monkeypatch, tmp_path):
    _seed_env(monkeypatch, tmp_path, with_thumb=True)
    resp = _get(monkeypatch, _IPFS_URL, "256")
    assert resp.status == 200
    assert resp.body == b"RIFF thumb bytes"
    assert resp.content_type == "image/webp"


def test_img_w_falls_back_to_full_still_when_thumb_missing(monkeypatch, tmp_path):
    _seed_env(monkeypatch, tmp_path, with_thumb=False)
    resp = _get(monkeypatch, _IPFS_URL, "256")
    assert resp.status == 200
    assert resp.body == b"\x89PNG full-size bytes"
    assert resp.content_type == "image/png"


def test_img_large_or_bad_w_serves_full_still(monkeypatch, tmp_path):
    _seed_env(monkeypatch, tmp_path, with_thumb=True)
    for w in ("1080", "0", "-1", "abc"):
        resp = _get(monkeypatch, _IPFS_URL, w)
        assert resp.status == 200
        assert resp.body == b"\x89PNG full-size bytes", w


def test_img_no_w_unchanged(monkeypatch, tmp_path):
    _seed_env(monkeypatch, tmp_path, with_thumb=True)
    resp = _get(monkeypatch, _IPFS_URL, None)
    assert resp.status == 200
    assert resp.body == b"\x89PNG full-size bytes"
