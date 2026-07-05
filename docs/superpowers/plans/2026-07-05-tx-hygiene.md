# XRPL Transaction Hygiene — Implementation Plan

**Spec:** docs/superpowers/specs/2026-07-05-tx-hygiene-design.md
**Issues:** #61 #75 #57 #54 #58 · **Date:** 2026-07-05

TDD throughout: each task writes the failing test first, then the minimal
implementation, then runs the full suite. New test files that import
`lfg_core` at module top copy the env-guard preamble **verbatim** from
`tests/test_seasons.py:1-18`. All PRs open as **draft**; flip ready for
CodeRabbit only when the branch is settled.

Note: #75's original fix (inline mint in main.py) is **obsolete** — main.py is
a shim; do NOT resurrect it. The live equivalent leak is Task 1.

---

## PR-1 — SourceTag closure (#61, closes #75 as obsolete) — SHIP FIRST

The detect-link gap (Task 1) is losing mainnet hackathon credit today.

### Task 1: tag `generate_static_payment_link` (the live leak)
- [ ] Test (extend `tests/test_xumm_source_tag.py`): call
      `generate_static_payment_link("r...")`, split the `/detect/` hex tail,
      `json.loads(bytes.fromhex(tail))`, assert `SourceTag == 2606160021`.
      Run → red.
- [ ] Fix: add `"SourceTag": config.SOURCE_TAG` to `transaction_json` in
      `lfg_core/xumm_ops.py:47`. Run → green.

### Task 2: tag `scripts/testnet_amm_setup.py`
- [ ] Test (in new `tests/test_tx_hygiene.py`, env-guard preamble): AST-parse
      the script; assert the `AccountSet(...)` and `AMMCreate(...)` calls carry
      a `source_tag` keyword. Red.
- [ ] Fix: add `source_tag=config.SOURCE_TAG` at :100 and :131. Green.

### Task 3: AST sweep — regression lock for all builders
- [ ] In `tests/test_tx_hygiene.py`: walk `lfg_core/xrpl_ops.py`,
      `scripts/testnet_amm_setup.py`, `surfaces/discord_bot/admin.py` ASTs;
      for every `Call` to `{NFTokenMint, NFTokenCreateOffer, NFTokenBurn,
      NFTokenModify, Payment, AMMCreate, AccountSet, TrustSet}` assert a
      `source_tag` kwarg or `**kwargs` fed by a dict literal containing
      `"source_tag"`. Should be green immediately after Tasks 1-2; commit as
      the standing guard.

### Task 4: verify + ship
- [ ] Full suite; testnet smoke: trigger one mint-fee payment via the deep
      link, **sign it in Xaman**, and confirm `SourceTag` on the validated tx
      (clio `tx`). REQUIRED gate — the assumption that Xaman's detect flow
      preserves `SourceTag` is unverified (spec §2.1). **If Xaman strips
      it**: switch mint_flow/swap_flow fee payments from the static detect
      link to `_create_xumm_payload` payloads (server-side stamp) and
      re-verify before closing #61.
- [ ] Draft PR → ready → CodeRabbit → merge. Comment-close **#75**
      (obsolete; point at spec §0/§7) and tick #61's checklist; close **#61**
      after on-chain verification. Close **#57** as superseded
      (source_tag half here, memo half in PR-3) — note this explicitly.

## PR-2 — `submit_checked` choke point + pre-submit checks (#58)

### Task 5: skeleton + SourceTag invariant gate
- [ ] New `tests/test_submit_checked.py` (env-guard): tx without
      `source_tag` → `TxHygieneError`, client stub asserts zero network
      calls. Red → implement `submit_checked` + exception types in
      `lfg_core/xrpl_ops.py` (initially: gate + plain `submit_and_wait`
      pass-through). Green.

### Task 6: reserve check (integer drops)
- [ ] Tests: stub `ServerState`/`AccountInfo` responses; cases: ample
      balance passes; balance below `reserve_base + (owner+Δ)*reserve_inc +
      fee` → `ReserveError` with no submit; owner-delta table per tx type;
      **xrp_outflow extraction**: XRP-drops `Amount` counted; a
      buy_and_burn-shaped Payment (IOU `Amount` + XRP-drops `SendMax`) →
      reserve check uses `SendMax`, not 0; IOU Amount with no SendMax → 0;
      all math `int` (assert no float creeps in via a crafted response).
- [ ] Implement per spec §4.1 step 3. Pre-flight *network* failure →
      warn + proceed (test that too).

### Task 7: simulation
- [ ] Tests (mock `xrpl.asyncio.transaction.simulate`): `tes*` → submits;
      `tem*`/`tef*`/`tec*` → `SimulationError`, no submit, no retry sleep —
      **including `tecPATH_DRY`** (deterministic, no carve-out; spec §4.1
      step 4); simulate transport error → warn + submit anyway;
      `PRESUBMIT_SIMULATE=0` skips.
- [ ] Implement per spec §4.1 step 4; add `PRESUBMIT_SIMULATE` to
      `lfg_core/config.py` + CLAUDE.md env list.
- [ ] **Endpoint verification (REQUIRED, assumption in spec §4.1):** run one
      live `simulate` call against BOTH `config.JSON_RPC_URL` endpoints
      (testnet + mainnet). If either lacks the method, flip the
      `PRESUBMIT_SIMULATE` default to `0` and note it in config.py.

### Task 8: classified retry (spec §4.1 step 5 table — one test per class)
- [ ] Tests, one representative code per class:
      - RETRY: connection error (transport); `terQUEUED` (`ter*`);
        `telINSUF_FEE_P`; `tefMAX_LEDGER` → re-autofill then retry.
      - NO RETRY, raise on attempt 1: `temMALFORMED` (`tem*`);
        `tefNO_PERMISSION` (other `tef*`); `tecPATH_DRY` and
        `tecUNFUNDED_PAYMENT` (**all `tec*` — fee already burned; flow-level
        journaling handles compensation, not the submit layer**).
      - Backoff shape: `RETRY_BASE_DELAY * 2**n` up to `RETRY_MAX_ATTEMPTS`.
- [ ] Implement; wire `config.RETRY_MAX_ATTEMPTS`/`RETRY_BASE_DELAY`
      (kill the hardcoded `retries = 5`).

### Task 9: migrate all submit sites
- [ ] Mechanically migrate the 5 `xrpl_ops` sites (:73, :123, :331, :363,
      :415), `surfaces/discord_bot/admin.py:52`, and
      `scripts/testnet_amm_setup.py:99,130` onto `submit_checked`; delete the
      duplicated retry loops; keep each op's `return None` failure envelope
      so swap/economy journaling semantics are unchanged.
- [ ] Existing suites must stay green untouched (test_xrpl_source_tag,
      economy/swap flow tests, test_discord_sourcetag_invariant).
- [ ] Testnet smoke: one mint + one deliberately underfunded tx (expect
      instant `ReserveError`, zero retries). Draft PR → CodeRabbit → merge.
      Close **#58**.

## PR-3 — provenance memos (#54, absorbs #57's memo half)

### Task 10: `lfg_core/tx_memo.py`
- [ ] New `tests/test_tx_memo.py` (env-guard): enum validation (free string
      → raises); hex round-trip of MemoType `lfg/prov`, MemoFormat
      `application/json`, compact-JSON MemoData; encoded size ≤ 256 bytes;
      `parse_provenance` returns `None` on absent/foreign/malformed/oversized
      memos and never raises; no PII fields possible (schema has no id slot).
- [ ] Implement `Flow`/`Surface`/`Actor` constants, `build_memo`,
      `build_memo_json`, `parse_provenance` per spec §3.1.

### Task 11: stamp bot-signed txs
- [ ] Tests: `submit_checked(..., memo_ctx=...)` attaches exactly one memo
      with the right decoded JSON; omitted `memo_ctx` → no memo (backward
      compatible).
- [ ] Add `memo_ctx` params through `xrpl_ops` builders; flows pass
      `flow` + `surface` (default `surface="cli"` so economy scripts run
      unchanged); `actor="bot"`.

### Task 12: stamp user-signed payloads
- [ ] Tests: `create_payment_payload`/`create_accept_offer_payload`/
      trustline txjson carry `Memos` with `actor:"user"`; SignIn has none;
      detect link JSON includes the memo.
- [ ] Implement in `_create_xumm_payload` + `generate_static_payment_link` +
      `surfaces/discord_bot/trustline.py`.

### Task 13: docs + verification + close-out
- [ ] Document the schema (README section or CLAUDE.md pointer, per #54
      acceptance). Testnet: mint + swap + trustline; confirm memos decode on
      validated txs.
- [ ] Draft PR → CodeRabbit → merge. Close **#54**; confirm **#57** closed
      (superseded). File a small follow-up issue for history-derivation
      consumption (`parse_provenance` → `history_events.py` per spec §3.3).
