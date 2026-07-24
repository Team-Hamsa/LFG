# Deep-link all Xaman sign requests (mobile-primary) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Xaman deep link the **primary, prominent** way to sign on
mobile — auto-open where the user explicitly asked to sign, QR demoted to a
"sign on another device" fallback — and fold the last hand-rolled sign surface
(Discord trustline) onto the shared payload builder so it too gets the deep link,
SourceTag, memos and push. Deep-link threading and push (#135/#212) already
exist; this plan is presentation + one consolidation, not a new mechanism.

**Architecture:** Three independent seams.
- **A (client, primary):** `isCoarsePointer()` + `applySignDelivery()` +
  `maybeAutoOpen()` in `webapp/client/app.js`, wired into `showFlow` and the
  market/economy inline sign panels; a "Show QR" disclosure in
  `webapp/client/index.html`.
- **B (server/discord):** route `surfaces/discord_bot/trustline.py` through
  `lfg_core/xumm_ops._create_xumm_payload`.
- **C:** documentation-only deferral of CLI economy scripts.

**Tech Stack:** Python 3 / aiohttp / asyncio / pytest; vanilla no-build JS client.

## Global Constraints

- **SourceTag = 2606160021 + provenance memos preserved on any tx.** Seam A
  builds no tx. Seam B must route the `TrustSet` through `_create_xumm_payload`
  (which stamps both) — verify with an assertion, never hand-roll the POST again.
- **Pre-push gate** (ruff, ruff-format, mypy, gitleaks, pytest,
  validate-trait-config) must pass; **never** `--no-verify`. Ensure the worktree
  `.venv` symlink exists so the gate actually runs.
- **Any `app.js` change bumps the `?v=` cache-buster** on the `app.js`
  `<script>` in `webapp/client/index.html` in the **same commit**; bump any
  touched ES-module import's `?v=` in lockstep.
- No `xumm://` scheme, no LFG-hosted redirect shim. Auto-open uses the exact
  `xumm_url` deep link already in hand via `openExternal(link)`.
- No-custody: presentation/delivery only; no txjson/signing/ledger changes in
  Seam A.

---

### Task 1: Client mobile predicate + sign-delivery helper (Seam A core)

**Files:**
- Modify: `webapp/client/app.js` (add `isCoarsePointer`, `applySignDelivery`,
  `maybeAutoOpen`; wire `showFlow`)
- Modify: `webapp/client/index.html` (add "Show QR" disclosure around `flow-qr`;
  bump `app.js` `?v=`)
- Test: manual/logic (no JS unit harness in repo — assert via the smoke doc +
  code review of the primacy table). If a JS test harness is added, add
  `applySignDelivery`/`maybeAutoOpen` unit tests.

**Interfaces:**
- Produces: `isCoarsePointer(): boolean`,
  `applySignDelivery(els, { link, qrData, push })`, `maybeAutoOpen(link)`.
- Consumes: existing `openExternal(url)` (`app.js:217`), `qrUrl(data)`, the
  `flow-qr` / `flow-link-btn` DOM elements.

- [ ] **Step 1: Write the failing test(s)** — Codify the primacy table as an
      assertion. Preferred: add a tiny pure module `applySignDelivery` split so it
      is testable in isolation, or (if staying inline) write a Python-side
      contract note + a smoke checklist. Minimum bar: a documented truth table in
      the PR that a reviewer can check:
      `(push='sent', any pointer) → QR collapsed`;
      `(push!=sent, coarse) → deep-link primary + QR collapsed + auto-open once`;
      `(push!=sent, fine) → QR primary (unchanged)`;
      `maybeAutoOpen(null) → no open`; `maybeAutoOpen(x) twice → one open`.
- [ ] **Step 2: Run to verify they fail** — Confirm current `showFlow` always
      shows `flow-qr` and never auto-opens (grep: no `matchMedia`, no auto-open);
      the truth table's mobile rows are all currently false.
- [ ] **Step 3: Implement** — Add `isCoarsePointer` (cached
      `matchMedia('(pointer: coarse)')`), `maybeAutoOpen` (dedup `Set` keyed on the
      raw `link`; `openExternal(link)` once), and `applySignDelivery(els, …)`
      implementing the truth table. In `showFlow` (`app.js:633`), replace the
      direct `flow-qr`/`flow-link-btn` visibility lines (644-647) with a call to
      `applySignDelivery`. Add the `<details class="qr-fallback">` (or
      hidden-toggle) around `flow-qr` in `index.html`. Auto-open policy per spec
      decision #1 (default: on for payment/accept screens; expose a param so
      passive screens pass it `false`).
- [ ] **Step 4: Run to verify they pass** — `WEBAPP_DEV_MODE=1` mock harness:
      force coarse-pointer (emulate touch) → deep-link button primary, QR behind
      disclosure, one auto-open; fine-pointer → QR primary unchanged.
- [ ] **Step 5: Wider suite / regression run** — `python -m pytest webapp/test_smoke.py`
      (server contract unaffected) + full `pytest`; confirm cache-buster bumped.
- [ ] **Step 6: Commit** — `feat(activity): mobile-primary Xaman deep link with QR fallback (#142)`

---

### Task 2: Uniform sign delivery across market/economy panels (Seam A cont.)

**Files:**
- Modify: `webapp/client/app.js` (market list/buy/cancel ~3338-3394; extract
  ~3394; closet/assemble/trait-sell ~1006-1121, 2108-2130, 2329, 2588)
- Modify: `webapp/client/index.html` (parallel "Show QR" wrappers; bump `?v=`)

**Interfaces:** Consumes `applySignDelivery`/`maybeAutoOpen` from Task 1.

- [ ] **Step 1: Write the failing test(s)** — Extend the Task 1 truth-table
      checklist to each panel: assert (via mock harness / review) that market and
      economy sign panels currently show QR-primary with no auto-open on mobile.
- [ ] **Step 2: Run to verify they fail** — In the mock harness, drive a market
      buy / extract panel on emulated touch; observe QR-primary, no auto-open.
- [ ] **Step 3: Implement** — Refactor each inline panel builder to route its
      `open` button + QR `<img>` through `applySignDelivery` on the same elements.
      Keep the panel-specific copy (`signText(...)`) intact.
- [ ] **Step 4: Run to verify they pass** — Mock harness: each panel now
      deep-link-primary on touch, QR behind disclosure, auto-open honored per the
      screen's policy.
- [ ] **Step 5: Wider suite / regression run** — full `pytest` + `webapp/test_smoke.py`;
      re-confirm cache-buster bump.
- [ ] **Step 6: Commit** — `feat(activity): uniform mobile deep-link delivery for market/economy sign panels (#142)`

---

### Task 3: Fold Discord trustline onto `_create_xumm_payload` (Seam B)

**Files:**
- Modify: `surfaces/discord_bot/trustline.py` (replace hand-rolled POST ~52-75)
- Modify: `surfaces/discord_bot/render.py` if the trustline embed needs the
  `push`/deep-link wiring (render already exposes an "Open in Xaman" link)
- Test: `tests/test_trustline_payload.py` (new; env-guard preamble at module top)

**Interfaces:**
- Consumes: `lfg_core.xumm_ops._create_xumm_payload(txjson, options, user_token,
  memos_json)`, `lfg_core.memos.build_memos_json(action="trustset",
  platform="discord-bot", …)`.

- [ ] **Step 1: Write the failing test(s)** — In `tests/test_trustline_payload.py`
      (env-guard preamble verbatim: `os.environ.setdefault("BUNNY_PULL_ZONE", …)`
      / `os.environ.setdefault("LAYER_SOURCE", "local")` before importing
      `lfg_core`), stub the XUMM POST and assert the trustline flow (a)
      calls/produces a `TrustSet` txjson whose `SourceTag == config.SOURCE_TAG`
      and (b) carries a `Memos` array with an `action=trustset` provenance memo,
      and (c) surfaces the `xumm_url` deep link.
- [ ] **Step 2: Run to verify they fail** — `python -m pytest
      tests/test_trustline_payload.py` fails: the current hand-rolled POST sets no
      SourceTag/memos and returns qr_png/next.always directly.
- [ ] **Step 3: Implement** — Replace the direct `requests.post` +
      `response_data["refs"]["qr_png"]`/`["next"]["always"]` (trustline.py:72-73)
      with `await _create_xumm_payload(trustset_txjson, options,
      user_token=<resolved-or-None>, memos_json=build_memos_json(...))`. Keep the
      `TrustSet` limit/currency shape. Surface the returned `xumm_url` + `push` in
      the embed (reuse `render.py`'s "Open in Xaman" link path).
- [ ] **Step 4: Run to verify they pass** — the new test is green; SourceTag +
      memos present.
- [ ] **Step 5: Wider suite / regression run** — full `pytest`; confirm the
      existing SourceTag-invariant tests still pass and no trustline consumer
      broke (Discord trustline button path).
- [ ] **Step 6: Commit** — `refactor(discord): route trustline through _create_xumm_payload for SourceTag/memos/push + deep link (#142)`

---

### Task 4: Document the CLI deferral + acceptance-criteria closeout (Seam C)

**Files:**
- Modify: this plan / the issue comment (no code) — record that
  `scripts/economy_extract.py` / `economy_deposit.py` / `economy_assemble.py`
  stay terminal-only by design (no identity/push context, per CLAUDE.md).

- [ ] **Step 1** — Add a short note to the PR description and prepare the issue
      comment mapping each #142 acceptance box to its landing seam (A: mobile
      deep-link primary + QR fallback; A: auto-open on mobile; A: QR retained as
      desktop→phone fallback; B: trustline covered; C: CLI explicitly deferred).
- [ ] **Step 2** — No test; verification is the acceptance-criteria mapping.
- [ ] **Step 3–6** — Folded into the Final Task PR (no separate commit needed
      unless docs are touched).

---

### Final Task: Full gate + PR

- [ ] Run the **full** pre-push gate: `python -m pytest`, `ruff check`,
      `ruff format --check`, `mypy` — all green; **never** `--no-verify`.
- [ ] Confirm the `app.js` `?v=` cache-buster (and any touched ES-module import
      `?v=`) was bumped in the same commits as the client changes.
- [ ] Push the branch and `gh pr create` **non-draft** (CodeRabbit is paid;
      LFG requires review before merge). **No AI attribution** in the PR body or
      commits.
- [ ] Wait for **Greptile** and **CodeRabbit**. Greptile's clean verdict lives
      only in the `Greptile Review` check-run summary (no comment on a pass) —
      check the run before concluding it skipped.
- [ ] Resolve every actionable bot finding: fix in code **and** reply on its
      thread naming the fixing commit (or why declined) before merging.
- [ ] After merge to `main` (staging auto-deploys), run the human mobile smoke
      (iPhone/Android with Xaman) from the spec's Testing section; promote to prod
      with `scripts/promote.sh` once verified.
- [ ] Comment on #142 with spec + plan permalinks (blob URLs at the merged SHA)
      and the acceptance-criteria mapping; close (or split Seam B to #27 F3 if the
      maintainer chose the scope trim in spec open-question #5).
