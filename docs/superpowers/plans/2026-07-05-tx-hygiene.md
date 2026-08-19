# XRPL Transaction Hygiene — Implementation Plan (#58 only)

**Spec:** docs/superpowers/specs/2026-07-05-tx-hygiene-design.md
**Issues:** #58 (OPEN) · #61 #75 #57 #54 (CLOSED — see "already landed")
**Date:** 2026-07-05 · **Last review:** 2026-08-19
**Status:** live — re-reviewed 2026-08-19 against main@27dc301

One PR remains: pre-submit `simulate` in `lfg_core/xrpl_ops.py`. TDD as usual —
failing test first, then the minimal implementation, then the full suite.
New test files need no env-guard preamble since #323 (the root `conftest.py`
supplies every mandatory var).

---

## 0. What changed since this was drafted

- **PR-1 (SourceTag) and PR-3 (memos) are done and merged.** #61/#75/#57 closed
  2026-07-09/10; #54 closed by PR #144 as `lfg_core/memos.py` (not
  `lfg_core/tx_memo.py`, and with `initiator`/`platform`/`action` rather than
  `actor`/`surface`/`flow`).
- **PR-2 was never built, and half of it must not be.** The `submit_checked`
  consolidation it described was overtaken by PR #188 (#179, merged
  2026-07-12): `xrpl_ops._submit_and_confirm` is already the single
  sign-once/submit-once choke point, and the "classified retry" table
  (old Task 8) would reintroduce the blind resubmit that PR deleted. Task 8 and
  Task 9 are **cut**.
- **Old Task 6 (integer-drops reserve check) is cut.** rippled computes it
  during simulated apply — verified 2026-08-19 that an over-balance mainnet
  Payment simulates `tecUNFUNDED_PAYMENT`.
- **Old Task 7's two "REQUIRED verification" gates are already satisfied.**
  `simulate` exists in xrpl-py 4.5.0 and 5.0.0 (`requirements.txt` pins
  neither) and both `config.JSON_RPC_URL` defaults answer the method. The
  `PRESUBMIT_SIMULATE` default stays `1`.
- **New constraint:** `simulate` rejects a *signed* transaction
  (`transactionSigned`), so the pre-flight runs on the unsigned model before
  `_autofill_and_sign_with_retry` / `autofill_and_sign`.
- **New constraint:** the root `conftest.py` must pin `PRESUBMIT_SIMULATE=0`, or
  ten existing test modules start issuing real network requests — their stubs
  (`autofill_and_sign` / `submit_and_wait`, sometimes `JsonRpcClient.request`)
  do not intercept a simulate, which xrpl-py issues through
  `client._request_impl` (spec §3.7).
- **Old Task 1's cross-spec note about #27 is moot** — #27 closed 2026-07-10,
  and `generate_static_payment_link` stopped being a payment route: since #262
  a mint session with no XUMM payload fails terminally instead of waiting
  behind the detect link. It is still untagged (spec §1.3), tracked outside
  this plan.

### Already landed (do not redo)

| Old task | Disposition |
|---|---|
| Task 1 — tag `generate_static_payment_link` | **Not done and not in scope.** The link still carries no `SourceTag`, but since #262 it is no longer a payment route at all (spec §1.3). File separately if it matters. |
| Task 2 — tag `scripts/testnet_amm_setup.py` | **Not done and not in scope.** Testnet ops tool, no mainnet volume. |
| Task 3 — AST sweep regression test | **Superseded.** The per-builder tests (`tests/test_xrpl_source_tag.py`, `test_xumm_source_tag.py`, `test_discord_sourcetag_invariant.py`, `test_telegram_sourcetag_invariant.py`, `test_signing_account.py`, `test_discord_trustline_sourcetag.py`, `test_market_payloads.py`) are the shipped guard. |
| Task 4 — verify + ship #61 | Done; #61/#75/#57 closed 2026-07-09/10. |
| Tasks 5, 8, 9 — `submit_checked` skeleton, classified retry, migrate submit sites | **Cut.** `_submit_and_confirm` already is the choke point, and retry is a deliberate non-goal (spec §3.5). |
| Task 6 — integer-drops reserve check | **Cut** (spec §3.4). |
| Tasks 10–13 — memos | Done; PR #144, `lfg_core/memos.py`, plus `tests/test_memos.py` and `tests/test_memos_transactions.py`. |

---

## PR — pre-submit simulation (#58)

Single PR. Ships flag-on in code, flag-off in the test suite, and is measured
on staging before prod carries it.

### Task 1: suite isolation first (must land before anything calls the network)

- [ ] Add `os.environ.setdefault("PRESUBMIT_SIMULATE", "0")` to the root
      `conftest.py` alongside the other pins, with a comment naming the reason
      (the existing `_submit_and_confirm` / sponsored-prepare tests stub
      `autofill_and_sign` / `submit_and_wait`, which does not intercept a
      simulate, so they would otherwise hit the network).
- [ ] Full suite green — no behavior change yet, this is the guard rail.

### Task 2: `_presubmit_simulate` helper

- [ ] New `tests/test_presubmit_simulate.py`. Monkeypatch a module-level
      `xrpl_ops.simulate` name (import it as one so tests can patch it exactly
      like `submit_and_wait`). Cases, all with `PRESUBMIT_SIMULATE=1` forced
      via `monkeypatch.setenv`:
      - `engine_result="tesSUCCESS"` → returns `None` (proceed).
      - `engine_result="terQUEUED"` → returns `None` (proceed; `ter*` is
        "retry later", not "wrong").
      - `engine_result="telINSUF_FEE_P"` → returns `None` (proceed; `tel*` is
        a node-local verdict, spec §3.3 step 4).
      - `engine_result` in `{"temREDUNDANT", "tefNFTOKEN_IS_NOT_TRANSFERABLE",
        "tecPATH_DRY", "tecUNFUNDED_PAYMENT"}` → returns that code. (Use real
        codes in the fixtures — there is no `tefNO_PERMISSION`; the
        no-permission code is `tec*`.)
      - `simulate` raises `XRPLRequestFailureException` → returns `None`
        (degrade open) and logs a warning.
      - `simulate` raises a plain transport exception → returns `None`.
      - response missing `engine_result` entirely → returns `None`.
      - flag off (`PRESUBMIT_SIMULATE=0`) → `simulate` is never called.
      Run → red.
- [ ] Implement `_presubmit_simulate` in `lfg_core/xrpl_ops.py` per spec §3.3.
      Gate on `config.env_flag("PRESUBMIT_SIMULATE", "1")` read at call time,
      never a frozen constant. Green.

### Task 3: wire it into `_submit_and_confirm`

- [ ] Tests (extend `tests/test_presubmit_simulate.py`):
      - A rejected simulate makes `mint_nft` return `None` **and**
        `autofill_and_sign` / `submit_and_wait` are never called (stub both to
        raise `AssertionError`).
      - The tx passed to `simulate` is the **unsigned** model — assert it is
        the same object `mint_nft` built and that `tx.is_signed()` is False.
        (Do not assert `signing_pub_key is None`: an unsigned xrpl-py
        `Transaction` carries `signing_pub_key == ""`, in both 4.5.0 and
        5.0.0, and rippled accepts that as unsigned.)
      - A rejected simulate never raises `IndeterminateResultError` (nothing
        was signed, so the outcome is known).
      - Simulate is invoked **before** the submission lock: patch
        `_submission_scope` to record ordering, assert `simulate` ran first.
      - Flag off → `_submit_and_confirm` behaves exactly as today (this is
        what the ten existing modules already assert; they must stay green
        untouched).
- [ ] Implement: call `_presubmit_simulate` at the top of
      `_submit_and_confirm`, above `async with _submission_scope(...)`; on a
      returned code, log `f"{label}: pre-submit simulate rejected ({code})"`
      and return `None`.
- [ ] Full suite green, and specifically the seven modules that drive
      `_submit_and_confirm` through stubs: `tests/test_xrpl_source_tag.py`,
      `test_xrpl_indeterminate.py`, `test_xrpl_malformed_poll_retry.py`,
      `test_xrpl_submit_lock.py`, `test_memos_transactions.py`,
      `test_nft_flags.py`, `test_signing_account.py`.
      (`test_shop_offer_builder.py` stubs `_submit_and_confirm` itself, so it
      exercises none of this — do not read it as coverage.)

### Task 4: wire the sponsored prepare paths

- [ ] Tests: `prepare_sponsored_mint` and `prepare_sponsored_burn` with a
      rejecting simulate return their `"failed"` preparation state carrying the
      engine code in the reason, and `autofill_and_sign` is never called (so no
      signed blob is journaled). Flag off → unchanged. Existing green:
      `tests/test_sponsored_burn.py`, `test_sponsored_burn_review.py`,
      `test_sponsored_final_review.py` (they stub `autofill_and_sign`, which
      does not shield a simulate — see Task 1).
- [ ] Implement inside each `_submission_scope` block, immediately before
      `autofill_and_sign`.
- [ ] Decide and document the burn-worker consequence: a `"failed"` burn
      preparation makes `sponsored_burn.process_one` re-queue the obligation
      (`status="pending"` with `_backoff(attempt_count)`), so a *deterministic*
      simulate rejection retries on a timer instead of terminating. Either
      accept that (it matches today's preparation-failure behavior) or record
      the engine code so an operator can see why it will never clear.
- [ ] Confirm the sponsored recovery/reconcile paths are untouched:
      `submit_sponsored_mint` / `submit_sponsored_burn` forward an
      already-signed blob and must **not** simulate (it would be rejected
      `transactionSigned` and, worse, is meaningless for a fixed identity).

### Task 5: admin burn (optional, same helper)

- [ ] Test: `surfaces/discord_bot/admin.py::burn_nft` returns `False` on a
      rejecting simulate without calling `submit_and_wait`.
- [ ] Implement by calling `xrpl_ops._presubmit_simulate` (promote it to a
      public name if importing a private one is objectionable in review).

### Task 6: config + docs

- [ ] Add `PRESUBMIT_SIMULATE=1` to the CLAUDE.md env block with a one-line
      description ("pre-submit `simulate` pre-flight on backend-signed txs;
      `0` disables").
- [ ] Note in the `xrpl_ops` module docstring or the helper docstring that
      `simulate` must see the unsigned model, and that a rejection maps to the
      existing `None` "definitive failure" contract — not
      `IndeterminateResultError`.

### Task 7: live verification

Staging first (`~/LFG-staging`, testnet, `stg-*` pm2 stack).

- [ ] Underfunded-account probe: simulate an `NFTokenCreateOffer` (or any
      owner-object-creating tx) from a testnet account funded just above the
      base reserve, and record the engine result. The spec claims
      `tecINSUFFICIENT_RESERVE` reaches us through simulate; this is the step
      that confirms it. If it does not, say so in the PR — the design still
      stands on `tecUNFUNDED_PAYMENT`/`tem*`/`tef*`.
- [ ] Happy path: one testnet mint end to end with the flag on; confirm the
      extra round-trips and that nothing regressed.
- [ ] Deliberate rejection: a `buy_and_burn` against a currency with no AMM
      path; expect an instant `tecPATH_DRY` refusal, no fee, and the engine
      code in the pm2 log. Use a **non-self** issuer: on testnet
      `config.SWAP_OFFER_ISSUER` defaults to the SEED address and
      `buy_and_burn` short-circuits to `"self-issuer-noop"` without submitting
      — or simulating — anything when `SIGNING_ACCOUNT == issuer`.
- [ ] Measure added latency on the bulk-mint path — 2 simulate calls per unit,
      each currently 2 requests (`ServerInfo` + `simulate`, spec §3.6) — before
      recommending the flag on prod.

### Task 8: ship

- [ ] PR ready (non-draft), Greptile + CodeRabbit, close every finding on its
      own thread.
- [ ] Merge to `main` (auto-deploys staging). Promote with `scripts/promote.sh`
      once the staging measurements look right.
- [ ] Close **#58** with the staging evidence: the simulate probe table and the
      `tecPATH_DRY` refusal log line.
- [ ] Optional follow-up issue, not part of this PR: the two residual untagged
      sites in spec §1.3 (`generate_static_payment_link`,
      `scripts/testnet_amm_setup.py`).
