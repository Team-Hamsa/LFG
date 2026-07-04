# tests/test_traits_affinity.py
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
import sqlite3  # noqa: E402

from lfg_core import trait_config, traits  # noqa: E402
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
affinity:
  Clothing:
    "Summer Dress": [female]
"""


def _mklayers(tmp_path):
    for body in ("male", "female"):
        for t, values in {
            "Background": ["Sunset"],
            "Body": ["Straight" if body == "male" else "Curved"],
            "Clothing": ["Summer Dress", "Hoodie"],
        }.items():
            d = tmp_path / "layers" / body / t
            d.mkdir(parents=True, exist_ok=True)
            for v in values:
                (d / f"{v}.png").write_bytes(b"x")
    return str(tmp_path / "layers")


def test_mint_selection_respects_affinity(tmp_path):
    cfg_path = tmp_path / "trait_config.yaml"
    cfg_path.write_text(CFG)
    trait_config.reset_config()
    trait_config.get_config(str(cfg_path))
    store = LocalLayerStore(_mklayers(tmp_path))
    conn = sqlite3.connect(":memory:")

    class ForceDress:  # rng whose choices always favor Summer Dress if present
        def random(self):
            return 0.0

        def choices(self, population, weights=None, k=1):
            for p in population:
                if p == "Summer Dress":
                    return ["Summer Dress"]
            return [population[0]]

        def choice(self, population):
            return population[0]

        def shuffle(self, x):
            pass

    try:
        _, attrs = asyncio.run(
            traits.select_random_attributes(
                store, "male", conn=conn, network="testnet", rng=ForceDress()
            )
        )
        clothing = next(a["value"] for a in attrs if a["trait_type"] == "Clothing")
        assert clothing != "Summer Dress"  # female-only; filtered before the pick
    finally:
        trait_config.reset_config()
        asyncio.set_event_loop(asyncio.new_event_loop())
