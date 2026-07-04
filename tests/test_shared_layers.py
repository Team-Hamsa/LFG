# tests/test_shared_layers.py
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

from lfg_core.layer_store import CdnLayerStore, LocalLayerStore  # noqa: E402


def test_shared_dir_union(tmp_path):
    (tmp_path / "male" / "Background").mkdir(parents=True)
    (tmp_path / "male" / "Background" / "Exclusive.png").write_bytes(b"x")
    (tmp_path / "shared" / "Background").mkdir(parents=True)
    (tmp_path / "shared" / "Background" / "Sunset.png").write_bytes(b"x")
    store = LocalLayerStore(str(tmp_path))
    try:
        assert asyncio.run(store.list_bodies()) == ["male"]  # shared is not a body
        assert asyncio.run(store.list_values("male", "Background")) == [
            "Exclusive",
            "Sunset",
        ]
        path = asyncio.run(store.resolve("male", "Background", "Sunset"))
        assert path and "shared/Background" in path
        assert asyncio.run(store.resolve("male", "Background", "Exclusive")).endswith(
            "male/Background/Exclusive.png"
        )
    finally:
        # asyncio.run() leaves the main-thread event loop unset on exit;
        # webapp tests later in full-suite order still rely on the legacy
        # asyncio.get_event_loop() auto-create, so restore a loop for them.
        asyncio.set_event_loop(asyncio.new_event_loop())


def test_shared_dir_missing_is_noop(tmp_path):
    (tmp_path / "male" / "Background").mkdir(parents=True)
    (tmp_path / "male" / "Background" / "Exclusive.png").write_bytes(b"x")
    store = LocalLayerStore(str(tmp_path))
    try:
        # No shared/ dir at all: union degrades to the body dir alone.
        assert asyncio.run(store.list_bodies()) == ["male"]
        assert asyncio.run(store.list_values("male", "Background")) == ["Exclusive"]
        assert asyncio.run(store.resolve("male", "Background", "Sunset")) is None
    finally:
        asyncio.set_event_loop(asyncio.new_event_loop())


def test_shared_dir_unions_trait_types(tmp_path):
    (tmp_path / "male" / "Background").mkdir(parents=True)
    (tmp_path / "shared" / "Backdrop").mkdir(parents=True)
    store = LocalLayerStore(str(tmp_path))
    try:
        assert asyncio.run(store.list_trait_types("male")) == ["Backdrop", "Background"]
    finally:
        asyncio.set_event_loop(asyncio.new_event_loop())


def test_cdn_shared_union(monkeypatch):
    store = CdnLayerStore()

    async def fake_list_dir(rel_path):
        listings = {
            "": [("male", True), ("shared", True)],
            "male": [("Background", True)],
            "shared": [("Background", True), ("Backdrop", True)],
            "male/Background": [("Exclusive.png", False)],
            "shared/Background": [("Sunset.png", False)],
        }
        if rel_path not in listings:
            raise Exception(f"CDN listing failed (404) for {rel_path or '/'}")
        return listings[rel_path]

    async def fake_download(rel_path):
        return f"/cache/{rel_path}"

    monkeypatch.setattr(store, "_list_dir", fake_list_dir)
    monkeypatch.setattr(store, "_download", fake_download)

    try:
        assert asyncio.run(store.list_bodies()) == ["male"]
        assert asyncio.run(store.list_trait_types("male")) == ["Backdrop", "Background"]
        assert asyncio.run(store.list_values("male", "Background")) == [
            "Exclusive",
            "Sunset",
        ]
        assert asyncio.run(store.resolve("male", "Background", "Sunset")) == (
            "/cache/shared/Background/Sunset.png"
        )
        assert asyncio.run(store.resolve("male", "Background", "Exclusive")) == (
            "/cache/male/Background/Exclusive.png"
        )
    finally:
        asyncio.set_event_loop(asyncio.new_event_loop())


def test_cdn_shared_dir_missing_is_noop(monkeypatch):
    store = CdnLayerStore()

    async def fake_list_dir(rel_path):
        listings = {
            "": [("male", True)],
            "male": [("Background", True)],
            "male/Background": [("Exclusive.png", False)],
        }
        if rel_path not in listings:
            raise Exception(f"CDN listing failed (404) for {rel_path or '/'}")
        return listings[rel_path]

    async def fake_download(rel_path):
        return f"/cache/{rel_path}"

    monkeypatch.setattr(store, "_list_dir", fake_list_dir)
    monkeypatch.setattr(store, "_download", fake_download)

    try:
        assert asyncio.run(store.list_bodies()) == ["male"]
        assert asyncio.run(store.list_trait_types("male")) == ["Background"]
        assert asyncio.run(store.list_values("male", "Background")) == ["Exclusive"]
        assert asyncio.run(store.resolve("male", "Background", "Sunset")) is None
        assert asyncio.run(store.resolve("male", "Background", "Exclusive")) == (
            "/cache/male/Background/Exclusive.png"
        )
    finally:
        asyncio.set_event_loop(asyncio.new_event_loop())
