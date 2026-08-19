# Admin tooling for authoring trait_config.yaml (#39) — Design

**Issue:** Team-Hamsa/LFG #39 "Admin tooling for authoring trait_config.yaml
(config-gen UI)" — OPEN, label `roadmap`.
**Date:** 2026-07-05
**Last review:** 2026-08-19
**Status:** live — re-reviewed 2026-08-19 against main@27dc301

The spec/plan links first commented on #39 belong to the rules *engine* (#40,
`2026-07-04-trait-rules-body-affinity-*`). This is the spec for the tooling.

## 0. What changed since this was drafted

- **milady is a fifth body** (#171, commit `2f616fb`, 2026-07-11).
  `trait_config.VALID_BODIES` is now `{ape, female, male, milady, skeleton}`;
  the July draft's four-body set was already wrong at that commit.
- **The file barely moved.** Today: 550 lines, 9 `layers`, 5 `z_overrides`,
  3 `swap_matrix` pairs, 515 affinity value entries, `exclusions: []` and
  `inclusions: []` still empty. The draft's "548 lines" was exactly right at
  `8a3e440` (`git show 8a3e440:trait_config.yaml | wc -l` → 548), and so was
  its "~20 comment lines": **21 lines carry a `#`** — the 3-line header, one
  trailing comment on the Retardio z-override, and **17 per-value
  `# low sample: N mint(s) — owner-confirmed 2026-07-04` annotations inside
  the affinity block**. Only the draft's parenthetical was wrong: they are
  not "section comments". (`grep -c '^\s*#'` returns 3 because 18 of the 21
  are *trailing*, not whole-line — a count that misses them understates the
  round-trip surface §8 has to preserve.)
- **A local-only admin web UI shipped** — `scripts/trait_dashboard.py` (#202,
  merged 2026-07-13): aiohttp, loopback-bound, reached over `ssh -L`, editing
  the live `trait_rarity` table with no restart. It is the closest living
  relative of what #39 asks for, and PR #202's own body scopes
  `trait_config.yaml` authoring out to #39. This reverses §3 of the draft,
  which ruled the web panel out via `2026-07-05-web-ui-rescope-design.md` §5 —
  that deferral was about burn buttons on the *public* origin, and #202 said so.
- **…but the dashboard only writes gitignored state** (`trait_rarity` in the
  app DB, `reports/trait_dashboard_audit.log`). `trait_config.yaml` is a
  **tracked** file, and both deployed checkouts are polled by
  `scripts/deployer.py`, which advances with `git merge --ff-only`
  (`run_once`). Locally modifying a tracked file on the box can abort that
  merge and stall the stack — the hazard CLAUDE.md already documents for
  `sourcetag_metrics.py --out`. An on-box config editor is a **worse** risk
  class than the dashboard, not an equal one.
- **The satisfiability failure this spec wanted to move to authoring time
  already happened in production.** PR #145 (#38, 2026-07-09): removing the
  ape `None.png` placeholders left `ape/Eyebrows` with zero affinity-legal
  values, so `select_random_attributes` raised and **every ape mint failed**.
  `scripts/validate_trait_config.py` did not catch it and still cannot.
- **The hook that would run such a check is mis-filtered.**
  `.pre-commit-config.yaml`'s `validate-trait-config` uses
  `files: ^(trait_config\.yaml|layers/)`. The art tree is gitignored
  (`.gitignore:15-17` is `layers/**` with `!layers/` and
  `!layers/seasons.json`), so no *art* path is ever staged; the one tracked
  path under `layers/` is the `seasons.json` sidecar, touched 3 times ever
  (`fbb15f5`, `3fe0d69`, `813b82d`). In practice the hook fires only when the
  YAML itself changes — never when the art tree changes, which is how #145
  happened. The sibling `audit-layer-dimensions` hook documents the same trap
  (its comment: "layers/ is gitignored, so file filters never match") and uses
  `always_run: true` instead. CI runs `pre-commit --all-files`, so the hook
  does run there, but CI has no art, so `validate_against_store` takes its
  empty-tree early-out and only structural validation happens. #39's own
  status comment (2026-07-05) nevertheless records the config as "guarded by
  scripts/validate_trait_config.py in pre-commit + CI" — that is the belief
  this bullet corrects.
- **A second class of over-constraint the draft didn't model:**
  `rarity.weighted_pick` selects `WHERE … enabled=1` and raises
  `All traits disabled for {body}/{category} on {network}`. Live mainnet
  `trait_rarity` state cannot be read from this checkout; per issue #198's
  body, the mainnet stopgap of 2026-07-12 set `enabled=0` on "8 Clothing,
  Accessory >3% (excl. Bible), Eyes >3%, and season-3 green/white/grey
  bodies" — i.e. whole groups are parked disabled, on that dated account
  rather than by direct inspection. `rarity.set_enabled` — used by both
  `scripts/rarity_admin.py disable` and the #202 dashboard's `apply_toggle` —
  has **no** last-one-standing guard, while `lfg_core.seasons.disable_season`
  refuses exactly that. So the "can't produce an invalid state" property #39
  is about is missing from the rarity tool that exists, not only from the
  config tool that doesn't.
- **Second raise path added:** `lfg_core/traits.py::fill_missing_face_traits`
  (#168) raises the same `trait rules leave no legal …` error on the legacy-ape
  face-fill path, so an over-constrained config now breaks *swaps* too, not
  just mints.
- **Affinity gained a display consumer.** `lfg_core/trait_images.py`
  `trait_image_url` picks a thumbnail body from `cfg.allowed_bodies`, feeding
  the `/api/layer` tiles across marketplace/shop/economy UIs
  (`lfg_service/app.py`, `webapp/mock_market.py`). `get_config()` call sites
  grew from the draft's 7 (in 4 modules) to 16 (in 5), all inside one
  process — see §9.
- **Measured edit rate contradicts the premise.** `git log -- trait_config.yaml`
  is **three commits, ever**: `8158136` (2026-07-04, #123), `cee0c8a`
  (2026-07-09, #145), `2f616fb` (2026-07-11, #171). Nothing in the 5+ weeks
  since. The July draft's §8 named "config edit frequency > ~weekly" as a
  trigger — for revisiting the *deferred web editor*, strictly — but the same
  measurement undercuts the CLI it did decide to build, since that CLI's
  stated value was making frequent hand edits safe.
- **The one substantial edit was bulk and predicate-driven, not surgical.**
  #171 (+97/−95 lines in one commit) added `milady` to **95 affinity entries**
  — all 59 female-allowed Clothing values (PR #171's body: "all 59
  female-allowed Clothing affinity entries now include `milady`") plus 36
  Accessory values — and two structural lines (a `swap_matrix` pair and the
  Retardio z-override comment). The per-value `set-affinity` subcommand below
  would barely have helped.
- **`scripts/validate_trait_config.py` grew a #301 guard**
  (`find_typo_body_files`, single-r `Iridescent ` Body-art denylist), so
  "run the same gate the hook runs" now means calling that script's `main()`,
  not just `load_config` + `validate_against_store`.
- **Test env isolation landed (#323).** The root `conftest.py` sets
  `LFG_SKIP_DOTENV=1` and pins every `_require` var, so per-file env-guard
  preambles are no-ops; new scripts must call
  `lfg_core.envload.load_dotenv_unless_skipped()`, never a bare `load_dotenv()`.
- **`.webm` layers are supported** (`layer_store.LAYER_EXTENSIONS =
  (".png", ".gif", ".webm", ".mp4")`), but `scripts/trait_dashboard.py`
  `_LAYER_EXTS` and `scripts/audit_body_affinity.py` `_dir_tree` still hand-roll
  `(".png", ".gif", ".mp4")`. New tooling must use `LAYER_EXTENSIONS`.
- **Line-number citations in the draft rotted** and are replaced with symbol
  names throughout: the `weighted_pick` self-bootstrap 153-154→189-190, the
  `recalculate_rarity` missing-`LFG` guard 197→270, `AdminView` 386→522
  (6 buttons→10, three rows), `economy_api` 185→235/298, `swap_compose`
  →34/70/102 (the draft's 23/59/91 were already off: at `8a3e440` the sites
  were 23/56/88). One draft citation did *not* rot — `def get_config` is at
  `lfg_core/trait_config.py:315` both at `8a3e440` and today; the draft's
  ":312" was the start of the surrounding `_config` block.
- Still true: `backups/` is un-ignored (`git check-ignore` exits 1);
  `reports/` is ignored at `.gitignore:37` and is where #202 already writes.

## 1. trait_config.yaml as of main@27dc301

- **File:** `trait_config.yaml` at repo root, 550 lines, 21 of which carry a
  comment (3-line header, 1 trailing on the Retardio z-override, 17 trailing
  `# low sample: …` annotations on affinity values). `layers` / `z_overrides`
  / `swap_matrix` use column-aligned **flow-style** entries
  (`- {name: Background, z: 10, shared: true}`); affinity values are quoted
  keys mapping to body lists (`"Balloon": [female, male, milady]`) — the same
  *line shape* `lfg_core/affinity_audit.py::render_affinity_yaml` emits, but
  **not** its byte-for-byte output: 19 of the 515 body lists are not
  alphabetically sorted (e.g. `"Banana": [ape, female, male, skeleton,
  milady]`) while the renderer sorts, and the 17 low-sample comments are
  hand-written (the renderer emits `# LOW CONFIDENCE: only N mint(s) — verify
  by eye`). See §8.
- **Schema** (`lfg_core/trait_config.py`; symbols verified present):
  - `version: 1` required (`load_config`).
  - `layers`: `[{name, z, shared?}]` → `LayerSpec`; duplicate names and an
    empty list are load errors.
  - `z_overrides`: `[{trait_type, value, z}]` → `ZOverride`; unknown layer is
    a load error.
  - `affinity`: `{trait_type: {value: [bodies]}}`; bodies must be in
    `VALID_BODIES = {ape, female, male, milady, skeleton}`.
  - `swap_matrix`: `universal_layers: [...]` + `pairs: [{bodies, layers |
    layers_except}]` → `SwapPair`; exactly one of `layers`/`layers_except`.
  - `exclusions`: shape strictly validated by `_check_exclusions` (including
    the bare-scalar-`values` substring-match footgun and unknown-layer
    references) — **still an empty list** 6 weeks on.
  - `inclusions`: scaffolded, still **no consumer**; only "is a list" is
    validated. **Out of scope for any editor** — do not offer authoring for a
    field nothing reads.
- **Validation entry points:** `load_config(path)` (structural, raises
  `TraitConfigError`); `validate_against_store(cfg, store)` (async cross-check
  against the `layers/**` tree, returns `(errors, warnings)`, early-outs to
  structural-only on an empty tree); `scripts/validate_trait_config.py` (the
  CLI gate that wraps both **plus** the #301 typo-art denylist).
- **What `validate_against_store` does NOT check:** that each (body × layer)
  still has ≥1 affinity-legal value. Its only error is "affinity claims bodies
  but no file exists in any claimed dir"; the reverse ("this body's dir has
  values but every one is vetoed") is not modelled at all. That is precisely
  the #145 outage.
- **Engine consumption:** lazily cached module-global singleton —
  `get_config()` loads once into `_config` and never reloads. `reset_config()`
  exists but is called only from tests (`tests/test_ape_face.py`,
  `tests/test_traits_affinity.py`, `tests/test_swap_cross_body_api.py`,
  `tests/test_trait_config.py`, `webapp/test_economy_api.py`).
  `get_config()` callers: `lfg_core/traits.py`,
  `lfg_core/swap_compose.py`, `webapp/economy_api.py`, `webapp/mock_market.py`,
  `lfg_service/app.py`. `lfg_core/trait_images.py` takes a `TraitConfig`
  argument instead of fetching one.
- **Existing fail-loud behavior:** `select_random_attributes` raises
  `ValueError("trait rules leave no legal <layer> value for body '<b>'")` when
  rules eliminate every candidate; `fill_missing_face_traits` raises the same
  on the ape face-fill path; `rarity.weighted_pick` separately raises
  `ValueError("All traits disabled for <body>/<category> on <network>")` when
  no candidate has an `enabled=1` row. All three fire **at mint/swap time, in
  production**.
- **Discord `/admin` today** (`surfaces/discord_bot/admin.py`): `AdminView`
  with 10 buttons over 3 rows (stats, lookup, burn, view-odds, boost-trait,
  toggle-trait, pause-X, sponsored-mint start/stop/refresh), heavier ops behind
  `Modal`s (max 5 text inputs each; selects cap at 25 options). Nothing touches
  trait_config. Discord views cap at 5 rows × 5 components, so a "Trait rules"
  button still fits — barely.

## 2. Who edits this — measured, not assumed

The July draft guessed "occasional and surgical". The git history says
otherwise, and says less:

| commit | date | shape |
|---|---|---|
| `8158136` (#123) | 2026-07-04 | file created by the rules-engine PR |
| `cee0c8a` (#145) | 2026-07-09 | bulk: add `ape` to all 160 face values |
| `2f616fb` (#171) | 2026-07-11 | bulk: add `milady` to 95 affinity entries — all 59 female-allowed Clothing values + 36 Accessory values (+97/−95) |

Three commits, zero in the last 5 weeks, and **no surgical single-value edit
has ever happened**. Both real edits were predicate-driven bulk rewrites
derived from the layer tree ("every value this body has art for") — a
different tool from the per-value `set-*` CLI designed in §7. There is still no
community-admin audience; the editors are the user and dev sessions, and the
workflow is "edit YAML on a dev checkout, commit, PR, promote".

## 3. The real gap

#39's stated requirement is *"the UI can't produce an invalid config"*. The
measured production incidents are not authoring typos — they are **config ×
layer-tree divergence**, and the guard rails in the repo do not cover them:

1. **Zero legal values for a (body, layer).** Caused a total ape-mint outage
   (#38 → PR #145). Triggered by deleting art, with **no config diff at all**,
   so even a perfect editor would not have prevented it. Not checked by
   `validate_against_store`.
2. **The pre-push hook cannot see it anyway.** `files: ^(trait_config\.yaml|layers/)`
   never matches an art path, because the art tree is gitignored — the only
   tracked path it could match is the `layers/seasons.json` sidecar. The
   machines that have the art (dev box, deploy box) are exactly the ones that
   skip the hook unless the YAML itself changed.
3. **Zero *enabled* values for a (body, category).** `rarity.set_enabled` has
   no last-one-standing guard, so `scripts/rarity_admin.py disable` and the
   #202 dashboard toggle can each break minting for a body in one click —
   while `seasons.disable_season` refuses the identical operation.

None of the three needs a UI. All three are gate work.

## 4. Surface choice, re-decided

| | Discord `/admin` embed | Local web panel (a `trait_config` tab in `scripts/trait_dashboard.py`) | Guided CLI (`scripts/trait_config_edit.py`) |
|---|---|---|---|
| Precedent | none for config | **#202 shipped exactly this shape** (loopback + ssh tunnel, no auth surface) | `rarity_admin.py`, `disable_season_traits.py` |
| Where it runs | the bot host (a deployed checkout) | the deploy box (that is where the dashboard is tunnelled to) | a dev checkout |
| Writes | tracked `trait_config.yaml` on a deployer-polled checkout | same | tracked file on a branch, reviewed as a diff |
| Deploy hazard | **yes** — `deployer.py`'s `merge --ff-only` can abort on a locally modified tracked file | **yes**, same | none |
| Fit to measured edits (§2) | modal-shaped ops only; cannot express "every value milady has art for" | could, with real work | can, with predicate subcommands |
| Effort | medium | high | low |

The July decision (guided CLI) survives, but **for a different and stronger
reason**: not "the web panel is deferred by web-ui-rescope §5" (dead argument —
#202 shipped a local web admin panel and said so in its PR body), but
**"trait_config.yaml is a tracked file and the deployed checkouts are
ff-only-polled, so config authoring belongs on a dev branch, not on the box"**.
That same fact is what keeps a config tab *out* of the #202 dashboard even
though the dashboard is otherwise the obvious host.

## 5. Recommended scope

Split #39 in two and build only the first half now.

- **Phase A — arm the gate (recommended, small).** No new surface, no new
  dependency. Closes the failure modes in §3, which is the part of #39 that has
  actually cost production time. Detail in §6.
- **Phase B — the guided CLI (designed, deferred).** Keep the design in §7-§8
  intact so it can be picked up without re-derivation, but do not build it
  against a measured edit rate of three commits ever. Revisit triggers: the
  first non-empty `exclusions` rule set, a sixth entry in
  `trait_config.VALID_BODIES`, or a real community-admin audience.

If only one thing is built from this document, it should be §6.

## 6. Phase A — arm the gate

All of this lands in existing files.

- **`scripts/validate_trait_config.py`: add a legal-values check.** For each
  body from `store.list_bodies()` and each layer in `swap_meta.TRAIT_ORDER`,
  compute `raw = await store.list_values(body, layer)` and `legal = [v for v in
  raw if cfg.value_allowed(body, layer, v)]`. `raw` non-empty with `legal`
  empty is an **error** naming body + layer — the deterministic mirror of the
  `select_random_attributes` raise, minus randomness. Guard it with the same
  empty-tree early-out `validate_against_store` already uses, so CI and fresh
  checkouts are unaffected.
  - **Known false-positive source:** `list_bodies()` is disk-derived, while
    the body actually minted comes from `weighted_pick(BODY_SENTINEL,
    BODY_CATEGORY, bodies)`, which skips `enabled=0` body rows. A body dir
    that is parked disabled in `trait_rarity` (see the #198 stopgap above) can
    therefore be flagged for a (body, layer) production never reaches. Decide
    at implementation time whether to consult `trait_rarity` or to accept the
    stricter reading; Task A1's manual run is what surfaces it.
- **`.pre-commit-config.yaml`: give `validate-trait-config` `always_run: true`**
  and drop the dead `files:` filter, mirroring the `audit-layer-dimensions`
  hook two entries below (whose comment already explains why file filters can
  never match a gitignored tree). Cost: one extra run per push of a script
  that only lists directories — no image decode, unlike
  `audit_layer_dimensions.py`. Not timed here.
- **`lfg_core/rarity.py`: give `set_enabled` a last-one-standing guard** when
  disabling — refuse if it would leave zero `enabled=1` rows for that
  `(network, body, category)`, same posture and message shape as
  `seasons.disable_season`. Both callers (`scripts/rarity_admin.py`,
  `scripts/trait_dashboard.py::apply_toggle`) inherit it. **Do not raise a bare
  `ValueError`**: the dashboard's `_error_mw` maps every `ValueError` to
  **404** ("missing (body, category, trait) row"), so a refusal would be
  reported as "not found". Raise a distinct exception type, or add a branch to
  that middleware, so the refusal answers 409/400.
- **Not in Phase A:** a sampled/draw-based gate for `exclusions`. `exclusions`
  is empty and has been for 6 weeks; the deterministic check above is complete
  for affinity, the only rule kind in use. Add the sampled gate in the same PR
  as the first real exclusion rule (design retained in §8).

## 7. Phase B — guided CLI (`scripts/trait_config_edit.py`), deferred

Design retained. Subcommands share one pipeline:
**parse → mutate → validate → diff → (confirm) → backup → write.**

```
.venv/bin/python scripts/trait_config_edit.py [--config PATH] \
    [--layers-dir layers] [--db PATH] [--draws N] \
    [--dry-run] [--yes] [--force-dirty] <subcommand> ...
    # --db: rarity DB for the sampled gate (default: in-memory; see §8)
    # --draws: sampled-gate draws per body (default 200)

  set-z | del-z          --trait-type Eyes --value Laser [--z 95]
  set-affinity |
  del-affinity           --trait-type Accessory --value Knife
                         --bodies male,milady
  grant-body             --body milady [--trait-type Clothing]
                         [--from-body female]
                         # bulk: add BODY to every entry it has art for
                         # (or that already allows --from-body). THE shape
                         # both real edits took (§2).
  add-exclusion |
  del-exclusion          --trait-type Head --value Crown
                         --excludes 'Accessory:*'
  add-layer              --name Aura --z 85 [--shared]
  reorder                --order Background,Back,... [--accept-z-drift]
  check                  # validate + satisfiability only, no edit
```

- `grant-body` is new in this revision and is the highest-value subcommand: it
  is what #145 and #171 actually needed. Its candidate set must come from
  `layer_store.LocalLayerStore.list_values` (body dir ∪ `shared/`), never a
  hand-rolled extension tuple.
- **Edit primitives in `lfg_core/trait_config_edit.py`** (pure functions over
  the parsed document plus a `Change` description); the script is argparse
  glue, so a later Discord flow can reuse the tested mutations.
- Exit codes: 0 written/clean, 1 validation failed, 2 satisfiability gate
  failed, 3 aborted at prompt. `--dry-run` prints diff + validation and exits;
  `--yes` skips the prompt.
- **`add-layer` caveat (still true):** `lfg_core/traits.py` carries the comment
  "Layers added only to trait_config.yaml won't mint until TRAIT_ORDER is
  updated too" (`lfg_core/swap_meta.py::TRAIT_ORDER`; parity test
  `tests/test_trait_config.py::test_default_config_parity_with_legacy_constants`).
  The subcommand prints this as a blocking notice naming the constant and the
  test. The editor never edits Python source.
- **`reorder` × `z_overrides` drift (unchanged, still the sharpest edge):**
  overrides carry *absolute* z — `Eyes/Wavy z=95` sits above the z=90 Accessory
  layer, and `Accessory/Retardio z=45` deliberately sits below Mouth z=50.
  Rewriting layer z to 10,20,… can silently move an override's *relative*
  position. The tool locates every override in the old and new scales and
  prints `override → old position → new position`; if any relative position
  changed it hard-fails **exit 2** unless `--accept-z-drift`. No automatic
  re-mapping — the author's intent ("above everything" vs "between these two")
  is not inferable.
- **No `inclusions` subcommand** (no consumer, §1).
- **Where it may run:** dev checkouts only. The tool should refuse to write
  when its target path resolves inside a deployer-managed checkout (`~/LFG`,
  `~/LFG-staging`) unless an explicit override flag is passed — §0 and §4 for
  why.

## 8. Round-trip safety (Phase B)

- **Primary: ruamel.yaml round-trip.** The file carries 21 comment-bearing
  lines — 3 header, 1 trailing on the Retardio z-override, and **17 trailing
  per-value `# low sample: …` annotations inside the affinity block** (§1) —
  plus deliberate flow style and column alignment. PyYAML `safe_dump` would
  destroy all of it — a 550-line diff for a one-line edit. `YAML(typ="rt")`
  preserves comments, flow style, quotes and key order. Cost: one pure-Python
  dependency, and it belongs in **`requirements-dev.txt`** (not
  `requirements.txt`) — the engine keeps `yaml.safe_load`, and §7 says the tool
  must not run on the deploy box, so the runtime dependency surface stays flat.
- **Fallback: a canonical emitter — weaker than it first looks.**
  `lfg_core/affinity_audit.py::render_affinity_yaml` emits the same line shape
  the file uses (`  <TraitType>:` / `    "<Value>": [a, b, c]`), so re-emitting
  the affinity block from a model is clearly feasible. But its output is **not**
  byte-equal to the committed file: it sorts body lists (19 of 515 entries in
  the file are unsorted) and it emits `# LOW CONFIDENCE: only N mint(s) —
  verify by eye`, not the 17 hand-written `# low sample: N mint(s) —
  owner-confirmed 2026-07-04` annotations. A canonical emitter therefore has
  to preserve *as data* the existing body-list order, the 17 affinity
  comments, the 3 header lines, the 1 z-override comment, and the column
  alignment in `layers`/`z_overrides` — or accept a one-time normalizing
  commit that reorders 19 lines and rewrites 17 comments. That is a real
  fallback, not line-surgery, but it is not free: budget it as comparable to
  the ruamel path, not as a cheap escape hatch.
- **Validate before write, through the real gate:** dump to a temp file, then
  run `scripts.validate_trait_config.main(["--config", tmp, "--layers-dir", …])`
  — the whole script, so the #301 typo guard and any future additions are
  included by construction. Non-zero → nothing written.
- **Satisfiability gate (authoring time):** the deterministic per-body × layer
  check of §6 (shared code), plus — only once `exclusions` is non-empty — N
  seeded `select_random_attributes` draws per body to catch order/combination
  dead ends. A bare `sqlite3.connect(":memory:")` is safe for those draws:
  `rarity.weighted_pick` calls `ensure_schema(conn)` and `_ensure_rows(...)`
  itself, and `recalculate_rarity` returns early when the `LFG` table is
  absent, so an empty DB degrades to uniform floor weights. **Note the blind
  spot this creates:** an in-memory DB has every trait `enabled=1`, so the
  sampled gate cannot see the `All traits disabled` failure mode the *real*
  mainnet rarity table can produce. Point a `--db` at the real app DB to
  exercise that dimension, or rely on the §6 `set_enabled` guard for it.
  `scripts/audit_layer_coverage.py` (on-chain coverage of *minted* NFTs) is a
  different question and is not part of this gate.
- **Backup / dirty-guard / atomic write:** copy to
  `reports/trait_config_backups/trait_config.<UTC-ts>.yaml` — `reports/` is
  already gitignored at `.gitignore:37` and is where #202 writes its audit log,
  which avoids the new-`backups/`-plus-`.gitignore`-line the July draft
  proposed. Keep the newest 20; refuse to write over a dirty
  `git status --porcelain trait_config.yaml` without `--force-dirty`; write via
  temp file + `os.replace`. `set-*` upserts; `del-*` on a missing entry exits 0
  with "no change" and writes nothing.

## 9. Restart semantics (corrected)

`get_config()` caches forever and no service calls `reset_config()`, so a
written config takes effect only after restarting the process that holds it.
The July draft's restart line was over-broad. Verified by walking the
transitive import graph from each pm2 entrypoint: `main.py` (`lfg-bot`),
`run_telegram.py` (`lfg-telegram`) and `scripts/onchain_listener.py` (the index
listeners) never reach `lfg_core.trait_config` — the surfaces route everything
through `lfg_service` over HTTP, and `main.py` is a shim. Only
`webapp/server.py` (`from lfg_service.app import main`) does, via
`traits` / `swap_compose` / `trait_images` / `economy_api` / `mock_market`. So
the only pm2 process holding a cached config is the service:

```
pm2 restart lfg-activity      # prod    (~/LFG, deploy branch, mainnet)
pm2 restart stg-activity      # staging (~/LFG-staging, main, testnet)
```

In practice the promote path (`scripts/promote.sh` → `scripts/deployer.py`
drain-restart) does this already; a hand restart is only for an out-of-band
edit, which §4 argues against. Hot reload stays unbuilt: the config also feeds
long-lived flows (mid-swap `swap_compose`, open mint sessions) and a mid-flow
config swap has unstudied consistency implications.

## 10. Not building

- **Drag-to-reorder / live-preview web editor** — the *risk* argument for
  deferring it is gone (#202); the *value* argument is stronger than ever
  (three config commits ever). Revisit only on a real non-dev admin audience.
- **A `trait_config` tab in `scripts/trait_dashboard.py`** — attractive host,
  wrong machine: it would write a tracked file inside a deployer-polled
  checkout (§0, §4).
- **Discord `/admin` trait-rules sub-flow** — same hazard, plus modals cannot
  address a 515-entry affinity map.
- **Affinity bulk re-derivation UI** — already solved:
  `scripts/audit_body_affinity.py` + `lfg_core/affinity_audit.py`
  (`render_affinity_yaml`) generate the whole block from mint history. It
  emits a *draft* for human review, not a drop-in replacement — see §8 for
  where its output differs from the committed file.
- **`inclusions` authoring** — blocked until a consumer PR defines the shape.
- **Hot reload** — §9.

## 11. Verified vs assumed

- **Verified against main@27dc301 by reading the tree:** the schema and every
  validator in `lfg_core/trait_config.py`; `VALID_BODIES` including milady; the
  file's size/shape/comment count (550 lines, 21 comment-bearing lines, 515
  affinity entries of which 19 have unsorted body lists);
  `get_config` caching and `reset_config`'s
  test-only callers; the three raise sites in `lfg_core/traits.py` and
  `lfg_core/rarity.py`; `set_enabled`'s missing guard vs
  `seasons.disable_season`'s present one; the `validate-trait-config` hook's
  dead `files:` filter vs `audit-layer-dimensions`' `always_run`; CI's
  `pre-commit run --all-files --hook-stage pre-push`; `deployer.py`'s
  `merge --ff-only`; the pm2 process lists in `ecosystem.prod.config.js` /
  `ecosystem.staging.config.js`; the transitive import graph from every pm2
  entrypoint (§9); `render_affinity_yaml`'s output shape *and* the two ways it
  diverges from the committed file (§8); `layer_store.LAYER_EXTENSIONS` vs the
  hand-rolled tuples; `git check-ignore` on `backups/` and `reports/`, and
  `git ls-files layers` (one tracked path, `seasons.json`).
- **Verified via gh:** #39 is OPEN with the `roadmap` label; PR #145's body
  ("Every ape mint fails", "all 160 face values"); PR #171's body ("all 59
  female-allowed Clothing affinity entries"); PR #202's body (loopback risk
  framing, and trait_config authoring explicitly left to #39); #198's body
  (the 2026-07-12 mainnet `enabled=0` stopgap). PRs #145/#171/#202 are all
  MERGED; #39 is the only OPEN item cited.
- **Not verifiable from this checkout — taken on a dated source:** live
  mainnet `trait_rarity` contents (which traits are actually parked
  `enabled=0` today). §0 and §6 attribute that to issue #198's body
  (2026-07-12); nobody has re-checked it since, and Phase A's `set_enabled`
  guard does not depend on it being still true.
- **Assumption (unchanged, still untested):** ruamel.yaml round-trips this
  file's flow style and alignment losslessly. Phase B's first task is a
  byte-diff spike; §8 names a fallback if it fails, but the fallback is
  comparable work, not a cheap escape.
- **Assumption:** the deterministic legal-values check in §6 has no false
  positives on the current tree. **Unverified here** — this was a docs-only
  review; the suite and the validator were not run. Phase A's first task is to
  run the new check against the real `layers/` tree and confirm it is silent
  before wiring it into the hook.
