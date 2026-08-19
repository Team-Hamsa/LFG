# XLS-56 Batch Accept Offers — implementation plan

**Date:** 2026-07-24
**Status:** live (blocked) — re-reviewed 2026-08-19 against main@27dc301;
blocked on the XLS-56 amendment (`Batch` unsupported since rippled 3.1.1,
replaced by `BatchV1_1`, still in mainnet voting — see the 2026-08-18
dependency-verification comment on issue #219)
**Issue:** #219
**Last review:** 2026-08-19

> **This is not a ready-to-execute task list.** Do not start at Task 1. The
> amendment this feature rides on cannot be enabled on any production network
> today, so the work would ship dead and against a withdrawn wire format.
> Section 2 is the gate: every precondition there must pass before any task in
> section 4 is opened. Design detail lives in
> `docs/superpowers/specs/2026-07-24-xls56-batch-accept-offers-design.md`.

**Goal (unchanged):** accept multiple pending free gift offers under **one**
Xaman signature via an XLS-56 Batch, chunked at the 8-inner cap, behind a
feasibility gate defaulted OFF.

## 0. What changed since this was drafted

- **The blocker is now negative, not unknown.** Per the 2026-08-18 comment on
  #219: `Batch` was marked unsupported in rippled 3.1.1 after a
  signer-validation bug and replaced by `BatchV1_1`, which is in mainnet
  voting. Amendment status is not checkable from this tree — that comment,
  with its date, is the source.
- **xrpl-py still models the OLD `Batch`.** `requirements.txt` pins `xrpl-py`
  unversioned (line 8, bare name); verified against the host's user-site 4.5.0,
  NOT the deployed `.venv`, which that comment reports at 5.0.0. Both predate
  `BatchV1_1`. An unpinned requirement also means an unrelated `pip install`
  can move the model under us — check the installed version at build time, not
  from this doc.
- **Inner wire rules are now known for the old amendment** (see spec §0/§3):
  `Fee: "0"`, `SigningPubKey: ""`, no `TxnSignature`/`Signers`/
  `LastLedgerSequence`, and an inner `Sequence` chained off the outer's
  (`outer.Sequence + 1, +2, …` — xrpl-py derives it, but only from an outer
  `Sequence` that is already set, and XUMM sets that at sign time). That last
  one is a new precondition (2.4) — it may make a XUMM-built Batch unworkable
  regardless of amendment status.
- **Task 4's premise is stale**: a JS unit harness exists
  (`tests/test_*_pure_js.py` under Node), so selection logic belongs in a pure
  module with real tests, not "manual smoke only".
- **Test preambles are obsolete (#323)**: the root `conftest.py` pins the suite
  environment; the env-guard preamble in the task code samples below has been
  removed.
- **File-name drift**: the stylesheet is `webapp/client/style.v24.css` (was
  `style.v22.css` in the draft) and `index.html` currently loads `app.js?v=59`.
- **New offer producers** feed the same tray, so the payoff grew: burn-to-mint
  (#397) delivers N gift offers per run like bulk mint; sponsored free mint
  (#328) adds one per wallet (`free_mint_claims` is `UNIQUE (network, wallet)`).
  **Push/deep-link delivery** (#135/#212, #142/#380) shrank the per-accept
  friction, so the urgency fell.

## 1. Already landed — cut from this plan

Nothing from this plan's own tasks has shipped: no `BATCH_ACCEPT_*` symbol
exists in `lfg_core/` or `lfg_service/`, and `gh pr list --search "XLS-56"`
returns only unrelated batch-named PRs (#225, #300, #379, #397). What has
landed is context the tasks must now build on:

- Pending-offers tray (#218, PR #300) — `handle_pending_offers` /
  `handle_pending_offer_accept`, `webapp/client/app.js::openOffers`.
- Per-kind tray rows (#327) — `_pending_offer_row` resolves characters on
  `XRPL_NETWORK`, trait tokens on `ECONOMY_NETWORK`.
- Sign-delivery module (#142/#380) — pure `webapp/client/signdelivery_pure.js`
  (`signDelivery`, `shouldAutoOpen`, `autoOpenOutcome`) consumed by
  `webapp/client/app.js::applySignDelivery`; new QR blocks must go through it.
- Multi-select precedent (#356, PR #379) — `webapp/client/harvest_pure.js` +
  `BATCH_HARVEST_MAX` in `lfg_service/app.py`; copy the shape.
- Node JS test harness — `tests/test_harvest_pure_js.py` et al.
- Central test env (#323) — root `conftest.py`.

## 2. Preconditions to re-check before starting

All five must be satisfied, in order. Stop at the first failure.

- [ ] **2.1 `BatchV1_1` is enabled on mainnet and testnet.** Check the XRPL
  Known Amendments page or an amendment dashboard (the 2026-08-18 comment on
  #219 links both), then confirm against a node. `feature` is understood to be
  admin-only on public endpoints, so expect to need a node you control or to
  read the ledger's Amendments object — that restriction is not verified from
  this tree, and neither is amendment status. Until enabled, a Batch
  submission returns `temDISABLED` and nothing below matters.
- [ ] **2.2 The installed xrpl-py models `BatchV1_1`.** `pip show xrpl-py` in
  the deployed `.venv`, then confirm the model's flags/fields against the
  final `BatchV1_1` spec. Today's `Batch` model (verified at 4.5.0) is for the
  withdrawn amendment. If the library lags, either pin a newer release in
  `requirements.txt` or hand-build the txjson and own the drift.
- [ ] **2.3 Xaman signs a single-account Batch payload end-to-end on testnet.**
  Post a `TransactionType: "Batch"` txjson with two inner
  `NFTokenAcceptOffer`s to the XUMM payload API, sign it in the app, confirm
  the tx validates. No Xaman documentation asserts support (2026-08-18 check),
  so this is an empirical test, not a doc lookup. **This is the gate for
  flipping `BATCH_ACCEPT_ENABLED` anywhere.**
- [ ] **2.4 Inner `Sequence`/`Fee` handling is settled.** From
  `xrpl.asyncio.transaction.main._autofill_batch`, inner txns end up with
  `Fee: "0"`, `SigningPubKey: ""` and a `Sequence` chained off the outer
  Batch's own sequence; the helper can only derive that chain because the
  outer `Sequence` is already set when it runs, and in a XUMM payload the
  outer `Sequence` is not set until sign time. Determine whether Xaman
  autofills the inner ones. If it does not, the backend must pin the outer
  `Sequence` and every inner one from a freshly read account sequence — a
  TOCTOU that any other wallet activity breaks (`tefPAST_SEQ`). If that is the
  only option, re-decide whether the feature is worth shipping at all before
  writing code.
- [ ] **2.5 Volume-attribution decision made.** `_create_xumm_payload` stamps
  `SourceTag`/`Memos` on the **outer** Batch only. Decide whether inner
  transactions must carry the hackathon tag (and stamp them in the builder if
  so) or outer-only credit is acceptable.

Only when 2.1–2.5 pass: re-read the spec, re-run the fact-check in its §0
(module paths and helper names rot), then open Task 1.

## 3. Global constraints (still current)

- **SourceTag 2606160021 + provenance memos** on every transaction; go through
  `xumm_ops._create_xumm_payload`, never around it. Memo:
  `initiator=user`, `platform=memos.platform_for_surface(...)`,
  `action=memos.ACTION_ACCEPT_OFFER`.
- **Signer pinning**: outer `Account` and every inner `Account` = the caller's
  wallet.
- **Pre-push gate** (ruff, ruff-format, mypy, gitleaks, pytest,
  validate-trait-config, check-repo-layout, audit-layer-dimensions) must pass;
  never `--no-verify`. The old "worktrees silently skip mypy/pytest" trap is
  fixed (#315): the local hooks run through `scripts/venv-python`, which
  resolves the main checkout's shared `.venv` from any worktree and **exits 1
  loudly** if it is missing.
- **Cache-buster**: any `app.js`/`index.html` change bumps `?v=` in the same
  commit; a new ES module import carries its own `?v=` pin.
- **The gate ships OFF.** No task here flips `BATCH_ACCEPT_ENABLED`; enabling
  is an ops step gated on §2.

## 4. The build, once unblocked

Only after every box in §2 is ticked. Then the usual worker convention applies
(superpowers:subagent-driven-development or superpowers:executing-plans,
task-by-task). TDD throughout: failing test first, then implementation. New
test files need no env-guard preamble (#323).

### Task 1: Config feasibility gate

**Files:** modify `lfg_core/config.py`; new `tests/test_batch_accept_config.py`.

**Produces:** `config.BATCH_ACCEPT_ENABLED_DEFAULT: str` (`"0"`),
`config.BATCH_ACCEPT_ENABLED: bool`, `config.BATCH_ACCEPT_MAX_INNER: int` (8).

- [ ] **Step 1: failing test.** Assert the shipped default via
  `config.env_flag("BATCH_ACCEPT_ENABLED", config.BATCH_ACCEPT_ENABLED_DEFAULT)
  is False` and `config.BATCH_ACCEPT_MAX_INNER == 8`. Never assert the frozen
  `config.BATCH_ACCEPT_ENABLED` constant directly (#323 rule).
- [ ] **Step 2: verify it fails** — `.venv/bin/pytest tests/test_batch_accept_config.py -q`
  (expect `AttributeError` on `BATCH_ACCEPT_ENABLED_DEFAULT`).
- [ ] **Step 3: implement** beside `BULK_MINT_UI_ENABLED` /
  `BURN_TO_MINT_ENABLED` in `lfg_core/config.py`, same `*_DEFAULT` convention
  (spec §4.0).
- [ ] **Step 4: verify it passes.**
- [ ] **Step 5: regression** — `.venv/bin/pytest tests/test_config*.py -q`.
- [ ] **Step 6: commit** — `feat(config): XLS-56 batch-accept feasibility gate (#219)`.

### Task 2: Payload builder + chunking helper

**Files:** modify `lfg_core/xumm_ops.py`; new `tests/test_batch_accept_payload.py`.

**Produces:** `xumm_ops.create_batch_accept_payload(account, offer_ids, *,
return_url=None, user_token=None, platform=memos.PLATFORM_BACKEND,
campaign=None)` and pure `xumm_ops.chunk_offer_ids(offer_ids, size)`.
**Consumes:** `_create_xumm_payload`, `_with_return_url`,
`memos.build_memos_json`, `memos.ACTION_ACCEPT_OFFER`, `memos.INITIATOR_USER`.

- [ ] **Step 1: failing test.** Monkeypatch `xumm_ops._post_xumm_payload` to
  capture `payload["txjson"]` and return
  `{"qr_url","xumm_url","uuid","pushed"}`. Assert: outer
  `TransactionType == "Batch"`, `Account` pinned, `len(RawTransactions) == 3`
  for 3 ids; each `RawTransaction` is an `NFTokenAcceptOffer` with the
  caller's `Account`, the requested `NFTokenSellOffer`,
  `Flags & 0x40000000`, `Fee == "0"`, `SigningPubKey == ""`; outer
  `SourceTag == config.SOURCE_TAG` and a non-empty `Memos`. Plus chunking:
  `chunk_offer_ids(list("abcdefghij"), 8) == [list("abcdefgh"), list("ij")]`.
- [ ] **Step 2: verify it fails.**
- [ ] **Step 3: implement** per spec §4.1 — preferring construction from the
  xrpl-py `Batch` model's `to_xrpl()` if 2.2 confirms the model is current, so
  wire drift arrives as a library upgrade. Inner `Sequence` per the 2.4
  decision.
- [ ] **Step 4: verify it passes.**
- [ ] **Step 5: regression** — `.venv/bin/pytest tests/ -k "xumm or offer" -q`.
- [ ] **Step 6: commit** — `feat(xumm): XLS-56 batch NFTokenAcceptOffer payload builder (#219)`.

### Task 3: Service endpoint + pending-offers capability flag

**Files:** modify `lfg_service/app.py`; extend `webapp/test_smoke.py` and/or
new `tests/test_batch_accept_endpoint.py`.

**Produces:** `POST /api/offers/accept-batch` →
`{"batches": [{"qr","link","push","count"}, …]}` or
`{"single": true, "offer_index": …}`; error codes `batch_disabled` (409),
`pending_unavailable` (503), `offer_gone` (410). `GET /api/offers/pending`
gains `"batch": bool`.

- [ ] **Step 1: failing tests.** (a) add `"/api/offers/accept-batch"` to
  `webapp/test_smoke.py::test_routes_registered`'s expected-path list — it
  holds canonical path strings only, no methods, and today carries no
  `/api/offers/*` entry at all;
  (b) `/api/offers/pending` includes `"batch"`; (c) gate patched OFF → 409
  `batch_disabled` with **no** XUMM call; (d) gate ON with
  `get_account_nft_offers`/`filter_claimable_offers` faked to 3 claimable →
  one `create_batch_accept_payload` call, `batches` length 1, `count == 3`;
  (e) 1 survivor → `{"single": true}`; (f) 0 survivors → 410 `offer_gone`.
- [ ] **Step 2: verify they fail.**
- [ ] **Step 3: implement** `handle_pending_offers_accept_batch` per spec §4.2
  (gate → dev-mode 501 → validate → on-ledger re-verify preserving request
  order → single/410 → chunk → payload per chunk), add `"batch"` to
  `handle_pending_offers`, register the route beside the existing
  `/api/offers/*` pair.
- [ ] **Step 4: verify they pass.**
- [ ] **Step 5: regression** — `.venv/bin/pytest webapp/test_smoke.py tests/test_pending_offers.py -q`.
- [ ] **Step 6: commit** — `feat(service): /api/offers/accept-batch behind feasibility gate (#219)`.

### Task 4: Tray multi-select UI (pure module + wiring)

**Files:** new `webapp/client/offers_pure.js`, new
`tests/test_offers_pure_js.py`; modify `webapp/client/app.js`,
`webapp/client/index.html` (cache-buster + new module `?v=` pin),
`webapp/client/style.v24.css`.

- [ ] **Step 1: failing test.** `tests/test_offers_pure_js.py`, copying the
  Node harness in `tests/test_harvest_pure_js.py`, over pure functions in
  `offers_pure.js` — `toggleSelected(selected, offerIndex)` (never mutates),
  `selectionSummary(offers, selected)` (count + chunk count at the server's
  cap), `chunkLabel(n)`. Keep DOM out of the module.
- [ ] **Step 2: verify it fails** (module missing).
- [ ] **Step 3: implement** the module, then wire `openOffers()`/`offerRow` per
  spec §4.3: checkbox per row and a sticky "Accept selected (1 signature)"
  button when the server advertises `batch` and there are ≥2 offers; POST the
  checked `offer_index` list; render one `.u-accept` block per chunk through
  `applySignDelivery`; `{single: true}` routes to `offerAccept`. Bump
  `app.js?v=` in `index.html` and pin the new import in the same commit.
- [ ] **Step 4: verify it passes.**
- [ ] **Step 5: regression** — `.venv/bin/pytest webapp/test_smoke.py
  tests/test_pending_offers.py tests/test_offers_pure_js.py -q`. Note
  `test_pending_offers.py::test_index_has_offers_panel_and_entry_button` and
  `::test_app_js_wires_offers_tray` are source-assertion guards that grep
  `index.html`/`app.js` for literal ids and call sites; extend them for the
  new control rather than working around them.
- [ ] **Step 6: commit** — `feat(client): multi-select batch accept in the pending-offers tray (#219)`.

### Final task: gate + PR

- [ ] Full suite `.venv/bin/pytest -q`; `.venv/bin/ruff check . &&
  .venv/bin/ruff format --check . && .venv/bin/mypy .` — the gate's mypy hook
  is `scripts/venv-python -m mypy .`, i.e. the whole tree, not a module list.
- [ ] Confirm `BATCH_ACCEPT_ENABLED` is still OFF by default (Task 1 test) —
  the branch ships dark.
- [ ] Add `BATCH_ACCEPT_ENABLED` and `BATCH_ACCEPT_MAX_INNER` to the CLAUDE.md
  `.env` block, marked optional/off by default (#219) with the amendment
  dependency named.
- [ ] `gh pr create` non-draft. The PR body must state the feature ships
  gate-off, name the amendment version it targets, and record the results of
  preconditions 2.1–2.5. No AI attribution in commits or PR body.
- [ ] Wait for Greptile + CodeRabbit; fix **and** reply on every actionable
  finding's thread before merge.
- [ ] Post the precondition results back to issue #219 so the enable step is
  tracked separately from the merge.
