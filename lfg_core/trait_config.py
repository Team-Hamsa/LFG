# lfg_core/trait_config.py
# Declarative trait rules: layer z-order, per-value z-overrides, per-value
# body affinity, cross-body swap matrix, and (empty-at-launch) exclusion /
# inclusion machinery. Single source: trait_config.yaml at the repo root.
# The layer *stores* stay the authority on which files exist; this config is
# the authority on which combinations are legal.

import os
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, cast

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
    exclusions: tuple[Any, ...] = ()
    inclusions: tuple[Any, ...] = ()

    def layer_order(self) -> list[str]:
        return [layer.name for layer in sorted(self.layers, key=lambda s: s.z)]

    def z_for(self, trait_type: str, value: str) -> float:
        for o in self.z_overrides:
            if o.trait_type == trait_type and o.value == value:
                return o.z
        for layer in self.layers:
            if layer.name == trait_type:
                return layer.z
        raise TraitConfigError(f"unknown layer {trait_type!r}")

    def sort_attributes(self, attrs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(attrs, key=lambda a: self.z_for(a["trait_type"], a["value"]))

    def allowed_bodies(self, trait_type: str, value: str) -> frozenset[str] | None:
        entry = self.affinity.get(trait_type, {}).get(value)
        return frozenset(entry) if entry is not None else None

    def value_allowed(self, body: str, trait_type: str, value: str) -> bool:
        allowed = self.allowed_bodies(trait_type, value)
        return allowed is None or body in allowed

    def swap_allowed(self, body_a: str, body_b: str, layer: str) -> bool:
        if body_a == body_b or layer in self.universal_layers:
            return True
        for pair in self.swap_pairs:
            if not {body_a, body_b} <= pair.bodies:
                continue
            if pair.layers is not None and layer in pair.layers:
                return True
            if pair.layers_except is not None and layer not in pair.layers_except:
                return True
        return False

    def conflicts(self, selected: list[dict[str, Any]], trait_type: str, value: str) -> bool:
        def _hits(rule: dict[str, Any], t: str, v: str) -> bool:
            values: Any = rule.get("values", "*")
            return rule["trait_type"] == t and (values == "*" or v in values)

        for entry in self.exclusions:
            entry_dict: dict = entry  # type: ignore
            src_t, src_v = entry_dict["trait_type"], entry_dict["value"]
            for rule in entry_dict.get("excludes", []):
                for sel in selected:
                    sel_dict: dict = sel  # type: ignore
                    # authored direction: candidate is the excluded side
                    if (
                        sel_dict["trait_type"] == src_t
                        and sel_dict["value"] == src_v
                        and _hits(rule, trait_type, value)
                    ):
                        return True
                    # symmetric direction: candidate is the authoring side
                    if (
                        trait_type == src_t
                        and value == src_v
                        and _hits(rule, sel_dict["trait_type"], sel_dict["value"])
                    ):
                        return True
        return False


async def validate_against_store(cfg: TraitConfig, store: Any) -> tuple[list[str], list[str]]:
    """Cross-check config claims against what the layer store actually has.
    Errors block; warnings are dir values falling back to dir-derived affinity."""
    errors: list[str] = []
    warnings: list[str] = []
    bodies = await store.list_bodies()
    tree: dict[str, dict[str, set[str]]] = {}
    for body in bodies:
        tree[body] = cast(dict[str, set[str]], {})
        for trait_type in await store.list_trait_types(body):
            tree[body][trait_type] = set(await store.list_values(body, trait_type))

    layer_names = {layer.name for layer in cfg.layers}
    seen_types = {t for types in tree.values() for t in types}
    for name in sorted(layer_names - seen_types):
        # warning, not error: the dirs are the authority on what exists; a
        # config layer with no directory anywhere just never yields a trait
        warnings.append(f"config layer {name!r} has no directory under any body")

    for trait_type, values in cfg.affinity.items():
        for value, claimed in values.items():
            present = {b for b in claimed if value in tree.get(b, {}).get(trait_type, set())}
            if not present:
                errors.append(
                    f"affinity {trait_type}/{value} claims {sorted(claimed)} "
                    "but no such file exists in any claimed body dir"
                )
    for body, types in tree.items():
        for trait_type, file_values in types.items():
            if trait_type not in layer_names:
                continue
            for value in file_values:
                if value != "None" and cfg.allowed_bodies(trait_type, value) is None:
                    warnings.append(
                        f"{body}/{trait_type}/{value} has no affinity entry "
                        "(dir-derived default applies)"
                    )
    return errors, warnings


def _check_bodies(bodies: Iterable[str], where: str) -> None:
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
        for entry in raw.get("layers") or []
    )
    names = [layer.name for layer in layers]
    if len(names) != len(set(names)):
        raise TraitConfigError("duplicate layer name in layers")
    if not layers:
        raise TraitConfigError("layers section is required")

    z_overrides = tuple(
        ZOverride(o["trait_type"], o["value"], float(o["z"])) for o in raw.get("z_overrides") or []
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
    universal = frozenset(matrix.get("universal_layers") or [])
    if not universal <= set(names):
        raise TraitConfigError("universal_layers contains unknown layer")
    pairs = []
    for p in matrix.get("pairs") or []:
        _check_bodies(p.get("bodies") or [], "swap_matrix pair")
        if ("layers" in p) == ("layers_except" in p):
            raise TraitConfigError("swap pair needs exactly one of layers or layers_except")
        pairs.append(
            SwapPair(
                bodies=frozenset(p["bodies"]),
                layers=frozenset(p["layers"]) if "layers" in p else None,
                layers_except=(frozenset(p["layers_except"]) if "layers_except" in p else None),
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
