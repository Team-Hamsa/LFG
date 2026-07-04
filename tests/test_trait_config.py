# tests/test_trait_config.py
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

import pytest  # noqa: E402

from lfg_core import trait_config  # noqa: E402

GOOD = """
version: 1
layers:
  - {name: Background, z: 10, shared: true}
  - {name: Back, z: 20, shared: true}
  - {name: Body, z: 30}
  - {name: Clothing, z: 40}
  - {name: Mouth, z: 50}
  - {name: Eyebrows, z: 60}
  - {name: Eyes, z: 70}
  - {name: Head, z: 80}
  - {name: Accessory, z: 90}
z_overrides:
  - {trait_type: Eyes, value: Wavy, z: 95}
affinity:
  Clothing:
    "Summer Dress": [female]
swap_matrix:
  universal_layers: [Accessory, Back]
  pairs:
    - {bodies: [ape, skeleton], layers: [Head, Clothing]}
    - {bodies: [male, female], layers_except: [Clothing]}
exclusions: []
inclusions: []
"""


def _write(tmp_path, text):
    p = tmp_path / "trait_config.yaml"
    p.write_text(text)
    return str(p)


def test_load_config_parses_all_sections(tmp_path):
    cfg = trait_config.load_config(_write(tmp_path, GOOD))
    assert [layer.name for layer in cfg.layers][:3] == ["Background", "Back", "Body"]
    assert cfg.layers[0].shared is True
    assert cfg.z_overrides[0].z == 95
    assert cfg.affinity["Clothing"]["Summer Dress"] == ["female"]
    assert "Accessory" in cfg.universal_layers


def test_load_config_rejects_duplicate_layers(tmp_path):
    bad = GOOD.replace("{name: Body, z: 30}", "{name: Background, z: 30}")
    with pytest.raises(trait_config.TraitConfigError, match="duplicate layer"):
        trait_config.load_config(_write(tmp_path, bad))


def test_load_config_rejects_unknown_body_in_affinity(tmp_path):
    bad = GOOD.replace("[female]", "[mermaid]")
    with pytest.raises(trait_config.TraitConfigError, match="unknown body"):
        trait_config.load_config(_write(tmp_path, bad))


def test_load_config_rejects_pair_with_both_layer_forms(tmp_path):
    bad = GOOD.replace(
        "layers_except: [Clothing]}", "layers_except: [Clothing], layers: [Eyes]}"
    )
    with pytest.raises(trait_config.TraitConfigError, match="layers or layers_except"):
        trait_config.load_config(_write(tmp_path, bad))


def test_get_config_singleton(tmp_path):
    trait_config.reset_config()
    path = _write(tmp_path, GOOD)
    assert trait_config.get_config(path) is trait_config.get_config()
    trait_config.reset_config()
