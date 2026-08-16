# Phase-Aware `_sync_then_persist` — Design

**Date:** 2026-07-05
**Issue:** #107 (raised by CodeRabbit on #106)
**Status:** Draft
**Scope:** `lfg_core/economy_flow.py`, `lfg_core/closet_token.py`, all five
economy flows (harvest / assemble / equip / extract / deposit), their tests,
and journal-state documentation. Pre-existing, cross-cutting bug — predates
Phase 4; fixed once, uniformly.

## 1. Problem statement

Every economy flow updates the Closet via `_sync_then_persist`
(`lfg_core/economy_flow.py:109-125`), which:

1. calls `bt.sync_closet(...)` — **on-chain**: upload new Closet metadata to
   the CDN, `NFTokenModify` the Closet token's URI, then persist the new URI
   to `closet_tokens` (`lfg_core/closet_token.py:182-202`);
2. calls `es.set_closet_contents(...)` — **local DB mirror**
   (`lfg_core/economy_store.py:210-231`).

All five flows wrap the call in a bare `except Exception` and compensate as
if **the ledger change never happened**:

- `run_harvest` → journals `harvested_pending_closet` (economy_flow.py:221-230)
- `run_assemble` → **burns the freshly minted character back** (economy_flow.py:328-350)
- `run_equip` → **NFTokenModify's the character back to its old URI** (economy_flow.py:456-477)
- `run_extract` → **burns the new trait token back** (economy_flow.py:547-564)
- `run_deposit` → journals `deposited_pending_closet` (economy_flow.py:671-683)

But the exception can originate **after** the on-chain modify committed:

- `sync_closet`'s trailing `economy_store.set_closet_token(...)` write
  (closet_token.py:202) raises after `modify_fn` returned a tx hash;
- `_sync_then_persist`'s `es.set_closet_contents(...)` (economy_flow.py:125)
  raises (sqlite locked/disk full) after `sync_closet` fully succeeded.

In those cases the compensation is **wrong**, and for three flows it is
actively destructive:

| Flow | On-chain state when DB-mirror write fails | Current compensation | Consequence |
|---|---|---|---|
| Harvest | Closet **already credited** with the burned character's 8 assets + body | journal `harvested_pending_closet`; operator instructed to re-apply the deposit | operator re-credit ⇒ **double-credit** (conservation drift the auditor flags) |
| Assemble | Closet **already drained** of body + 8 assets | burn the new mint back (`char_burn_fn`, economy_flow.py:332) | mint destroyed while Closet stays drained on-chain ⇒ **user loses body + 8 assets** |
| Equip | Closet **already swapped** (-incoming, +displaced) | modify character back to old URI (economy_flow.py:461) | character reverted but Closet keeps the swap ⇒ **incoming trait lost, displaced duplicated** |
| Extract | Closet **already decremented** | burn the trait token back (`trait_burn_fn`, economy_flow.py:549) | token destroyed while Closet stays decremented ⇒ **user loses the trait** |
| Deposit | Closet **already credited** | journal `deposited_pending_closet`; operator re-credits | **double-credit** |

The window is narrow (local write failing after a successful on-chain tx) and
partially bounded by the listener (`nft_listener._apply_closet`,
lfg_core/nft_listener.py:108-133, rebuilds `closet_assets`/`closet_bodies`
from the Closet token's metadata on every observed Modify) — but the
*compensating on-chain actions* (burn-back / modify-back) are **not**
listener-recoverable: they destroy or revert real tokens against a Closet
that already moved.

### Current vs correct behavior

| Failure point | Ledger committed? | Current treatment | Correct treatment |
|---|---|---|---|
| `get_closet_record` returns None; `upload_fn` raises (propagates raw); `modify_fn` returns `None` (closet_token.py:194-201) | No | compensate on-chain (correct today) | unchanged: compensate on-chain / journal ledger-failure state |
| `modify_fn` **raises** (network timeout after submit) | **Unknown** | treated as "not committed" → may double-apply via compensation | fail-closed: no on-chain compensation; journal an *indeterminate* state; reconcile-from-chain |
| `set_closet_token` raises (closet_token.py:202) | **Yes** | treated as "not committed" → wrong compensation | no on-chain compensation; proceed / journal `*_pending_mirror`; listener reconciles |
| `es.set_closet_contents` raises (economy_flow.py:125) | **Yes** | same wrong compensation | same as above |

## 2. Proposed design

### 2.1 Exception taxonomy (in `lfg_core/closet_token.py`)

`ClosetError` (closet_token.py:31) stays the base. Two subclasses:

```python
class ClosetMirrorError(ClosetError):
    """The on-chain NFTokenModify COMMITTED; only a local DB write failed.
    Do NOT run on-chain compensation — reconcile from chain instead."""
    def __init__(self, msg: str, tx_hash: str):
        super().__init__(msg)
        self.tx_hash = tx_hash

class ClosetIndeterminateError(ClosetError):
    """modify_fn raised; whether the modify committed is unknown.
    Fail-closed: no on-chain compensation, admin/listener reconciliation."""
```

Plain `ClosetError` keeps its existing meaning: **ledger not committed,
on-chain compensation is safe** (missing record, `modify_fn` returned falsy).
An `upload_fn` raise is deliberately NOT wrapped — it propagates raw (current
behavior) and lands in each flow's trailing generic `except Exception`, which
is the ledger-failed branch, i.e. the correct compensation. Wrapping would add
code for zero behavioral difference.

### 2.2 `sync_closet` becomes phase-aware (closet_token.py:182-202)

```python
url = await upload_fn(...)                      # pre-ledger; a raise propagates RAW (ledger-failed branch)
try:
    tx_hash = await modify_fn(nft_id, owner, url)
except Exception as e:
    raise ClosetIndeterminateError(f"...: {e}") from e
if not tx_hash:
    raise ClosetError("failed to modify Closet NFToken URI")   # unchanged
try:
    economy_store.set_closet_token(conn, ...)   # post-ledger
except Exception as e:
    raise ClosetMirrorError(f"...", tx_hash) from e
return tx_hash
```

`sync_closet` returns the tx hash (new; today it returns `None`) so flows can
journal it.

### 2.3 `_sync_then_persist` (economy_flow.py:109-125)

The `es.set_closet_contents` call is wrapped the same way, **with a mandatory
rollback**:

```python
tx_hash = await bt.sync_closet(...)             # may raise ClosetError/Mirror/Indeterminate
try:
    es.set_closet_contents(deps.conn, owner, asset_list, body_list)
except Exception as e:
    deps.conn.rollback()   # REQUIRED — see open-transaction hazard below
    raise bt.ClosetMirrorError(f"closet contents mirror failed: {e}", tx_hash) from e
return tx_hash
```

**Open-transaction hazard:** `set_closet_contents`
(economy_store.py:220-231) executes `DELETE FROM closet_assets` /
`DELETE FROM closet_bodies` *before* its final `conn.commit()`. If it raises
mid-function, the **shared** `deps.conn` is left holding a half-applied,
uncommitted delete — and any later `commit()` by an unrelated codepath on the
same connection would persist the partial delete, silently corrupting the
mirror worse than doing nothing. The `ClosetMirrorError` path therefore MUST
`deps.conn.rollback()` before re-raising, restoring the pre-call mirror state
(stale-but-consistent, which the listener then converges). The same applies to
the `ClosetMirrorError` raised inside `sync_closet` for `set_closet_token`
(closet_token.py:202): wrap-with-rollback there too.

Note the `modify_fn` raise no longer falls through to flow-level generic
`except Exception` — flows now catch `bt.ClosetError` subclasses explicitly
around the `_sync_then_persist` call, ordered most-specific-first. Pre-ledger
raises (compose/upload) still reach the generic handler, which remains the
ledger-failed branch.

### 2.4 Per-flow compensation matrix

Each flow's existing `except Exception` around `_sync_then_persist` becomes
three branches. **Ledger-failed** (`ClosetError`, not a subclass) keeps
today's behavior verbatim. **Mirror-failed** (`ClosetMirrorError`): the chain
is fully consistent — the flow must NOT undo anything on-chain; it proceeds
(where there are later steps) and journals a `*_pending_mirror` status so the
auditor knows the DB may lag until the listener catches up.
**Indeterminate** (`ClosetIndeterminateError`): fail-closed, no on-chain
compensation, journal `sync_indeterminate` variant, session FAILED with an
admin-facing message ("reconcile from chain").

| Flow | Ledger-failed (`ClosetError`) — unchanged | Mirror-failed (`ClosetMirrorError`) — NEW | Indeterminate — NEW |
|---|---|---|---|
| Harvest | `harvested_pending_closet` journal, FAILED (assets recoverable by re-applying the deposit) | session **DONE**, journal `complete_pending_mirror` (listener rebuilds mirror from the already-updated Closet token) | FAILED, journal `harvest_sync_indeterminate`; admin reconciles from chain before any re-credit |
| Assemble | burn mint back → `reverted_mint` / `failed_revert_mint` (economy_flow.py:332-349) | **do NOT burn**; continue to offer+accept (economy_flow.py:353-366), journal `complete_pending_mirror` on success path | FAILED, journal `assemble_sync_indeterminate`; mint kept (nft_id in journal), no burn |
| Equip | modify character back → `reverted_modify` / `failed_revert` (economy_flow.py:459-476) | **do NOT revert**; session DONE, journal `complete_pending_mirror` | FAILED, journal `equip_sync_indeterminate`; character keeps new URI, no revert |
| Extract | burn trait back → `reverted_mint` / `failed_revert_mint` (economy_flow.py:549-563) | **do NOT burn**; continue to trait_tokens upsert + offer (economy_flow.py:575-588), journal `complete_pending_mirror` | FAILED, journal `extract_sync_indeterminate`; trait token kept (nft_id in journal), no burn |
| Deposit | `deposited_pending_closet` journal, FAILED (operator credits the Closet) | session **DONE**, journal `complete_pending_mirror` — operator must NOT re-credit | FAILED, journal `deposit_sync_indeterminate`; reconcile-from-chain before any credit |

**Journal precedence for mirror-fail + later-step-fail (assemble, extract).**
When the mirror-failed branch continues to the delivery steps and a *later*
step also fails, the later step's failure status **wins the `status` field**
(assemble: `minted_no_offer`, economy_flow.py:360; extract: whatever the
offer/accept path reports) and the session state follows that later step's
existing semantics — but the pending-mirror fact must not be lost. Every
session record therefore carries two independent fields:

- `sync_tx_hash: str | None` — set the moment the Closet modify commits;
- `mirror_pending: bool` — set `True` on the `ClosetMirrorError` branch and
  never cleared for that session.

So a combined failure journals e.g. `{"status": "minted_no_offer",
"mirror_pending": true, "sync_tx_hash": "..."}` — the operator re-offers the
mint (existing recipe) and knows the DB mirror lags until the listener
converges. `complete_pending_mirror` is emitted only when all later steps
succeed and `mirror_pending` is set (it is `complete` + the flag, kept as a
distinct status for auditor grep-ability).

Extract already has precedent for `complete_pending_mirror`
(economy_flow.py:582 — trait_tokens mirror failure is journaled but the
session completes); this design extends the same pattern to the Closet
mirror.

### 2.5 Journal states added / changed

New statuses (append-only; no existing status changes meaning):
- `complete_pending_mirror` — extended from extract-only to all five flows;
  now also carries `sync_tx_hash`.
- `<op>_sync_indeterminate` (5 statuses) — modify outcome unknown.

Existing statuses `harvested_pending_closet` / `deposited_pending_closet`
**narrow in meaning**: after this change they are emitted only when the
ledger modify definitively did NOT commit, so the operator re-apply recipe
attached to them becomes safe (no double-credit path).

Session records gain two fields via each `_record()` method:
`sync_tx_hash` (None unless the modify committed) and `mirror_pending`
(bool; sticky once set — survives later-step failure statuses, per the
precedence rule in §2.4).

### 2.6 Recovery / admin path

- **Rollback guarantee:** because the `ClosetMirrorError` path rolls back the
  shared connection (§2.3), a mirror failure leaves the DB at its pre-op
  contents — stale relative to chain but internally consistent — until the
  listener overwrites it. No journal state ever coexists with a half-applied
  local delete.
- **`*_pending_mirror` / `mirror_pending: true`**: self-healing. The listener's `_apply_closet`
  (nft_listener.py:108-133) rebuilds `closet_assets`/`closet_bodies` and the
  `closet_tokens` row from the token's on-chain metadata when it sees the
  Modify tx. If the listener was down, `scripts/backfill_onchain.py` or a
  listener restart reconciles. `scripts/audit_trait_economy.py` remains the
  drift detector.
- **`*_sync_indeterminate`** and `*_pending_closet`: operator checks the
  Closet token's current on-chain URI/metadata (the journal has the owner and
  intended contents) and decides — **reconcile-from-chain, never blind
  re-apply**, per the issue. Document the recipe in the ops section of the
  journal-status table (docstring in `economy_flow.py`); no new script in
  this issue's scope (the listener + auditor cover the mechanical part).

### 2.7 Listener interaction

No listener changes. The listener already treats the Closet token as source
of truth and overwrites the mirror on every observed Modify — which is
exactly why the mirror-failed branch is safe to complete: the DB converges
without operator action. The design preserves the module's stated invariant
(economy_flow.py:8-11): token modified before DB, DB always rebuildable from
chain.

## 3. Alternatives considered

1. **Return a result object instead of exceptions** (`SyncResult(committed,
   tx_hash, error)`). Rejected: every caller would need `if result.error`
   plumbing, and non-`_sync_then_persist` raisers (compose/upload before the
   call) still flow through `except Exception`; the exception taxonomy keeps
   flow bodies close to their current shape and makes "forgot to handle"
   impossible (unhandled `ClosetMirrorError` still fails the session, just
   with the old wrong compensation — hence flows catch subclasses first).
2. **Retry the DB mirror write instead of classifying.** Rejected: retries
   shrink but don't close the window, and do nothing for the
   assemble/equip/extract destructive-compensation cases or the
   indeterminate case.
3. **Per-flow ad-hoc fixes** (e.g. only fix extract/deposit from #106).
   Rejected by the issue itself: "Do it once across ALL flows, not per-flow."
4. **Make the flows verify on-chain state before compensating** (read the
   Closet URI back and diff). Rejected for now: adds a network read to every
   failure path and re-introduces indeterminacy (the read can also fail);
   the taxonomy gives the same information for free at the raise site. Could
   layer on later for the indeterminate branch.

## 4. Non-goals

- No changes to the ordering principle (chain-before-DB) — this fixes the
  *classification* of failures, not the ordering.
- No new recovery script / admin CLI (listener + auditor + journal suffice).
- No changes to swap_flow.py (`run_swap_session` has an analogous shape but
  its own revert machinery and journals; out of scope, can follow the same
  taxonomy later if audited to need it).
- No changes to `harvested_pending_closet` / `deposited_pending_closet`
  operator recipes beyond documenting that they now definitively mean
  "ledger did not commit".
- Not addressing `es.delete_trait_token` in deposit (economy_flow.py:665)
  failing after the burn — that mirror row is listener-rebuilt and count-only;
  existing behavior stands.

## 5. Risks

- **Behavior change on mirror failure: sessions now end DONE.** Callers
  (scripts/economy_*.py print `State:`; webapp economy API) will report
  success while the local DB briefly lags. Mitigated by the journal record +
  listener convergence; tests assert the journal is written.
- **`ClosetIndeterminateError` on transient exceptions from `modify_fn`**
  could turn previously-auto-compensated failures (where the modify in fact
  never submitted) into admin-attention journals. Accepted: fail-closed is
  the house rule for irreversible ops (#101 precedent); frequency is expected
  to be very low, and the real deps (`scripts/_economy_deps.py:147` →
  `xrpl_ops.modify_nft`) mostly return `None` on failure rather than raising.
- **Test-fake drift:** existing test fakes simulate failure via
  `closet_modify` returning `None` (tests/test_economy_flow_deposit.py:26,
  65-69), which is the ledger-failed branch — all existing tests should pass
  unchanged; new tests must inject post-modify DB failures (e.g. a raising
  `set_closet_contents` via a wrapped connection or monkeypatch) to cover the
  new branches.
