# Fresh mints upload to the foldered CDN layout <edition>/<edition>_0.*
# (matching the swap convention; first swap writes _1, no collision). The
# pre-2026-07-11 flat lfg_<n>.png / metadata_<n>.json layout is retired for
# new mints; existing flat files stay because on-chain URIs point at them.
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
from typing import Any  # noqa: E402

from lfg_core import mint_flow  # noqa: E402


def test_mint_uploads_use_foldered_cdn_paths(monkeypatch):
    captured: dict[str, Any] = {}

    async def fake_wait_for_payment(**kwargs):
        return True

    async def fake_buy_and_burn(*a, **k):
        return "BURNHASH"

    async def fake_allocate():
        return 4001

    async def fake_select(store):
        return "male", [{"trait_type": "Body", "value": "Straight"}]

    async def fake_compose(attributes, body, store, basename):
        return "/tmp/out.png", False

    async def fake_upload_output(path, is_video, upload_fn, basename, keep_still=None):
        captured["image_basename"] = basename
        return "https://cdn.example/4001/4001_0.png", None

    async def fake_upload_bunny(name, data, ctype):
        captured["metadata_name"] = name
        return f"https://cdn.example/{name}"

    async def fake_mint_nft(**kwargs):
        return None  # stop the flow right after the uploads

    monkeypatch.setattr(mint_flow.xrpl_ops, "wait_for_payment", fake_wait_for_payment)
    monkeypatch.setattr(mint_flow.xrpl_ops, "buy_and_burn", fake_buy_and_burn)
    monkeypatch.setattr(mint_flow.xrpl_ops, "mint_nft", fake_mint_nft)
    monkeypatch.setattr(mint_flow, "_allocate_nft_number", fake_allocate)
    monkeypatch.setattr(mint_flow.layer_store, "get_layer_store", lambda: object())
    monkeypatch.setattr(mint_flow.traits, "select_random_attributes", fake_select)
    monkeypatch.setattr(mint_flow.swap_compose, "compose_nft", fake_compose)
    monkeypatch.setattr(mint_flow.swap_compose, "upload_output", fake_upload_output)
    monkeypatch.setattr(mint_flow, "_upload_to_bunny", fake_upload_bunny)

    session = mint_flow.MintSession(discord_id="1", wallet_address="rUser")
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(mint_flow.run_mint_session(session))
    finally:
        loop.close()

    assert captured["image_basename"] == "4001/4001_0"
    assert captured["metadata_name"] == "4001/4001_0.json"
