# tests/test_layer_store.py
import asyncio
import os

from lfg_core import layer_store


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
