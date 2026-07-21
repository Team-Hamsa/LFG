# A swap's CDN output path must be unique per swap. The revision counter
# alone is not enough: economy-written metadata (scripts/_economy_deps) carries
# no `burnCount`, so the next swap restarts at 1 and would overwrite the
# `<edition>_1.png/.json` an earlier swap already published — after which every
# URL-keyed cache (browser, Bunny edge, the listener's metadata fetch) keeps
# serving the pre-swap art/attributes.
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

import asyncio  # noqa: E402
import re  # noqa: E402

from lfg_core import swap_compose, swap_flow  # noqa: E402


def _capture_basenames(monkeypatch, nft, calls=2):
    seen = []
    returned = []

    async def fake_compose(attributes, gender, store, basename):
        seen.append(("compose", basename))
        return f"/tmp/{basename}.png", False

    async def fake_upload(path, is_video, upload, cdn_basename, keep_still=None):
        seen.append(("upload", cdn_basename))
        return f"https://cdn.example/{cdn_basename}.png", None

    monkeypatch.setattr(swap_compose, "compose_nft", fake_compose)
    monkeypatch.setattr(swap_compose, "upload_output", fake_upload)
    for _ in range(calls):
        returned.append(
            asyncio.get_event_loop().run_until_complete(
                swap_flow._build_and_upload(nft, nft["attributes"], object(), "tok")
            )
        )
    return seen, returned


def test_swap_cdn_basename_is_unique_per_swap(monkeypatch):
    # Same NFT, same burn_count (an economy op wiped the counter): the two
    # swaps must NOT land on the same CDN path.
    nft = {"number": 4134, "burn_count": 0, "gender": "male", "attributes": []}
    seen, _ = _capture_basenames(monkeypatch, nft)
    uploads = [b for kind, b in seen if kind == "upload"]
    assert len(set(uploads)) == len(uploads), f"CDN path reused across swaps: {uploads}"


def test_swap_cdn_basename_keeps_edition_and_revision_prefix(monkeypatch):
    nft = {"number": 4134, "burn_count": 1, "gender": "male", "attributes": []}
    seen, returned = _capture_basenames(monkeypatch, nft, calls=1)
    kinds = dict(seen)
    assert re.fullmatch(r"4134/4134_2_[0-9a-f]{8}", kinds["upload"]), kinds["upload"]
    # The local compose filename shares the same unique stem, so two
    # concurrent swaps on one edition cannot collide on the temp file either.
    assert kinds["compose"] == kinds["upload"].split("/", 1)[1]
    # The stem is returned so the metadata JSON lands beside its image.
    _image_url, _video_url, new_burn, stem = returned[0]
    assert new_burn == 2
    assert kinds["upload"] == f"4134/{stem}"
