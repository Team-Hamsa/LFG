# Fix misspelled "Iridescent Skeleton" Body value Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Correct the misspelled Body attribute `Iridescent Skeleton` (single-r,
no art) → `Irridescent Skeleton` (double-r, has art) on the affected mainnet
tokens via an idempotent, `--apply`-gated `NFTokenModify` script that also syncs
the `onchain_nfts` index and the `LFG` app table, plus a narrow recurrence guard.

**Architecture:** Three independent seams —
1. **Pure metadata rewrite** (`rewrite_body_value`): given a metadata dict,
   swap only the Body attribute string; idempotent. No I/O.
2. **Correction driver** (`scripts/fix_iridescent_body.py`): discover targets in
   the index + app DB, verify mutability on-ledger, rebuild+upload metadata via
   `lfg_core/cdn`, modify on-ledger via `xrpl_ops.modify_nft`, sync mirrors. All
   ledger/CDN calls go through existing helpers (SourceTag + memos guaranteed).
3. **Recurrence guard** (layer-tree denylist lint) + a resolution regression
   test, keeping `audit_trait_files.py` green as the standing gate.

**Tech Stack:** Python 3 / aiohttp / asyncio / pytest; no client changes.

## Global Constraints

- **SourceTag=2606160021 + provenance memos** must ride on the `NFTokenModify`.
  Route through `lfg_core.xrpl_ops.modify_nft` (which stamps
  `source_tag=config.SOURCE_TAG` and `memos.build_memo_models(INITIATOR_BACKEND,
  platform, ACTION_MODIFY)`); never build a raw tx.
- **Pre-push gate** (ruff --fix, ruff-format, mypy from `.venv`, gitleaks,
  pytest, validate-trait-config) must pass. Never `--no-verify`. In a worktree,
  ensure the `.venv` symlink exists or the gate silently skips.
- **No client/app.js change** in this work, so no cache-buster bump needed.
- **Ledger is source of truth:** modify the token first, then update DB/index
  from the confirmed result — never mutate mirrors ahead of the ledger.
- **Dry-run by default; `--apply` to mutate.** Re-runs are no-ops.

---

### Task 1: Pure metadata Body-value rewrite

**Files:**
- Create `lfg_core/body_fix.py` (tiny pure module: `BAD`, `GOOD`,
  `rewrite_body_value(meta: dict) -> tuple[dict, bool]` returning the (possibly
  unchanged) metadata and a `changed` flag).
- Test `tests/test_body_fix.py`.

**Interfaces:**
- Produces: `rewrite_body_value(meta)` — swaps only the `attributes[i]` whose
  `trait_type == "Body"` and `value == BAD` to `GOOD`; returns `changed=False`
  when already `GOOD` or Body absent.
- Consumes: nothing (pure).

- [ ] **Step 1: Write the failing test(s)** — TDD. Env-guard preamble at top:
  ```python
  import os
  os.environ.setdefault("BUNNY_PULL_ZONE", "https://pull.example.net")
  os.environ.setdefault("LAYER_SOURCE", "local")

  from lfg_core.body_fix import BAD, GOOD, rewrite_body_value

  def _meta(body_val):
      return {"edition": 64, "image": "https://cdn/x.png", "video": "https://cdn/x.mp4",
              "burnCount": 1,
              "attributes": [
                  {"trait_type": "Background", "value": "Moving Pink Clouds"},
                  {"trait_type": "Body", "value": body_val},
                  {"trait_type": "Head", "value": "Cap Black"}]}

  def test_rewrites_only_body_value():
      meta, changed = rewrite_body_value(_meta(BAD))
      assert changed is True
      bodies = [a["value"] for a in meta["attributes"] if a["trait_type"] == "Body"]
      assert bodies == [GOOD]
      # every non-Body field untouched
      assert meta["image"] == "https://cdn/x.png" and meta["video"] == "https://cdn/x.mp4"
      assert meta["burnCount"] == 1 and meta["edition"] == 64
      assert [a["value"] for a in meta["attributes"] if a["trait_type"] == "Background"] == ["Moving Pink Clouds"]

  def test_idempotent_when_already_good():
      _, changed = rewrite_body_value(_meta(GOOD))
      assert changed is False

  def test_no_body_attr_is_noop():
      _, changed = rewrite_body_value({"attributes": [{"trait_type": "Head", "value": "Cap Black"}]})
      assert changed is False
  ```
- [ ] **Step 2: Run to verify they fail** — `.venv/bin/pytest tests/test_body_fix.py -q` (ImportError / assertion failures expected).
- [ ] **Step 3: Implement** `lfg_core/body_fix.py` with the two constants and a deep-ish copy that mutates only the matching Body attribute.
- [ ] **Step 4: Run to verify they pass** — `.venv/bin/pytest tests/test_body_fix.py -q`.
- [ ] **Step 5: Wider suite / regression run** — `.venv/bin/pytest -q tests/test_body_fix.py tests/ -k "swap or trait or audit" -q`.
- [ ] **Step 6: Commit** — `fix(#301): pure Body-value rewrite helper (Iridescent→Irridescent)`.

---

### Task 2: Correction driver script

**Files:**
- Create `scripts/fix_iridescent_body.py`.
- Test `tests/test_fix_iridescent_body.py` (drives the script's functions against temp DBs with stubbed ledger/CDN).

**Interfaces:**
- Produces: `discover_targets(index_conn, app_conn) -> list[Target]`;
  `async def correct_token(target, http, *, apply: bool) -> Result`;
  `main()` with `--network` (default `mainnet`) and `--apply`.
- Consumes: `lfg_core.nft_index` (`index_db_path`, `fetch_metadata_multi`,
  `upsert`, `_row_to_nft`/`OnchainNft`), `lfg_core.body_fix.rewrite_body_value`,
  `lfg_core.cdn.upload_to_bunny`, `lfg_core.xrpl_ops` (`nft_info`, `modify_nft`,
  `convert_str_to_hex`), `lfg_core.config` (`SWAP_CDN_FOLDER`, `XRPL_NETWORK`),
  `lfg_core.db_path` for the app DB.

- [ ] **Step 1: Write the failing test(s)** — TDD, env-guard preamble at top:
  ```python
  import os
  os.environ.setdefault("BUNNY_PULL_ZONE", "https://pull.example.net")
  os.environ.setdefault("LAYER_SOURCE", "local")
  import sqlite3, json, asyncio
  import scripts.fix_iridescent_body as fx
  from lfg_core.body_fix import BAD, GOOD
  ```
  Seed a temp `onchain_nfts` (one live row, `attributes_json` containing BAD,
  `is_burned=0`, `mutable=1`) and a temp `LFG` row (`Body=BAD`). Tests:
  - `test_discover_targets_finds_live_bad_only` — burned/already-GOOD rows excluded.
  - `test_dry_run_mutates_nothing` — monkeypatch `xrpl_ops.modify_nft`,
    `cdn.upload_to_bunny`, `nft_index.fetch_metadata_multi`, `xrpl_ops.nft_info`;
    run with `apply=False`; assert `modify_nft` NOT called and DB rows unchanged.
  - `test_apply_rewrites_ledger_and_mirrors` — stub `nft_info` → mutable,
    `fetch_metadata_multi` → metadata with BAD, `upload_to_bunny` → a URL,
    `modify_nft` → a fake hash; run `apply=True`; assert `modify_nft` called once
    with the new URL and the token's owner, `onchain_nfts.attributes_json` now
    contains GOOD (not BAD), and `LFG.Body == GOOD`.
  - `test_non_mutable_is_skipped` — stub `nft_info` → not mutable; assert
    `modify_nft` NOT called and a `skipped_non_mutable` result recorded.
  - `test_rerun_is_noop` — after apply, discover_targets returns empty.
- [ ] **Step 2: Run to verify they fail** — `.venv/bin/pytest tests/test_fix_iridescent_body.py -q`.
- [ ] **Step 3: Implement** the script: discovery SQL (`attributes_json LIKE
  '%Iridescent Skeleton%' AND attributes_json NOT LIKE '%Irridescent Skeleton%'
  AND (is_burned IS NULL OR is_burned=0)`), per-token mutability check via
  `nft_info`, `fetch_metadata_multi` → `rewrite_body_value` → `upload_to_bunny`
  under stem `f"{edition}/{edition}_fix_{uuid4().hex[:8]}"` →
  `modify_nft(nft_id, owner, url, platform=memos.PLATFORM_BACKEND)` → on hash,
  `nft_index.upsert` the rewritten row (new `uri_hex`, new `attributes_json`) and
  `UPDATE LFG SET Body=GOOD WHERE Body=BAD` for the edition. Guard on `--apply`;
  journal to `reports/`. Re-raise `IndeterminateResultError`; treat `None` as a
  logged per-token failure that does not abort the batch.
- [ ] **Step 4: Run to verify they pass** — `.venv/bin/pytest tests/test_fix_iridescent_body.py -q`.
- [ ] **Step 5: Wider suite / regression run** — `.venv/bin/pytest -q`.
- [ ] **Step 6: Commit** — `fix(#301): idempotent NFTokenModify correction script for misspelled skeleton Body`.

---

### Task 3: Recurrence guard + resolution regression

**Files:**
- Modify the `validate-trait-config` hook target (grep the hook command in
  `.pre-commit-config.yaml`; extend that script — likely
  `scripts/validate_trait_config.py` or `lfg_core/trait_config.py` validation —
  or add a tiny sibling check invoked by the same hook) to reject a single-r
  `Iridescent *` file under any `layers/<body>/Body/` dir.
- Test `tests/test_body_typo_guard.py`.

**Interfaces:**
- Produces: `find_typo_body_files(layers_dir) -> list[str]` returning any
  disallowed single-r `Iridescent ` Body stems.
- Consumes: filesystem walk of `layers/*/Body/`.

- [ ] **Step 1: Write the failing test(s)** — env-guard preamble at top; build a
  temp `layers/skeleton/Body/` with `Irridescent Skeleton.webm` (allowed) and a
  planted `Iridescent Skeleton.webm` (disallowed); assert the guard flags exactly
  the single-r file and passes when only the double-r file is present. Add a
  resolution regression: with a fake store exposing the double-r asset, assert
  `swap_compose.missing_layers` returns `[]` for GOOD and a Body gap for BAD.
- [ ] **Step 2: Run to verify they fail** — `.venv/bin/pytest tests/test_body_typo_guard.py -q`.
- [ ] **Step 3: Implement** the narrow denylist check and wire it into the
  existing `validate-trait-config` hook so a stray single-r Body art file can
  never be committed/synced.
- [ ] **Step 4: Run to verify they pass** — `.venv/bin/pytest tests/test_body_typo_guard.py -q`.
- [ ] **Step 5: Wider suite / regression run** — `.venv/bin/pytest -q` and run the hook locally: `.venv/bin/pre-commit run validate-trait-config --all-files`.
- [ ] **Step 6: Commit** — `fix(#301): guard against single-r "Iridescent" Body art re-entering the pool`.

---

### Final Task: Full gate + PR

- [ ] Run the full gate: `.venv/bin/pytest -q`, `.venv/bin/ruff check .`,
  `.venv/bin/ruff format --check .`, `.venv/bin/mypy .`,
  `.venv/bin/pre-commit run --all-files`. Fix anything red; never `--no-verify`.
- [ ] **Ops (out of code, note in PR body) — do NOT run here:** on the box with
  mainnet creds, dry-run `scripts/fix_iridescent_body.py --network mainnet`,
  review the plan, then `--apply`; re-run `scripts/audit_trait_files.py
  --network mainnet` to confirm the value is clean; spot-check one token via
  `nft_info`. Non-mutable edition-77 token is a maintainer decision (see spec
  open question 2).
- [ ] Push the branch and `gh pr create` (non-draft, per repo rules — **no AI
  attribution** in the commit trailers or PR body). Wait for **Greptile** and
  **CodeRabbit**; resolve every actionable finding (fix in code AND reply on the
  thread naming the fixing commit) before merge.
