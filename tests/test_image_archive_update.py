# Swap/mint local-archive updates (#163): stills are staged to pending/ at
# upload time, promoted (atomic replace + thumb refresh) once the on-chain
# change is final, and discarded on failure/revert.
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DISCORD_BOT_TOKEN", "x")
os.environ.setdefault("XUMM_API_KEY", "x")
os.environ.setdefault("XUMM_API_SECRET", "x")
os.environ.setdefault("BUNNY_CDN_ACCESS_KEY", "x")
os.environ.setdefault("BUNNY_CDN_STORAGE_ZONE", "x")
os.environ.setdefault("SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2")
os.environ.setdefault("TOKEN_ISSUER_ADDRESS", "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
os.environ.setdefault("TOKEN_CURRENCY_HEX", "4C46474F00000000000000000000000000000000")
os.environ.setdefault("BUNNY_PULL_ZONE", "nft.pullzone.example")
os.environ.setdefault("LAYER_SOURCE", "local")

from PIL import Image

from lfg_core import image_archive, swap_compose


def _write_png(path: str, color=(255, 0, 0), size=(64, 64)) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    Image.new("RGB", size, color).save(path, "PNG")


def test_stage_then_promote_updates_still_and_thumb(tmp_path, monkeypatch):
    monkeypatch.setenv("IMAGES_DIR", str(tmp_path))
    # Pre-existing stale archive state: old .gif still + old thumb.
    _write_png(str(tmp_path / "42.gif"))
    _write_png(str(tmp_path / "thumbs" / "42.webp"))

    staged = image_archive.pending_still_path("mainnet", 42)
    _write_png(staged, color=(0, 255, 0))

    assert image_archive.promote_still("mainnet", 42) is True
    # New still in place, staged copy consumed, stale .gif dropped.
    assert os.path.exists(tmp_path / "42.png")
    assert not os.path.exists(staged)
    assert not os.path.exists(tmp_path / "42.gif")
    # Thumb rebuilt from the new still.
    with Image.open(tmp_path / "thumbs" / "42.webp") as im:
        r, g, b = im.convert("RGB").getpixel((0, 0))
        assert g > 200 and r < 50  # green (new art), not red (old thumb)


def test_promote_without_staged_still_is_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("IMAGES_DIR", str(tmp_path))
    assert image_archive.promote_still("mainnet", 7) is False
    assert not os.path.exists(tmp_path / "7.png")


def test_discard_removes_staged_still(tmp_path, monkeypatch):
    monkeypatch.setenv("IMAGES_DIR", str(tmp_path))
    staged = image_archive.pending_still_path("mainnet", 9)
    _write_png(staged)
    image_archive.discard_still("mainnet", 9)
    assert not os.path.exists(staged)
    # Never raises when nothing is staged.
    image_archive.discard_still("mainnet", 9)


def test_promote_bad_image_removes_stale_thumb(tmp_path, monkeypatch):
    # If the thumb rebuild fails, the stale thumb must be REMOVED so /api/img
    # falls back to the fresh full still instead of serving old art.
    monkeypatch.setenv("IMAGES_DIR", str(tmp_path))
    _write_png(str(tmp_path / "thumbs" / "5.webp"))
    staged = image_archive.pending_still_path("mainnet", 5)
    os.makedirs(os.path.dirname(staged), exist_ok=True)
    with open(staged, "wb") as f:
        f.write(b"not a png")
    assert image_archive.promote_still("mainnet", 5) is True
    assert os.path.exists(tmp_path / "5.png")
    assert not os.path.exists(tmp_path / "thumbs" / "5.webp")


def test_upload_output_keep_still_stages_instead_of_deleting(tmp_path):
    src = tmp_path / "out.png"
    _write_png(str(src))
    dest = tmp_path / "pending" / "12.png"
    os.makedirs(dest.parent, exist_ok=True)

    async def upload(path_on_cdn, data, content_type):
        return f"https://cdn.example/{path_on_cdn}"

    image_url, video_url = asyncio.get_event_loop().run_until_complete(
        swap_compose.upload_output(str(src), False, upload, "12/12_1", keep_still=str(dest))
    )
    assert image_url.endswith("12/12_1.png")
    assert video_url is None
    assert not src.exists()
    assert dest.exists()


def test_upload_output_without_keep_still_deletes(tmp_path):
    src = tmp_path / "out.png"
    _write_png(str(src))

    async def upload(path_on_cdn, data, content_type):
        return "u"

    asyncio.get_event_loop().run_until_complete(
        swap_compose.upload_output(str(src), False, upload, "x/y")
    )
    assert not src.exists()
