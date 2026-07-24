# Session resume for swap / economy / market flows — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** After a Discord-mobile Activity webview relaunch (app-switch to Xaman),
re-attach the client to a still-running swap / marketplace / economy / shop
session instead of dumping the user on the mint home screen — mirroring the #216
mint-resume pattern (`GET /api/mint/active` + `resumeMint()`).

**Architecture:** Two independent seams.
1. **Server** — one consolidated `GET /api/sessions/active` endpoint reusing the
   existing `_active_session` / `_prune_sessions` primitives over the
   `mint/bulk/swap/market/economy/shop` session dicts; plus additive `kind` keys
   on the market/economy status payloads so the client can route a resumed
   session to the right poller.
2. **Client** — a Node-testable `resume_pure.js` priority picker and one
   `resumeAnyFlow()` boot dispatcher that reuses every existing renderer/poller
   (`pollSwap`, `pollMarketFlow`, `pollEconomyOp`, `resumeShopBuy`).

**Tech Stack:** Python 3 / aiohttp / asyncio / pytest; vanilla no-build JS
client (ES-module imports with `?v=` cache-busters).

## Global Constraints

- **No transaction is built by this feature** — resume is read-only re-attach.
  SourceTag=2606160021 and provenance memos were already stamped at payload
  build time and are untouched. Do not add any tx path.
- The **pre-push gate** (ruff `--fix`, ruff-format, mypy from `.venv`, gitleaks,
  pytest, validate-trait-config) must pass. Never `--no-verify`. In a worktree,
  ensure the `.venv` symlink exists or the gate silently skips.
- **Any `app.js` change bumps the cache-buster** in `webapp/client/index.html`
  in the same commit; a change to `resume_pure.js` (new file) means adding its
  `import ... ?v=N` in `app.js` and the corresponding entry — keep import `?v=`
  and any HTML reference in lockstep.
- Session isolation is load-bearing: every lookup filters on `discord_id` **and**
  `platform` via `_active_session`. Preserve both.

---

### Task 1: Surface `kind` in market + economy status payloads

**Files:**
- Modify: `lfg_core/market_flow.py` (add `"kind"` to `to_dict()` on
  `ListSession`, `BuySession`, `CancelSession`, `BidSession`, `BidAcceptSession`,
  `TraitSellSession`)
- Modify: `webapp/economy_api.py` (add `"kind": kind` to the `base` dict in
  `economy_session_dict`)
- Test: `tests/test_session_resume.py` (new)

**Interfaces:**
- Produces: each market `to_dict()` now includes `kind` ∈
  `list|buy|cancel|bid|bid_accept|trait_list`; `economy_session_dict()` includes
  `kind` ∈ `harvest|assemble|equip|extract|deposit`.
- Consumes: existing `.kind` dataclass fields (already present on every session).

- [ ] **Step 1: Write the failing test(s)** — TDD. Module-top env-guard preamble:
  ```python
  import os
  os.environ.setdefault("BUNNY_PULL_ZONE", "https://example.b-cdn.net")
  os.environ.setdefault("LAYER_SOURCE", "local")

  from lfg_core import market_flow
  from webapp import economy_api

  def test_market_sessions_emit_kind():
      s = market_flow.BuySession(
          discord_id="1", wallet_address="rBuyer", offer_index="OI",
          nft_id="00080000AA", listing_kind="character", network="testnet",
          amount_drops=1000,
      )
      assert s.to_dict()["kind"] == "buy"
      ls = market_flow.ListSession(
          discord_id="1", wallet_address="rS", nft_id="00080000AA",
          listing_kind="character", amount_drops=1000,
      )
      assert ls.to_dict()["kind"] == "list"

  def test_economy_session_dict_emits_kind():
      class _Dep:  # minimal stand-in for a deposit inner session
          id = "abc"; state = "running"; error = None; slot = "Hat"; value = "Wizard Hat"
      d = economy_api.economy_session_dict("deposit", _Dep())
      assert d["kind"] == "deposit"
  ```
  (Add analogous asserts for `cancel`/`bid`/`bid_accept`/`trait_list` and one more economy kind.)
- [ ] **Step 2: Run to verify they fail** — `\.venv/bin/python -m pytest tests/test_session_resume.py -q` → `KeyError: 'kind'` / assertion failures.
- [ ] **Step 3: Implement** — add `"kind": self.kind,` to each market `to_dict()`
  return, and `base = {"id": s.id, "state": s.state, "error": s.error, "kind": kind}`
  in `economy_session_dict`.
- [ ] **Step 4: Run to verify they pass** — same pytest command, green.
- [ ] **Step 5: Wider suite / regression run** — `\.venv/bin/python -m pytest tests/ -q -k "market or economy"` to confirm no existing consumer asserted the exact old dict shape.
- [ ] **Step 6: Commit** — `feat(sessions): surface kind in market/economy status payloads (#221)`

---

### Task 2: Consolidated `GET /api/sessions/active` endpoint

**Files:**
- Modify: `lfg_service/app.py` (add `handle_sessions_active` + route registration)
- Test: `tests/test_session_resume.py`

**Interfaces:**
- Produces: `GET /api/sessions/active` → `{"mint","bulk","swap","market","economy","shop"}`,
  each `session.to_dict()` or `null`, per-user + per-platform, terminal-pruned.
- Consumes: `_active_session`, `_prune_sessions`, `_prune_shop_sessions`, and the
  module-level `mint_sessions`/`bulk_sessions`/`swap_sessions`/`market_sessions`/
  `economy_sessions`/`shop_sessions` dicts + each flow's `TERMINAL_STATES`.

- [ ] **Step 1: Write the failing test(s)** — use the existing app test fixtures
  (grep `tests/` for how `handle_mint_active` is exercised — reuse that auth'd
  client + user seeding). Seed a live `SwapSession` into `swap_sessions` for the
  test user, assert `GET /api/sessions/active` returns it under `"swap"` with the
  live state and `null` for the other flows. Add a case seeding a session for a
  *different* platform and asserting it is **absent** (isolation), and a terminal
  session asserting it is pruned/omitted.
- [ ] **Step 2: Run to verify they fail** — endpoint 404 (route not registered).
- [ ] **Step 3: Implement** — add the handler exactly as sketched in the spec
  (`@require_auth`, prune each dict, `pick(store, terminal)` helper, bulk handled
  via its `.to_dict()`), and register
  `app.router.add_get("/api/sessions/active", handle_sessions_active)` alongside
  the other routes in `create_app`. Reference `_SHOP_TERMINAL_STATES` /
  `_prune_shop_sessions` for the shop terminal set (grep for the exact name;
  `_prune_shop_sessions` already exists at ~line 2556).
- [ ] **Step 4: Run to verify they pass** — pytest green.
- [ ] **Step 5: Wider suite / regression run** — `\.venv/bin/python -m pytest tests/ -q` for the service module; confirm `/api/mint/active` still passes unchanged.
- [ ] **Step 6: Commit** — `feat(sessions): add GET /api/sessions/active consolidated resume endpoint (#221)`

---

### Task 3: `resume_pure.js` priority picker (Node-testable)

**Files:**
- Create: `webapp/client/resume_pure.js`
- Test: `webapp/client/resume_pure.test.mjs` (or the repo's existing pure-module
  test harness — grep for how `mint_pure` is tested and match it)

**Interfaces:**
- Produces: `export function pickActiveFlow(sessions) -> {flow, session} | null`.
- Consumes: the `/api/sessions/active` response envelope.

- [ ] **Step 1: Write the failing test(s)** — assert: an all-`null` payload →
  `null`; a payload with only a live `swap` → `{flow:'swap'}`; a payload with a
  live `mint` **and** a live `market` → picks `mint` (priority); a payload whose
  only session is in a terminal state → `null`; a `market` session with
  `kind:'buy'` is returned intact so the caller can route it.
- [ ] **Step 2: Run to verify they fail** — run the Node test (module not created yet).
- [ ] **Step 3: Implement** — the `ORDER` array, per-flow `TERMINAL` sets, and
  `pickActiveFlow` from the spec. Keep terminal sets aligned with the server
  `TERMINAL_STATES` (verify bulk's set against `bulk_mint_flow.TERMINAL_STATES`).
- [ ] **Step 4: Run to verify they pass** — Node test green.
- [ ] **Step 5: Wider suite / regression run** — run the full client pure-module test set.
- [ ] **Step 6: Commit** — `feat(client): resume_pure priority picker for cold-boot session resume (#221)`

---

### Task 4: `resumeAnyFlow()` boot dispatcher + per-flow attach helpers

**Files:**
- Modify: `webapp/client/app.js` (import `resume_pure.js?v=1`; add
  `resumeAnyFlow()` and `attachMarketResume`/`attachEconomyResume` helpers;
  refactor the two boot sites to call `resumeAnyFlow()`; factor `resumeMint`/
  `resumeBulkMint` bodies to accept a pre-fetched session so no double fetch)
- Modify: `webapp/client/index.html` (bump `app.js` `?v=`; add `resume_pure.js`
  reference if the HTML enumerates modules)

**Interfaces:**
- Produces: `resumeAnyFlow()` returns `true` when a flow resumed.
- Consumes: `pollSwap`, `openSwapper`, `pollMarketFlow` + `MARKET_STATUS_PATH` +
  the per-kind render fns, `pollEconomyOp`, `resumeShopBuy`, and the existing
  mint/bulk resume bodies.

- [ ] **Step 1: Write the failing test(s)** — where pure logic can be isolated,
  extend `resume_pure` with a `flowToPoller(flow)`-style mapping test if you
  introduce one; the DOM-coupled dispatch itself is covered by the Task 5 manual
  smoke. (Do not fabricate a jsdom harness the repo doesn't have — grep first;
  if none exists, keep the routing table data-driven in `resume_pure.js` and
  unit-test *that table*, leaving only the thin DOM glue in `app.js`.)
- [ ] **Step 2: Run to verify they fail** — Node test for the routing table fails.
- [ ] **Step 3: Implement** —
  - Add `import * as resumePure from './resume_pure.js?v=1';` at the top of `app.js`.
  - Write `resumeAnyFlow()` (spec sketch): one `api('/api/sessions/active')`
    call, `resumePure.pickActiveFlow(...)`, `switch` on `flow`.
  - `attachMarketResume(session)`: `showPanel('flow-panel')`; build a
    `RENDER[session.kind]` map from the render fns `marketFlow(...)` already
    passes; call `pollMarketFlow(session.kind, session.id, RENDER[session.kind])`;
    show the `🔄 Reconnecting…` banner first.
  - `attachEconomyResume(session)`: reveal the economy panel and
    `pollEconomyOp(session.kind, session)` with a reconnect banner.
  - swap: `openSwapper(); pollSwap(session.id);` shop: `await resumeShopBuy(session.id);`
  - Refactor `resumeMint()`/`resumeBulkMint()` to accept the already-fetched
    `session` (drop their own `/api/mint/active` fetch when called from
    `resumeAnyFlow`) — keep a thin back-compat wrapper if simpler.
  - Replace **both** boot sites:
    `if (!(await resumeAnyFlow())) showMintHome();`
  - Bump `app.js` `?v=` in `index.html`.
- [ ] **Step 4: Run to verify they pass** — Node routing-table test green; load
  the client in `WEBAPP_DEV_MODE=1` and confirm no console errors on boot.
- [ ] **Step 5: Wider suite / regression run** — full client pure-module tests +
  `\.venv/bin/python -m pytest webapp/ -q` smoke.
- [ ] **Step 6: Commit** — `feat(client): resume swap/market/economy/shop sessions on Activity relaunch (#221)`

---

### Final Task: Full gate + PR

- [ ] Run the full suite: `\.venv/bin/python -m pytest -q` (all green) plus
  `ruff check .`, `ruff format --check .`, `mypy` from `.venv`, and the client
  pure-module tests.
- [ ] Confirm the `app.js` cache-buster bump landed in the same commit as the
  `app.js` edit and that `resume_pure.js`'s `import ... ?v=` matches.
- [ ] Push the feature branch and `gh pr create` **non-draft** against
  `Team-Hamsa/LFG` per repo rules: **no AI attribution** in the commit trailer
  or PR body. Body: summary, the two seams, "no tx built → SourceTag/memos
  untouched", the manual-smoke checklist from the spec.
- [ ] Wait for **Greptile** and **CodeRabbit**. Greptile's clean verdict lives
  only in the `Greptile Review` check-run summary (no comment on a pass). Resolve
  every actionable finding — fix in code **and** reply on the finding's thread
  naming the fixing commit — before merge. This touches application code, so it
  is a normal reviewed PR (not a trivial direct-push).
- [ ] After merge to `main` (auto-deploys **staging**), do the real repro on
  Discord mobile against staging, then promote to prod with `scripts/promote.sh`.
