# Swap burn-remint BRIX-trustline precheck — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent burn-remint trait swaps from burning originals and then failing
the BRIX-priced replacement offer with `tecNO_LINE` when the NFT issuer holds no
BRIX trustline. Add a pre-burn precondition check that fails-closed (no
destructive step precedes it), gracefully falling back to the trustline-safe XRP
fee path, plus an ops audit assertion.

**Architecture:** Two independent seams.
1. `lfg_core/swap_flow.py::run_swap_session` — a precheck inserted right after
   `detect_swap_payment`, before `_collect_modify_fee`/mint/modify/burn. Gates on
   `burn_items and pay_with == "BRIX"`; on a missing issuer trustline it re-prices
   the session onto XRP (reusing `xrpl_ops.get_amm_xrp_cost`), or fails cleanly
   pre-burn if the AMM can't quote.
2. `scripts/audit_swap_preconditions.py` (new) — a CI/pre-deploy exit-code audit
   that asserts the NFT issuer holds the BRIX trustline while the BRIX fee path is
   live. Independent of Seam 1.

**Tech Stack:** Python 3 / aiohttp / asyncio / pytest. No client (server-side
`lfg_core` + ops script only); no `app.js` change.

## Global Constraints

- **SourceTag=2606160021 + provenance memos preserved on every tx.** This change
  adds NO new transaction — the precheck is a read-only `account_lines` lookup,
  and the XRP-fallback offer is the already-shipped native-drops offer that
  already carries SourceTag + memos via `xrpl_ops.create_nft_offer`. Do not build
  any new tx.
- **The burn is the point of no return (#211).** The precheck MUST run before
  `_collect_modify_fee`, `mint_nft`, `modify_nft`, and `burn_nft`. No destructive
  or fee-collecting step may precede it.
- **Pre-push gate must pass** (ruff `--fix`, ruff-format, mypy from `.venv`,
  gitleaks, pytest, validate-trait-config). Never `--no-verify`. Ensure the
  worktree `.venv` symlink exists or the gate silently skips.
- **Tests must carry the env-guard preamble** (`os.environ.setdefault`
  BUNNY_PULL_ZONE / LAYER_SOURCE / SEED / …) at module top — copy verbatim from
  `tests/test_swap_offer_recovery.py`, or frozen config constants strand
  `webapp/test_smoke` in full-suite order.
- No `app.js` / client change, so no cache-buster bump needed.

---

### Task 1: Pre-burn issuer-trustline precheck + XRP fallback in `run_swap_session`

**Files:**
- Modify: `lfg_core/swap_flow.py`
- Test: `tests/test_swap_trustline_precheck.py` (new)

**Interfaces:**
- Produces: `swap_flow._issuer_holds_offer_trustline() -> bool` (new helper);
  modified control flow in `run_swap_session` that may set `session.pay_with =
  "XRP"` / `session.fee_per_nft` / `session.state = FAILED` before any on-chain
  step.
- Consumes (all existing): `xrpl_ops.get_trustline_balance(address, currency,
  issuer) -> Decimal | None` (`xrpl_ops.py:638`); `xrpl_ops.get_amm_xrp_cost(
  currency, issuer, token_amount) -> Decimal | None` (`xrpl_ops.py:661`);
  `config.SWAP_ISSUER_ADDRESS`, `config.SWAP_OFFER_CURRENCY_HEX`,
  `config.SWAP_OFFER_ISSUER`, `config.SWAP_XRP_FEE_BUFFER`; `swap_fee_total`,
  `_offer_amount`.

- [ ] **Step 1: Write the failing test(s)** — `tests/test_swap_trustline_precheck.py`
  with the env-guard preamble copied from `tests/test_swap_offer_recovery.py`.
  Reuse that file's `_run(coro)` fresh-loop helper and a minimal fake NFT dict
  (mutable=False burn item, plus one mutable item for the mixed case). Patch the
  `xrpl_ops` calls with `monkeypatch`/`unittest.mock`. Cases:
  - `test_issuer_has_trustline_keeps_brix`: `get_trustline_balance` → `Decimal("5")`;
    stub the rest of the flow (mint/modify/burn/offer) with async mocks; assert
    `session.pay_with == "BRIX"` at the point the first offer is priced.
  - `test_missing_trustline_falls_back_to_xrp`: `get_trustline_balance` → `None`,
    `get_amm_xrp_cost` → `Decimal("0.5")`; run through the precheck; assert
    `session.pay_with == "XRP"` and `_offer_amount(session)` returns a drops
    string (native), and that `xrpl_ops.burn_nft` was NOT called before the flip.
  - `test_missing_trustline_and_no_amm_quote_fails_pre_burn`:
    `get_trustline_balance` → `None`, `get_amm_xrp_cost` → `None`; assert
    `session.state == FAILED`, an error message is set, and `mint_nft` / `burn_nft`
    mocks were never awaited.
  - `test_modify_only_session_skips_precheck`: `burn_items` empty (all mutable),
    `get_trustline_balance` → `None`; assert precheck does NOT flip `pay_with`
    (stays BRIX) and does not fail the session on trustline grounds.

  Concrete snippet shape:
  ```python
  def test_missing_trustline_falls_back_to_xrp(monkeypatch):
      monkeypatch.setattr(xrpl_ops, "get_trustline_balance",
                          _amock(return_value=None))
      monkeypatch.setattr(xrpl_ops, "get_amm_xrp_cost",
                          _amock(return_value=Decimal("0.5")))
      burn_called = _amock(return_value="BURNHASH")
      monkeypatch.setattr(xrpl_ops, "burn_nft", burn_called)
      # ... stub mint_nft/create_nft_offer/compose to short-circuit after offer pricing
      session = _make_burn_session()
      _run(swap_flow.run_swap_session(session))
      assert session.pay_with == "XRP"
  ```

- [ ] **Step 2: Run to verify they fail** —
  `.venv/bin/python -m pytest tests/test_swap_trustline_precheck.py -q`.
  Expected: fails because `run_swap_session` never flips `pay_with` / the
  `_issuer_holds_offer_trustline` helper does not exist (`AttributeError`).

- [ ] **Step 3: Implement** — in `lfg_core/swap_flow.py`:
  1. Add `async def _issuer_holds_offer_trustline() -> bool` returning
     `await xrpl_ops.get_trustline_balance(config.SWAP_ISSUER_ADDRESS,
     config.SWAP_OFFER_CURRENCY_HEX, config.SWAP_OFFER_ISSUER) is not None`.
  2. In `run_swap_session`, after `session.pay_with, total = await
     detect_swap_payment(...)` (currently ~L693) and after `burn_items` is
     computed (~L704), insert the gate: when `burn_items and session.pay_with ==
     "BRIX" and not await _issuer_holds_offer_trustline()`, log `logging.error(...)`
     (LOUD, ops-facing, names issuer + currency + issuer), then compute the XRP
     quote via `xrpl_ops.get_amm_xrp_cost(config.SWAP_OFFER_CURRENCY_HEX,
     config.SWAP_OFFER_ISSUER, Decimal(swap_fee_total(2)))`; if `None`, set
     `session.state = FAILED` + user message and `return`; else set
     `session.pay_with = "XRP"`, recompute `total` (× `SWAP_XRP_FEE_BUFFER`,
     quantize ROUND_UP 6dp) and `session.fee_per_nft`. Keep the existing
     `fee_per_nft` re-quantize line consistent (the precheck must recompute it
     when it flips the currency). Ensure the gate is BEFORE the `_collect_modify_fee`
     block, the mint loop, and the burn loop.

- [ ] **Step 4: Run to verify they pass** —
  `.venv/bin/python -m pytest tests/test_swap_trustline_precheck.py -q`. All green.

- [ ] **Step 5: Wider suite / regression run** —
  `.venv/bin/python -m pytest tests/test_swap_offer_recovery.py
  tests/test_swap_cancel_regenerate.py tests/test_brix_payment.py
  tests/test_sdk_mint_swap.py -q` then the full `tests/` suite to catch
  full-suite-order config stranding.

- [ ] **Step 6: Commit** — `fix(swap): precheck issuer BRIX trustline before burn, fall back to XRP (#166)`

---

### Task 2: Ops audit assertion for the issuer BRIX trustline

**Files:**
- Create: `scripts/audit_swap_preconditions.py`
- Test: `tests/test_audit_swap_preconditions.py` (new)

**Interfaces:**
- Produces: a CLI `--network testnet|mainnet` script; exit 0 = OK, exit 1 =
  issuer missing the trustline while the BRIX fee path is live, exit 2 = lookup
  failed/indeterminate. Mirrors `scripts/audit_trait_files.py`'s exit-code
  contract.
- Consumes: `xrpl_ops.get_trustline_balance`, `config.SWAP_ISSUER_ADDRESS`,
  `config.SWAP_OFFER_CURRENCY_HEX`, `config.SWAP_OFFER_ISSUER`.

- [ ] **Step 1: Write the failing test(s)** — `tests/test_audit_swap_preconditions.py`
  with the env-guard preamble. Import the script's `check()` coroutine (factor the
  logic into an importable async function so it's testable without a subprocess).
  Cases: trustline present (`Decimal`) → returns OK/0; trustline `None` →
  returns the failure code with a remediation message; same-issuer config
  (testnet, `SWAP_ISSUER_ADDRESS == SWAP_OFFER_ISSUER`) → trivially OK (no
  cross-account trustline needed — an account implicitly "holds" its own IOU).

- [ ] **Step 2: Run to verify they fail** —
  `.venv/bin/python -m pytest tests/test_audit_swap_preconditions.py -q`
  (ModuleNotFoundError / missing `check`).

- [ ] **Step 3: Implement** — `scripts/audit_swap_preconditions.py`: an
  `async def check() -> tuple[int, str]` that short-circuits OK when
  `config.SWAP_ISSUER_ADDRESS == config.SWAP_OFFER_ISSUER` (same account), else
  looks up `get_trustline_balance(SWAP_ISSUER_ADDRESS, SWAP_OFFER_CURRENCY_HEX,
  SWAP_OFFER_ISSUER)` and maps present→(0,ok) / `None`→(1,remediation). Print
  and `sys.exit` from `__main__` with `argparse --network`. Keep it loopback /
  read-only — no on-chain writes.

- [ ] **Step 4: Run to verify they pass** —
  `.venv/bin/python -m pytest tests/test_audit_swap_preconditions.py -q`.

- [ ] **Step 5: Wider suite / regression run** — full `tests/` suite.

- [ ] **Step 6: Commit** — `feat(scripts): audit issuer BRIX trustline precondition for swaps (#166)`

---

### Final Task: Full gate + PR

- [ ] Run the full pre-push gate locally: `.venv/bin/python -m pytest -q`,
      `ruff check --fix .`, `ruff format .`, `mypy` (from `.venv`). Confirm the
      worktree `.venv` symlink exists so the gate actually runs. Never `--no-verify`.
- [ ] Push the branch and `gh pr create` **non-draft** against `main`
      (Team-Hamsa/LFG). PR body: describe the pre-burn precheck + XRP fallback and
      the ops audit; reference #166. **No AI attribution / Co-Authored-By trailer.**
- [ ] Wait for **Greptile** and **CodeRabbit**. Read Greptile's verdict from the
      `Greptile Review` check-run summary (a clean review posts no comment). Close
      out every actionable finding on its own thread (fix in code AND reply naming
      the fixing commit) before merge.
- [ ] Note in the PR that the **secondary DB cleanup** (testnet rows 3550–3554 in
      `lfg_nfts.db`) from #166 is intentionally out of scope and should be a
      separate issue.
