# lfg_core/trait_config.py
# Declarative trait rules: layer z-order, per-value z-overrides, per-value
# body affinity, cross-body swap matrix, and (empty-at-launch) exclusion /
# inclusion machinery. Single source: trait_config.yaml at the repo root.
# The layer *stores* stay the authority on which files exist; this config is
# the authority on which combinations are legal.

import os
from dataclasses import dataclass, field

import yaml

VALID_BODIES = frozenset({"ape", "female", "male", "skeleton"})

DEFAULT_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "trait_config.yaml"
)


class TraitConfigError(Exception):
    pass


@dataclass(frozen=True)
class LayerSpec:
    name: str
    z: float
    shared: bool = False


@dataclass(frozen=True)
class ZOverride:
    trait_type: str
    value: str
    z: float


@dataclass(frozen=True)
class SwapPair:
    bodies: frozenset[str]
    layers: frozenset[str] | None = None
    layers_except: frozenset[str] | None = None


@dataclass(frozen=True)
class TraitConfig:
    layers: tuple[LayerSpec, ...]
    z_overrides: tuple[ZOverride, ...]
    affinity: dict[str, dict[str, list[str]]]
    universal_layers: frozenset[str]
    swap_pairs: tuple[SwapPair, ...]
    exclusions: tuple = ()
    inclusions: tuple = ()


def _check_bodies(bodies, where: str) -> None:
    unknown = set(bodies) - VALID_BODIES
    if unknown:
        raise TraitConfigError(f"unknown body {sorted(unknown)} in {where}")


def load_config(path: str) -> TraitConfig:
    with open(path) as f:
        raw = yaml.safe_load(f) or {}
    if raw.get("version") != 1:
        raise TraitConfigError("trait_config version must be 1")

    layers = tuple(
        LayerSpec(entry["name"], float(entry["z"]), bool(entry.get("shared", False)))
        for entry in raw.get("layers", [])
    )
    names = [layer.name for layer in layers]
    if len(names) != len(set(names)):
        raise TraitConfigError("duplicate layer name in layers")
    if not layers:
        raise TraitConfigError("layers section is required")

    z_overrides = tuple(
        ZOverride(o["trait_type"], o["value"], float(o["z"]))
        for o in raw.get("z_overrides", [])
    )
    for o in z_overrides:
        if o.trait_type not in names:
            raise TraitConfigError(f"z_override for unknown layer {o.trait_type!r}")

    affinity: dict[str, dict[str, list[str]]] = raw.get("affinity", {}) or {}
    for trait_type, values in affinity.items():
        if trait_type not in names:
            raise TraitConfigError(f"affinity for unknown layer {trait_type!r}")
        for value, bodies in values.items():
            _check_bodies(bodies, f"affinity {trait_type}/{value}")

    matrix = raw.get("swap_matrix", {}) or {}
    universal = frozenset(matrix.get("universal_layers", []))
    if not universal <= set(names):
        raise TraitConfigError("universal_layers contains unknown layer")
    pairs = []
    for p in matrix.get("pairs", []):
        _check_bodies(p.get("bodies", []), "swap_matrix pair")
        if ("layers" in p) == ("layers_except" in p):
            raise TraitConfigError("swap pair needs exactly one of layers or layers_except")
        pairs.append(
            SwapPair(
                bodies=frozenset(p["bodies"]),
                layers=frozenset(p["layers"]) if "layers" in p else None,
                layers_except=(
                    frozenset(p["layers_except"]) if "layers_except" in p else None
                ),
            )
        )

    return TraitConfig(
        layers=layers,
        z_overrides=z_overrides,
        affinity=affinity,
        universal_layers=universal,
        swap_pairs=tuple(pairs),
        exclusions=tuple(raw.get("exclusions", []) or ()),
        inclusions=tuple(raw.get("inclusions", []) or ()),
    )


_config: TraitConfig | None = None


def get_config(path: str | None = None) -> TraitConfig:
    global _config
    if _config is None:
        _config = load_config(path or DEFAULT_CONFIG_PATH)
    return _config


def reset_config() -> None:
    global _config
    _config = None
