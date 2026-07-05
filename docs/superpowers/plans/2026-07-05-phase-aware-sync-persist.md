# Phase-Aware `_sync_then_persist` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every economy flow distinguish "the on-chain Closet modify never committed" from "it committed but the local DB mirror write failed", and choose the compensating action + journal status accordingly — closing #107.

**Architecture:** An exception taxonomy in `lfg_core/closet_token.py` (`ClosetError` = ledger not committed, safe to compensate on-chain; `ClosetMirrorError(tx_hash)` = ledger committed, DB-only failure, reconcile-from-chain; `ClosetIndeterminateError` = unknown, fail-closed). `sync_closet` and `_sync_then_persist` raise the precise type; each of the five flows grows three catch branches per the compensation matrix in the spec.

**Tech Stack:** Python 3.10+, sqlite3, pytest. No new dependencies. No new XRPL tx paths (no SourceTag work).

**Spec:** `docs/superpowers/specs/2026-07-05-phase-aware-sync-persist-design.md`

## Global Constraints

- Every new test file that imports `lfg_core` at module top MUST start with the env-guard preamble — copy it **verbatim** from `tests/test_seasons.py` lines 1–18. (Tasks below extend *existing* economy test files, which already load safely — new files only if noted.)
- One PR, opened as **draft** (`gh pr create --draft`), branch `fix/phase-aware-sync-persist`; CodeRabbit review before merge.
- Run `.venv/bin/python -m pytest` (repo venv). Pre-commit runs ruff format.
- Existing ledger-failed behavior must not change: all current tests in `tests/test_economy_flow_{harvest,assemble,equip,extract,deposit}.py` must pass **unmodified** except where a task explicitly says otherwise.
- Standard mirror-failure injection for tests: pass a `deps.conn` wrapper whose `execute` raises on `DELETE FROM closet_bodies` (the SECOND statement of `es.set_closet_contents`, `lfg_core/economy_store.py:220-231`) — this fails the mirror *after* `closet_modify` succeeded AND after the `closet_assets` delete executed, leaving a genuinely half-applied uncommitted transaction so rollback behavior is exercised. The wrapper delegates `rollback()`/`commit()` to the real conn. Define it once (Task 2, `flaky_mirror_conn`) and reuse.
- Journal precedence (spec §2.4): later-step failure statuses win the `status` field; `mirror_pending: true` + `sync_tx_hash` are sticky record fields that survive them.

## File Structure

```
lfg_core/closet_token.py          # MOD Task 1: ClosetMirrorError / ClosetIndeterminateError; sync_closet phases + returns tx hash
lfg_core/economy_flow.py          # MOD Tasks 2-7: _sync_then_persist wrap; five flows' catch branches; sync_tx_hash in records
tests/test_closet_token.py        # MOD Task 1: taxonomy tests
tests/test_economy_flow_harvest.py   # MOD Task 3
tests/test_economy_flow_assemble.py  # MOD Task 4
tests/test_economy_flow_equip.py     # MOD Task 5
tests/test_economy_flow_extract.py   # MOD Task 6
tests/test_economy_flow_deposit.py   # MOD Task 7
```

---

### Task 1: Exception taxonomy + phase-aware `sync_closet`

**Files:** `lfg_core/closet_token.py` (lines 31, 182–202), `tests/test_closet_token.py`

- [ ] **Step 1: Write failing tests** in `tests/test_closet_token.py`:
  - `test_sync_closet_returns_tx_hash_on_success` — happy path returns the modify tx hash (currently returns `None`).
  - `test_sync_closet_modify_none_raises_plain_closet_error` — `modify_fn` returns `None` → `ClosetError` raised and it is NOT a `ClosetMirrorError`/`ClosetIndeterminateError`.
  - `test_sync_closet_modify_raise_is_indeterminate` — `modify_fn` raises `RuntimeError` → `ClosetIndeterminateError`.
  - `test_sync_closet_set_token_failure_is_mirror_error` — `modify_fn` returns `"HASH"`, then `economy_store.set_closet_token` raises (monkeypatch) → `ClosetMirrorError` with `.tx_hash == "HASH"`.
  - `test_sync_closet_upload_raise_propagates_raw` — `upload_fn` raises `RuntimeError` → the same `RuntimeError` propagates (NOT any `ClosetError` subclass); this pins the spec §2.2 decision that pre-ledger upload failures reach the flows' generic ledger-failed branch unchanged.
- [ ] **Step 2:** `.venv/bin/python -m pytest tests/test_closet_token.py -v` → new tests FAIL (missing classes / `None` return).
- [ ] **Step 3: Implement** in `closet_token.py`: add the two subclasses beside `ClosetError` (line 31); restructure `sync_closet` per spec §2.2 (`try` around `modify_fn` → `ClosetIndeterminateError`; `try` around `set_closet_token` → `ClosetMirrorError(msg, tx_hash)`; `return tx_hash`).
- [ ] **Step 4: Verify:** `.venv/bin/python -m pytest tests/test_closet_token.py tests/test_closet_token_lifecycle.py -v` — all green.

### Task 2: Phase-aware `_sync_then_persist` + shared test helper

**Files:** `lfg_core/economy_flow.py:109-125`, `tests/sdk_helpers.py` (or a small `tests/economy_helpers.py` — no lfg_core import at top needed if it only wraps a conn)

- [ ] **Step 1: Write failing tests** (put in `tests/test_economy_flow_harvest.py`, where deps fakes already exist):
  - `test_sync_then_persist_mirror_failure_is_typed` — calling `_sync_then_persist` with a conn whose `execute` raises on `DELETE FROM closet_bodies` (i.e. AFTER the `closet_assets` delete has executed, so a transaction is genuinely half-applied) raises `bt.ClosetMirrorError` carrying the tx hash from `closet_modify`. Add the reusable `flaky_mirror_conn(real_conn, fail_on="DELETE FROM closet_bodies")` wrapper helper here — it must delegate `rollback()`/`commit()` to the real conn.
  - `test_sync_then_persist_mirror_failure_rolls_back` — **open-transaction hazard (spec §2.3)**: seed `closet_assets` with existing rows for the owner; trigger the same mid-`set_closet_contents` failure; after catching `ClosetMirrorError`, assert `es.read_closet_assets(conn)` still returns the ORIGINAL rows (the uncommitted `DELETE FROM closet_assets` was rolled back, not left pending on the shared conn), and assert a subsequent unrelated `conn.commit()` does not make rows disappear.
- [ ] **Step 2:** Run → FAIL (`Exception` propagates un-typed / no tx hash; rows vanish after the follow-up commit).
- [ ] **Step 3: Implement:** wrap `es.set_closet_contents` in `_sync_then_persist` per spec §2.3 — `except Exception: deps.conn.rollback(); raise bt.ClosetMirrorError(..., tx_hash)`; return `tx_hash`; apply the same rollback-before-raise inside `sync_closet`'s `set_closet_token` wrap (Task 1 code, adjust + extend Task 1's mirror-error test to assert rollback if not already); update the docstring to describe the three raise types and the rollback guarantee.
- [ ] **Step 4: Verify:** `.venv/bin/python -m pytest tests/test_economy_flow_harvest.py -v`.

### Task 3: Harvest branches

**Files:** `lfg_core/economy_flow.py:184-236` (`run_harvest`, catch at 221–230), `tests/test_economy_flow_harvest.py`

- [ ] **Step 1: Failing tests:**
  - `test_harvest_mirror_failure_completes_pending_mirror` — burn OK, `closet_modify` OK, mirror write raises → `session.state == DONE`, journal status `complete_pending_mirror`, journal has `sync_tx_hash`, no extra burn calls.
  - `test_harvest_indeterminate_sync_journals_and_fails` — `closet_modify` raises → FAILED, journal `harvest_sync_indeterminate`, moved_assets still in journal.
  - Existing `harvested_pending_closet` tests (lines 172, 200) stay untouched and must still pass (modify returns `None` path).
- [ ] **Step 2:** Run → FAIL (both currently land in `harvested_pending_closet`).
- [ ] **Step 3: Implement:** replace the single `except Exception` around `_sync_then_persist` with `except bt.ClosetMirrorError` → DONE + `complete_pending_mirror`; `except bt.ClosetIndeterminateError` → FAILED + `harvest_sync_indeterminate`; `except Exception` → existing `harvested_pending_closet` path. Add `sync_tx_hash` + `mirror_pending` to `HarvestSession._record`.
- [ ] **Step 4: Verify:** `.venv/bin/python -m pytest tests/test_economy_flow_harvest.py -v`.

### Task 4: Assemble branches (destructive-compensation fix)

**Files:** `lfg_core/economy_flow.py:281-371` (catch at 328–350), `tests/test_economy_flow_assemble.py`

- [ ] **Step 1: Failing tests:**
  - `test_assemble_mirror_failure_does_not_burn_and_delivers` — mint OK, `closet_modify` OK, mirror raises → **`char_burn_fn` NOT called**, offer+accept still run, `session.state == DONE`, `session.new_nft_id` retained, journal `complete_pending_mirror`.
  - `test_assemble_indeterminate_keeps_mint_no_burn` — `closet_modify` raises → FAILED, no burn, `new_nft_id` retained in journal, status `assemble_sync_indeterminate`.
  - `test_assemble_mirror_fail_then_offer_fail_precedence` — mirror raises AND `char_offer_fn` returns `None` → journal status is `minted_no_offer` (later-step failure wins, economy_flow.py:360), session FAILED per that path's existing semantics, **and** the record carries `mirror_pending: true` + `sync_tx_hash` (pending-mirror fact not lost); no burn.
  - Existing `reverted_mint`/`failed_revert_mint` tests unchanged.
- [ ] **Step 2:** Run → FAIL (current code burns the mint in both new cases).
- [ ] **Step 3: Implement** the three-branch catch; mirror-failed branch sets `session.mirror_pending = True` (a new session field emitted by `_record()`) and falls through to the offer/accept block (economy_flow.py:353-366); the terminal write emits `complete_pending_mirror` when `mirror_pending` and all later steps succeeded, otherwise the later step's existing status with `mirror_pending: true` in the record. Add `sync_tx_hash` + `mirror_pending` to `AssembleSession._record`.
- [ ] **Step 4: Verify:** `.venv/bin/python -m pytest tests/test_economy_flow_assemble.py -v`.

### Task 5: Equip branches (destructive-compensation fix)

**Files:** `lfg_core/economy_flow.py:417-483` (catch at 456–477), `tests/test_economy_flow_equip.py`

- [ ] **Step 1: Failing tests:**
  - `test_equip_mirror_failure_keeps_new_traits` — char modify OK, closet modify OK, mirror raises → **no revert modify** (char_modify called exactly once), DONE, journal `complete_pending_mirror`.
  - `test_equip_indeterminate_no_revert` — closet `modify_fn` raises → FAILED, no revert, journal `equip_sync_indeterminate`.
  - Existing revert test (line ~139) unchanged.
- [ ] **Step 2:** Run → FAIL (current code reverts the character).
- [ ] **Step 3: Implement** the three-branch catch; add `sync_tx_hash` to `EquipSession._record`.
- [ ] **Step 4: Verify:** `.venv/bin/python -m pytest tests/test_economy_flow_equip.py -v`.

### Task 6: Extract branches (destructive-compensation fix)

**Files:** `lfg_core/economy_flow.py:517-591` (catch at 547–564), `tests/test_economy_flow_extract.py`

- [ ] **Step 1: Failing tests:**
  - `test_extract_mirror_failure_does_not_burn_and_offers` — trait mint OK, closet modify OK, mirror raises → **`trait_burn_fn` NOT called**, `nft_id` retained, offer/accept still run, DONE, journal `complete_pending_mirror` with `sync_tx_hash`. (Distinct from the existing trait_tokens-mirror test at line 136 — this one fails the *Closet* mirror.)
  - `test_extract_indeterminate_keeps_token_no_burn` — closet `modify_fn` raises → FAILED, no burn, journal `extract_sync_indeterminate`.
  - `test_extract_mirror_fail_then_offer_fail_precedence` — mirror raises AND `closet_offer_fn` returns `None` → the offer-path outcome wins the `status` field (today extract still ends DONE with `accept=None`, economy_flow.py:585-588 — assert that outcome's status), record carries `mirror_pending: true` + `sync_tx_hash`; no burn.
  - Existing `reverted_mint`/`failed_revert_mint` tests unchanged.
- [ ] **Step 2:** Run → FAIL (current code burns the trait token back).
- [ ] **Step 3: Implement** three-branch catch; mirror-failed branch sets `session.mirror_pending = True` and continues to the trait_tokens upsert + offer block (economy_flow.py:575-588). If BOTH the Closet mirror and trait_tokens mirror fail, a single `complete_pending_mirror` journal suffices (`mirror_pending` covers both). Add `sync_tx_hash` + `mirror_pending` to `ExtractSession._record`.
- [ ] **Step 4: Verify:** `.venv/bin/python -m pytest tests/test_economy_flow_extract.py -v`.

### Task 7: Deposit branches (double-credit fix)

**Files:** `lfg_core/economy_flow.py:626-689` (catch at 671–683), `tests/test_economy_flow_deposit.py`

- [ ] **Step 1: Failing tests:**
  - `test_deposit_mirror_failure_completes_pending_mirror` — burn OK, closet modify OK, mirror raises → DONE, journal `complete_pending_mirror` (NOT `deposited_pending_closet`), `sync_tx_hash` set — the operator recipe attached to `deposited_pending_closet` must never fire here.
  - `test_deposit_indeterminate_journals_and_fails` — closet `modify_fn` raises → FAILED, journal `deposit_sync_indeterminate`, slot/value preserved for reconciliation.
  - Existing `test_deposit_burn_then_credit_fails_journals` (line 204, `fail_sync=True` = modify returns `None`) unchanged — it is the true ledger-failed case.
- [ ] **Step 2:** Run → FAIL (both new cases currently journal `deposited_pending_closet`).
- [ ] **Step 3: Implement** three-branch catch; add `sync_tx_hash` to `DepositSession._record`.
- [ ] **Step 4: Verify:** `.venv/bin/python -m pytest tests/test_economy_flow_deposit.py -v`.

### Task 8: Documentation + journal-status table + full-suite gate

**Files:** `lfg_core/economy_flow.py` (module docstring), `CLAUDE.md` (Phase 2/4 sections if status lists are mentioned), spec cross-check

- [ ] **Step 1:** Add a journal-status table to the `economy_flow.py` module docstring: every status, its meaning, and the operator action (`*_pending_mirror` → none, listener converges; `*_sync_indeterminate` / `*_pending_closet` → reconcile-from-chain, never blind re-apply).
- [ ] **Step 2:** Grep for consumers of journal statuses (`grep -rn "pending_closet\|complete_pending_mirror" scripts/ webapp/ lfg_service/`) and confirm none branch on statuses in a way the new states break; note findings in the PR description.
- [ ] **Step 3: Verify:** full suite `.venv/bin/python -m pytest` green; ruff clean via pre-commit.
- [ ] **Step 4:** Open draft PR referencing #107 with the compensation matrix from the spec in the body; flip ready-for-review when settled; address CodeRabbit before merge.
