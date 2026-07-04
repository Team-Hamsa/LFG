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

from lfg_core import swap_compose, trait_config  # noqa: E402
from lfg_core.layer_store import CdnLayerStore, LocalLayerStore  # noqa: E402

# Minimal trait config mirroring tests/test_cross_body_resolve.py: ape<->skeleton
# may swap Head/Clothing; Eyes is NOT matrix-permitted between them.
CFG = """
version: 1
layers:
  - {name: Background, z: 10}
  - {name: Back, z: 20}
  - {name: Body, z: 30}
  - {name: Clothing, z: 40}
  - {name: Mouth, z: 50}
  - {name: Eyebrows, z: 60}
  - {name: Eyes, z: 70}
  - {name: Head, z: 80}
  - {name: Accessory, z: 90}
swap_matrix:
  universal_layers: [Accessory, Back]
  pairs:
    - {bodies: [ape, skeleton], layers: [Head, Clothing]}
"""


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


def test_resolve_layer_shared_hop_bypasses_matrix(tmp_path):
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(CFG)
    cfg = trait_config.load_config(str(cfg_path))
    layers = tmp_path / "layers"
    # Eyes is matrix-FORBIDDEN between ape and skeleton, but the value lives
    # in shared/: store.resolve's shared hop short-circuits BEFORE the
    # foreign-body loop, so no matrix gating applies to shared values.
    (layers / "ape" / "Eyes").mkdir(parents=True)
    (layers / "shared" / "Eyes").mkdir(parents=True)
    (layers / "shared" / "Eyes" / "Hypno.png").write_bytes(b"x")
    store = LocalLayerStore(str(layers))
    try:
        path = asyncio.run(swap_compose.resolve_layer(store, cfg, "ape", "Eyes", "Hypno"))
        assert path and path.endswith("shared/Eyes/Hypno.png")
    finally:
        asyncio.set_event_loop(asyncio.new_event_loop())


def test_resolve_layer_foreign_fallback_survives_shared_dir(tmp_path):
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(CFG)
    cfg = trait_config.load_config(str(cfg_path))
    layers = tmp_path / "layers"
    # shared/ exists but misses this value; ape misses it too; skeleton has it
    # and ape<->skeleton Head is matrix-permitted: the foreign fallback must
    # still fire (the shared hop can't break it or match shared/ as a body).
    (layers / "ape" / "Head").mkdir(parents=True)
    (layers / "shared" / "Head").mkdir(parents=True)
    (layers / "shared" / "Head" / "Halo.png").write_bytes(b"x")
    (layers / "skeleton" / "Head").mkdir(parents=True)
    (layers / "skeleton" / "Head" / "Crown.png").write_bytes(b"x")
    store = LocalLayerStore(str(layers))
    try:
        path = asyncio.run(swap_compose.resolve_layer(store, cfg, "ape", "Head", "Crown"))
        assert path and path.endswith("skeleton/Head/Crown.png")
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
