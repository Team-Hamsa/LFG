# Marketplace follow-ups (settlement + browse polish) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close out the still-actionable remainder of #130 — detach trait
settlement from the buy poll, make the settlement-sweep attempt counter durable,
hide 0-drop character listings from browse, and turn the trait-value filter into
a datalist. The architecture item (per-network trait on-ledger ops) and the
conditional/already-addressed items are recommended for descope in the spec and
are NOT implemented here.

**Architecture:** Four independent seams —
1. `lfg_service/app.py` buy-status handler (async detach; no schema).
2. `lfg_core/market_store.py` + `lfg_core/shop_store.py` schema + helpers, wired
   into `lfg_service/app.py`'s two sweeps (durable counter).
3. `lfg_core/market_store.py::browse` (0-drop character filter).
4. `webapp/client/*` (datalist; no server change — reuses `/api/shop/catalog`).

**Tech Stack:** Python 3 / aiohttp / asyncio / pytest; vanilla no-build JS client.

## Global Constraints

- **SourceTag = 2606160021 + provenance memos** must remain on every tx. No task
  here builds a new tx; settlement re-uses `economy_flow.run_deposit`
  (`action=deposit`, already stamped). Do not alter the tx it builds.
- **Fail-closed settlement invariant:** `settled=0` on the `market_listings` row
  is the durable "still owed" flag; `settle_pending_trait_sales` /
  `unsettled_trait_sales` are the backstop of record. Detaching the primary
  trigger must not make a settlement failure invisible or lose the row.
- **Pre-push gate** (ruff `--fix`, ruff-format, mypy from `.venv`, gitleaks,
  pytest, validate-trait-config) must pass — never `--no-verify`. New test files
  importing `lfg_core` at module top MUST carry the env-guard preamble
  (`os.environ.setdefault("BUNNY_PULL_ZONE", ...)`, `os.environ.setdefault(
  "LAYER_SOURCE", "local")`) before the import or they strand frozen config.
- **Any `app.js` change bumps the cache-buster** in `webapp/client/index.html`
  (`app.js?v=NN`) in the same commit, or the client never sees it.

---

### Task 1: Detach trait settlement from the buy poll

**Files:**
- Modify: `lfg_service/app.py` (buy-status `sold` branch; add `_spawn_bg` helper)
- Test: `tests/test_market_buy_settlement.py` (new or extend existing buy-poll test)

**Interfaces:**
- Produces: `_spawn_bg(coro) -> asyncio.Task` (module-private; logs exceptions in
  a done-callback, never swallows).
- Consumes: existing `_settle_trait_sale`, `_close_listing_sync`,
  `market_flow.advance_buy_session`.

- [ ] **Step 1: Write the failing test(s)** — TDD. With the env-guard preamble at
  module top, drive the buy-status handler to the `sold` outcome for a trait
  listing with `_settle_trait_sale` patched to an `asyncio.Event`-gated coroutine
  that never completes within the test. Assert: (a) the handler's HTTP response
  is produced without waiting on settlement (the event is never set, yet the poll
  returns), (b) `_close_listing_sync(..., "sold", wallet)` ran, (c) a background
  task was scheduled for `_settle_trait_sale`.
  ```python
  import os
  os.environ.setdefault("BUNNY_PULL_ZONE", "https://example.b-cdn.net")
  os.environ.setdefault("LAYER_SOURCE", "local")
  # ... patch market_flow.advance_buy_session -> "sold", capture asyncio tasks,
  #     assert the poll returns before the (gated) settle coro completes.
  ```
- [ ] **Step 2: Run to verify they fail** — `.venv/bin/python -m pytest
  tests/test_market_buy_settlement.py -q`; expect failure (today the handler
  awaits settlement inline, so the gated coro would hang/deadlock the test).
- [ ] **Step 3: Implement** — add `_spawn_bg` near the sweep helpers; in the
  `outcome == "sold"` branch replace `await _settle_trait_sale(...)` (trait case)
  with `_spawn_bg(_settle_trait_sale(...))`. Keep the `_close_listing_sync`
  `sold` write (with `session.wallet_address` buyer) exactly as-is, before the
  spawn.
- [ ] **Step 4: Run to verify they pass** — same pytest command, green.
- [ ] **Step 5: Wider suite / regression run** — `.venv/bin/python -m pytest
  tests/ -q -k "market or buy or settle"` then the full `tests/` suite.
- [ ] **Step 6: Commit** — `perf(marketplace): detach trait settlement from the buy poll (#130)`

---

### Task 2: Durable settlement-sweep attempt counter

**Files:**
- Modify: `lfg_core/market_store.py` (schema ALTER + `bump/reset_settle_attempts`)
- Modify: `lfg_core/shop_store.py` (analogous `shop_orders.settle_attempts`)
- Modify: `lfg_service/app.py` (`settle_pending_trait_sales`,
  `sweep_shop_orders`: use the durable count; delete `_sweep_attempts` /
  `_shop_settle_attempts` in-memory dicts)
- Test: `tests/test_market_store.py`, `tests/test_shop_store.py` (extend)

**Interfaces:**
- Produces: `market_store.bump_settle_attempts(conn, offer_index) -> int`,
  `market_store.reset_settle_attempts(conn, offer_index) -> None`; matching
  `shop_store` helpers keyed by `session_id`/order id.
- Consumes: existing `init_schema` migration block, `unsettled_trait_sales`.

- [ ] **Step 1: Write the failing test(s)** — env-guard preamble at top. Assert:
  (a) after `init_schema` on a DB created from the *pre-migration* CREATE (no
  `settle_attempts`), the column exists and defaults 0; (b) `bump_settle_attempts`
  increments and returns the new value; (c) the value persists across a fresh
  `sqlite3.connect` to the same file (simulated restart); (d) `reset_settle_attempts`
  zeroes it. Mirror for `shop_store`.
- [ ] **Step 2: Run to verify they fail** — `.venv/bin/python -m pytest
  tests/test_market_store.py tests/test_shop_store.py -q`; expect
  `no such column: settle_attempts` / `AttributeError`.
- [ ] **Step 3: Implement** — add `settle_attempts INTEGER NOT NULL DEFAULT 0` to
  the `_SCHEMA` CREATE and to the forward-only ALTER block in `init_schema`
  (follow the `buyer`/`amount_brix` precedent: guard on `col not in cols`); add
  the two helpers; same in `shop_store`. In `lfg_service/app.py` rewrite the two
  sweeps to read/increment the durable count (`bump_settle_attempts` on failure,
  threshold check against `_SWEEP_MAX_ATTEMPTS` / `_SHOP_SWEEP_MAX_ATTEMPTS`,
  `reset` on success) and remove the `_sweep_attempts` / `_shop_settle_attempts`
  module dicts and their reads.
- [ ] **Step 4: Run to verify they pass** — same pytest commands, green.
- [ ] **Step 5: Wider suite / regression run** — `.venv/bin/python -m pytest
  tests/ -q -k "sweep or settle or shop or market"` then full `tests/`.
- [ ] **Step 6: Commit** — `fix(marketplace): durable settlement-sweep attempt counter (#130)`

---

### Task 3: Hide 0-drop character listings from browse

**Files:**
- Modify: `lfg_core/market_store.py` (`browse` / `_browse_character_rows`)
- Test: `tests/test_market_store.py` (extend `browse` cases)

**Interfaces:** no signature change to `browse`; behavior change only.

- [ ] **Step 1: Write the failing test(s)** — env-guard preamble. Seed
  `market_listings` with three live character rows: `amount_drops=0`,
  `amount_drops=None`, `amount_drops=1_000_000`, plus one live trait row
  (`amount_brix`). Assert `browse(kind='character')` returns only the positive
  character row; assert `browse(kind='trait')` still returns the BRIX row
  (unaffected).
- [ ] **Step 2: Run to verify they fail** — `.venv/bin/python -m pytest
  tests/test_market_store.py -q -k browse`; expect the 0/None rows present.
- [ ] **Step 3: Implement** — in `browse`, right after fetching character rows,
  drop rows where `amount_drops is None or amount_drops <= 0` (character kind
  only). Add a one-line comment tying it to #130 and the free-transfer rationale.
- [ ] **Step 4: Run to verify they pass** — same command, green.
- [ ] **Step 5: Wider suite / regression run** — `.venv/bin/python -m pytest
  tests/ -q -k "market"` then full `tests/`.
- [ ] **Step 6: Commit** — `fix(marketplace): hide non-positive-price character listings from browse (#130)`

---

### Task 4: Trait value filter datalist

**Files:**
- Modify: `webapp/client/index.html` (add `<datalist>`; bump `app.js?v=`)
- Modify: `webapp/client/app.js` (populate the datalist on slot change from the
  shop catalog)
- Modify: `webapp/client/market_pure.js` (pure helper: catalog rows -> `slot ->
  sorted values` map)
- Test: existing JS pure-helper test harness if present (mirror
  `market_pure.js`'s existing tested exports); otherwise a minimal node/pytest-
  invoked assertion of the pure helper.

**Interfaces:**
- Produces: `market_pure.catalogSlotValues(rows) -> {slot: [value, ...]}` (pure,
  sorted, deduped).
- Consumes: `GET /api/shop/catalog` rows (already fetched by the shop UI;
  `handle_shop_catalog`), the existing `market-trait-slot` / `market-trait-value`
  inputs in `loadMarketBrowse`.

- [ ] **Step 1: Write the failing test(s)** — add `catalogSlotValues` to the
  existing `market_pure.js` test file (same pattern as `traitFilterToken` /
  `buildListingsParams` tests). Assert grouping by slot, sort, and dedupe from a
  fixture of `[{slot, value}, ...]` rows.
- [ ] **Step 2: Run to verify they fail** — run the repo's JS test command (the
  one that already covers `market_pure.js`); expect `catalogSlotValues` undefined.
- [ ] **Step 3: Implement** — export `catalogSlotValues` from `market_pure.js`;
  in `app.js`, fetch `/api/shop/catalog` once (cache in `marketState`), build the
  map, and on `market-trait-slot` change repopulate a `<datalist>` bound to
  `market-trait-value` via `list=`. Add the `<datalist id="market-trait-values">`
  to `index.html` and set `list="market-trait-values"` on the value input. Bump
  `app.js?v=32` → `?v=33` in `index.html`.
- [ ] **Step 4: Run to verify they pass** — JS test command, green.
- [ ] **Step 5: Wider suite / regression run** — full `pytest tests/` (the client
  change touches no Python, but the gate runs it) + a manual `WEBAPP_DEV_MODE=1`
  load if available to eyeball the datalist.
- [ ] **Step 6: Commit** — `feat(marketplace): trait value filter datalist (#130)` (includes the cache-buster bump)

---

### Final Task: Full gate + PR

- [ ] Run the complete gate locally: `.venv/bin/python -m pytest tests/ -q`,
  `ruff check .`, `ruff format --check .`, `mypy` (from `.venv`), and
  `validate-trait-config`. All green; never `--no-verify`.
- [ ] Confirm `app.js?v=` was bumped in the Task 4 commit and no stale
  cache-buster remains.
- [ ] Push the branch and open a **non-draft** PR against `Team-Hamsa/LFG`
  (`main`), body summarizing the four items closed and explicitly noting items
  1/4/7/8 of #130 are recommended for descope (with the spec's rationale) so the
  reviewer/maintainer can close #130. **No AI attribution** in the commit
  trailers or PR body.
- [ ] Wait for **Greptile** and **CodeRabbit**; close out every actionable
  finding on its own thread (fix in code AND reply naming the fixing commit)
  before merge, per repo convention.
