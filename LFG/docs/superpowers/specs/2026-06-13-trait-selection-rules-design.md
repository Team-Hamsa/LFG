# Trait Selection Rules Engine — Design

**Date:** 2026-06-13
**Status:** Approved (design); implementation pending
**Related:** Feature request [#39](https://github.com/Team-Hamsa/Mint-Bot/issues/39) (config-authoring admin UI — out of scope here)

## Problem

Trait selection today is **independent per layer**: `lfg_core/traits.select_random_attributes`
loops over a hardcoded `TRAIT_ORDER` list and makes one rarity-weighted pick per
layer with no awareness of what other layers chose. Layer stacking order is the
fixed `TRAIT_ORDER` in `lfg_core/swap_meta.py`, and the only z-index escape hatch
is a hardcoded `TOP_TRAITS` list in `lfg_core/swap_compose.py`.

We need a declarative, data-driven way to express collection rules:

1. **Exclusion** — choosing trait X forbids trait(s) Y.
2. **Inclusion** — choosing trait X forces trait Y.
3. **Z-index overrides** — a specific value renders at a custom depth in the stack.
4. **Layer order** — defined in config, not code (so the project generalizes to
   other collections without code changes).
5. **Color/tag grouping** — a stub for future color-theory rules.

## Goals / Non-goals

**Goals**
- Single declarative config file as the source of truth for layer order, z-index,
  and selection rules.
- Non-dev-friendly authoring (YAML, commented template).
- Rules authored **directionally**, enforced **order-independently**.
- Backward compatible: current output (TRAIT_ORDER + laser-eyes-on-top) is
  preserved by the default shipped config.

**Non-goals (this iteration)**
- Implementing color-theory/group-rule semantics (stub only).
- An admin/Activity UI for editing the config (tracked in #39).
- Changing the rarity-weighting math itself.

## Decisions (from brainstorming)

| Topic | Decision |
|-------|----------|
| Where rules live | A single declarative data file (not code, not DB). |
| File format | **YAML** primary; loader is format-agnostic by extension (`.yaml`/`.yml`/`.json`). |
| Exclusion shapes | value→value, value→values (list), value→layer (`"*"`). |
| Exclusion symmetry | Authored directionally, **enforced symmetrically** (Option 1). |
| Inclusion | Forced assignment; **directional only** (not symmetric). |
| Z-index | Per-value explicit numeric z-index (floats allowed, slot between layers). |
| Layer order | Explicit-index array in config; replaces hardcoded `TRAIT_ORDER`. |
| Color theory | Tags (inert) + a no-op `group_rules` hook with future-impl docstring. |

## Config file: `trait_config.yaml`

Lives at the repo project root (`LFG/trait_config.yaml`). Path override via env
`TRAIT_CONFIG_PATH`. Loader picks YAML vs JSON by extension; both produce the same
validated `TraitConfig`.

```yaml
# Layer stacking order, bottom (z=0) to top. The z number is the layer's default
# depth; per-value z_overrides below can slot a value between these.
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

# Per-value depth overrides. Floats slot a value between layers.
z_overrides:
  - { trait_type: Accessory, value: "Girl's Best Friend", z: 3.5 }  # above Clothing, below Mouth
  - { trait_type: Eyes,      value: "Laser Eyes",         z: 999 }  # render on top of everything
  - { trait_type: Eyes,      value: "Wavy",               z: 999 }
  - { trait_type: Mouth,     value: "Rainbow Puke",       z: 999 }

# Exclusions: choosing `when` forbids `exclude`. Authored directionally,
# enforced symmetrically (order of selection does not matter).
# `exclude` accepts: value (str), values (list), or value: "*" (whole layer).
exclusions:
  - when:    { trait_type: Head, value: "Full Helmet" }
    exclude: { trait_type: Eyes, value: "*" }                       # value→layer
  - when:    { trait_type: Background, value: "Lava" }
    exclude: { trait_type: Clothing, values: ["Ice Suit", "Parka"] } # value→values

# Inclusions: choosing `when` forces `include`. Directional only.
# (Example below does not apply to this collection; kept as a template.)
inclusions:
  - when:    { trait_type: Clothing, value: "Angel Wings" }
    include: { trait_type: Head,     value: "Halo" }

# Arbitrary tags per trait value. Inert in v1; consumed by future group_rules.
tags:
  Background:
    Lava: { color: warm, palette: neon }

# Placeholder for future color-theory / grouping constraints. Inert in v1.
group_rules: []
```

### Schema validation rules (enforced at load)
- `layers`: non-empty; unique `trait_type`s; unique numeric `z`.
- `z_overrides`: each references a `trait_type` present in `layers`.
- `exclusions`/`inclusions`: `when`/`exclude`/`include` reference known
  `trait_type`s; exactly one of `value`/`values` on the target side.
- **Conflict check:** if an inclusion and an exclusion target the same
  `(trait_type, value)`, raise a config error (no silent resolution).
- Type-coercion guard for YAML footguns (the "Norway problem"): trait values are
  coerced to / validated as strings after load, so bare `No`/`Yes`/`On`/`Off` or
  number-like names don't silently become bool/int.

## New module: `lfg_core/trait_config.py`

Owns all config parsing so nothing else touches raw YAML/JSON.

```
load_config(path=None) -> TraitConfig          # cached; env TRAIT_CONFIG_PATH; default LFG/trait_config.yaml
TraitConfig.layer_order            -> list[str] # derived from `layers`, sorted by z
TraitConfig.effective_z(tt, value) -> float     # z_override if present else layer z
TraitConfig.selection_order()      -> list[str] # pick order: triggers before targets (topo over rules), else layer_order
TraitConfig.candidates_after(chosen, tt, all_values) -> list[str]
                                                # filter a layer's candidates vs already-chosen attrs,
                                                # applying exclusions SYMMETRICALLY (when->exclude and reverse)
TraitConfig.forced_value(chosen, tt)  -> str|None  # inclusion: forced assignment for this layer, if any
TraitConfig.apply_group_rules(attributes) -> attributes  # NO-OP stub (docstring sketches future semantics)
```

### Enforcement model (Option 1)
- **Directional authoring, symmetric enforcement.** Each exclusion is treated as a
  mutual conflict at evaluation time. When picking *any* layer,
  `candidates_after` removes values that conflict with anything already chosen,
  in either rule direction.
- **`selection_order()`** derives a pick order so trigger layers are chosen before
  their targets (topological-ish ordering over the rule graph), guaranteeing no
  "backward" conflict against an already-locked earlier pick. Falls back to
  `layer_order` when rules impose no ordering constraint. Cycles in the rule graph
  fall back to `layer_order` and log a warning (symmetric exclusion filtering still
  applies, so output stays valid).
- **Inclusions** are forced assignments applied when the target layer is reached
  (after its trigger, guaranteed by `selection_order()`); they override the
  rarity-weighted pick for that layer.

### Backward-compat shims
- `swap_meta.TRAIT_ORDER` becomes a thin alias sourced from `load_config().layer_order`.
- `swap_compose.TOP_TRAITS` is reconstructed from `z_overrides` with `z >= 999`
  (or removed in favor of `effective_z` sorting — see wiring).
- Existing imports keep working unchanged.

## Wiring changes

- **`lfg_core/traits.select_random_attributes`**
  - Loop over `config.selection_order()` instead of `TRAIT_ORDER`.
  - For each layer: if `config.forced_value(chosen, tt)` is set, use it; else
    filter `store.list_values(...)` through `config.candidates_after(chosen, tt, values)`
    before the rarity-weighted `weighted_pick`.
  - Accumulate `chosen` as we go so later layers see earlier picks.
  - Return attributes re-sorted into canonical `layer_order` for metadata.
  - Call `config.apply_group_rules(attributes)` at the end (pass-through today).
- **`lfg_core/swap_compose._ordered_traits`**
  - Sort visible traits by `config.effective_z(tt, value)` instead of
    `TRAIT_ORDER.index(...)` + the `TOP_TRAITS` shuffle. `None` values still skipped.
- **`lfg_core/swap_meta` / `swap_compose`** constants become config-backed shims.

## Edge cases & failure handling

- **Missing config file:** fall back to a built-in default config that reproduces
  today's behavior (current `TRAIT_ORDER` + laser/wavy/rainbow-puke on top); log a
  warning. The repo ships an explicit `trait_config.yaml` so this is a safety net.
- **Over-constrained layer** (all candidates excluded): the layer resolves to
  `None` (valid — `None` means "no layer file") and logs a warning rather than
  raising, so a mint never hard-fails on rules alone.
- **Inclusion target value not in store:** raise at compose time via the existing
  `missing_layers` check (unchanged) so it surfaces before any on-chain action.
- **Malformed config:** raise a clear, line-referenced error at load.

## Dependencies

- Add `PyYAML` to `requirements.txt`.

## Testing plan

Unit tests (`tests/test_trait_config.py`, `tests/test_traits_rules.py`):
- **Symmetric exclusion:** rule fires regardless of which of the two layers is
  picked first.
- **All three exclusion shapes:** value→value, value→values, value→layer (`"*"`).
- **Inclusion:** trigger forces target value, overriding the rarity pick;
  directionality (reverse does not fire).
- **Z-index ordering:** Girl's Best Friend composes above Clothing and below
  Mouth; laser/wavy/rainbow-puke render on top.
- **`selection_order()`:** triggers ordered before targets; cycle → fallback +
  warning.
- **Validation:** unknown trait_type, both `value` and `values` set,
  inclusion/exclusion conflict, YAML "Norway" coercion.
- **Default-config snapshot:** with no config file, output matches current
  TRAIT_ORDER + TOP_TRAITS behavior (regression guard).
- **Format-agnostic load:** identical `TraitConfig` from equivalent `.yaml` and
  `.json` inputs.

## Out of scope (tracked)

- Admin/Activity UI for authoring the config — issue #39.
- Actual color-theory `group_rules` semantics — stub + docstring only.
