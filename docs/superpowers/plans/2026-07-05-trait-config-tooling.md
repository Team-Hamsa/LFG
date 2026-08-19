# Trait-config authoring tooling (#39) — Implementation Plan

**Issue:** Team-Hamsa/LFG #39 — OPEN, label `roadmap`.
**Spec:** `docs/superpowers/specs/2026-07-05-trait-config-tooling-design.md`
**Date:** 2026-07-05
**Last review:** 2026-08-19
**Status:** live — re-reviewed 2026-08-19 against main@27dc301

**Branch:** new feature branch off `main` (check `git branch --show-current`
first — parallel sessions run in this repo). Open the PR ready, not draft, per
house rules. TDD throughout: each task writes the failing test first.

## 0. What changed since this was drafted

- **The plan was one phase; it is now two.** Spec §5 splits #39 into
  **Phase A — arm the gate** (recommended, small, no new surface) and
  **Phase B — the guided CLI** (designed, deferred). Tasks 1-7 of the July plan
  are all Phase B. Phase A is new and is where the value is.
- **Evidence that reprioritised it:** PR #145 (#38) — an ape-mint outage
  ("Every ape mint fails") caused by zero affinity-legal `ape/Eyebrows` values
  after art was deleted. No config diff was involved, so no editor could have
  prevented it; only a gate could.
- **The gate that should have caught it is not armed.**
  `.pre-commit-config.yaml`'s `validate-trait-config` carries
  `files: ^(trait_config\.yaml|layers/)`, and the art tree is gitignored
  (`.gitignore:15-17`; the only tracked path under `layers/` is the
  `seasons.json` sidecar, touched 3 times ever), so in practice the hook runs
  only when the YAML itself changes — never on an art-tree change, which is
  what #145 was. `audit-layer-dimensions`, two entries below, documents the
  same trap and uses `always_run: true`.
- **Measured edit rate undercuts Phase B's premise:**
  `git log -- trait_config.yaml` is three commits, ever (`8158136` 2026-07-04,
  `cee0c8a` 2026-07-09, `2f616fb` 2026-07-11), none in 5+ weeks. The spec's own
  revisit trigger was ">~weekly".
- **The subcommand set changed shape.** Both real edits were bulk and
  predicate-driven (add a body to every entry it has art for), not per-value —
  #145 touched 160 face values, #171 touched 95 affinity entries (59 Clothing
  + 36 Accessory). Phase B gains a `grant-body` subcommand; `set-affinity`
  drops in priority.
- **The file's comment surface is bigger than a whole-line grep suggests.**
  21 lines carry a `#`: the 3-line header, 1 trailing comment on the Retardio
  z-override, and 17 trailing `# low sample: N mint(s) — owner-confirmed
  2026-07-04` annotations on affinity values. `grep -c '^\s*#'` returns 3 and
  misses 18 of them. This is the surface Task B1 has to preserve, and it is
  why spec §8's canonical-emitter fallback is not cheap.
- **Task 4's `backups/` + `.gitignore` line is dropped.** `reports/` is already
  ignored (`.gitignore:37`) and is where `scripts/trait_dashboard.py` writes its
  audit log; backups go to `reports/trait_config_backups/`, no `.gitignore`
  change.
- **The env-guard preamble instruction is obsolete (#323).** The root
  `conftest.py` sets `LFG_SKIP_DOTENV=1` and pins every `_require` var, so new
  test files need no preamble. New *scripts* must call
  `lfg_core.envload.load_dotenv_unless_skipped()` instead of a bare
  `load_dotenv()`.
- **"Run the gate" now means running the whole script.**
  `scripts/validate_trait_config.py` gained a #301 recurrence guard
  (`find_typo_body_files`), so the validate-before-write step calls that
  script's `main()`, not `load_config` + `validate_against_store` directly.
- **Restart line corrected.** `lfg-bot` and `lfg-telegram` do not import
  `trait_config`; the only pm2 process caching it is `lfg-activity`
  (`stg-activity` on staging).
- **New hard constraint:** `trait_config.yaml` is tracked and both deployed
  checkouts are polled by `scripts/deployer.py`, which advances with
  `git merge --ff-only`. The editor must not be run inside `~/LFG` or
  `~/LFG-staging`.
- **Line-number citations replaced with symbol names** (`rarity.py:153-154` →
  `weighted_pick`'s `ensure_schema` / `_ensure_rows` calls, now :189-190;
  `rarity.py:197` → `recalculate_rarity`'s missing-`LFG` guard, now :270;
  `trait_config.py:177-225` → `_check_exclusions`, which happens not to have
  moved — the symbol name is simply the durable citation).
- **Nothing from the July plan has shipped.** `lfg_core/trait_config_edit.py`,
  `scripts/trait_config_edit.py` and `tests/test_trait_config_edit.py` do not
  exist; `ruamel.yaml` is in neither requirements file.

### Already landed (cut from this plan)

Nothing from Tasks 1-7. What landed *adjacent* to them, and therefore no longer
needs planning here:

- `scripts/trait_dashboard.py` (#202) — the rarity half of the admin-tooling
  itch: loopback web UI, live `trait_rarity` edits, no restart.
- `lfg_core/affinity_audit.py::render_affinity_yaml` +
  `scripts/audit_body_affinity.py` — bulk affinity generation from mint
  history. "Affinity bulk editing / CSV import-export" is permanently out of
  scope; this is the tool.
- `lfg_core/seasons.py::disable_season` — the last-one-standing guard pattern
  Phase A copies for `rarity.set_enabled`.

---

# Phase A — arm the gate (recommended; do this one)

New files: none. Touches `scripts/validate_trait_config.py`,
`.pre-commit-config.yaml`, `lfg_core/rarity.py`, and their tests.

## Task A1 — legal-values gate in the validator

**Test first** (extend `tests/test_trait_config.py`, or add
`tests/test_validate_trait_config_gate.py`):
- `test_gate_flags_stranded_body_layer` — tmpdir `LocalLayerStore` with
  `<body>/Eyebrows/` holding two values, plus an affinity block excluding that
  body from both → the gate returns an error naming body **and** layer. This is
  the #145 regression expressed as a test.
- `test_gate_silent_when_a_legal_value_remains` — same fixture, one value still
  allowed → no error.
- `test_gate_silent_on_empty_layer_tree` — empty store → no error (same
  early-out as `validate_against_store`), so CI stays green.
- `test_gate_ignores_layers_with_no_art_for_a_body` — `raw` empty → not an
  error (a body legitimately lacking a layer is coverage, not over-constraint;
  mirrors `select_random_attributes`, which only raises when `raw_values` is
  non-empty).

**Implement:** a function in `scripts/validate_trait_config.py` taking
`(cfg, store)` and returning `list[str]`, iterating `await store.list_bodies()`
× `swap_meta.TRAIT_ORDER` and appending an error when
`raw and not [v for v in raw if cfg.value_allowed(body, layer, v)]`. Call it
from `main()` alongside `validate_against_store` and fold the result into
`error_list`. Reuse `LocalLayerStore` — no new store code.

**Known false-positive source (decide before implementing):**
`store.list_bodies()` is disk-derived, but the body a mint actually uses comes
from `rarity.weighted_pick(BODY_SENTINEL, BODY_CATEGORY, bodies)`, which skips
`enabled=0` body rows. A body dir parked disabled in `trait_rarity` (per
issue #198's 2026-07-12 mainnet stopgap, season-3 bodies were) can be flagged
for a (body, layer) that production never reaches. Either consult
`trait_rarity` or accept the stricter reading — but state which, in the test
docstring.

**Manual check before proceeding (spec §11's open assumption):** run
`.venv/bin/python scripts/validate_trait_config.py` against the real `layers/`
tree and confirm **0 errors**. A pre-existing hit means either the live config
is over-constrained for some body/layer, or the false positive above — triage
which, and report it, rather than weakening the check.

## Task A2 — make the hook actually run

**Test first:** not practical for a pre-commit config; verify by observation and
record the observation in the PR body.

**Implement:** in `.pre-commit-config.yaml`, replace
`files: ^(trait_config\.yaml|layers/)` on the `validate-trait-config` hook with
`always_run: true`, matching the `audit-layer-dimensions` entry
(`pass_filenames: false` is already set on both), and carry a one-line comment
explaining why a `files:` filter cannot match a gitignored art tree.

**Verify:** on a branch with an unrelated one-line change, run
`pre-commit run --hook-stage pre-push validate-trait-config` (or push) and
confirm the hook executes rather than reporting `(no files to check) Skipped`.

## Task A3 — last-one-standing guard on `rarity.set_enabled`

**Tests first** (`tests/test_rarity.py`):
- `test_set_enabled_refuses_last_enabled_trait` — a `(network, body, category)`
  with one `enabled=1` row; disabling it raises and the row is unchanged.
- `test_set_enabled_allows_disable_when_survivors_remain`.
- `test_set_enabled_always_allows_enable` — the guard applies to disabling only.

**Implement:** in `lfg_core/rarity.py::set_enabled`, when `enabled` is falsy,
count surviving `enabled=1` rows for that `(network, body, category)` excluding
the target and refuse when the count is 0, with the same message shape
`seasons.disable_season` uses. Commit only on success. **Raise a distinct
exception type, not a bare `ValueError`** — see the wire-up note below.

**Wire-up test** (`tests/test_trait_dashboard.py`): `apply_toggle` on the last
enabled trait must not answer 404. `scripts/trait_dashboard.py`'s `_error_mw`
maps every `ValueError` to **404** (its comment says "missing (body, category,
trait) row"), so a bare `ValueError` refusal would read as "not found". Either
raise a dedicated exception and add a 409 branch to `_error_mw`, or catch it in
`apply_toggle` and re-raise the module's `_BadInput` (already mapped to 400).

## Task A4 — ship Phase A

1. Full suite: `.venv/bin/python -m pytest -q` (whole suite, not just the new
   files — collection order matters; see CLAUDE.md "Test env isolation").
2. `ruff check --fix .` + `ruff format` (the pre-push gate runs both anyway).
3. PR ready (not draft), title
   `fix(#39): arm the trait-config satisfiability gate + guard rarity toggles`.
   The body must state the #145 regression the gate reproduces, and that the
   hook-filter change is why the gate can fire at all. Wait for Greptile **and**
   CodeRabbit; reply on every finding's own thread before merging.
4. After merge: comment on #39 with blob permalinks at the merge SHA to this
   plan and the spec, and note that Phase B (the CLI) remains open with its
   revisit triggers.

---

# Phase B — guided CLI (deferred; design retained)

Build only when spec §5's triggers fire (first non-empty `exclusions`, a sixth
entry in `trait_config.VALID_BODIES`, or a real community-admin audience). Kept
here so it can be executed without re-deriving the design.

New files:
- `lfg_core/trait_config_edit.py` — round-trip load/dump + pure edit primitives
  + the validation pipeline.
- `scripts/trait_config_edit.py` — argparse CLI over the primitives. Must call
  `lfg_core.envload.load_dotenv_unless_skipped()`, never a bare `load_dotenv()`.
- `tests/test_trait_config_edit.py` — **no env-guard preamble needed** (#323;
  the root `conftest.py` supplies every pinned var).

New dependency: `ruamel.yaml>=0.18` in **`requirements-dev.txt`** (editor-only;
the engine keeps PyYAML `safe_load`, and the tool must never run on a deploy
box).

## Task B1 — round-trip fidelity spike (gates the approach)

**Test first:** `test_roundtrip_is_byte_identical` — load the real
`trait_config.yaml` with `ruamel.yaml.YAML(typ="rt")`, dump to a string, assert
byte-equal to the original. Plus `test_roundtrip_single_edit_minimal_diff` —
change one affinity value's body list, dump, assert the unified diff touches
≤ 2 lines and that **all 21 comment-bearing lines survive**: the 3 header
lines, the trailing Retardio z-override comment, and the 17 trailing
`# low sample: …` annotations in the affinity block.

**Implement:** `load_doc(path)` / `dump_doc(doc) -> str` (width=100, preserve
quotes).

**Decision gate:** if byte-identity fails on cosmetic grounds (flow-map spacing,
column alignment), relax to "semantic-equal + comments preserved +
diff-minimal" and record the delta in the test docstring. If comments or flow
style are lost, STOP and switch to the canonical-emitter fallback in spec §8 —
`affinity_audit.render_affinity_yaml` proves the affinity block's *line shape*
is machine-emittable, but not that its output matches this file: it sorts body
lists (19 of 515 entries here are unsorted) and emits `# LOW CONFIDENCE: …`
rather than the 17 hand-written `# low sample: …` annotations. The fallback's
remaining work is therefore the aligned `layers` / `z_overrides` blocks **plus**
carrying all 21 comment-bearing lines and the existing body-list order in the
model — comparable effort to the ruamel path, so treat "ruamel fails" as a
schedule hit, not a free pivot.

## Task B2 — edit primitives (pure, no I/O)

**Tests first**, one per primitive, on a small fixture YAML (comments + flow
style included) plus the real file where cheap:

- `grant_body(doc, body, *, trait_type=None, from_body=None, store)` — **the
  priority primitive** (spec §7). Adds `body` to every affinity entry whose
  value has art under that body (via `LocalLayerStore.list_values`, so `shared/`
  is unioned in and `LAYER_EXTENSIONS` covers `.webm`), or, with `from_body`, to
  every entry already allowing `from_body`. Tests: reproduces the #171 milady
  Clothing edit on a fixture; idempotent on re-run; never invents an entry for a
  value with no existing affinity key (the dir-derived default already applies).
- `set_z_override` / `del_z_override` — upsert / `changed: bool`; list style
  preserved.
- `set_affinity` / `del_affinity` — upsert, bodies ∉
  `trait_config.VALID_BODIES` rejected early (mirror `_check_bodies`). Sort the
  body list **only on entries the primitive touches** — 19 of the 515 existing
  entries are unsorted, and a blanket sort would turn a one-line edit into a
  20-line diff.
- `add_exclusion` / `del_exclusion` — `excludes` parsed from `Layer:*` /
  `Layer:V1,V2`; the emitted shape must pass `_check_exclusions` (`values`
  always a list or the literal `"*"`, never a bare scalar).
- `add_layer` — inserts in z-sorted position, flow style matching siblings;
  duplicate name → error.
- `reorder_layers(doc, names)` — permutation check, rewrite z as 10,20,30…,
  return a **z_override drift analysis** per spec §7: each override's relative
  position in the old scale (between which two layers, or above-all/below-all)
  vs the new, as structured rows the CLI renders as a table. Tests: (a) a
  reorder preserving every override's relative position → all rows `unchanged`,
  no drift flag; (b) a reorder that moves `Accessory/Retardio` (z=45, currently
  below `Mouth` z=50) relative to `Mouth` → drift flagged.

Each primitive returns a short human-readable change summary, reused by CLI
output and any future Discord flow.

## Task B3 — validation + satisfiability pipeline

**Tests first:**
- `test_validate_rejects_bad_edit` — a primitive-produced doc with an unknown
  layer in a `z_override` → the pipeline surfaces `load_config`'s error and
  nothing is written.
- `test_pipeline_runs_the_real_script_gate` — the pipeline invokes
  `scripts.validate_trait_config.main([...])` so the #301 typo guard is
  included; assert a seeded typo-art file makes the pipeline fail.
- `test_exhaustive_gate_catches_stranded_body` — shares Phase A / Task A1's code
  path; assert the same error text.
- `test_sampled_gate_catches_exclusion_deadend` — only meaningful once
  `exclusions` is non-empty; seeded `random.Random`, assert the failure
  reproduces and the message includes the seed.
- `test_gates_skip_on_empty_layer_tree` — warnings only.

**Implement** `validate_pipeline(doc, layers_dir, draws=200) -> PipelineResult(
errors, warnings, diff)`: dump to a temp file → run the validator script's
`main()` → the exhaustive per-body × layer check (shared with Phase A) →
optional seeded `select_random_attributes` draws with a bare
`sqlite3.connect(":memory:")` (safe: `weighted_pick` self-bootstraps via
`ensure_schema` + `_ensure_rows`, and `recalculate_rarity` returns early with no
`LFG` table) → a `difflib` unified diff. **Document in the docstring** that the
in-memory conn has everything `enabled=1`, so the sampled gate is blind to the
`All traits disabled for <body>/<category>` failure mode; a `--db` pointing at
the real app DB is the way to exercise it.

## Task B4 — write path: backup, dirty-guard, atomic replace

**Tests first (tmp git repo fixture):**
- dirty `trait_config.yaml` → refuses without `--force-dirty`.
- a successful write creates
  `reports/trait_config_backups/trait_config.<ts>.yaml` plus the new content;
  the write goes through `os.replace`.
- a no-op edit (`del-*` of a missing entry) → exit 0, no write, no backup.
- retention: 25 pre-existing backups → a write leaves exactly the 20 newest
  (sorted-glob unlink loop).
- **deploy-checkout refusal:** a target path inside a deployer-managed checkout
  → refuses without the explicit override flag (spec §7).

**Implement** `commit_edit(path, new_text, force_dirty=False)` with the
keep-last-20 prune. **No `.gitignore` change** — `reports/` is already ignored.

## Task B5 — CLI (`scripts/trait_config_edit.py`)

**Tests first** (invoke `main(argv)` directly, no subprocess):
- `set-z --dry-run` prints the diff, exits 0, file untouched.
- an invalid edit exits 1 with the engine's error text.
- a satisfiability failure exits 2.
- `--yes` writes; success output contains the literal restart line
  `pm2 restart lfg-activity`.
- `add-layer` output contains `TRAIT_ORDER` and
  `test_default_config_parity_with_legacy_constants`.
- `check` runs the pipeline on the current file with no mutation.
- `grant-body --body milady --from-body female --dry-run` produces a
  multi-entry diff and exits 0.
- `reorder` with a drifting z_override → exit 2 with the drift table on stdout;
  the same invocation with `--accept-z-drift` writes and still prints the table.

**Implement** argparse per spec §7; show the interactive confirm prompt only
when stdin is a TTY and neither `--yes` nor `--dry-run` is present.

## Task B6 — docs + wiring

- CLAUDE.md: a short subsection under the rules-engine notes — subcommand
  examples, the `lfg-activity` restart requirement, `check` usage, and the
  "never run this inside `~/LFG` / `~/LFG-staging`" rule. (The direct-to-main
  docs rule does not apply; this rides the code PR.)
- One integration test: edit → write →
  `validate_trait_config.main(["--config", written, "--layers-dir", fixture])`
  on the result → exit 0. (Pass both flags explicitly; a bare `main([])`
  defaults to the repo's own `trait_config.yaml` and `layers/`, not the
  fixture.)
- README mention only if a natural spot exists; do not force it.
  `scripts/check_repo_layout.py` requires README-tree entries only for
  `lfg_core/*_flow.py` modules and `surfaces/<pkg>/` packages, so neither new
  file obliges a README edit.

## Task B7 — ship Phase B

1. Full suite `.venv/bin/python -m pytest -q`; `ruff format`.
2. Manual smoke on the real file from a **non-deployed** checkout:
   `scripts/trait_config_edit.py --dry-run set-z --trait-type Eyes --value Wavy
   --z 95` (an idempotent no-op) and one real `--dry-run` change; verify the
   diff is minimal and the comments are intact.
3. PR ready, title `feat(#39): guided CLI for trait_config.yaml authoring`.
   Both bots; close every finding on its own thread.
4. After merge: comment on #39 with blob permalinks at the merge SHA, note that
   the Discord and web surfaces remain out of scope (spec §10), and close or
   split #39 per the user's preference.

## Risks / open questions

- **Phase A Task A1's manual check is the real go/no-go for Phase A**: if the
  live config already strands a (body, layer), arming the hook turns every push
  red. Run it before touching `.pre-commit-config.yaml`.
- **Phase A Task A2 raises pre-push cost slightly** for everyone; the sibling
  `audit-layer-dimensions` hook already walks the whole art tree on every push
  (decoding only new/changed files, cached per `(size, mtime)`), so the
  precedent for an `always_run` art-tree hook is established. The new check
  only lists directories — no decode. Neither is timed here.
- **Phase B Task B1 remains the architecture go/no-go**, and the downside is
  only partly bounded — spec §8's canonical-emitter fallback is concrete but
  costs comparable work once the 21 comment lines and the unsorted body lists
  have to be carried in the model.
- **Sampled-gate cost:** 200 draws × 5 bodies repeatedly hits the layer store;
  fine locally (directory listings), but keep `N` configurable (`--draws`).
- **`reorder` × absolute `z_overrides`** stays the sharpest edge; the drift
  table + exit 2 + `--accept-z-drift` is the defined behavior, and the tool
  never auto-remaps.
- **Parallel sessions:** the dirty-guard (Task B4) is the mitigation; also
  re-check `git branch --show-current` before committing.
