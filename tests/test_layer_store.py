# tests/test_layer_store.py
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

from lfg_core import layer_store  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_local_resolve_asset_found(tmp_path):
    ape = tmp_path / "ape"
    ape.mkdir()
    (ape / "Nose.png").write_bytes(b"x")
    store = layer_store.LocalLayerStore(str(tmp_path))
    assert _run(store.resolve_asset("ape/Nose.png")) == os.path.join(
        str(tmp_path), "ape", "Nose.png"
    )


def test_local_resolve_asset_missing(tmp_path):
    store = layer_store.LocalLayerStore(str(tmp_path))
    assert _run(store.resolve_asset("ape/Nose.png")) is None


def test_cdn_resolve_asset_lists_parent_then_downloads(monkeypatch):
    store = layer_store.CdnLayerStore()

    async def fake_list(rel_path):
        assert rel_path == "ape"
        return [("Nose.png", False), ("Eyes", True)]

    async def fake_download(rel_path):
        assert rel_path == "ape/Nose.png"
        return "/cache/ape/Nose.png"

    monkeypatch.setattr(store, "_list_dir", fake_list)
    monkeypatch.setattr(store, "_download", fake_download)
    assert _run(store.resolve_asset("ape/Nose.png")) == "/cache/ape/Nose.png"


def test_cdn_resolve_asset_absent_returns_none(monkeypatch):
    store = layer_store.CdnLayerStore()

    async def fake_list(rel_path):
        return [("Eyes", True)]

    monkeypatch.setattr(store, "_list_dir", fake_list)
    assert _run(store.resolve_asset("ape/Nose.png")) is None
