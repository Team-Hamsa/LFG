# tests/test_trait_config.py
# Env-guard preamble: importing lfg_core.config freezes its constants (e.g.
# IMG_PROXY_ALLOWED_BASES, LAYER_SOURCE) at import time; set the same defaults
# test_smoke.py uses so collection order can't strand them. (Copy the block
# verbatim from tests/test_server_identity_wiring.py — same keys/values.)
import asyncio
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
    bad = GOOD.replace("layers_except: [Clothing]}", "layers_except: [Clothing], layers: [Eyes]}")
    with pytest.raises(trait_config.TraitConfigError, match="layers or layers_except"):
        trait_config.load_config(_write(tmp_path, bad))


def test_load_config_rejects_pair_layers_with_unknown_layer(tmp_path):
    bad = GOOD.replace(
        "{bodies: [ape, skeleton], layers: [Head, Clothing]}",
        "{bodies: [ape, skeleton], layers: [Head, Wingspan]}",
    )
    with pytest.raises(trait_config.TraitConfigError, match="unknown layer"):
        trait_config.load_config(_write(tmp_path, bad))


def test_load_config_rejects_pair_layers_not_a_list(tmp_path):
    bad = GOOD.replace(
        "{bodies: [ape, skeleton], layers: [Head, Clothing]}",
        "{bodies: [ape, skeleton], layers: Head}",
    )
    with pytest.raises(trait_config.TraitConfigError, match="must be a list of strings"):
        trait_config.load_config(_write(tmp_path, bad))


def test_load_config_rejects_null_layers_section(tmp_path):
    # YAML parses a bare "layers:" key as None; the loader must raise
    # TraitConfigError ("layers section is required"), not a bare TypeError.
    bad = "version: 1\nlayers:\n"
    with pytest.raises(trait_config.TraitConfigError, match="layers section is required"):
        trait_config.load_config(_write(tmp_path, bad))


def test_get_config_singleton(tmp_path):
    trait_config.reset_config()
    path = _write(tmp_path, GOOD)
    assert trait_config.get_config(path) is trait_config.get_config()
    trait_config.reset_config()


def _cfg(tmp_path):
    return trait_config.load_config(_write(tmp_path, GOOD))


def test_layer_order_sorted_by_z(tmp_path):
    assert _cfg(tmp_path).layer_order() == [
        "Background",
        "Back",
        "Body",
        "Clothing",
        "Mouth",
        "Eyebrows",
        "Eyes",
        "Head",
        "Accessory",
    ]


def test_z_for_override_beats_layer_z(tmp_path):
    cfg = _cfg(tmp_path)
    assert cfg.z_for("Eyes", "Wavy") == 95
    assert cfg.z_for("Eyes", "Hypno") == 70


def test_sort_attributes_moves_override_on_top(tmp_path):
    cfg = _cfg(tmp_path)
    attrs = [
        {"trait_type": "Eyes", "value": "Wavy"},
        {"trait_type": "Body", "value": "Straight"},
        {"trait_type": "Background", "value": "Sunset"},
    ]
    assert [a["value"] for a in cfg.sort_attributes(attrs)] == [
        "Sunset",
        "Straight",
        "Wavy",
    ]


def test_affinity_queries(tmp_path):
    cfg = _cfg(tmp_path)
    assert cfg.allowed_bodies("Clothing", "Summer Dress") == frozenset({"female"})
    assert cfg.allowed_bodies("Clothing", "Hoodie") is None
    assert cfg.value_allowed("female", "Clothing", "Summer Dress")
    assert not cfg.value_allowed("male", "Clothing", "Summer Dress")
    assert cfg.value_allowed("male", "Clothing", "Hoodie")  # no entry -> dirs decide


def test_swap_allowed_matrix(tmp_path):
    cfg = _cfg(tmp_path)
    assert cfg.swap_allowed("male", "male", "Clothing")  # same body
    assert cfg.swap_allowed("ape", "female", "Accessory")  # universal layer
    assert cfg.swap_allowed("ape", "skeleton", "Head")  # pair layers
    assert not cfg.swap_allowed("ape", "skeleton", "Eyes")  # not in pair layers
    assert cfg.swap_allowed("male", "female", "Eyes")  # layers_except
    assert not cfg.swap_allowed("male", "female", "Clothing")  # excepted
    assert not cfg.swap_allowed("ape", "male", "Head")  # no pair


EXCL = GOOD.replace(
    "exclusions: []",
    """exclusions:
  - trait_type: Eyes
    value: Laser
    excludes:
      - {trait_type: Head, values: [Crown]}
""",
)


def test_conflicts_enforced_symmetrically(tmp_path):
    cfg = trait_config.load_config(_write(tmp_path, EXCL))
    laser = [{"trait_type": "Eyes", "value": "Laser"}]
    crown = [{"trait_type": "Head", "value": "Crown"}]
    assert cfg.conflicts(laser, "Head", "Crown")  # authored direction
    assert cfg.conflicts(crown, "Eyes", "Laser")  # symmetric direction
    assert not cfg.conflicts(laser, "Head", "Beanie Black")
    assert not cfg.conflicts([], "Head", "Crown")


def test_load_config_rejects_scalar_exclusion_values(tmp_path):
    # A bare scalar (`values: Crown` instead of `values: [Crown]`) would make
    # conflicts()'s `v in values` a substring test ("Cr" in "Crown" -> True)
    # instead of membership. Reject it at load time instead of letting it
    # silently misfire at query time.
    bad = GOOD.replace(
        "exclusions: []",
        """exclusions:
  - trait_type: Eyes
    value: Laser
    excludes:
      - {trait_type: Head, values: Crown}
""",
    )
    with pytest.raises(trait_config.TraitConfigError, match="values"):
        trait_config.load_config(_write(tmp_path, bad))


def test_load_config_rejects_exclusion_missing_excludes_key(tmp_path):
    bad = GOOD.replace(
        "exclusions: []",
        """exclusions:
  - trait_type: Eyes
    value: Laser
""",
    )
    with pytest.raises(trait_config.TraitConfigError, match="excludes"):
        trait_config.load_config(_write(tmp_path, bad))


def test_load_config_rejects_exclusion_entry_with_unknown_layer(tmp_path):
    bad = GOOD.replace(
        "exclusions: []",
        """exclusions:
  - trait_type: Wingspan
    value: Laser
    excludes:
      - {trait_type: Head, values: [Crown]}
""",
    )
    with pytest.raises(trait_config.TraitConfigError, match="unknown layer"):
        trait_config.load_config(_write(tmp_path, bad))


def test_load_config_rejects_exclusion_rule_with_unknown_layer(tmp_path):
    bad = GOOD.replace(
        "exclusions: []",
        """exclusions:
  - trait_type: Eyes
    value: Laser
    excludes:
      - {trait_type: Wingspan, values: [Crown]}
""",
    )
    with pytest.raises(trait_config.TraitConfigError, match="unknown layer"):
        trait_config.load_config(_write(tmp_path, bad))


def test_load_config_valid_exclusions_still_loads_and_conflicts_works(tmp_path):
    cfg = trait_config.load_config(_write(tmp_path, EXCL))
    laser = [{"trait_type": "Eyes", "value": "Laser"}]
    assert cfg.conflicts(laser, "Head", "Crown")
    assert not cfg.conflicts(laser, "Head", "Beanie Black")


def test_validate_against_store(tmp_path):
    import asyncio

    from lfg_core.layer_store import LocalLayerStore

    layers = tmp_path / "layers"
    (layers / "female" / "Clothing").mkdir(parents=True)
    (layers / "female" / "Clothing" / "Summer Dress.png").write_bytes(b"x")
    (layers / "female" / "Background").mkdir()
    (layers / "female" / "Background" / "Sunset.png").write_bytes(b"x")
    (layers / "female" / "Body").mkdir()
    (layers / "female" / "Body" / "Curved.png").write_bytes(b"x")
    (layers / "female" / "Eyes").mkdir()
    (layers / "female" / "Eyes" / "Hypno.png").write_bytes(b"x")

    cfg = trait_config.load_config(
        _write(
            tmp_path,
            GOOD.replace(
                '"Summer Dress": [female]',
                '"Summer Dress": [female]\n    "Ghost Coat": [female]',
            ),
        )
    )
    store = LocalLayerStore(str(layers))
    try:
        errors, warnings = asyncio.run(trait_config.validate_against_store(cfg, store))
    finally:
        # asyncio.run() leaves the main-thread event loop unset on exit;
        # webapp tests later in full-suite order still rely on the legacy
        # asyncio.get_event_loop() auto-create, so restore a loop for them.
        asyncio.set_event_loop(asyncio.new_event_loop())
    assert any("Ghost Coat" in e for e in errors)  # claimed, no file
    assert any("Hypno" in w for w in warnings)  # file, no entry
    assert any("Accessory" in w for w in warnings)  # layer-with-no-dir warning path


def test_validate_against_store_empty_tree_skips_checks(tmp_path):
    # layers/** is gitignored, so a clean checkout (CI) has a `layers/` dir
    # with no body subdirectories at all. That must NOT produce hundreds of
    # false "no such file" errors -- it should skip cross-checks and return
    # a single explanatory warning instead.
    from lfg_core.layer_store import LocalLayerStore

    empty_layers = tmp_path / "layers"
    empty_layers.mkdir()
    cfg = trait_config.load_config(_write(tmp_path, GOOD))
    store = LocalLayerStore(str(empty_layers))
    try:
        errors, warnings = asyncio.run(trait_config.validate_against_store(cfg, store))
    finally:
        asyncio.set_event_loop(asyncio.new_event_loop())
    assert errors == []
    assert len(warnings) == 1
    assert "empty" in warnings[0]
    assert "skipped" in warnings[0]


def test_validate_cli_exit_codes(tmp_path, capsys):
    from scripts.validate_trait_config import main

    layers = tmp_path / "layers"
    (layers / "female" / "Background").mkdir(parents=True)
    (layers / "female" / "Background" / "Sunset.png").write_bytes(b"x")
    (layers / "female" / "Body").mkdir()
    (layers / "female" / "Body" / "Curved.png").write_bytes(b"x")
    (layers / "female" / "Eyes").mkdir()
    (layers / "female" / "Eyes" / "Wavy.png").write_bytes(b"x")
    (layers / "female" / "Clothing").mkdir()
    (layers / "female" / "Clothing" / "Summer Dress.png").write_bytes(b"x")

    good = _write(tmp_path, GOOD)
    try:
        assert main(["--config", good, "--layers-dir", str(layers)]) == 0
    finally:
        asyncio.set_event_loop(asyncio.new_event_loop())

    bad = tmp_path / "bad.yaml"
    bad.write_text(GOOD.replace("[female]", "[male]"))  # claims male, file is female-only
    try:
        assert main(["--config", str(bad), "--layers-dir", str(layers)]) == 1
    finally:
        asyncio.set_event_loop(asyncio.new_event_loop())


def test_validate_cli_missing_config_prints_error_and_exits_1(tmp_path, capsys):
    from scripts.validate_trait_config import main

    missing = str(tmp_path / "does-not-exist.yaml")
    assert main(["--config", missing, "--layers-dir", str(tmp_path)]) == 1
    assert "ERROR" in capsys.readouterr().out


def test_validate_cli_malformed_yaml_exits_1_no_traceback(tmp_path, capsys):
    from scripts.validate_trait_config import main

    bad = tmp_path / "malformed.yaml"
    # Unbalanced flow mapping -> yaml.YAMLError (a YAMLError subclass), not
    # a TraitConfigError -- must be caught by the same consolidated handler.
    bad.write_text("version: 1\nlayers: [{name: Background, z: 10\n")
    assert main(["--config", str(bad), "--layers-dir", str(tmp_path)]) == 1
    assert "ERROR" in capsys.readouterr().out


def test_validate_cli_nonexistent_layers_dir_exits_1(tmp_path, capsys):
    from scripts.validate_trait_config import main

    good = _write(tmp_path, GOOD)
    missing_layers = str(tmp_path / "does-not-exist")
    try:
        assert main(["--config", good, "--layers-dir", missing_layers]) == 1
    finally:
        asyncio.set_event_loop(asyncio.new_event_loop())
    assert "ERROR" in capsys.readouterr().out


def test_default_config_parity_with_legacy_constants():
    from lfg_core import ape_face
    from lfg_core.swap_meta import TRAIT_ORDER

    trait_config.reset_config()
    cfg = trait_config.get_config()  # loads repo-root trait_config.yaml
    assert cfg.layer_order() == TRAIT_ORDER
    for top in ape_face.TOP_TRAITS:
        assert cfg.z_for(top["trait_type"], top["value"]) > max(layer.z for layer in cfg.layers), (
            f"{top} must render above all layers"
        )
    assert cfg.universal_layers == frozenset({"Accessory", "Back"})
    assert cfg.swap_allowed("ape", "skeleton", "Clothing")
    assert cfg.swap_allowed("male", "female", "Eyes")
    assert not cfg.swap_allowed("male", "female", "Clothing")
    trait_config.reset_config()


def test_compose_ordering_uses_config_sort():
    import inspect

    from lfg_core import swap_compose

    src = inspect.getsource(swap_compose)
    assert "sort_attributes" in src, "compose must order layers via trait_config"


def test_compose_nft_resolves_top_trait_after_accessory(tmp_path, monkeypatch):
    """Behavioral check for the same rule: a stub store records the order
    compose_nft resolves layers in. A Wavy-Eyes attr (z_override 95) must
    resolve AFTER Accessory (z 90) — i.e. ordering flows from trait_config's
    z-order, not the legacy TRAIT_ORDER position (where Eyes sorts before
    Accessory). Non-ape body keeps the stub store simple (no resolve_asset
    needed for the ape nose/mask structural assets)."""
    from lfg_core import swap_compose

    class _RecordingStore:
        def __init__(self) -> None:
            self.order: list[str] = []

        async def resolve(self, body, trait_type, value):
            self.order.append(trait_type)
            return f"/fake/{trait_type}.png"

    def fake_run(files, output_path, is_video):
        with open(output_path, "wb") as f:
            f.write(b"x")

    monkeypatch.setattr(swap_compose, "_run_ffmpeg", fake_run)

    store = _RecordingStore()
    attrs = [
        {"trait_type": "Accessory", "value": "Hat"},
        {"trait_type": "Eyes", "value": "Wavy"},
    ]

    trait_config.reset_config()
    try:
        asyncio.run(swap_compose.compose_nft(attrs, "female", store, "out", out_dir=str(tmp_path)))
    finally:
        asyncio.set_event_loop(asyncio.new_event_loop())
        trait_config.reset_config()

    assert store.order.index("Eyes") > store.order.index("Accessory")
