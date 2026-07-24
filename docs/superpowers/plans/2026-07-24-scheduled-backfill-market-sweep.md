# Scheduled backfill_market sweep Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run `scripts/backfill_market.py` on a nightly per-network pm2 cron so
the `market_listings` / `buy_offers` index self-heals offer-index drift from
listener downtime, with legible drift logging and no risk of fighting the live
listener.

**Architecture:** Two independent seams — (1) harden
`scripts/backfill_market.py` (structured logging, drift WARNING, sqlite
`busy_timeout`, optional `--report` drift log); (2) add a cron process to
`ecosystem.prod.config.js` (mainnet) and `ecosystem.staging.config.js`
(testnet), mirroring the existing `lfg-snapshot` / `stg-snapshot` entries. The
two seams touch disjoint files; the ecosystem edits depend only on the script
name/flags being stable.

**Tech Stack:** Python 3 / asyncio / pytest; pm2 ecosystem config (JS object
literals). No client/JS-app changes.

## Global Constraints

- **SourceTag=2606160021 + provenance memos:** N/A here — the sweep builds no
  transaction and asks for no signature (read-only ledger queries + local index
  writes). Do NOT add either; there is no tx surface. Preserve this: the sweep
  must never gain a tx-submitting path in this change.
- **Pre-push gate** (ruff `--fix`, ruff-format, mypy from `.venv`, gitleaks,
  pytest, validate-trait-config) must pass; never `--no-verify`. New test
  files that import `lfg_core` at module top MUST carry the env-guard preamble
  (`os.environ.setdefault("BUNNY_PULL_ZONE", ...)` / `LAYER_SOURCE`) or they
  strand frozen config constants and break full-suite ordering.
- **No app.js / client change** in this plan, so no cache-buster bump needed.
- Keep the manual CLI behavior byte-compatible: `--report` defaults OFF so an
  operator running the script by hand sees the same output as today.

---

### Task 1: Harden `backfill_market.py` — logging, drift WARNING, lock safety, `--report`

**Files:**
- Modify: `scripts/backfill_market.py`
- Create: `tests/test_backfill_market_scheduled.py`

**Interfaces:**
- Produces: `_amain` sets `logging.basicConfig(...)`, opens the index conn with
  `PRAGMA busy_timeout = 30000`, emits the summary via `logging` (WARNING when
  any of `closed_stale` / `bids_closed_stale` / `fetch_failures` /
  `bid_fetch_failures` is non-zero, INFO otherwise), and — when `--report` is
  passed — appends one JSON line to `reports/backfill_market_drift.log`.
- New CLI flag: `--report` (store_true, default False) added in `_build_parser`.
- Consumes: existing `backfill_market(conn) -> dict` counts (unchanged
  signature).

- [ ] **Step 1: Write the failing test(s)** — `tests/test_backfill_market_scheduled.py`, env-guard preamble at module top:
  ```python
  import os
  os.environ.setdefault("BUNNY_PULL_ZONE", "https://example.b-cdn.net")
  os.environ.setdefault("LAYER_SOURCE", "local")

  import json
  import logging
  import importlib

  import pytest

  bm = importlib.import_module("scripts.backfill_market")

  def test_report_flag_default_off_writes_nothing(tmp_path, monkeypatch):
      # _build_parser accepts --report; default is False
      args = bm._build_parser().parse_args(["--network", "testnet"])
      assert args.report is False

  def test_drift_line_warns_when_stale_nonzero(caplog):
      counts = {"characters_swept": 10, "traits_swept": 0, "live_listings": 9,
                "closed_stale": 2, "fetch_failures": 0, "live_bids": 0,
                "bids_closed_stale": 0, "bid_fetch_failures": 0}
      with caplog.at_level(logging.INFO):
          bm._log_summary("testnet", counts)   # new helper
      drift = [r for r in caplog.records if "backfill_market drift" in r.getMessage()]
      assert drift and drift[0].levelno == logging.WARNING

  def test_no_drift_logs_info_not_warning(caplog):
      counts = {"characters_swept": 10, "traits_swept": 0, "live_listings": 10,
                "closed_stale": 0, "fetch_failures": 0, "live_bids": 0,
                "bids_closed_stale": 0, "bid_fetch_failures": 0}
      with caplog.at_level(logging.INFO):
          bm._log_summary("testnet", counts)
      assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

  def test_report_appends_json_line(tmp_path, monkeypatch):
      path = tmp_path / "drift.log"
      counts = {"characters_swept": 5, "traits_swept": 1, "live_listings": 4,
                "closed_stale": 1, "fetch_failures": 0, "live_bids": 0,
                "bids_closed_stale": 0, "bid_fetch_failures": 0}
      bm._append_drift_report(str(path), "testnet", counts)   # new helper
      rec = json.loads(path.read_text().strip())
      assert rec["network"] == "testnet" and rec["closed_stale"] == 1
  ```
  (Extract two small pure helpers — `_log_summary(network, counts)` and
  `_append_drift_report(path, network, counts)` — so they're unit-testable
  without running the async sweep or touching the ledger.)

- [ ] **Step 2: Run to verify they fail** — `.venv/bin/python -m pytest tests/test_backfill_market_scheduled.py -q` → expect `AttributeError`/`ArgumentError` (no `--report`, no `_log_summary` / `_append_drift_report`).

- [ ] **Step 3: Implement** in `scripts/backfill_market.py`:
  - Add `--report` (`action="store_true"`) to `_build_parser`.
  - Add `_log_summary(network, counts)`: builds the existing multi-line human
    summary via `logging.info`, then computes `drift = counts["closed_stale"] +
    counts["bids_closed_stale"] + counts["fetch_failures"] +
    counts["bid_fetch_failures"]` and emits a single
    `logging.warning("backfill_market drift: net=%s closed_stale=%d
    bids_closed_stale=%d fetch_failures=%d bid_fetch_failures=%d
    live_listings=%d", ...)` when `drift` else the same line at `logging.info`.
  - Add `_append_drift_report(path, network, counts)`: `os.makedirs(dirname,
    exist_ok=True)` then append `json.dumps({"ts": <iso utc>, "network":
    network, **selected counts})` + `"\n"`.
  - In `_amain`: `logging.basicConfig(level=logging.INFO, format="%(asctime)s
    %(levelname)s %(message)s")`; after `nft_index.init_db(...)` run
    `conn.execute("PRAGMA busy_timeout = 30000")`; replace the block of
    `print(...)` with `_log_summary(args.network, counts)`; if `args.report`,
    call `_append_drift_report(os.path.join(REPO_ROOT, "reports",
    "backfill_market_drift.log"), args.network, counts)`. Keep `return 0`.

- [ ] **Step 4: Run to verify they pass** — `.venv/bin/python -m pytest tests/test_backfill_market_scheduled.py -q`.

- [ ] **Step 5: Wider suite / regression run** — `.venv/bin/python -m pytest tests/ -q` (ensure no full-suite ordering breakage from the new module; confirm any existing `backfill_market` tests still pass).

- [ ] **Step 6: Commit** — `feat(scripts): scheduled-run logging + drift signal + busy_timeout for backfill_market (#288)`

---

### Task 2: Wire the pm2 cron into both ecosystem files

**Files:**
- Modify: `ecosystem.prod.config.js`
- Modify: `ecosystem.staging.config.js`
- (Docs) Modify: `CLAUDE.md` "Running (two pm2 stacks…)" section — add
  `lfg-market-backfill` / `stg-market-backfill` to the process table + a line
  on going live (`pm2 start ecosystem.<stack>.config.js --only <name>`; shows
  "stopped" between runs, like `lfg-snapshot`).

**Interfaces:**
- Produces: a new app object per file, mirroring the `lfg-snapshot` entry
  (`autorestart: false`, `cron_restart: "30 3 * * *"`, `interpreter: PY`),
  with `args: ["--network", "mainnet", "--report"]` (prod) /
  `["--network", "testnet", "--report"]` (staging).
- Consumes: the stable `scripts/backfill_market.py` CLI (`--network`,
  `--report`) from Task 1.

- [ ] **Step 1: Add the prod entry** to `ecosystem.prod.config.js` `apps`:
  ```js
  { name: "lfg-market-backfill", cwd: CWD, script: "scripts/backfill_market.py",
    interpreter: PY, args: ["--network", "mainnet", "--report"],
    cron_restart: "30 3 * * *", autorestart: false },
  ```

- [ ] **Step 2: Add the staging entry** to `ecosystem.staging.config.js` `apps`:
  ```js
  { name: "stg-market-backfill", cwd: CWD, script: "scripts/backfill_market.py",
    interpreter: PY, args: ["--network", "testnet", "--report"],
    cron_restart: "30 3 * * *", autorestart: false },
  ```

- [ ] **Step 3: Validate the JS is well-formed** — `node -e "require('./ecosystem.prod.config.js'); require('./ecosystem.staging.config.js'); console.log('ok')"` (or `node --check` on each). Confirm the new app names appear and each carries the correct `--network`.

- [ ] **Step 4: Update `CLAUDE.md`** — add the two processes to the prod/staging
  pm2 table and a one-line go-live note (cron slot 03:30 UTC, offset from
  `lfg-snapshot`'s 00:10; parks "stopped" between runs). Docs-only edit.

- [ ] **Step 5: Manual ops smoke (documented, run by operator, not CI)** —
  `pm2 start ecosystem.staging.config.js --only stg-market-backfill` then
  `pm2 logs stg-market-backfill --lines 50`: confirm the summary logs, drift
  line prints, process parks "stopped", and `reports/backfill_market_drift.log`
  gained a JSON line.

- [ ] **Step 6: Commit** — `ops(marketplace): nightly backfill_market cron per stack (#288)`
  (Note: ecosystem + CLAUDE.md are ops/docs; the `scripts/` change in Task 1 is
  application code, so the whole change ships as one reviewed PR, not a direct
  push.)

---

### Final Task: Full gate + PR

- [ ] Run the full pre-push gate locally: `.venv/bin/python -m pytest tests/ -q`, `ruff check .`, `ruff format --check .`, `mypy` (from `.venv`). Fix anything red; never `--no-verify`.
- [ ] Confirm the worktree `.venv` symlink exists so the pre-push hook actually runs the gate (a missing symlink silently skips it).
- [ ] Push the branch and `gh pr create` (Team-Hamsa/LFG, **non-draft**, no AI attribution in the body/commits per repo rules).
- [ ] Wait for **Greptile** + **CodeRabbit**. Greptile's clean verdict lives only in the `Greptile Review` check-run summary (no comment on a pass). Close out every actionable finding — fix in code AND reply on its thread naming the fixing commit — before merging.
- [ ] After merge, the deployer auto-deploys **staging only**; going live on either stack is the ops step (`pm2 start … --only <name>` + `pm2 save`). Promote to prod with `scripts/promote.sh`, then start `lfg-market-backfill` on the prod box.
