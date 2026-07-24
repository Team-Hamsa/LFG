# Share-Link Mint Attribution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the share funnel from click → mint. Send the client-stashed
`lfg_ref` when a mint starts, validate it server-side (checksum + reject
self-referral), persist it as a nullable `LFG.referrer` column on the mint
record, and add an ops CLI that reports per-referrer conversion (visits from
`share_clicks` vs mints from `LFG.referrer`).

**Architecture:** Four independent seams —
1. **Validation/aggregation core** (`lfg_core/referral.py`) — pure functions,
   no I/O beyond a passed sqlite path; fully unit-testable.
2. **Persistence** (`lfg_core/db_helpers.py`) — self-migrating `referrer`
   column on `LFG`, threaded through `mint_flow.py`.
3. **Service intake** (`lfg_service/app.py::handle_mint_start`) — read body,
   clean referrer, pass to `MintSession`.
4. **Client + metrics readout** (`webapp/client/app.js`, `scripts/share_metrics.py`).

**Tech Stack:** Python 3 / aiohttp / asyncio / pytest; vanilla no-build JS client.

## Global Constraints

- **No on-ledger transaction is created by this feature** (DB-only bookkeeping),
  so there is no new tx to stamp — but the app-wide rule stands: any tx built
  anywhere MUST carry `SourceTag = 2606160021` + provenance memos
  (`lfg_core/memos.py`). Do not touch the existing mint tx's SourceTag/memos.
- The pre-push gate (ruff `--fix`, ruff-format, mypy from `.venv`, gitleaks,
  pytest, validate-trait-config) must pass — never `--no-verify`.
- Any `webapp/client/app.js` change MUST bump the `app.js` cache-buster query in
  `webapp/client/index.html` in the **same commit**.
- Referrer handling is **best-effort**: a malformed/hostile/absent referrer must
  never fail, block, or slow a mint (mirror `share_clicks.record_click`).
- Network-aware: metrics + reads resolve the passed network's app DB
  (`db_path.app_db_path(network)` / `config.DB_PATH`), never hard-coded mainnet.
- New `tests/` files import `lfg_core` at module top → copy the env-guard
  preamble (`os.environ.setdefault("BUNNY_PULL_ZONE", ...)`,
  `os.environ.setdefault("LAYER_SOURCE", "local")`) before the imports.

---

### Task 1: Referral validation + conversion core (`lfg_core/referral.py`)

**Files:**
- Create: `lfg_core/referral.py`
- Test: `tests/test_referral.py`

**Interfaces:**
- Produces: `clean_referrer(raw: str | None, minter_wallet: str) -> str | None`
  (checksum-valid, non-self, else `None`);
  `referrer_conversion(app_db_path: str, network: str) -> list[dict]` returning
  `{"referrer", "clicks", "mints", "conversion_rate"}` sorted by mints desc.
- Consumes: `xrpl.core.addresscodec.is_valid_classic_address`, `sqlite3`.

- [ ] **Step 1: Write the failing test(s)** — `tests/test_referral.py` with the
  env-guard preamble. Cover `clean_referrer`: a real checksum-valid classic
  address passes; a shape-valid but checksum-broken string → `None`;
  `raw == minter_wallet` → `None`; `None`/`""`/`123` (non-str) → `None`. Cover
  `referrer_conversion`: build a temp sqlite with `share_clicks`
  (`lfg_core.share_clicks.init_db`) + a minimal `LFG` table, insert clicks for
  walletB (incl. one `is_bot=1` that must be excluded) and walletC (clicks, no
  mints), insert `LFG` rows with `referrer=walletB` on the right `network`
  (and a wrong-network row that must be excluded), assert the returned
  clicks/mints/rate per wallet.

  ```python
  import os
  os.environ.setdefault("BUNNY_PULL_ZONE", "https://example.b-cdn.net")
  os.environ.setdefault("LAYER_SOURCE", "local")
  from lfg_core import referral

  def test_clean_referrer_rejects_self_and_junk():
      good = "rLUnD5mskBnHfwFxCjakDA3RVgK584XQXG"
      assert referral.clean_referrer(good, "rOther1111111111111111111111") == good
      assert referral.clean_referrer(good, good) is None          # self-referral
      assert referral.clean_referrer("rNOTVALIDCHECKSUM", "rX") is None
      assert referral.clean_referrer(None, "rX") is None
      assert referral.clean_referrer(123, "rX") is None            # non-str
  ```

- [ ] **Step 2: Run to verify they fail** — `.venv/bin/python -m pytest
  tests/test_referral.py -q` → fails with `ModuleNotFoundError: lfg_core.referral`.

- [ ] **Step 3: Implement** `lfg_core/referral.py`. `clean_referrer` guards
  type, calls `is_valid_classic_address`, rejects `raw == minter_wallet`.
  `referrer_conversion` opens the db read-only, runs the two GROUP BY queries
  (`share_clicks` where `ref_wallet IS NOT NULL AND is_bot = 0`; `LFG` where
  `referrer IS NOT NULL AND network = ?`), merges in Python (full-outer via a
  keyed dict), computes `conversion_rate = mints / clicks if clicks else None`,
  returns sorted by mints desc then clicks desc. Swallow `sqlite3.OperationalError`
  for a missing `referrer` column / table (return what exists) so it runs before
  the first migrated mint.

- [ ] **Step 4: Run to verify they pass** — `.venv/bin/python -m pytest
  tests/test_referral.py -q`.

- [ ] **Step 5: Wider suite / regression run** — `.venv/bin/python -m pytest -q`.

- [ ] **Step 6: Commit** — `feat(referral): validation + conversion aggregation core (#273)`.

---

### Task 2: Persist referrer on the mint record (`db_helpers` + `mint_flow`)

**Files:**
- Modify: `lfg_core/db_helpers.py` (`record_nft_mint`), `lfg_core/mint_flow.py`
  (`MintSession.__init__`, `mint_one_unit`, `run_mint_session`)
- Test: `tests/test_referral.py` (extend) or `tests/test_db_helpers_referrer.py`

**Interfaces:**
- Produces: `record_nft_mint(..., referrer: str | None = None)` self-migrates a
  nullable `LFG.referrer` column and stores it; `MintSession(..., referrer=None)`
  attribute; `mint_one_unit(..., referrer=None)` kwarg forwarded to the record.
- Consumes: existing `new_columns` ALTER pattern, `get_nft_data`.

- [ ] **Step 1: Write the failing test(s)** — on a temp DB, call
  `record_nft_mint(..., referrer="rB...")` against a fresh `LFG` table (no
  `referrer` column) and assert `get_nft_data(n)` (or a direct SELECT) returns
  the referrer; assert `referrer=None` stores NULL; assert a second call with a
  different mint still works (column already present, no error).

- [ ] **Step 2: Run to verify they fail** — `.venv/bin/python -m pytest
  tests/test_db_helpers_referrer.py -q` → fails (unexpected `referrer` kwarg).

- [ ] **Step 3: Implement** — in `record_nft_mint` add `referrer: str | None =
  None` param, `"referrer": "TEXT"` in `new_columns`, and add `referrer` to the
  INSERT column list + values tuple. In `MintSession.__init__` add
  `referrer: str | None = None` → `self.referrer = referrer`. In `mint_one_unit`
  add keyword-only `referrer: str | None = None` and `"referrer": referrer` in
  the `record` dict. In `run_mint_session` pass `referrer=session.referrer` into
  `mint_one_unit(...)`. Keep the existing on-chain/DB-failure try/except intact.

- [ ] **Step 4: Run to verify they pass** — `.venv/bin/python -m pytest
  tests/test_db_helpers_referrer.py tests/test_referral.py -q`.

- [ ] **Step 5: Wider suite / regression run** — `.venv/bin/python -m pytest -q`
  (confirm no mint-flow test broke on the new optional kwarg).

- [ ] **Step 6: Commit** — `feat(mint): thread + persist referrer on mint record (#273)`.

---

### Task 3: Service intake (`handle_mint_start`)

**Files:**
- Modify: `lfg_service/app.py` (`handle_mint_start`)
- Test: `tests/test_mint_start_referrer.py` (aiohttp test client, or a focused
  unit test of the body-read + `clean_referrer` wiring if the full handler is
  too heavy to stand up)

**Interfaces:**
- Consumes: `referral.clean_referrer`, `request["wallet"]`, request JSON body.
- Produces: `MintSession(..., referrer=<clean>)`.

- [ ] **Step 1: Write the failing test(s)** — POST `/api/mint` with body
  `{"referrer": "<valid non-self wallet>"}` → the created session's `referrer`
  equals that wallet (inspect via the returned session dict if surfaced, or by
  stubbing `mint_flow.MintSession`/`run_mint_session` to capture the kwarg).
  POST with a self-referral (referrer == the caller's wallet), with garbage, and
  with no body each yield a session whose `referrer` is `None` and a normal
  (non-error) start. Assert a malformed body never 500s the mint.

- [ ] **Step 2: Run to verify they fail** — `.venv/bin/python -m pytest
  tests/test_mint_start_referrer.py -q`.

- [ ] **Step 3: Implement** — at the top of `handle_mint_start`, add a guarded
  `body = await request.json()` (except → `{}`), compute
  `referrer = referral.clean_referrer(body.get("referrer"), request["wallet"])`
  BEFORE the await-free one-active-session guard, and pass `referrer=referrer`
  into the `MintSession(...)` constructor. Import `referral` at module top.

- [ ] **Step 4: Run to verify they pass** — `.venv/bin/python -m pytest
  tests/test_mint_start_referrer.py -q`.

- [ ] **Step 5: Wider suite / regression run** — `.venv/bin/python -m pytest -q`.

- [ ] **Step 6: Commit** — `feat(service): accept + validate mint referrer (#273)`.

---

### Task 4: Client send + metrics CLI

**Files:**
- Modify: `webapp/client/app.js` (`startMint`, add `stashedRef` helper),
  `webapp/client/index.html` (cache-buster bump)
- Create: `scripts/share_metrics.py`
- Test: `tests/test_share_metrics_cli.py` (invoke the CLI's aggregation path /
  `referral.referrer_conversion` against a seeded temp DB — the `app.js` change
  is covered by the manual smoke, matching the repo's no-JS-harness convention)

**Interfaces:**
- Produces: mint POST body `{ ...discordCtx(), referrer: stashedRef() }`;
  `scripts/share_metrics.py --network <net> [--min-clicks N] [--json]`.
- Consumes: `localStorage['lfg_ref']`, `XRPL_ADDR_RE`,
  `referral.referrer_conversion`, `db_path.app_db_path`.

- [ ] **Step 1: Write the failing test(s)** — `tests/test_share_metrics_cli.py`
  (env-guard preamble): seed a temp app DB with `share_clicks` + `LFG` rows,
  call the CLI's report function (factor the render out of `main()` so it's
  importable) and assert the ranked rows / `--json` shape match
  `referrer_conversion`. (Reuses Task 1's core; this proves the CLI wiring +
  network resolution.)

- [ ] **Step 2: Run to verify they fail** — `.venv/bin/python -m pytest
  tests/test_share_metrics_cli.py -q` → fails (`scripts/share_metrics.py` missing).

- [ ] **Step 3: Implement** —
  - `scripts/share_metrics.py`: argparse `--network` (required),
    `--min-clicks`, `--json`; resolve `db_path.app_db_path(network)`; call
    `referral.referrer_conversion`; print a ranked table (referrer, clicks,
    mints, conversion%) or JSON. Loopback ops tool — no service wiring.
  - `webapp/client/app.js`: add `stashedRef()` (try/catch read of
    `localStorage['lfg_ref']`, return only if `XRPL_ADDR_RE.test(...)`, else
    `null`); in `startMint()` change the body to
    `JSON.stringify({ ...discordCtx(), referrer: stashedRef() })`.
  - `webapp/client/index.html`: bump the `app.js?v=` cache-buster.

- [ ] **Step 4: Run to verify they pass** — `.venv/bin/python -m pytest
  tests/test_share_metrics_cli.py -q`.

- [ ] **Step 5: Wider suite / regression run** — `.venv/bin/python -m pytest -q`.

- [ ] **Step 6: Commit** — `feat(share): send stashed ref on mint + conversion metrics CLI (#273)`
  (client + index.html cache-buster + CLI in one commit).

---

### Final Task: Full gate + PR

- [ ] Run the full suite + linters: `.venv/bin/python -m pytest -q`, `ruff check
  .`, `ruff format --check .`, `mypy` (via the pre-push config / `.venv`).
- [ ] Manual smoke (testnet): visit `PUBLIC_SHARE_BASE_URL/nft/<n>?ref=<walletB>`
  → confirm `lfg_ref` set; mint from walletA in the Activity → new `LFG` row has
  `referrer = walletB`; `scripts/share_metrics.py --network testnet` lists
  walletB with 1 mint; repeat with `?ref=<walletA>` then mint from walletA →
  `referrer` NULL (self-referral rejected).
- [ ] Push the feature branch and `gh pr create` **non-draft** (per repo rules;
  **no AI attribution** in the commit trailers or PR body).
- [ ] Wait for **Greptile** + **CodeRabbit**; resolve every actionable finding
  (fix in code AND reply on its thread naming the fixing commit) before merge.
- [ ] Note in the PR body: no new on-ledger tx (SourceTag/memos untouched);
  reward payout is explicitly out of scope (issue #273) and needs a separate
  sybil-resistance pass before any referral reward ships.
