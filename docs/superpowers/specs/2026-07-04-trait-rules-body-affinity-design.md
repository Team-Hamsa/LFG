# Trait Rules Engine + Body-Affinity System — Design

**Date:** 2026-07-04
**Issues:** #40 (rules engine), #28 (legacy rules), #30 (cross-body swaps), #39 (admin tooling)
**Status:** Approved (brainstormed 2026-07-04)
**Supersedes:** `2026-06-13-trait-selection-rules-design.md` (on branch
`feat/trait-selection-rules`) — that spec predates the unified layer store,
`LAYER_SOURCE=local`, the seasons manifest (#114–#117), and the ape-face
compose rule (#110). Its core ideas (declarative `trait_config.yaml`,
z-overrides, exclusion machinery) carry forward; its integration points do not.

## 1. Motivation & archaeology findings

Mainnet-launch concern: "NFTs might not generate correctly" unless the rules
in #40/#28/#30 are sorted. A full audit of every legacy archive
(`~/linode-backup`, `~/Mint-Bot/LFG/legacy`, the `LFG-history-backup` bundle)
established what the legacy generation code actually enforced:

1. **Season-3 exclusion list** — already ported (seasons manifest, #115–#117).
2. **TOP_TRAITS z-order** (Wavy Eyes, Rainbow Puke, Laser Eyes render on top)
   — already in current code.
3. **Body-shape determination** (Straight→male, Curved→female, Ape→ape,
   else skeleton) + body-agnostic Background/Back — already in current code.
4. **Ape-face compose rule** — reimplemented in #110.

**No pairwise "trait X excludes trait Y" rules exist in any legacy script.**
#28's premise (port exclusion rules from legacy scripts) is therefore moot.
The *real* historical rules are **per-value body affinity**: e.g. curved-body
clothing never appears on straight/ape/skeleton bodies; skeletons have no
facial traits (`layers/skeleton/{Eyes,Eyebrows,Mouth}` contain only
`None.png`); some Head/Eyes/Eyebrows values are historically female-only or
male-only while others are deliberately shared.

**Current state.** Mint selection (`lfg_core/traits.py:select_random_attributes`)
is *structurally* body-safe: it only lists values from
`layers/<body>/<trait_type>/`, so the directory tree IS the compatibility
matrix. But that matrix is implicit, unvalidated, and unauditable:

- Nothing verifies dir contents against mint history (a misplaced file mints
  wrong art silently — cf. the June CDN audit's misplaced Ape assets).
- Shared sets are duplicated, not shared: Background is 4 identical copies
  (48 files × 4 bodies); Back likewise. Drift between copies is invisible.
- Swap/economy paths (equip, assemble, deposit, trait tokens) carry
  `(slot, value)` with no body tag; compatibility is enforced only by "does
  the file exist under this body," failing late at compose instead of
  filtering options up front. Cross-body "shared" values work by filename
  coincidence.
- #30's deliberate cross-body swapping has no data structure to live in.

## 2. Issue reframing

| Issue | Was | Becomes |
|---|---|---|
| #28 | Port legacy exclusion rules | **Derive and encode the historical body-affinity matrix** from the 3,535-edition mint history. Closes when the audit-confirmed matrix ships in config. |
| #40 | Rules engine (June-13 spec) | Same engine, spec refreshed for current codebase; `trait_config.yaml` gains **affinity** and **swap_matrix** sections; pairwise exclusion/inclusion machinery ships engine-supported with empty rule lists. |
| #30 | Cross-body swap rules | Builds on the engine: API-level enforcement + UI filtering per its matrix. Ships pre-launch (hackathon volume). |
| #39 | Admin config-gen UI | Launch-critical slice is only "human confirms the rules" → satisfied by the **audit report review** + a validation CLI in CI. The Activity admin panel remains open in #39 as post-launch work. |

## 3. Phase 1 — Body-affinity audit

`scripts/audit_body_affinity.py` mines `onchain_mainnet.db` (all editions
**including burned** — history is the point) crossed with `layers/`.

- For each `(trait_type, value)`: mint counts per body shape, with body
  derived from the Body attribute via the existing Straight/Curved/Ape/else
  mapping.
- Classification: `female-only / male-only / shared-MF / universal /` other
  observed subsets. Values minted fewer than 3 times get a
  **low-confidence flag**.
- Two dir cross-checks:
  - value present in `layers/<body>/<type>/` but never minted on that body →
    *candidate misplacement or intentionally-new* (human decides);
  - value minted historically but absent from dirs → *coverage gap*.
- Output: `reports/body_affinity_report.md` (+ machine-readable JSON) **and a
  generated draft `affinity:` section for `trait_config.yaml`**.

The user reviews and corrects the report — the single human-input gate in the
sequence, front-loaded so nothing downstream blocks on it twice.

## 4. Phase 2 — Rules engine (#40)

`lfg_core/trait_config.py` owns load/validate/query of a single
`trait_config.yaml`:

```yaml
version: 1
layers:                      # explicit z-order — replaces TRAIT_ORDER
  - {name: Background, z: 10, shared: true}
  - {name: Back,       z: 20, shared: true}
  - {name: Body,       z: 30}
  # ...
z_overrides:                 # absorbs TOP_TRAITS
  - {trait_type: Eyes, value: Wavy, z: 95}
affinity:                    # per-value body allow-list (generated, Phase 1)
  Clothing:
    "Summer Dress": [female]
    "Hoodie":       [male, female]
swap_matrix:                 # #30
  universal_layers: [Accessory, Back]
  pairs:
    - {bodies: [ape, skeleton],  layers: [Head, Clothing]}
    - {bodies: [male, female],   layers_except: [Clothing]}
exclusions: []               # pairwise machinery present, empty at launch
inclusions: []
```

**Load-time validation fails loudly:**
- every affinity value must exist in the dirs it claims;
- dir values without an affinity entry default to dir-derived affinity
  (warn, don't fail);
- inclusion-rule cycles error at load;
- over-constrained layers (no legal value) error at load.

**Integration points:**
- `select_random_attributes` filters candidate values by affinity and
  re-rolls on (future) exclusion hits.
- `swap_compose` takes layer order + z-overrides from config;
  `TRAIT_ORDER` / `TOP_TRAITS` become shims over the config.
- The ape-face rule **stays code** — it is raster masking, not a declarative
  selection rule (YAGNI).
- `scripts/validate_trait_config.py` runs in CI/pre-commit.

**Parity guarantee:** the shipped default config is generated from the
confirmed Phase-1 audit; a test asserts engine-on output ≡ current behavior
on the default config.

## 5. Phase 3 — Cross-body swapping (#30)

One query — `allowed_values(character_body, layer)` = own-dir values ∪
(swap-matrix-permitted bodies' values ∩ affinity) — used in three places:

1. **Swap API rejects** invalid targets (rule enforced server-side, per the
   issue's acceptance criteria — not just UI).
2. **Dressing Room / Trait Swapper UI** lists only valid targets for the
   selected character body + layer.
3. **Economy ops** (equip, assemble, deposit) gate on the same check instead
   of failing late at compose.

**Rendering subtlety (explicit):** a cross-body swap renders the *source
body's* asset file, so asset resolution order is
own dir → shared → matrix-permitted foreign dir.

Same-body swap behavior is untouched and regression-tested.

## 6. Phase 4 — Physical `layers/shared/` restructure (own PR, last)

Audit-confirmed universal values (all Background/Back, the universal subset
of Accessory — current per-body Accessory counts: ape 14 / female 36 /
male 41 / skeleton 18) move to `layers/shared/<trait_type>/`; per-body dirs
keep only body-specific values.

- `layer_store.list_values(body, type)` returns union(body dir, shared dir);
  `resolve_asset` falls back to shared.
- Config layers marked `shared: true` read only the shared dir.
- Migration script + `upload_layers_cdn.py` + coverage-audit updates;
  seasons-manifest key impact verified in the plan.
- Lands **last**, isolated, after the engine's validation suite exists to
  catch mistakes. Everything before it works without it.

## 7. Delivery — sequenced draft PRs

| # | Content | Depends on |
|---|---|---|
| PR-1 | Phase-1 audit script + report generation | — |
| gate | **User reviews affinity report** (only human gate) | PR-1 run |
| PR-2 | Engine core: `trait_config.py` + generated default config + validation CLI + CI hook | gate |
| PR-3 | Mint/compose integration + parity tests | PR-2 |
| PR-4 | #30: swap API enforcement + UI filtering + economy gating | PR-3 |
| PR-5 | `layers/shared/` restructure | PR-4 |
| post-launch | #39 Activity admin panel (issue stays open) | — |

All PRs follow the repo's draft-first CodeRabbit flow. New application code
lands in `lfg_core/`, `scripts/`, and `webapp/` (hackathon LoC counts real
work only).

## 8. Testing strategy

- TDD per repo convention; new test files copy the env-guard preamble.
- **Parity test:** default config reproduces today's selection behavior.
- **Property test:** N random mints under the engine yield only
  affinity-valid combos.
- **#30 boundary tests** from its acceptance criteria: valid cross-body swap,
  invalid cross-body swap rejected at API, same-body swap unaffected.
- Audit script tested against a fixture DB with known co-occurrence patterns.

## 9. Out of scope

- #39's admin UI (post-launch).
- Pairwise exclusion *content* (machinery ships; no known rules exist).
- Color-theory tags / group rules from the June-13 spec (stubs only, unchanged).
- Rarity re-weighting (existing `lfg_core/rarity` untouched).
