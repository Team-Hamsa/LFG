# Trait-config authoring CLI (#39) — Implementation Plan

**Spec:** `docs/superpowers/specs/2026-07-05-trait-config-tooling-design.md`
**Branch:** new feature branch off `main` (check `git branch --show-current`
first — parallel sessions run in this repo). **Draft PR** → CodeRabbit →
ready-for-review, per house rules. TDD throughout: each task writes the
failing test first.

New files:
- `lfg_core/trait_config_edit.py` — ruamel round-trip load/dump + pure edit
  primitives + validation/satisfiability pipeline.
- `scripts/trait_config_edit.py` — argparse CLI over the primitives.
- `tests/test_trait_config_edit.py` — **must start with the env-guard
  preamble copied verbatim from `tests/test_seasons.py` lines 1-18** (it
  imports lfg_core at module top; see test-env-guard memory).

New dependency: `ruamel.yaml>=0.18` in `requirements.txt` (editor-only; the
engine keeps PyYAML `safe_load`).

---

## Task 1 — Round-trip fidelity spike (gates the whole approach)

**Test first:** `test_roundtrip_is_byte_identical` — load the real
`trait_config.yaml` with `ruamel.yaml.YAML(typ="rt")`, dump to a string,
assert byte-equal to the original file. Also
`test_roundtrip_single_edit_minimal_diff`: change one affinity value's body
list, dump, assert the unified diff against the original touches ≤ 2 lines
and every comment line survives.

**Implement:** `load_doc(path)` / `dump_doc(doc) -> str` in
`lfg_core/trait_config_edit.py` (width=100, preserve quotes).

**Decision gate:** if byte-identity fails on cosmetic grounds (e.g. flow-map
spacing), relax to "semantic-equal + comments preserved + diff-minimal" and
record the delta in the test docstring. If comments/flow style are lost,
STOP — fall back to the line-edit alternative in spec §9 and re-plan.

## Task 2 — Edit primitives (pure, no I/O)

**Tests first**, one per primitive, on a small fixture YAML (comments + flow
style included) plus the real file where cheap:
- `set_z_override(doc, trait_type, value, z)` — upsert; preserves list style.
- `del_z_override(doc, trait_type, value)` — returns `changed: bool`.
- `set_affinity(doc, trait_type, value, bodies)` — upsert, bodies sorted,
  rejects bodies ∉ `VALID_BODIES` early (mirror trait_config.py:171-174).
- `del_affinity(doc, trait_type, value)`.
- `add_exclusion(doc, trait_type, value, excludes)` / `del_exclusion(...)` —
  `excludes` parsed from `Layer:*` / `Layer:V1,V2` strings; emitted shape must
  pass `_check_exclusions` (values always a list or literal `"*"` — never a
  bare scalar, per trait_config.py:177-225).
- `add_layer(doc, name, z, shared)` — inserts in z-sorted position, flow
  style matching siblings; duplicate name → error.
- `reorder_layers(doc, names)` — permutation check against existing names,
  rewrite z as 10,20,30…; returns a **z_override drift analysis** per spec
  §4: for every override, its relative position in the old scale (between
  which two layers, or above-all/below-all) vs the new scale, as structured
  rows the CLI renders as a table. **Tests:** (a) reorder preserving every
  override's relative position → all rows `unchanged`, no drift flag;
  (b) reorder with an existing override whose relative position *changes* →
  primitive flags drift (CLI behavior tested in Task 5: exit 2 + table
  unless `--accept-z-drift`; with the flag, writes and still prints the
  table).

Each primitive returns a short human-readable change summary string (reused
by CLI output and any future Discord flow).

## Task 3 — Validation + satisfiability pipeline

**Tests first:**
- `test_validate_rejects_bad_edit`: primitive-produced doc with an unknown
  layer in a z_override → pipeline returns errors from `load_config`, nothing
  written.
- `test_exhaustive_gate_catches_stranded_body`: fixture layer store (tmpdir
  `LocalLayerStore`) + affinity that removes every Head value for `skeleton`
  → gate fails naming body+layer (mirrors traits.py:58-64).
- `test_sampled_gate_catches_exclusion_deadend`: exclusion set that can
  strand a draw; seeded `random.Random`, assert failure reproduces and the
  message includes the seed.
- `test_gates_skip_on_empty_layer_tree`: empty store → warnings only (same
  degradation as trait_config.py:124-135).

**Implement** `validate_pipeline(doc, layers_dir, draws=200) ->
PipelineResult(errors, warnings, diff)`:
1. dump doc → temp file (scratchpad-style tmpdir) → `load_config`.
2. `validate_against_store(cfg, LocalLayerStore(layers_dir))`.
3. Exhaustive per-body×layer `value_allowed` filter check.
4. If cfg.exclusions non-empty: N seeded `select_random_attributes` draws per
   body with a bare `sqlite3.connect(":memory:")` conn — **verified safe**:
   `rarity.weighted_pick` self-bootstraps via `ensure_schema(conn)` +
   `_ensure_rows(...)` (lfg_core/rarity.py:153-154), so an empty DB yields
   uniform floor weights, and `recalculate_rarity` guards for a missing LFG
   table before querying (rarity.py:197). No fixture seeding needed. Any
   ValueError → error with body/layer/seed.
5. Unified diff (difflib) original vs new text.

## Task 4 — Write path: backup, dirty-guard, atomic replace

**Tests first (tmp git repo fixture):**
- dirty `trait_config.yaml` → refuses without `--force-dirty`.
- successful write creates `backups/trait_config/trait_config.<ts>.yaml` and
  the new file content; write is via `os.replace`.
- no-op edit (del of missing entry) → exit 0, no write, no backup.
- **retention:** with 25 pre-existing backups in `backups/trait_config/`, a
  write leaves exactly 20 (the 20 newest by timestamped filename) — prune is
  a sorted-glob unlink loop over `trait_config.*.yaml` after the new copy.

**Implement** `commit_edit(path, new_text, force_dirty=False)` including the
keep-last-20 prune. **Add `backups/` to `.gitignore`** — it is a new path,
nothing ignores it today (verified: no existing rule matches); include the
`.gitignore` line in this PR.

## Task 5 — CLI (`scripts/trait_config_edit.py`)

Copy the env-guard `os.environ.setdefault` block from
`scripts/validate_trait_config.py` (it imports layer_store the same way).

**Tests first** (invoke `main(argv)` directly, no subprocess):
- `set-z --dry-run` prints diff, exits 0, file untouched.
- invalid edit exits 1 with the engine's error text.
- satisfiability failure exits 2.
- `--yes` writes; success output contains the literal restart line
  `pm2 restart lfg-activity lfg-bot lfg-telegram`.
- `add-layer` output contains `TRAIT_ORDER` and
  `test_default_config_parity_with_legacy_constants`.
- `check` subcommand runs pipeline on the current file with no mutation.
- `reorder` with a drifting z_override → **exit 2** with the drift table on
  stdout; same invocation `--accept-z-drift` → writes, table still printed.

**Implement** argparse per spec §4; interactive confirm prompt only when
stdin is a TTY and `--yes`/`--dry-run` absent.

## Task 6 — Docs + wiring

- CLAUDE.md: short subsection under the rules-engine notes — subcommand
  examples, restart requirement, `check` usage. (Direct-to-main docs rule
  does NOT apply — this rides the code PR.)
- Ensure `scripts/validate_trait_config.py` still passes on an
  editor-written file (add one integration test: edit → write → run
  `validate_trait_config.main([])` on the result → exit 0).
- README/tooling mention only if a natural spot exists; do not force it.

## Task 7 — Ship

1. Full suite: `.venv/bin/python -m pytest -q` (watch for env-guard/order
   issues — run full suite, not just the new file).
2. `ruff format` (pre-push runs it anyway).
3. Manual smoke on the real file:
   `scripts/trait_config_edit.py --dry-run set-z --trait-type Eyes --value Wavy --z 95`
   (idempotent no-op) and one real `--dry-run` change; verify diff is
   minimal and comments intact.
4. Draft PR (`gh pr create --draft`), title
   `feat(#39): guided CLI for trait_config.yaml authoring`; wait for
   CodeRabbit after flipping ready; resolve findings.
5. After merge: comment on #39 with permalinks (blob URLs at merge SHA) to
   this spec + plan per the CLAUDE.md workflow rule, and note that the
   Discord/web surfaces remain phase-2 (spec §8) — keep #39 open or split a
   phase-2 issue per user preference.

## Risks / open questions

- **Task 1 is the go/no-go**: ruamel fidelity on this exact file decides the
  architecture; do it first, alone, before any other code.
- Sampled gate cost: 200 draws × 4 bodies hits the layer store repeatedly —
  fine locally (LocalLayerStore is dir listings); keep N configurable
  (`--draws`).
- `reorder` + absolute z_overrides is the sharpest edge but now has defined
  behavior (drift table + exit 2 + `--accept-z-drift`, spec §4); the residual
  risk is only interpreting an author's *intent* for an override ("above
  everything" vs "between these two") — the tool deliberately never
  auto-remaps.
- Parallel sessions: dirty-guard (Task 4) is the mitigation; also re-check
  branch before committing (memory: concurrent-sessions).
