# Marketplace follow-ups (browse polish + helper cleanup) Implementation Plan

**Date:** 2026-07-24
**Status:** live — re-reviewed 2026-08-19 against main@27dc301
**Issue:** #130
**Last review:** 2026-08-19

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close #130. Three small code changes (publish `market_store`'s
attribute helpers and drop the `cast` type-lie; hide non-positive-price
character listings from browse; give the trait-value filter a datalist) plus
one doc-only correction, then tick or strike every remaining checkbox on the
issue so it can be closed.

**Not implemented here** (see §3 of the spec for the reasoning):
- Item 1, per-network trait on-ledger ops — obsolete; with `ECONOMY_ENABLED`
  on, `config.validate_economy_config` raises at import on a split, so no
  stack can run the topology the item targets.
- Item 2, detaching settlement from the buy poll — won't-fix as specified;
  the await is deliberate and the UI copy depends on it.
- Item 3, durable sweep attempt counter — dropped; durable is worse under a
  crash-loop.

**Already landed, do not redo:**
- All six Hardening/polish boxes — PR #150 (merged 2026-07-10).
- Item 7, trait thumbnail heuristic — the resolver now lives in
  `lfg_core/trait_images.py::trait_image_url` and, under a `LocalLayerStore`
  (`LAYER_SOURCE=local`, the deployed setting), disk-verifies the body via
  `LocalLayerStore.find_display_body`; PR #357 (merged 2026-08-15) fixed the
  Mine surface too.
- Item 8, brokered accept — `lfg_core/nft_listener.py::_accept_new_owner`,
  covered by `tests/test_market_listener.py::
  test_accept_brokered_persists_buy_offer_owner_not_broker`.

**Architecture:** Four independent seams —
1. `lfg_core/market_store.py` + its three call sites (helper rename, typing).
2. `lfg_core/market_store.py::browse` (non-positive character price filter).
3. `webapp/client/*` (datalist; no server change — reuses `/api/shop/catalog`).
4. Prose only: `CLAUDE.md`, two `lfg_service/app.py` docstrings
   (`_market_network`, `_compute_mine_data`), two test comments.

**Tech Stack:** Python 3 / aiohttp / pytest; vanilla no-build ES-module client
with a Node-executed pure-helper test harness.

## Global Constraints

- **SourceTag = 2606160021 + provenance memos** stay on every tx. No task here
  builds or alters a transaction.
- **Do not relax `config.validate_economy_config`** and do not delete
  `lfg_service/app.py::_market_network`. Task 4 corrects why the seam exists,
  not whether it should.
- **Pre-push gate** (ruff `--fix`, ruff-format, mypy from `.venv`, gitleaks,
  pytest, validate-trait-config, check-repo-layout, audit-layer-dimensions)
  must pass — never `--no-verify`. Per #323 the root `conftest.py` supplies
  the mandatory env vars, so new test files need no env-guard preamble.
- **Cache-busters:** an `app.js` edit bumps `app.js?v=59` in
  `webapp/client/index.html`; a `market_pure.js` edit bumps the `?v=23` on
  its `import` at the top of `app.js`. Both, in the same commit as the
  change — **and the two tests that assert the literal `"app.js?v=59"`**
  (`tests/test_harvest_pure_js.py::test_index_html_cache_buster_bumped`,
  `tests/test_app_js_deeplink.py::test_cache_busters_bumped`), or the gate
  fails. Nothing pins `market_pure.js?v=23` literally.
- **Blocked on a product answer:** Task 2 assumes the maintainer chose *hide*
  for #130's 0-drop question. If they choose *accept free transfers*, skip
  Task 2 and record the decision on the issue instead.

---

### Task 1: Publish `attributes_match` / `row_attrs`, drop the `cast`

The issue conditions this on "if a second caller appears". Two have.

**Files:**
- Modify: `lfg_core/market_store.py` (rename `_attributes_match` →
  `attributes_match`, `_row_attrs` → `row_attrs`; retype `row_attrs`' first
  parameter from `sqlite3.Row` to `Any`)
- Modify: `lfg_service/app.py` (call sites in `handle_market_listings`'
  post-cache trait filter; delete `cast(sqlite3.Row, r)` and the four-line
  comment apologising for it. Both the `cast` and `sqlite3` imports **stay** —
  `cast` is still used by `cast(_Handler, wrapper)` at the end of the
  `require_market` decorator and `sqlite3` is used throughout the module)
- Modify: `webapp/mock_market.py` (`browse`'s `_attributes_match` call)
- Test: `tests/test_market_store.py` (extend)

**Interfaces:**
- Produces: `market_store.attributes_match(attrs, filters) -> bool`,
  `market_store.row_attrs(row: Any, kind: str) -> list[dict[str, str]]`.
- Consumes: nothing new.

> **Do not annotate `row` as `Mapping[str, Any]`.** `sqlite3.Row` is not a
> `Mapping` subclass, so `browse`'s own internal `row_attrs(r, kind)` call
> (which passes raw `sqlite3.Row` objects — `browse` only converts with
> `dict(r)` on the way out) would fail the `strict = true` mypy gate:
> `Argument 1 … has incompatible type "Row"; expected "Mapping[str, Any]"
> [arg-type]`. Verified with mypy 2026-08-19. Use `Any`, matching the
> existing `market_store.listing_price(row: Any)` in the same module; a local
> `Protocol` with `def __getitem__(self, key: str) -> Any: ...` also
> typechecks for both `sqlite3.Row` and `dict` if a tighter bound is wanted.

- [ ] **Step 1: Write the failing test(s)** — in `tests/test_market_store.py`,
  assert `market_store.row_attrs` accepts a plain `dict` (not just a
  `sqlite3.Row`) for both kinds: a character dict carrying
  `attributes_json`, and a trait dict carrying `slot`/`value`. Assert
  `market_store.attributes_match` is reachable under its public name.
- [ ] **Step 2: Run to verify they fail** — `.venv/bin/python -m pytest
  tests/test_market_store.py -q`; expect `AttributeError` on the public names.
- [ ] **Step 3: Implement** — rename both functions, retype `row_attrs`'
  first parameter to `Any` (see the warning above; `Any` is already imported
  in `market_store.py`), update the three call sites (`browse`,
  `lfg_service/app.py`, `webapp/mock_market.py`) and delete the
  `cast(sqlite3.Row, r)` plus its explanatory comment. `mock_market` builds
  its attrs list inline, so only its `attributes_match` call needs the new
  name. Behavior must not change anywhere.
- [ ] **Step 4: Run to verify they pass** — same pytest command, green.
- [ ] **Step 5: Wider suite / regression run** — `.venv/bin/python -m pytest
  tests/ -q -k "market"`, then the full `tests/` suite, then mypy. mypy is
  the real gate for this task: it must accept both the cast removal in
  `app.py` and `browse`'s internal `sqlite3.Row` call under the new
  annotation.
- [ ] **Step 6: Commit** — `refactor(marketplace): publish market_store's attribute helpers and drop the sqlite3.Row cast (#130)`

---

### Task 2: Hide non-positive-price character listings from browse

Gated on the product answer (see Global Constraints).

**Files:**
- Modify: `lfg_core/market_store.py` (`browse`, character branch)
- Test: `tests/test_market_store.py` (extend the browse cases)

**Interfaces:** no signature change to `browse`; behavior change only.

- [ ] **Step 1: Write the failing test(s)** — extend `TestBrowseCharacters`
  (and assert non-interference from `TestBrowseTraits` / `TestBrowseBrix`):
  seed `market_listings` with three live character rows (`amount_drops` =
  `0`, `None`, `1_000_000`) plus one live BRIX trait row, with the
  `onchain_nfts` / `trait_tokens` owner rows the browse joins require.
  Assert: `browse(kind='character')` returns only the positive row; the
  default `price_asc` page no longer leads with the 0-drop row;
  `browse(kind='trait')` still returns the BRIX row.
- [ ] **Step 2: Run to verify they fail** — `.venv/bin/python -m pytest
  tests/test_market_store.py -q -k Browse` (the browse cases live in
  `TestBrowse*` classes; individual test names do not contain "browse");
  expect the 0/None rows present and sorted first.
- [ ] **Step 3: Implement** — in `browse`, immediately after the character
  fetch, drop rows where `amount_drops is None or amount_drops <= 0`
  (character kind only; trait rows are BRIX-denominated). One comment tying
  it to #130 and the free-transfer rationale, and one noting this is
  pre-cache by construction (`lfg_service/app.py::_compute_market_rows`
  caches `browse`'s output) and is safe only because the rule is
  unconditional. Leave `webapp/mock_market.py::browse` alone — none of its
  seed rows is a non-positive-drops *character* (its `amount_drops: None`
  seeds are all `kind="trait"`, priced in `amount_brix`) — and say so in the
  commit body.
- [ ] **Step 4: Run to verify they pass** — same command, green.
- [ ] **Step 5: Wider suite / regression run** — `.venv/bin/python -m pytest
  tests/ -q -k "market"`, then full `tests/`.
- [ ] **Step 6: Commit** — `fix(marketplace): hide non-positive-price character listings from browse (#130)`

---

### Task 3: Trait value filter datalist

**Files:**
- Modify: `webapp/client/market_pure.js` (new pure helper)
- Modify: `webapp/client/app.js` (populate the datalist on slot change; bump
  the `market_pure.js?v=` import)
- Modify: `webapp/client/index.html` (add the `<datalist>` — the file has
  none today — `list=` on the value input, bump `app.js?v=59` → `?v=60`)
- Test: `tests/test_market_pure_js.py` (extend)
- Test: `tests/test_harvest_pure_js.py`, `tests/test_app_js_deeplink.py`
  (retarget the literal `"app.js?v=59"` assertions to `?v=60`)

**Interfaces:**
- Produces: `market_pure.catalogSlotValues(items) -> {slot: [value, ...]}`
  (pure, sorted, deduped, null-tolerant).
- Consumes: `GET /api/shop/catalog` → `{"items": [{slot, value, price_brix,
  image_url}, ...]}`, already cached in `app.js`'s `shopState.items`; the
  existing `market-trait-slot` / `market-trait-value` controls read by
  `loadMarketBrowse`.

- [ ] **Step 1: Write the failing test(s)** — add `catalogSlotValues` cases to
  `tests/test_market_pure_js.py` alongside the existing `filterShopItems` /
  `shopSlotCounts` ones: grouping by slot, A→Z value sort, dedupe, and
  `null`/`[]` input returning `{}`. (That file executes the module under
  Node and skips when `node` is absent — no `lfg_core` import, no preamble.)
- [ ] **Step 2: Run to verify they fail** — `.venv/bin/python -m pytest
  tests/test_market_pure_js.py -q`; expect `catalogSlotValues` undefined.
- [ ] **Step 3: Implement** — export `catalogSlotValues` from
  `market_pure.js`. In `app.js`, reuse `shopState.items` when populated and
  otherwise fetch `/api/shop/catalog` once (cache it on the market state);
  build the map and repopulate `<datalist id="market-trait-values">` when
  `market-trait-slot` changes. In `index.html` add the datalist and set
  `list="market-trait-values"` on `#market-trait-value`. Degrade silently to
  today's free-text behavior when the catalog is empty — `handle_shop_catalog`
  returns `{"items": []}` with `ECONOMY_ENABLED` off. Keep the input free-text
  (a `<select>` would make a shop-excluded but market-listed value
  unfilterable). Bump both cache-busters, and retarget the two literal
  `"app.js?v=59"` assertions in `tests/test_harvest_pure_js.py` /
  `tests/test_app_js_deeplink.py` to the new value.
- [ ] **Step 4: Run to verify they pass** — same pytest command, green.
- [ ] **Step 5: Wider suite / regression run** — full `.venv/bin/python -m
  pytest tests/ -q` (the gate runs it even for a client-only change; the
  cache-buster assertions above are why) plus a `WEBAPP_DEV_MODE=1` load to
  eyeball the datalist and confirm a typo still yields the empty grid.
- [ ] **Step 6: Commit** — `feat(marketplace): trait value filter datalist (#130)` (includes both cache-buster bumps)

---

### Task 4: Correct the stale split-topology prose (doc only)

No code paths change. Per repo convention a docs/comments-only change can go
straight to `main` without a PR — but if it rides along with Tasks 1-3 in one
branch, keep it as its own commit.

**Files:**
- Modify: `CLAUDE.md` ("Per-kind network seam" paragraph)
- Modify: `lfg_service/app.py` (the `_market_network` docstring **and** the
  `_compute_mine_data` docstring, which says "the networks can differ in the
  deployed topology")
- Modify: `tests/test_market_api.py` (the "Split-network topology" section
  header block) and `tests/test_config_economy_validate.py` (the comment in
  `test_boot_ok_when_disabled_and_networks_mismatch` — the mildest of the
  five: it describes the still-legal *economy-disabled* posture, so just drop
  the "still" and the present tense, and leave the test itself alone)

- [ ] **Step 1: Verify the claim before rewriting it** — the tree-verifiable
  fact is `config.validate_economy_config` (called at import in
  `lfg_core/config.py`): with `ECONOMY_ENABLED` on it raises on
  `ECONOMY_NETWORK != XRPL_NETWORK`, so no economy-enabled process can run a
  split. The deployment fact is attributed, not verifiable here (the `.env`
  files are untracked): issue #185's closing comment (2026-08-16) records the
  2026-07-21 `ECONOMY_ENABLED=1` + `ECONOMY_NETWORK=mainnet` flip. **Do not
  cite `docs/ops/env.staging.example`** as evidence that staging is matched —
  it never mentions `ECONOMY_NETWORK` and its header says to copy prod's
  `.env` and apply only the listed overrides, so staging inherits prod's
  value rather than falling back to the `testnet` default. (That combination
  plus its `ECONOMY_ENABLED=1` reads as unbootable; file it separately if you
  want the deltas file fixed — it is not part of #130.)
- [ ] **Step 2: Rewrite** — the seam is defensive, not descriptive of a live
  split: `ECONOMY_NETWORK` and `XRPL_NETWORK` are separate knobs and a trait
  read resolved on the wrong one silently returns empty for every user, so
  every trait-economy-backed table must keep resolving via `ECONOMY_NETWORK`.
  Keep the "do not simplify to a single network" warning. Drop "until the
  economy reaches mainnet" and "stays testnet-gated". State that
  `config.validate_economy_config` refuses to boot a split when
  `ECONOMY_ENABLED` is on, which is why the trait on-ledger ops can keep
  assuming one chain.
- [ ] **Step 3: Verify nothing else drifted** — run
  `grep -rnE 'testnet-gated|until the economy reaches mainnet|deployed topology' --include='*.py' --include='*.md' --include='*.js' .`
  and confirm the only remaining hits are under `docs/superpowers/` (which
  keeps the historical record — `2026-07-15-staging-prod-stacks-design.md`
  legitimately describes the topology as of its own date and must not be
  rewritten). Note the glob quoting: unquoted `--include=*.py` is expanded by
  zsh before grep sees it.
- [ ] **Step 4: Commit** — `docs: the per-kind network seam is defensive, not a live split (#130)`

---

### Final Task: Gate, PR, and close the issue

- [ ] Run the complete gate locally: `.venv/bin/python -m pytest tests/ -q`,
  `ruff check .`, `ruff format --check .`, mypy from `.venv`, gitleaks,
  `validate-trait-config`. All green; never `--no-verify`.
- [ ] Confirm `app.js?v=` and the `market_pure.js?v=` import were both bumped
  in the Task 3 commit, that the two literal `"app.js?v="` test assertions
  moved with them, and that no other stale cache-buster remains.
- [ ] Push and open a **non-draft** PR against `Team-Hamsa/LFG` (`main`). The
  body states which #130 boxes the PR closes (4, 5, 6 and the item-1 doc
  residue) and which are recommended won't-fix with their reasons (1, 2, 3),
  so the maintainer can close the issue in one pass. No AI attribution in
  commits or the PR body.
- [ ] Wait for **Greptile** and **CodeRabbit**; close out every actionable
  finding on its own thread (fix in code AND reply naming the fixing commit)
  before merge. Greptile posts no review object on a clean pass — read the
  `Greptile Review` check-run summary.
- [ ] After merge, tick items 4/5/6/7/8 and strike 1/2/3 on #130 with a
  one-line reason each, then close it.
