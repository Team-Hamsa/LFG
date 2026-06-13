# Trait Selection Rules Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a declarative, data-driven trait-selection rules engine (layer order, per-value z-index overrides, directional-authored/symmetrically-enforced exclusions, directional inclusions, inert tag/group-rule stubs) driven by a single `trait_config.yaml`.

**Architecture:** A new `lfg_core/trait_config.py` owns loading + validating + querying the config (YAML or JSON, format-agnostic by extension). It exposes a `TraitConfig` object with helpers for layer order, effective z-index, candidate filtering (symmetric exclusions), forced values (inclusions), selection order, and a no-op group-rule hook. The mint selection (`traits.py`), compositor (`swap_compose.py`), and canonical-order constant (`swap_meta.py`) are rewired to source from `TraitConfig` while keeping their public names as backward-compat shims. Fail loudly on cyclic rules and over-constrained layers.

**Tech Stack:** Python 3, PyYAML, pytest, existing `lfg_core` (rarity, layer_store).

---

## File Structure

- **Create `lfg_core/trait_config.py`** — config loader/validator + `TraitConfig` query API. The only module that parses the config file.
- **Create `trait_config.yaml`** (repo project root, `LFG/trait_config.yaml`) — shipped default config reproducing current behavior.
- **Modify `lfg_core/swap_meta.py`** — `TRAIT_ORDER` becomes a config-backed shim.
- **Modify `lfg_core/swap_compose.py`** — `_ordered_traits` sorts by effective z-index; `TOP_TRAITS` becomes a config-backed shim.
- **Modify `lfg_core/traits.py`** — `select_random_attributes` loops over `selection_order()`, filters candidates, applies forced values + group-rule hook.
- **Modify `requirements.txt`** — add `PyYAML`.
- **Create `tests/test_trait_config.py`** — config loading/validation/query unit tests.
- **Create `tests/test_traits_rules.py`** — selection-integration tests (exclusion/inclusion/forced-value behavior).

Each task is TDD: write failing test → run (fail) → implement → run (pass) → commit. Run all tests with `python -m pytest -q` from `LFG/`.

> **Worktree note:** the project root is the `Team-Hamsa/Mint-Bot` repo, with code under `LFG/`. Run `pytest` and `git` from `/home/hamsa/LFG` (or repo root). All paths below are relative to `LFG/`.

---

## Task 1: Add PyYAML dependency

**Files:**
- Modify: `requirements.txt`

- [ ] **Step 1: Add PyYAML to requirements**

Add this line to `requirements.txt` (alphabetical-ish near other libs is fine):

```
PyYAML>=6.0
```

- [ ] **Step 2: Install it**

Run: `pip install "PyYAML>=6.0"`
Expected: `Successfully installed PyYAML-6.x` (or "already satisfied").

- [ ] **Step 3: Verify import**

Run: `python -c "import yaml; print(yaml.__version__)"`
Expected: prints a version like `6.0.1`.

- [ ] **Step 4: Commit**

```bash
git add requirements.txt
git commit -m "build: add PyYAML for trait config loading"
```

---

## Task 2: Config dataclasses + format-agnostic loader (no rules yet)

Build the parsing skeleton: read YAML/JSON by extension, parse `layers` into an ordered list, expose `layer_order` and `effective_z`. Validation of layers included.

**Files:**
- Create: `lfg_core/trait_config.py`
- Test: `tests/test_trait_config.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_trait_config.py`:

```python
# Tests for the declarative trait-selection config (lfg_core/trait_config.py).
import os
import sys
import json
import textwrap

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# Dummy env so lfg_core.config import (pulled in transitively) doesn't fail.
os.environ.setdefault("DISCORD_BOT_TOKEN", "x")
os.environ.setdefault("XUMM_API_KEY", "x")
os.environ.setdefault("XUMM_API_SECRET", "x")
os.environ.setdefault("BUNNY_CDN_ACCESS_KEY", "x")
os.environ.setdefault("BUNNY_CDN_STORAGE_ZONE", "x")
os.environ.setdefault("SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2")
os.environ.setdefault("TOKEN_ISSUER_ADDRESS", "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
os.environ.setdefault("TOKEN_CURRENCY_HEX", "4C46474F00000000000000000000000000000000")
os.environ.setdefault("XRPL_NETWORK", "testnet")

from lfg_core import trait_config  # noqa: E402

MINIMAL_YAML = textwrap.dedent("""
    layers:
      - { z: 0, trait_type: Background }
      - { z: 1, trait_type: Body }
      - { z: 2, trait_type: Eyes }
    z_overrides:
      - { trait_type: Eyes, value: "Laser Eyes", z: 999 }
""")


def write(tmp_path, name, content):
    p = tmp_path / name
    p.write_text(content)
    return str(p)


def test_layer_order_sorted_by_z(tmp_path):
    cfg = trait_config.load_config(write(tmp_path, "c.yaml", MINIMAL_YAML))
    assert cfg.layer_order == ["Background", "Body", "Eyes"]


def test_effective_z_uses_layer_default(tmp_path):
    cfg = trait_config.load_config(write(tmp_path, "c.yaml", MINIMAL_YAML))
    assert cfg.effective_z("Body", "anything") == 1


def test_effective_z_uses_override(tmp_path):
    cfg = trait_config.load_config(write(tmp_path, "c.yaml", MINIMAL_YAML))
    assert cfg.effective_z("Eyes", "Laser Eyes") == 999
    assert cfg.effective_z("Eyes", "Normal") == 2


def test_json_and_yaml_equivalent(tmp_path):
    data = {
        "layers": [
            {"z": 0, "trait_type": "Background"},
            {"z": 1, "trait_type": "Body"},
            {"z": 2, "trait_type": "Eyes"},
        ],
        "z_overrides": [{"trait_type": "Eyes", "value": "Laser Eyes", "z": 999}],
    }
    jcfg = trait_config.load_config(write(tmp_path, "c.json", json.dumps(data)))
    ycfg = trait_config.load_config(write(tmp_path, "c.yaml", MINIMAL_YAML))
    assert jcfg.layer_order == ycfg.layer_order
    assert jcfg.effective_z("Eyes", "Laser Eyes") == 999


def test_duplicate_trait_type_raises(tmp_path):
    bad = "layers:\n  - { z: 0, trait_type: Body }\n  - { z: 1, trait_type: Body }\n"
    with pytest.raises(trait_config.ConfigError):
        trait_config.load_config(write(tmp_path, "c.yaml", bad))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_trait_config.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'lfg_core.trait_config'`.

- [ ] **Step 3: Write minimal implementation**

Create `lfg_core/trait_config.py`:

```python
# lfg_core/trait_config.py
# Declarative trait-selection config: layer order, per-value z-index overrides,
# exclusion/inclusion rules, tags, and a group-rule stub. Single source of truth
# for the layer system, loaded from YAML or JSON (chosen by file extension).
#
# This module is the ONLY place that parses the raw config; everything else
# consumes the validated TraitConfig object.

import os
import json
import logging

import yaml

log = logging.getLogger(__name__)

# Default config path (relative to the LFG project root). Override with env
# TRAIT_CONFIG_PATH. If the file is absent, a built-in default is used.
DEFAULT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "trait_config.yaml")

ON_TOP_Z = 999  # sentinel z-index meaning "render above everything"


class ConfigError(ValueError):
    """Raised when the trait config is structurally invalid."""


def _load_raw(path):
    with open(path, "r") as f:
        text = f.read()
    if path.endswith((".yaml", ".yml")):
        return yaml.safe_load(text) or {}
    if path.endswith(".json"):
        return json.loads(text) if text.strip() else {}
    raise ConfigError(f"Unsupported config extension: {path}")


class TraitConfig:
    def __init__(self, raw):
        self._raw = raw or {}
        self._layers = self._parse_layers(self._raw.get("layers", []))
        self._layer_z = {l["trait_type"]: l["z"] for l in self._layers}
        self._z_overrides = self._parse_z_overrides(self._raw.get("z_overrides", []))

    # ---- parsing / validation ----
    def _parse_layers(self, layers):
        if not isinstance(layers, list) or not layers:
            raise ConfigError("config 'layers' must be a non-empty list")
        seen_tt, seen_z = set(), set()
        out = []
        for entry in layers:
            if not isinstance(entry, dict) or "trait_type" not in entry or "z" not in entry:
                raise ConfigError(f"each layer needs trait_type and z: {entry!r}")
            tt = str(entry["trait_type"])
            z = entry["z"]
            if not isinstance(z, (int, float)) or isinstance(z, bool):
                raise ConfigError(f"layer z must be a number: {entry!r}")
            if tt in seen_tt:
                raise ConfigError(f"duplicate layer trait_type: {tt}")
            if z in seen_z:
                raise ConfigError(f"duplicate layer z: {z}")
            seen_tt.add(tt)
            seen_z.add(z)
            out.append({"trait_type": tt, "z": z})
        out.sort(key=lambda l: l["z"])
        return out

    def _parse_z_overrides(self, overrides):
        out = {}
        for o in overrides or []:
            tt = str(o["trait_type"])
            if tt not in self._layer_z:
                raise ConfigError(f"z_override references unknown layer: {tt}")
            z = o["z"]
            if not isinstance(z, (int, float)) or isinstance(z, bool):
                raise ConfigError(f"z_override z must be a number: {o!r}")
            out[(tt, str(o["value"]))] = z
        return out

    # ---- query API ----
    @property
    def layer_order(self):
        return [l["trait_type"] for l in self._layers]

    def effective_z(self, trait_type, value):
        return self._z_overrides.get((trait_type, str(value)),
                                     self._layer_z[trait_type])


def load_config(path=None):
    """Load and validate the trait config. Resolution order:
    explicit path arg -> env TRAIT_CONFIG_PATH -> DEFAULT_PATH -> built-in default."""
    path = path or os.environ.get("TRAIT_CONFIG_PATH") or DEFAULT_PATH
    if not os.path.exists(path):
        log.warning("trait config %s not found; using built-in default", path)
        return TraitConfig(_BUILTIN_DEFAULT)
    return TraitConfig(_load_raw(path))


# Built-in default reproduces current behavior (TRAIT_ORDER + on-top traits).
_BUILTIN_DEFAULT = {
    "layers": [
        {"z": 0, "trait_type": "Background"},
        {"z": 1, "trait_type": "Back"},
        {"z": 2, "trait_type": "Body"},
        {"z": 3, "trait_type": "Clothing"},
        {"z": 4, "trait_type": "Mouth"},
        {"z": 5, "trait_type": "Eyebrows"},
        {"z": 6, "trait_type": "Eyes"},
        {"z": 7, "trait_type": "Head"},
        {"z": 8, "trait_type": "Accessory"},
    ],
    "z_overrides": [
        {"trait_type": "Eyes", "value": "Wavy", "z": ON_TOP_Z},
        {"trait_type": "Mouth", "value": "Rainbow Puke", "z": ON_TOP_Z},
        {"trait_type": "Eyes", "value": "Laser Eyes", "z": ON_TOP_Z},
        {"trait_type": "Eyes", "value": "Laser", "z": ON_TOP_Z},
    ],
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_trait_config.py -q`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add lfg_core/trait_config.py tests/test_trait_config.py
git commit -m "feat(traits): config loader with layer order and z-index"
```

---

## Task 3: Exclusion rules (symmetric enforcement)

Add `exclusions` parsing + `candidates_after(chosen, trait_type, all_values)` that filters a layer's candidate list against already-chosen attributes, enforcing exclusions symmetrically and supporting value→value, value→values, and value→layer (`"*"`).

**Files:**
- Modify: `lfg_core/trait_config.py`
- Test: `tests/test_trait_config.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_trait_config.py`:

```python
EXCL_YAML = textwrap.dedent("""
    layers:
      - { z: 0, trait_type: Background }
      - { z: 1, trait_type: Clothing }
      - { z: 2, trait_type: Eyes }
      - { z: 3, trait_type: Head }
    exclusions:
      - when:    { trait_type: Head, value: "Full Helmet" }
        exclude: { trait_type: Eyes, value: "*" }
      - when:    { trait_type: Background, value: "Lava" }
        exclude: { trait_type: Clothing, values: ["Ice Suit", "Parka"] }
""")


def test_value_to_layer_exclusion_forward(tmp_path):
    cfg = trait_config.load_config(write(tmp_path, "c.yaml", EXCL_YAML))
    chosen = [{"trait_type": "Head", "value": "Full Helmet"}]
    assert cfg.candidates_after(chosen, "Eyes", ["Normal", "Wavy"]) == []


def test_value_to_layer_exclusion_symmetric(tmp_path):
    # Eyes picked first; Head=Full Helmet must then be filtered out.
    cfg = trait_config.load_config(write(tmp_path, "c.yaml", EXCL_YAML))
    chosen = [{"trait_type": "Eyes", "value": "Normal"}]
    assert cfg.candidates_after(chosen, "Head", ["Full Helmet", "Cap"]) == ["Cap"]


def test_value_to_values_exclusion(tmp_path):
    cfg = trait_config.load_config(write(tmp_path, "c.yaml", EXCL_YAML))
    chosen = [{"trait_type": "Background", "value": "Lava"}]
    assert cfg.candidates_after(chosen, "Clothing",
                                ["Ice Suit", "Parka", "Tee"]) == ["Tee"]


def test_no_exclusion_passes_all(tmp_path):
    cfg = trait_config.load_config(write(tmp_path, "c.yaml", EXCL_YAML))
    chosen = [{"trait_type": "Background", "value": "Sky"}]
    assert cfg.candidates_after(chosen, "Clothing",
                                ["Ice Suit", "Tee"]) == ["Ice Suit", "Tee"]


def test_exclusion_unknown_trait_type_raises(tmp_path):
    bad = textwrap.dedent("""
        layers:
          - { z: 0, trait_type: Eyes }
        exclusions:
          - when:    { trait_type: Nope, value: X }
            exclude: { trait_type: Eyes, value: Y }
    """)
    with pytest.raises(trait_config.ConfigError):
        trait_config.load_config(write(tmp_path, "c.yaml", bad))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_trait_config.py -q`
Expected: FAIL — `AttributeError: 'TraitConfig' object has no attribute 'candidates_after'`.

- [ ] **Step 3: Write minimal implementation**

In `lfg_core/trait_config.py`, parse exclusions in `__init__` and add the helper. Add to `__init__` after the `_z_overrides` line:

```python
        self._exclusions = self._parse_exclusions(self._raw.get("exclusions", []))
```

Add a helper used by exclusion/inclusion parsing (place above `_parse_exclusions`):

```python
    def _check_tt(self, tt, where):
        if tt not in self._layer_z:
            raise ConfigError(f"{where} references unknown trait_type: {tt}")
        return tt

    @staticmethod
    def _target_values(spec, where):
        has_value = "value" in spec
        has_values = "values" in spec
        if has_value == has_values:  # exactly one required
            raise ConfigError(f"{where} needs exactly one of value/values: {spec!r}")
        if has_values:
            return [str(v) for v in spec["values"]]
        return [str(spec["value"])]  # "*" stays "*"
```

Add the exclusion parser + query method:

```python
    def _parse_exclusions(self, rules):
        # Normalize to directional pairs, then store BOTH directions for
        # symmetric enforcement. Each stored rule: (a_tt, a_val) forbids
        # (b_tt, b_vals) where "*" means the whole layer.
        out = []
        for r in rules or []:
            when, exclude = r.get("when"), r.get("exclude")
            if not isinstance(when, dict) or not isinstance(exclude, dict):
                raise ConfigError(f"exclusion needs when/exclude dicts: {r!r}")
            a_tt = self._check_tt(str(when["trait_type"]), "exclusion.when")
            a_val = str(when["value"])
            b_tt = self._check_tt(str(exclude["trait_type"]), "exclusion.exclude")
            b_vals = self._target_values(exclude, "exclusion.exclude")
            out.append((a_tt, a_val, b_tt, b_vals))
        return out

    def _value_blocked_by(self, trait_type, value, chosen_tt, chosen_val):
        """True if (trait_type,value) conflicts with an already-chosen
        (chosen_tt,chosen_val) under any exclusion, in either direction."""
        for a_tt, a_val, b_tt, b_vals in self._exclusions:
            # forward: chosen is the trigger, candidate is the target
            if chosen_tt == a_tt and chosen_val == a_val and trait_type == b_tt:
                if "*" in b_vals or value in b_vals:
                    return True
            # reverse (symmetric): candidate is the trigger, chosen is the target
            if trait_type == a_tt and value == a_val and chosen_tt == b_tt:
                if "*" in b_vals or chosen_val in b_vals:
                    return True
        return False

    def candidates_after(self, chosen, trait_type, all_values):
        """Filter all_values for `trait_type`, removing any that conflict with
        an already-chosen attribute. `chosen` is a list of
        {trait_type, value} dicts."""
        result = []
        for value in all_values:
            blocked = any(
                self._value_blocked_by(trait_type, str(value),
                                       c["trait_type"], str(c["value"]))
                for c in chosen)
            if not blocked:
                result.append(value)
        return result
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_trait_config.py -q`
Expected: PASS (10 passed).

- [ ] **Step 5: Commit**

```bash
git add lfg_core/trait_config.py tests/test_trait_config.py
git commit -m "feat(traits): symmetric exclusion rule enforcement"
```

---

## Task 4: Inclusion rules (directional forced assignment) + conflict validation

Add `inclusions` parsing, `forced_value(chosen, trait_type)`, and the load-time conflict check (an inclusion and exclusion targeting the same `(trait_type, value)` is an error).

**Files:**
- Modify: `lfg_core/trait_config.py`
- Test: `tests/test_trait_config.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_trait_config.py`:

```python
INCL_YAML = textwrap.dedent("""
    layers:
      - { z: 0, trait_type: Clothing }
      - { z: 1, trait_type: Head }
    inclusions:
      - when:    { trait_type: Clothing, value: "Angel Wings" }
        include: { trait_type: Head,     value: "Halo" }
""")


def test_inclusion_forces_value(tmp_path):
    cfg = trait_config.load_config(write(tmp_path, "c.yaml", INCL_YAML))
    chosen = [{"trait_type": "Clothing", "value": "Angel Wings"}]
    assert cfg.forced_value(chosen, "Head") == "Halo"


def test_inclusion_is_directional(tmp_path):
    cfg = trait_config.load_config(write(tmp_path, "c.yaml", INCL_YAML))
    chosen = [{"trait_type": "Head", "value": "Halo"}]
    assert cfg.forced_value(chosen, "Clothing") is None


def test_no_inclusion_returns_none(tmp_path):
    cfg = trait_config.load_config(write(tmp_path, "c.yaml", INCL_YAML))
    chosen = [{"trait_type": "Clothing", "value": "Tee"}]
    assert cfg.forced_value(chosen, "Head") is None


def test_inclusion_exclusion_conflict_raises(tmp_path):
    bad = textwrap.dedent("""
        layers:
          - { z: 0, trait_type: Clothing }
          - { z: 1, trait_type: Head }
        exclusions:
          - when:    { trait_type: Clothing, value: "Angel Wings" }
            exclude: { trait_type: Head,     value: "Halo" }
        inclusions:
          - when:    { trait_type: Clothing, value: "Angel Wings" }
            include: { trait_type: Head,     value: "Halo" }
    """)
    with pytest.raises(trait_config.ConfigError):
        trait_config.load_config(write(tmp_path, "c.yaml", bad))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_trait_config.py -q`
Expected: FAIL — `AttributeError: ... has no attribute 'forced_value'`.

- [ ] **Step 3: Write minimal implementation**

In `__init__`, after the exclusions line add:

```python
        self._inclusions = self._parse_inclusions(self._raw.get("inclusions", []))
        self._validate_no_incl_excl_conflict()
```

Add the methods:

```python
    def _parse_inclusions(self, rules):
        # Directional: (trigger_tt, trigger_val) -> (target_tt, target_val)
        out = []
        for r in rules or []:
            when, include = r.get("when"), r.get("include")
            if not isinstance(when, dict) or not isinstance(include, dict):
                raise ConfigError(f"inclusion needs when/include dicts: {r!r}")
            a_tt = self._check_tt(str(when["trait_type"]), "inclusion.when")
            a_val = str(when["value"])
            b_tt = self._check_tt(str(include["trait_type"]), "inclusion.include")
            b_val = str(include["value"])
            out.append((a_tt, a_val, b_tt, b_val))
        return out

    def _validate_no_incl_excl_conflict(self):
        excl_targets = set()
        for a_tt, a_val, b_tt, b_vals in self._exclusions:
            for v in b_vals:
                excl_targets.add((a_tt, a_val, b_tt, v))
        for a_tt, a_val, b_tt, b_val in self._inclusions:
            if ((a_tt, a_val, b_tt, b_val) in excl_targets
                    or (a_tt, a_val, b_tt, "*") in excl_targets):
                raise ConfigError(
                    f"inclusion and exclusion both target "
                    f"{a_tt}={a_val} -> {b_tt}={b_val}")

    def forced_value(self, chosen, trait_type):
        """If an inclusion's trigger is in `chosen` and targets `trait_type`,
        return the forced value; else None. Directional only."""
        chosen_set = {(c["trait_type"], str(c["value"])) for c in chosen}
        for a_tt, a_val, b_tt, b_val in self._inclusions:
            if b_tt == trait_type and (a_tt, a_val) in chosen_set:
                return b_val
        return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_trait_config.py -q`
Expected: PASS (14 passed).

- [ ] **Step 5: Commit**

```bash
git add lfg_core/trait_config.py tests/test_trait_config.py
git commit -m "feat(traits): directional inclusion rules + conflict validation"
```

---

## Task 5: Selection order (triggers before targets; cycle = fail loudly)

Add `selection_order()` returning a pick order where exclusion/inclusion trigger layers precede their target layers. Cyclic constraints raise at load.

**Files:**
- Modify: `lfg_core/trait_config.py`
- Test: `tests/test_trait_config.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_trait_config.py`:

```python
def test_selection_order_default_is_layer_order(tmp_path):
    cfg = trait_config.load_config(write(tmp_path, "c.yaml", MINIMAL_YAML))
    assert cfg.selection_order() == ["Background", "Body", "Eyes"]


def test_selection_order_trigger_before_target(tmp_path):
    # Head excludes Eyes -> Head (trigger) must come before Eyes (target),
    # overriding the default z order where Eyes precedes Head.
    cfg = trait_config.load_config(write(tmp_path, "c.yaml", EXCL_YAML))
    order = cfg.selection_order()
    assert order.index("Head") < order.index("Eyes")
    assert order.index("Background") < order.index("Clothing")


def test_selection_order_cycle_raises(tmp_path):
    bad = textwrap.dedent("""
        layers:
          - { z: 0, trait_type: A }
          - { z: 1, trait_type: B }
        exclusions:
          - when:    { trait_type: A, value: x }
            exclude: { trait_type: B, value: y }
        inclusions:
          - when:    { trait_type: B, value: y }
            include: { trait_type: A, value: z }
    """)
    with pytest.raises(trait_config.ConfigError):
        trait_config.load_config(write(tmp_path, "c.yaml", bad))
```

Note: the cycle test uses an *exclusion* A→B (which only constrains ordering one way: trigger A before target B) combined with an *inclusion* B→A (trigger B before target A), producing A→B and B→A. See Step 3 for which rule types contribute ordering edges.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_trait_config.py -q`
Expected: FAIL — `AttributeError: ... has no attribute 'selection_order'`.

- [ ] **Step 3: Write minimal implementation**

Ordering edges (trigger_layer -> target_layer) come from BOTH rule types:
exclusions need the trigger picked first so the symmetric filter has the trigger
in `chosen`; inclusions need the trigger first so `forced_value` fires. Build the
edge set, topologically sort, and tie-break by layer z so the default order is
preserved when unconstrained. Validate the cycle at load time.

In `__init__`, after `_validate_no_incl_excl_conflict()`:

```python
        self._selection_order = self._compute_selection_order()
```

Add:

```python
    def _ordering_edges(self):
        edges = set()  # (trigger_tt, target_tt)
        for a_tt, _a_val, b_tt, _b_vals in self._exclusions:
            if a_tt != b_tt:
                edges.add((a_tt, b_tt))
        for a_tt, _a_val, b_tt, _b_val in self._inclusions:
            if a_tt != b_tt:
                edges.add((a_tt, b_tt))
        return edges

    def _compute_selection_order(self):
        order = self.layer_order  # already z-sorted
        edges = self._ordering_edges()
        if not edges:
            return list(order)
        # Kahn's algorithm with z-order tie-break (stable, preserves default).
        indeg = {tt: 0 for tt in order}
        adj = {tt: [] for tt in order}
        for a, b in edges:
            adj[a].append(b)
            indeg[b] += 1
        ready = [tt for tt in order if indeg[tt] == 0]  # order is z-sorted
        result = []
        while ready:
            tt = ready.pop(0)
            result.append(tt)
            for nxt in adj[tt]:
                indeg[nxt] -= 1
                if indeg[nxt] == 0:
                    # insert keeping z-order among ready nodes
                    ready.append(nxt)
                    ready.sort(key=lambda t: order.index(t))
        if len(result) != len(order):
            raise ConfigError(
                "trait rules form a cycle; selection order cannot be derived")
        return result

    def selection_order(self):
        return list(self._selection_order)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_trait_config.py -q`
Expected: PASS (17 passed).

- [ ] **Step 5: Commit**

```bash
git add lfg_core/trait_config.py tests/test_trait_config.py
git commit -m "feat(traits): selection order derivation with cycle detection"
```

---

## Task 6: Group-rule stub + tags

Add inert `tags` storage and a no-op `apply_group_rules` hook with a docstring describing the future implementation.

**Files:**
- Modify: `lfg_core/trait_config.py`
- Test: `tests/test_trait_config.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_trait_config.py`:

```python
TAGS_YAML = textwrap.dedent("""
    layers:
      - { z: 0, trait_type: Background }
    tags:
      Background:
        Lava: { color: warm, palette: neon }
""")


def test_tags_accessible(tmp_path):
    cfg = trait_config.load_config(write(tmp_path, "c.yaml", TAGS_YAML))
    assert cfg.tags_for("Background", "Lava") == {"color": "warm", "palette": "neon"}
    assert cfg.tags_for("Background", "Sky") == {}


def test_apply_group_rules_is_passthrough(tmp_path):
    cfg = trait_config.load_config(write(tmp_path, "c.yaml", TAGS_YAML))
    attrs = [{"trait_type": "Background", "value": "Lava"}]
    assert cfg.apply_group_rules(attrs) == attrs
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_trait_config.py -q`
Expected: FAIL — `AttributeError: ... has no attribute 'tags_for'`.

- [ ] **Step 3: Write minimal implementation**

In `__init__`, after `_selection_order`:

```python
        self._tags = self._raw.get("tags", {}) or {}
        self._group_rules = self._raw.get("group_rules", []) or []
```

Add:

```python
    def tags_for(self, trait_type, value):
        """Arbitrary tags attached to a trait value (e.g. color/palette).
        Inert in v1 — provided for future group_rules. Returns {} if none."""
        return dict(self._tags.get(trait_type, {}).get(str(value), {}))

    def apply_group_rules(self, attributes):
        """Apply collection-wide group constraints to a finished attribute set.

        NO-OP in v1 — returns `attributes` unchanged.

        Future implementation sketch: `group_rules` would describe color-theory
        constraints over the tags from `tags_for`, e.g.
            - {type: must_match, tag: palette}  # all visible traits share a palette
            - {type: must_differ, tag: color, between: [Background, Clothing]}
        This hook would, after selection, validate/repair the attribute set so it
        satisfies those constraints (re-rolling or substituting offending values,
        respecting exclusions/inclusions). Until the semantics are finalized it is
        intentionally a pass-through so callers can wire it in now.
        """
        return attributes
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_trait_config.py -q`
Expected: PASS (19 passed).

- [ ] **Step 5: Commit**

```bash
git add lfg_core/trait_config.py tests/test_trait_config.py
git commit -m "feat(traits): inert tags + group_rules stub hook"
```

---

## Task 7: Ship the default `trait_config.yaml`

Create the real config file at the project root, matching the built-in default, with comments and the Girl's Best Friend / template examples from the spec.

**Files:**
- Create: `trait_config.yaml`
- Test: `tests/test_trait_config.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_trait_config.py`:

```python
def test_shipped_default_config_loads_and_matches_builtin():
    # The shipped file at the project root must load and reproduce the
    # built-in default layer order + on-top z-overrides.
    cfg = trait_config.load_config(trait_config.DEFAULT_PATH)
    assert cfg.layer_order == ["Background", "Back", "Body", "Clothing",
                               "Mouth", "Eyebrows", "Eyes", "Head", "Accessory"]
    assert cfg.effective_z("Eyes", "Laser Eyes") == trait_config.ON_TOP_Z
    assert cfg.effective_z("Mouth", "Rainbow Puke") == trait_config.ON_TOP_Z
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_trait_config.py::test_shipped_default_config_loads_and_matches_builtin -q`
Expected: FAIL — file not found warning + `layer_order` mismatch, or assertion error (the built-in fallback would actually pass; to ensure the file is real, this test loads `DEFAULT_PATH` explicitly and the absence makes `load_config` warn-and-fallback — assertion on Girl's Best Friend below forces a real file). To make the failure unambiguous, also assert the override that is NOT in the built-in default:

```python
    # Girl's Best Friend override exists only in the shipped file.
    assert cfg.effective_z("Accessory", "Girl's Best Friend") == 3.5
```

(Append that line to the test before running.)

- [ ] **Step 3: Create the config file**

Create `trait_config.yaml`:

```yaml
# Trait selection rules — single source of truth for the layer system.
# Loaded by lfg_core/trait_config.py. See
# docs/superpowers/specs/2026-06-13-trait-selection-rules-design.md.

# Layer stacking order, bottom (z=0) to top. The z number is each layer's
# default depth; z_overrides below can slot a specific value between layers.
layers:
  - { z: 0, trait_type: Background }
  - { z: 1, trait_type: Back }
  - { z: 2, trait_type: Body }
  - { z: 3, trait_type: Clothing }
  - { z: 4, trait_type: Mouth }
  - { z: 5, trait_type: Eyebrows }
  - { z: 6, trait_type: Eyes }
  - { z: 7, trait_type: Head }
  - { z: 8, trait_type: Accessory }

# Per-value depth overrides. Floats slot a value between layers; 999 = on top.
z_overrides:
  - { trait_type: Accessory, value: "Girl's Best Friend", z: 3.5 }  # above Clothing, below Mouth
  - { trait_type: Eyes,      value: "Wavy",               z: 999 }
  - { trait_type: Mouth,     value: "Rainbow Puke",       z: 999 }
  - { trait_type: Eyes,      value: "Laser Eyes",         z: 999 }
  - { trait_type: Eyes,      value: "Laser",             z: 999 }

# Exclusions: choosing `when` forbids `exclude`. Authored directionally,
# enforced symmetrically. `exclude` accepts value, values (list), or "*" (layer).
# (None active for this collection yet — examples kept commented as templates.)
exclusions: []
# exclusions:
#   - when:    { trait_type: Head, value: "Full Helmet" }
#     exclude: { trait_type: Eyes, value: "*" }
#   - when:    { trait_type: Background, value: "Lava" }
#     exclude: { trait_type: Clothing, values: ["Ice Suit", "Parka"] }

# Inclusions: choosing `when` forces `include`. Directional only.
inclusions: []
# inclusions:
#   - when:    { trait_type: Clothing, value: "Angel Wings" }
#     include: { trait_type: Head,     value: "Halo" }

# Arbitrary tags per trait value. Inert in v1; reserved for future group_rules.
tags: {}

# Placeholder for future color-theory / grouping constraints. Inert in v1.
group_rules: []
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_trait_config.py -q`
Expected: PASS (20 passed).

- [ ] **Step 5: Commit**

```bash
git add trait_config.yaml tests/test_trait_config.py
git commit -m "feat(traits): ship default trait_config.yaml"
```

---

## Task 8: Wire compositor to effective z-index (replace TOP_TRAITS)

Rewrite `swap_compose._ordered_traits` to sort by `effective_z`, and make `TOP_TRAITS` a config-backed shim so existing imports still work.

**Files:**
- Modify: `lfg_core/swap_compose.py:13-32`
- Test: `tests/test_traits_rules.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_traits_rules.py`:

```python
# Integration tests for rules-aware selection + composition ordering.
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DISCORD_BOT_TOKEN", "x")
os.environ.setdefault("XUMM_API_KEY", "x")
os.environ.setdefault("XUMM_API_SECRET", "x")
os.environ.setdefault("BUNNY_CDN_ACCESS_KEY", "x")
os.environ.setdefault("BUNNY_CDN_STORAGE_ZONE", "x")
os.environ.setdefault("SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2")
os.environ.setdefault("TOKEN_ISSUER_ADDRESS", "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
os.environ.setdefault("TOKEN_CURRENCY_HEX", "4C46474F00000000000000000000000000000000")
os.environ.setdefault("XRPL_NETWORK", "testnet")

from lfg_core import swap_compose  # noqa: E402


def test_ordered_traits_puts_laser_eyes_on_top():
    attrs = [
        {"trait_type": "Background", "value": "Sky"},
        {"trait_type": "Eyes", "value": "Laser Eyes"},
        {"trait_type": "Head", "value": "Cap"},
        {"trait_type": "Accessory", "value": "Chain"},
    ]
    ordered = swap_compose._ordered_traits(attrs)
    assert ordered[-1]["value"] == "Laser Eyes"


def test_ordered_traits_z_override_slots_between_layers():
    # Girl's Best Friend (z=3.5) renders above Clothing (3), below Mouth (4).
    attrs = [
        {"trait_type": "Clothing", "value": "Tee"},
        {"trait_type": "Accessory", "value": "Girl's Best Friend"},
        {"trait_type": "Mouth", "value": "Grin"},
    ]
    ordered = [a["value"] for a in swap_compose._ordered_traits(attrs)]
    assert ordered == ["Tee", "Girl's Best Friend", "Grin"]


def test_ordered_traits_skips_none():
    attrs = [
        {"trait_type": "Background", "value": "Sky"},
        {"trait_type": "Eyes", "value": "None"},
    ]
    ordered = [a["value"] for a in swap_compose._ordered_traits(attrs)]
    assert ordered == ["Sky"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_traits_rules.py -q`
Expected: FAIL — `test_ordered_traits_z_override_slots_between_layers` fails (current code uses TRAIT_ORDER index, so Girl's Best Friend, an Accessory, sorts last instead of between Clothing and Mouth).

- [ ] **Step 3: Rewrite `_ordered_traits` and shim TOP_TRAITS**

Replace lines 13–32 of `lfg_core/swap_compose.py` (the `from lfg_core.swap_meta import TRAIT_ORDER`, the `TOP_TRAITS` list, and `_ordered_traits`) with:

```python
from lfg_core.swap_meta import TRAIT_ORDER  # noqa: F401  (kept for back-compat imports)
from lfg_core.trait_config import load_config

# Back-compat shim: values flagged on-top in the config. Some callers/tests
# import this name. Derived from z_overrides at the on-top sentinel.
_cfg = load_config()
TOP_TRAITS = [
    {"trait_type": tt, "value": val}
    for (tt, val), z in _cfg._z_overrides.items() if z >= 999
]


def _ordered_traits(attributes: list) -> list:
    """Trait layers sorted by effective z-index (config layer order, with
    per-value z_overrides applied). 'None' values are skipped (no layer file)."""
    cfg = load_config()
    visible = [a for a in attributes if a.get("value") and a["value"] != "None"]
    return sorted(visible, key=lambda a: cfg.effective_z(a["trait_type"], a["value"]))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_traits_rules.py -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add lfg_core/swap_compose.py tests/test_traits_rules.py
git commit -m "feat(traits): compose by effective z-index from config"
```

---

## Task 9: Make `swap_meta.TRAIT_ORDER` config-backed

`TRAIT_ORDER` is used as a module constant in `swap_meta.py` (normalize/order) and imported elsewhere. Source it from the config while keeping the name and value identical for the default config.

**Files:**
- Modify: `lfg_core/swap_meta.py:14-20`
- Test: `tests/test_trait_config.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_trait_config.py`:

```python
def test_swap_meta_trait_order_matches_config():
    from lfg_core import swap_meta
    cfg = trait_config.load_config(trait_config.DEFAULT_PATH)
    assert list(swap_meta.TRAIT_ORDER) == cfg.layer_order
```

- [ ] **Step 2: Run test to verify it fails**

This passes only if the values coincide; to ensure `TRAIT_ORDER` is actually sourced from config (not a duplicated literal), the change in Step 3 is still required for single-source-of-truth. Run:
`python -m pytest tests/test_trait_config.py::test_swap_meta_trait_order_matches_config -q`
Expected: PASS already (values match) — this test is a regression guard. Proceed to Step 3 to remove the duplicated literal.

- [ ] **Step 3: Source TRAIT_ORDER from config**

In `lfg_core/swap_meta.py`, replace the literal (lines 14–17, the `TRAIT_ORDER = [...]` block) with:

```python
# Layering / canonical attribute order, sourced from the trait config so the
# layer system has a single source of truth. Body is structural, never swapped.
from lfg_core.trait_config import load_config as _load_trait_config

TRAIT_ORDER = _load_trait_config().layer_order
```

Leave `SWAPPABLE_TRAITS` and `BACK_VALUES` (lines 18–21) unchanged.

> **Note:** confirm no circular import — `trait_config.py` imports only stdlib + `yaml` (not `swap_meta`), so this is safe.

- [ ] **Step 4: Run the full suite**

Run: `python -m pytest -q`
Expected: PASS (all tests, including existing `test_rarity.py`).

- [ ] **Step 5: Commit**

```bash
git add lfg_core/swap_meta.py tests/test_trait_config.py
git commit -m "refactor(traits): source TRAIT_ORDER from trait config"
```

---

## Task 10: Wire rules-aware selection into `select_random_attributes`

Loop over `selection_order()`, filter candidates via `candidates_after`, honor `forced_value`, accumulate chosen, return canonically ordered attributes, and call `apply_group_rules`.

**Files:**
- Modify: `lfg_core/traits.py`
- Test: `tests/test_traits_rules.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_traits_rules.py`:

```python
import asyncio
import random as _random
from lfg_core import traits, trait_config


class FakeStore:
    """Minimal LayerStore stand-in returning fixed candidate lists."""
    def __init__(self, bodies, values):
        self._bodies = bodies
        self._values = values  # {(body, trait_type): [values]}

    async def list_bodies(self):
        return self._bodies

    async def list_values(self, body, trait_type):
        return list(self._values.get((body, trait_type), []))


def _excl_config(tmp_path_factory):
    import textwrap
    p = tmp_path_factory.mktemp("cfg") / "c.yaml"
    p.write_text(textwrap.dedent("""
        layers:
          - { z: 0, trait_type: Background }
          - { z: 1, trait_type: Eyes }
          - { z: 2, trait_type: Head }
        exclusions:
          - when:    { trait_type: Head, value: "Full Helmet" }
            exclude: { trait_type: Eyes, value: "*" }
        inclusions:
          - when:    { trait_type: Background, value: "Heaven" }
            include: { trait_type: Head,     value: "Halo" }
    """))
    return str(p)


def test_selection_respects_inclusion(monkeypatch, tmp_path_factory):
    cfg_path = _excl_config(tmp_path_factory)
    monkeypatch.setenv("TRAIT_CONFIG_PATH", cfg_path)
    trait_config.load_config.cache_clear() if hasattr(
        trait_config.load_config, "cache_clear") else None
    store = FakeStore(
        ["male"],
        {("male", "Background"): ["Heaven"],
         ("male", "Eyes"): ["Normal"],
         ("male", "Head"): ["Cap", "Halo"]})
    # Force Background=Heaven; expect Head forced to Halo by inclusion.
    body, attrs = asyncio.get_event_loop().run_until_complete(
        traits.select_random_attributes(store, body="male", network="testnet",
                                        rng=_random.Random(0)))
    head = next(a["value"] for a in attrs if a["trait_type"] == "Head")
    assert head == "Halo"
```

> **Caching note:** `load_config` is cached per Task 11. If Task 11 is not yet done, `load_config` re-reads each call and the env var is honored directly — the `cache_clear` guard above is a no-op in that case and the test still works. After Task 11, the guard clears the cache so the env override takes effect.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_traits_rules.py::test_selection_respects_inclusion -q`
Expected: FAIL — current `select_random_attributes` ignores rules; Head is a random pick, not forced to Halo (flaky/asserts wrong value).

- [ ] **Step 3: Rewrite `select_random_attributes`**

Replace the body of `select_random_attributes` in `lfg_core/traits.py` (keep the signature and docstring intent). New implementation:

```python
import random

from lfg_core import rarity
from lfg_core.trait_config import load_config


async def select_random_attributes(store, body: str = None, *, conn=None,
                                   network=None, now=None, rng=random):
    """Pick a body (rarity-weighted unless given) and one value per trait type
    from the unified layer store, honoring the declarative trait config:
    exclusions (symmetric), inclusions (forced values), and selection order.
    Returns (body, attributes) as a metadata-style [{trait_type, value}] list
    in canonical layer order."""
    cfg = load_config()
    own_conn = conn is None
    if own_conn:
        conn = rarity.connect()
    try:
        if body is None:
            bodies = await store.list_bodies()
            if not bodies:
                raise ValueError("Layer store has no body directories")
            body = rarity.weighted_pick(
                conn, rarity.BODY_SENTINEL, rarity.BODY_CATEGORY, bodies,
                network=network, now=now, rng=rng)

        chosen = []  # [{trait_type, value}] accumulated as we pick
        for trait_type in cfg.selection_order():
            forced = cfg.forced_value(chosen, trait_type)
            if forced is not None:
                chosen.append({"trait_type": trait_type, "value": forced})
                continue
            values = await store.list_values(body, trait_type)
            if not values:
                continue
            allowed = cfg.candidates_after(chosen, trait_type, values)
            if not allowed:
                raise ValueError(
                    f"Trait rules left no candidates for layer "
                    f"'{trait_type}' on body '{body}'")
            value = rarity.weighted_pick(conn, body, trait_type, allowed,
                                         network=network, now=now, rng=rng)
            chosen.append({"trait_type": trait_type, "value": value})

        if not chosen:
            raise ValueError(f"No trait layers found for body '{body}'")

        # Canonical (layer) order for metadata, not selection order.
        order = cfg.layer_order
        chosen.sort(key=lambda a: order.index(a["trait_type"]))
        attributes = cfg.apply_group_rules(chosen)
        return body, attributes
    finally:
        if own_conn:
            conn.close()
```

Remove the now-unused `from lfg_core.swap_meta import TRAIT_ORDER` and
`from lfg_core.swap_meta import TRAIT_ORDER`-based loop. (The old import of
`TRAIT_ORDER` at the top of `traits.py` should be deleted.)

> **Over-constrained = fail loudly:** the `raise ValueError(...)` when `allowed`
> is empty implements the spec's "raise at selection time" requirement.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_traits_rules.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add lfg_core/traits.py tests/test_traits_rules.py
git commit -m "feat(traits): rules-aware random attribute selection"
```

---

## Task 11: Cache `load_config`

Avoid re-reading/re-parsing the file on every call. Add caching with a clear cache hook for tests.

**Files:**
- Modify: `lfg_core/trait_config.py`
- Test: `tests/test_trait_config.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_trait_config.py`:

```python
def test_load_config_is_cached(tmp_path):
    path = write(tmp_path, "c.yaml", MINIMAL_YAML)
    a = trait_config.load_config(path)
    b = trait_config.load_config(path)
    assert a is b  # same cached instance
    trait_config.load_config.cache_clear()
    c = trait_config.load_config(path)
    assert c is not a
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_trait_config.py::test_load_config_is_cached -q`
Expected: FAIL — `a is b` is False (new instance each call) and/or no `cache_clear` attribute.

- [ ] **Step 3: Add caching**

In `lfg_core/trait_config.py`, add `import functools` near the top, and decorate
`load_config`. Because the default depends on env at call time, cache on the
resolved path:

```python
import functools
```

Refactor `load_config` into a cached inner keyed by resolved path:

```python
@functools.lru_cache(maxsize=8)
def _load_cached(path):
    if not os.path.exists(path):
        log.warning("trait config %s not found; using built-in default", path)
        return TraitConfig(_BUILTIN_DEFAULT)
    return TraitConfig(_load_raw(path))


def load_config(path=None):
    """Load and validate the trait config (cached). Resolution order:
    explicit path arg -> env TRAIT_CONFIG_PATH -> DEFAULT_PATH -> built-in default."""
    path = path or os.environ.get("TRAIT_CONFIG_PATH") or DEFAULT_PATH
    return _load_cached(path)


load_config.cache_clear = _load_cached.cache_clear
```

(Delete the previous non-cached `load_config` definition.)

- [ ] **Step 4: Run the full suite**

Run: `python -m pytest -q`
Expected: PASS (all tests). If any test that sets `TRAIT_CONFIG_PATH` now sees a
stale cache, ensure it calls `trait_config.load_config.cache_clear()` after
setting the env var (the Task 10 test already does).

- [ ] **Step 5: Commit**

```bash
git add lfg_core/trait_config.py tests/test_trait_config.py
git commit -m "perf(traits): cache parsed trait config by path"
```

---

## Task 12: Full-suite regression + docs cross-check

**Files:**
- Test: all
- Modify (if needed): none expected

- [ ] **Step 1: Run the entire suite**

Run: `python -m pytest -q`
Expected: PASS — all of `test_rarity.py`, `test_trait_config.py`, `test_traits_rules.py`.

- [ ] **Step 2: Smoke-import the touched modules**

Run:
```bash
python -c "import os; os.environ.setdefault('DISCORD_BOT_TOKEN','x'); \
os.environ.setdefault('XUMM_API_KEY','x'); os.environ.setdefault('XUMM_API_SECRET','x'); \
os.environ.setdefault('BUNNY_CDN_ACCESS_KEY','x'); os.environ.setdefault('BUNNY_CDN_STORAGE_ZONE','x'); \
os.environ.setdefault('SEED','sEdTM1uX8pu2do5XvTnutH6HsouMaM2'); \
os.environ.setdefault('TOKEN_ISSUER_ADDRESS','rrrrrrrrrrrrrrrrrrrrrhoLvTp'); \
os.environ.setdefault('TOKEN_CURRENCY_HEX','4C46474F00000000000000000000000000000000'); \
import lfg_core.traits, lfg_core.swap_compose, lfg_core.swap_meta, lfg_core.trait_config; \
print('TRAIT_ORDER', lfg_core.swap_meta.TRAIT_ORDER); \
print('TOP_TRAITS', lfg_core.swap_compose.TOP_TRAITS)"
```
Expected: prints the 9-layer order and the on-top traits list without error.

- [ ] **Step 3: Confirm CLAUDE.md still accurate / note follow-ups**

Verify the spec doc reference and that no code still imports a removed symbol:
Run: `grep -rn "TOP_TRAITS\|TRAIT_ORDER" lfg_core/ main.py`
Expected: only the shim definitions + legitimate uses; no dangling references.

- [ ] **Step 4: Commit any cleanup (if Step 3 found issues)**

```bash
git add -A
git commit -m "chore(traits): regression cleanup for rules engine"
```

(Skip if nothing changed.)

---

## Self-Review Notes

- **Spec coverage:** layer-order-in-config (T2/T7/T9), per-value z-index incl. floats/Girl's Best Friend (T2/T7/T8), exclusions value→value/values/layer symmetric (T3), inclusions directional + conflict error (T4), selection order + cycle fail-loud (T5), over-constrained fail-loud (T10), tags + group_rules stub (T6), YAML/JSON loader + PyYAML dep (T1/T2), backward-compat shims (T8/T9), default config reproduces current behavior (T7), full test plan (all tasks). All spec sections map to tasks.
- **Type consistency:** `candidates_after(chosen, trait_type, all_values)`, `forced_value(chosen, trait_type)`, `effective_z(trait_type, value)`, `selection_order()`, `apply_group_rules(attributes)`, `tags_for(trait_type, value)` — names used identically across tasks. `chosen` is consistently a list of `{trait_type, value}` dicts.
- **Note on `_cfg._z_overrides` access in T8 shim:** uses the private dict to derive TOP_TRAITS for back-compat; acceptable as same-module-family glue, removable once no caller imports `TOP_TRAITS`.
