# tests/test_cross_body_resolve.py
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
from lfg_core.layer_store import LocalLayerStore  # noqa: E402

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


def test_resolve_layer_falls_back_to_permitted_foreign_dir(tmp_path):
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(CFG)
    cfg = trait_config.load_config(str(cfg_path))
    d = tmp_path / "layers" / "skeleton" / "Head"
    d.mkdir(parents=True)
    (d / "Crown.png").write_bytes(b"x")
    store = LocalLayerStore(str(tmp_path / "layers"))

    try:
        # ape has no Crown file; skeleton does, and ape<->skeleton Head is permitted
        path = asyncio.run(swap_compose.resolve_layer(store, cfg, "ape", "Head", "Crown"))
        assert path and path.endswith("skeleton/Head/Crown.png")
        # Eyes is not matrix-permitted for ape<->skeleton: no fallback
        (tmp_path / "layers" / "skeleton" / "Eyes").mkdir()
        (tmp_path / "layers" / "skeleton" / "Eyes" / "Hypno.png").write_bytes(b"x")
        assert asyncio.run(swap_compose.resolve_layer(store, cfg, "ape", "Eyes", "Hypno")) is None
    finally:
        # asyncio.run() leaves the main-thread event loop unset on exit;
        # webapp tests later in full-suite order still rely on the legacy
        # asyncio.get_event_loop() auto-create, so restore a loop for them.
        asyncio.set_event_loop(asyncio.new_event_loop())
