# Equip fail-closed Build UI reconcile — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** After a fail-closed equip whose on-ledger outcome is unknown
(`equip_sync_indeterminate`) or whose revert did not land (`failed_revert`), the
Build UI must stop asserting "your save failed, here's your old look." Surface a
machine-readable `resolution` on the equip session, and have the client render
an honest "outcome uncertain — refresh, don't re-save" state (distinct from a
clean `reverted` failure) and gate further equips on that character until a
refresh.

**Architecture:** Two independent seams.
1. **Server signal** — a stored `resolution: str | None` on
   `EquipSession` (`lfg_core/economy_flow.py`), classified per terminal branch,
   surfaced through `webapp/economy_api.economy_session_dict` (equip branch).
2. **Client reconcile** — `webapp/client/app.js::saveBuild` branches on
   `final.resolution`; the three-way `committed | reverted | uncertain`
   classification is a pure helper in `webapp/client/build_pure.js`; a
   client-session `reconcileUncertainIds` Set drives a banner + a staging/save
   gate (same pattern as `harvestingIds` / `saveBusy`).

No new on-ledger transaction, no DB migration.

**Tech Stack:** Python 3 / aiohttp / asyncio / pytest; vanilla no-build JS client
(ES-module `build_pure.js` tested under Node).

## Global Constraints

- **No transaction is built or changed** by this work — `SourceTag = 2606160021`
  and provenance memos remain correct on the equip `NFTokenModify` (untouched).
  Do not add, remove, or reorder any ledger op.
- **Do not change the fail-closed taxonomy** (`ClosetError` /
  `ClosetMirrorError` / `ClosetIndeterminateError`), the revert logic, or the
  index-stamping policy. This is a read-side signal only.
- **Pre-push gate must pass** (ruff `--fix`, ruff-format, mypy from `.venv`,
  gitleaks, pytest, validate-trait-config). Never `--no-verify`. In a worktree,
  ensure the `.venv` symlink exists or the gate silently skips (see memory
  "Batched Build save").
- **Any `app.js` change bumps `app.js?v=` in `webapp/client/index.html`** in the
  same commit (currently `v=32` → `v=33`). If a new `build_pure.js` export is
  imported, its import cache key must be bumped in lockstep too.
- **Tests importing `lfg_core` at module top** carry the env-guard preamble
  (`os.environ.setdefault("BUNNY_PULL_ZONE", ...)` / `LAYER_SOURCE`). Pure-JS
  tests (`test_build_pure_js.py`) import no `lfg_core` and need no preamble.

---

### Task 1: Classify equip outcome — `resolution` on `EquipSession`

**Files:**
- Modify: `lfg_core/economy_flow.py` (`EquipSession` dataclass + `run_equip`
  terminal branches)
- Test: `tests/test_economy_flow_equip.py`

**Interfaces:**
- Produces: `EquipSession.resolution: str | None`
  (`"committed" | "reverted" | "uncertain" | None`), set before each terminal
  return in `run_equip`.
- Consumes: nothing new.

- [ ] **Step 1: Write the failing test(s).** Extend existing branch tests in
  `tests/test_economy_flow_equip.py` (module already has the env-safe imports and
  fakes — no new preamble needed):
  ```python
  def test_equip_indeterminate_sets_uncertain(tmp_path):
      conn, f = _conn_with_bucket(), _Fakes(raise_closet_modify=True)
      s = ef.EquipSession(owner="rUser", character=_char(), changes=[("Head", "Crown")])
      _run(ef.run_equip(s, _deps(conn, f, tmp_path)))
      assert s.state == ef.FAILED
      assert s.resolution == "uncertain"          # NEW
      record = json.loads((tmp_path / f"equip-{s.id}.json").read_text())
      assert record["status"] == "equip_sync_indeterminate"
  ```
  Add analogous assertions: `failed_revert` (undecodable-URI and
  revert-not-landing tests) → `resolution == "uncertain"`;
  `reverted_modify` test → `"reverted"`; happy path and mirror-failure test →
  `"committed"`.
- [ ] **Step 2: Run to verify they fail** —
  `.venv/bin/python -m pytest tests/test_economy_flow_equip.py -q`
  Expect `AttributeError: 'EquipSession' object has no attribute 'resolution'`
  (or assertion failures once the field defaults to `None`).
- [ ] **Step 3: Implement.** Add `resolution: str | None = None` to
  `EquipSession`. In `run_equip`, set it immediately before each terminal
  return:
  - success (`complete`) and `complete_pending_mirror` → `session.resolution = "committed"`
  - `reverted_modify`, `failed_modify`, and the precheck/empty/stale early
    fails → `session.resolution = "reverted"`
  - `equip_sync_indeterminate` and `failed_revert` → `session.resolution = "uncertain"`
  - leave the generic outer-catch (`failed`) at the `None` default.
  Do not alter journal `status` strings, revert logic, or ordering.
- [ ] **Step 4: Run to verify they pass** —
  `.venv/bin/python -m pytest tests/test_economy_flow_equip.py -q`
- [ ] **Step 5: Wider suite / regression run** —
  `.venv/bin/python -m pytest tests/test_economy_flow_equip.py tests/test_economy_owner_lock.py tests/test_trait_economy_phase2.py -q`
- [ ] **Step 6: Commit** —
  `feat(economy): classify equip terminal outcome as committed/reverted/uncertain (#316)`

---

### Task 2: Surface `resolution` in the equip session dict

**Files:**
- Modify: `webapp/economy_api.py` (`economy_session_dict`, equip branch)
- Test: `tests/test_economy_deps_trait.py` (or a small new
  `tests/test_economy_session_dict.py`)

**Interfaces:**
- Produces: `economy_session_dict("equip", s)["resolution"]`, `getattr`-safe
  (`None` when absent).
- Consumes: `EquipSession.resolution` from Task 1.

- [ ] **Step 1: Write the failing test(s).** With the tests/ env-guard preamble
  at module top:
  ```python
  import os
  os.environ.setdefault("BUNNY_PULL_ZONE", "https://example.test")
  os.environ.setdefault("LAYER_SOURCE", "local")

  from webapp import economy_api

  class _Fake:
      id = "e1"; state = "failed"; error = "outcome unknown"
      displaced = {"Head": "Crown"}; resolution = "uncertain"

  def test_equip_dict_carries_resolution():
      d = economy_api.economy_session_dict("equip", _Fake())
      assert d["resolution"] == "uncertain"

  def test_equip_dict_resolution_defaults_none_when_absent():
      class _Old:  # predates the field / a mock fake
          id = "e2"; state = "done"; error = None; displaced = {}
      d = economy_api.economy_session_dict("equip", _Old())
      assert d["resolution"] is None
  ```
- [ ] **Step 2: Run to verify they fail** —
  `.venv/bin/python -m pytest tests/test_economy_session_dict.py -q`
  Expect `KeyError: 'resolution'`.
- [ ] **Step 3: Implement.** In `economy_session_dict`, equip branch, add
  `base["resolution"] = getattr(s, "resolution", None)`.
- [ ] **Step 4: Run to verify they pass** —
  `.venv/bin/python -m pytest tests/test_economy_session_dict.py -q`
- [ ] **Step 5: Wider suite / regression run** —
  `.venv/bin/python -m pytest tests/ -k "economy" -q`
- [ ] **Step 6: Commit** —
  `feat(economy-api): expose equip resolution to the client poller (#316)`

---

### Task 3: Pure client classifier for the save outcome

**Files:**
- Modify: `webapp/client/build_pure.js` (new export `saveOutcome`)
- Test: `tests/test_build_pure_js.py`

**Interfaces:**
- Produces: `buildPure.saveOutcome(finalSession)` → `"committed" | "reverted" |
  "uncertain"`. Maps `resolution === "uncertain"` → `"uncertain"`;
  `state === "done"` or `resolution === "committed"` → `"committed"`; everything
  else (incl. `state === "failed"` with `resolution` `"reverted"` / `null` /
  unknown) → `"reverted"`.
- Consumes: the `/api/equip/{id}` terminal session object.

- [ ] **Step 1: Write the failing test(s)** in `tests/test_build_pure_js.py`
  (Node harness, no env preamble — imports no `lfg_core`):
  ```python
  def test_save_outcome_uncertain():
      assert run_js('M.saveOutcome({state:"failed", resolution:"uncertain"})') == "uncertain"
  def test_save_outcome_reverted_on_null():
      assert run_js('M.saveOutcome({state:"failed", resolution:null})') == "reverted"
  def test_save_outcome_committed_on_done():
      assert run_js('M.saveOutcome({state:"done"})') == "committed"
  ```
- [ ] **Step 2: Run to verify they fail** —
  `.venv/bin/python -m pytest tests/test_build_pure_js.py -q`
  Expect a Node error (`M.saveOutcome is not a function`).
- [ ] **Step 3: Implement** `export function saveOutcome(s) { ... }` in
  `build_pure.js` per the mapping above.
- [ ] **Step 4: Run to verify they pass** —
  `.venv/bin/python -m pytest tests/test_build_pure_js.py -q`
- [ ] **Step 5: Wider suite / regression run** —
  `.venv/bin/python -m pytest tests/test_build_pure_js.py -q`
- [ ] **Step 6: Commit** —
  `feat(build): pure saveOutcome classifier for equip results (#316)`

---

### Task 4: Wire the uncertain-outcome UX into the Build panel

**Files:**
- Modify: `webapp/client/app.js` (`saveBuild`, `stagePendingEquip`,
  `renderCanvas`/Build panel banner, a module-level `reconcileUncertainIds` Set,
  and the import of `saveOutcome`)
- Modify: `webapp/client/index.html` (bump `app.js?v=32` → `v=33`; bump the
  `build_pure.js` import cache key if versioned)
- Test: covered by Task 3 (pure logic) + manual smoke (Step 5)

**Interfaces:**
- Consumes: `final.resolution` via `buildPure.saveOutcome(final)`.
- Produces: client-session `reconcileUncertainIds: Set<nft_id>`; a banner
  render; staging/save refusal for flagged characters.

- [ ] **Step 1: Write the failing test(s).** No new pytest — the branch logic is
  in `saveOutcome` (Task 3). Define the manual acceptance criteria here (checked
  in Step 5): uncertain → distinct message + banner + gated re-save; reverted →
  today's message, no banner.
- [ ] **Step 2: Run to verify they fail** — N/A (UI wiring); confirm Task 3
  tests are green before wiring.
- [ ] **Step 3: Implement** in `app.js`:
  - Add `import { saveOutcome } from './build_pure.js?v=...'` (match existing
    build_pure import style) and a module-level `const reconcileUncertainIds = new Set();`.
  - In `saveBuild`, after `pollEconomyOp`, compute
    `const outcome = saveOutcome(final);`. Keep `committed` behavior
    (`applySavedLocally` + refetch). For `uncertain`: add `activeNftId` to
    `reconcileUncertainIds`, show the distinct message *"We couldn't confirm your
    save on the ledger — your character may or may not be wearing the new traits.
    Support is reconciling. Refresh to re-check; don't re-save until then."*,
    still refetch `/api/economy` (best-effort) but do not treat the redraw as
    proof. For `reverted`/anything else: today's clean-fail message + refetch.
  - In `stagePendingEquip` and at the top of `saveBuild`, early-return with the
    reconcile message when `reconcileUncertainIds.has(activeNftId)` (same shape
    as the existing `saveBusy` guard).
  - Render a persistent banner on a flagged character in `renderCanvas` / the
    Build panel.
  - Clear the flag when a successful `/api/economy` refetch completes for that
    character (e.g. in `selectCharacter` / the refresh path) — `.delete(nft_id)`.
  - Bump `app.js?v=` in `index.html` (and the `build_pure.js` import key).
- [ ] **Step 4: Run to verify they pass** —
  `.venv/bin/python -m pytest tests/test_build_pure_js.py tests/test_economy_session_dict.py -q`
- [ ] **Step 5: Manual smoke (Activity, staging).** Force an
  `equip_sync_indeterminate` (fake / injected `raise_closet_modify`) on a
  batched Build save; verify the UI shows the "couldn't confirm — refresh, don't
  re-save" message (NOT "here's your old look"), the character carries the
  reconcile banner, staging/saving on it is refused until Refresh, and a Refresh
  that returns clears the banner. Verify a clean `reverted` save still shows the
  old look + plain message + no banner. Confirm the served `app.js` version via
  `GET /app.js` (Discord can serve a stale client).
- [ ] **Step 6: Commit** —
  `feat(build): reconcile Build UI on uncertain equip outcome; gate re-save (#316)`

---

### Final Task: Full gate + PR

- [ ] Run the full suite: `.venv/bin/python -m pytest -q`
- [ ] Run linters/types: `.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy lfg_core webapp lfg_service`
- [ ] Confirm `webapp/client/index.html` cache-buster was bumped alongside every
  `app.js`/`build_pure.js` change.
- [ ] Push the feature branch (never `--no-verify`; ensure the worktree `.venv`
  symlink exists so the pre-push gate actually runs).
- [ ] `gh pr create` **non-draft** against `main`, per repo rules: **no AI
  attribution** in the commit trailers or PR body. Body: summarize the
  read-side-only nature (no tx, no migration), the `committed/reverted/uncertain`
  taxonomy, and reference #316 (Related: #313).
- [ ] Wait for **Greptile** and **CodeRabbit**; resolve every actionable finding
  (fix in code **and** reply on its thread) before merge. Remember Greptile's
  clean verdict lives only in the `Greptile Review` check-run summary.
- [ ] Before merge, re-confirm the open question in the spec: check
  `ECONOMY_RECORDS_DIR` for real `equip_sync_indeterminate` / `failed_revert`
  occurrences — if effectively never, the maintainer may choose to ship
  Seam 1 + the message only and drop the banner/gate (Task 4 partial).
