# Bulk Minting — Design

**Issue:** [#215](https://github.com/Team-Hamsa/LFG/issues/215)
**Date:** 2026-07-14
**Status:** Design approved, ready for implementation plan

## Problem

Users are asking for **bulk minting**: turn in several tokens at once and
receive an equal number of freshly minted NFTs, instead of repeating the
single-mint flow N times. The single-mint flow (`lfg_core/mint_flow.py`)
handles one NFT per session: one payment, one mint, one offer, one Xaman
accept signature.

## Key insight: two fully decoupled phases

The design hinges on decoupling fulfillment from acceptance:

- **Phase A — Fulfillment (backend-owned, no user in the loop).** Once the
  single N× payment lands, the backend creates → mints → offers all N NFTs on
  its own. It waits on the user for *nothing*. The job is done when all N are
  minted-and-offered.
- **Phase B — Acceptance (ambient, user-owned, out of scope for this spec).**
  The N offers are created **without an `Expiration`** (verified current
  behavior — `lfg_core/xrpl_ops.py::create_nft_offer` sets no `Expiration`, so
  XRPL offers persist on-ledger until accepted or cancelled). The user accepts
  them whenever, from anywhere (Xaman directly today). They can switch apps,
  let their phone die, come back tomorrow — the offers are still waiting.

This deletes the entire "accept-queue babysitting" problem from the bulk flow.
A bulk mint is therefore a **durable batch job**, not a long-lived interactive
session.

### Explicit non-goals / follow-ups

- **In-app "pending offers" tray** — a notification-style surface where a user
  sees all their outstanding NFT offers and accepts any of them at any time.
  Valuable on its own (also fixes single-mint QR-loss recovery: distracted /
  app-switched / dead-phone users can recover their offer later). **Separate
  follow-up issue.**
- **XLS-56 Batch single-signature accept** — wrapping multiple
  `NFTokenAcceptOffer` under one signature once Xaman/XUMM support is
  confirmed. **Separate follow-up**, pairs with burn-to-mint (below).
- **Burn-to-mint ("infinite" minting)** — burning live LFG NFTs in exchange
  for fresh mints. Only a **stub / seam** in this spec (see §3); full logic is
  a later initiative that pairs with Batch.

## 1. Architecture

A bulk mint reuses the **entire** existing single-mint pipeline
(`traits.select_random_attributes` → `swap_compose.compose_nft` → CDN upload →
`xrpl_ops.mint_nft` → `xrpl_ops.create_nft_offer`), wrapped in a loop with
durable per-unit progress. No new XRPL primitives.

```
pay N× once ──▶ [ mint + offer ] × N  ──▶ job done (all N offered)
                      │
                 persisted after each unit → resumable on restart

offers sit on-ledger (no Expiration) ──▶ user accepts anytime, anywhere  (Phase B, out of scope)
```

## 2. Components & data model

### New: `lfg_core/bulk_mint_flow.py`

The batch-job state machine, sibling to `mint_flow.py`, reusing its helpers
(`_allocate_nft_number`, `_upload_to_bunny`, `_save_recovery_record`, the
compose/mint/offer calls).

```
BulkMintJob:
    id, discord_id, wallet_address, platform, push_user_token
    quantity K                      # clamped to supply headroom (see §3)
    unit_price, pay_with (LFGO|XRP), pay_amount (= K × unit_price)
    entitlement                     # { source: "payment" | "burn", ... }  (§3)
    payment_uuid, payment_link
    state: awaiting_payment → paid → fulfilling → done
                                    | failed | payment_timeout | cancelled
    units: [ Unit × K ]
    created_at, paid_at

Unit:
    index, state, nft_number, nft_id, image_url, offer_id, error
    state: pending → minted → offered | failed
```

- **Job `state`:** `fulfilling` is **non-terminal and resumable** (unlike
  single-mint's `offer_ready`, which is terminal). Job reaches `done` when
  every unit is `offered` (or terminally `failed` → converted to mint credit).
- **`_allocate_nft_number`** is already lock-guarded and reservation-aware, so
  K sequential allocations inside the loop are safe unchanged.

### Durability: `bulk_mint_jobs/` (JSON per job)

Mirrors the on-disk pattern of `_save_recovery_record`. The job record is
written:
- on payment confirmation (`paid`), and
- after **each** unit state transition.

A **startup sweep** in `lfg_service` re-loads any job left in `paid` /
`fulfilling` and resumes minting its `pending` units. Terminal jobs are cleaned
up after a retention window.

### New: `mint_credits` table (last-resort tail)

`(discord_id, network, credits, updated_at)`. A permanently-undeliverable unit
(cap-hit race, or exhausted retries) increments it; redeemable later with **no
re-payment**. This is the rare fallback, not the primary mechanism — the
durable batch job is the recovery layer.

## 3. Payment, supply cap & entitlement sources

### Payment accounting

Single `Payment` of `pay_amount = K × unit_price` (K = clamped quantity),
matched by the existing `xrpl_ops.wait_for_payment`. LFGO-vs-XRP path detection
is unchanged from single mint: per-wallet trustline balance ≥ K×price → LFGO
(burned on arrival at the issuer); else XRP (bot wallet buys-and-burns the LFGO
off the DEX).

### Supply cap (hard requirement)

New config `MAX_COLLECTION_SIZE` (default **10000**). Current live size is read
from the authoritative census (the `onchain_nfts` live-count, i.e. the same
number the economy conservation audit tracks; `get_next_nft_number − 1` is the
app-DB proxy). Enforced at **two points**:

1. **Request time (before payment).**
   `remaining = MAX_COLLECTION_SIZE − current_supply`.
   - `remaining == 0` → reject with `collection_full`.
   - `remaining < N` → **clamp** the job to `K = remaining`; tell the user
     "only K left, you'll pay for K" *before* they sign. The payment payload is
     built for `K×`, never `N×`. We never take payment for mints we can't
     deliver.
2. **Per-unit at fulfillment.** Each unit re-checks supply immediately before
   `mint_nft` (a concurrent bulk job on another surface could consume the
   tail). A unit that finds the cap hit does **not** mint — it converts to a
   **mint credit** rather than failing, so a race never costs the user money.

### Entitlement source seam (burn-to-mint stub)

The fulfillment loop must not care *why* the user is owed K mints. The job
carries an `entitlement`:

```
entitlement: { source: "payment" | "burn", ...source-specific }
```

- **`source: "payment"`** — the only path built now (pay K× up front).
- **`source: "burn"`** — **stub only**: a documented `BurnEntitlement`
  dataclass + a `NotImplementedError` factory. Spec note: burning M live LFG
  NFTs mints M fresh ones and is **exempt from `MAX_COLLECTION_SIZE`** (net
  supply-neutral: −M burned, +M minted → the "infinite minting" path). It
  pairs with the XLS-56 Batch follow-up (burn-M + accept-M under one
  signature). No burn logic is written now — only the seam, so Phase A's loop
  already reads its K from an `entitlement` rather than hard-wiring "payment".

This keeps the fulfillment loop identical for both sources — only entitlement
*acquisition* differs.

## 4. Error handling, fulfillment ordering & recovery

### Per-unit fail-safe ordering

Mirrors single-mint's promote-after-confirm discipline:

1. `_allocate_nft_number()` → compose → CDN upload (still **staged**, not yet
   promoted to the local image archive).
2. **Supply re-check** → `mint_nft`.
   - success → promote still to archive, `record_nft_mint`, rarity boost-clock
     update.
   - failure → discard staged still, release the reserved number, retry with
     existing backoff.
3. `create_nft_offer` (no `Expiration`) → unit `offered`, persist `offer_id`.
4. Persist the job record after each unit transition (durable progress).

### Failure taxonomy per unit

| Case | Handling |
|------|----------|
| Transient (RPC/CDN/XUMM blip) | Retry within the job using existing backoff. |
| Minted-but-offer-failed | NFT exists — re-attempt `create_nft_offer` on the existing `nft_id` (**no** re-mint). Persistent failure → journal for admin; unit counts as delivered-pending-offer. |
| DB-insert-failed-after-mint | Existing `_save_recovery_record` path, reused verbatim. |
| Cap-hit / permanently-unmintable | Convert to **mint credit**. |

### Server-restart recovery

Startup sweep loads `bulk_mint_jobs/*.json` in `paid` / `fulfilling` state and
resumes the `pending` units. Because payment already landed and each unit's
on-chain state is recorded, resume **never double-charges** (`wait_for_payment`
not re-invoked) and **never double-mints** (a unit past `minted` is skipped).

### Cancellation

Legal only while `awaiting_payment` (same rule as single mint — once paid,
fulfillment must complete). Reuses `MintSession.cancel()`'s synchronous
state-guard pattern (no `await` between the state check and the assignment, so
it can't race the background task on the single event loop).

## 5. Service surface

New endpoints in `lfg_service/app.py` (all surfaces — Discord Activity, bot,
Telegram — funnel through the service, so all inherit bulk minting):

- `POST /api/mint/bulk` — start a bulk job (body: `quantity`). Returns the
  clamped K, `pay_amount`, and the payment payload — or `collection_full` /
  `409` if a bulk job is already in flight for this user/platform.
- `GET /api/mint/bulk/{id}` — poll job + per-unit progress.
- The job surfaces in the existing `GET /api/mint/active` (from PR
  [#216](https://github.com/Team-Hamsa/LFG/pull/216)) as a non-terminal
  `fulfilling` job, so the client's `resumeMint()` re-attaches after a webview
  kill.

Per-user active-job lock: one bulk job in flight per user/platform (reuses the
`_active_session` pattern).

## 6. SourceTag & memos

Every bulk-minted `NFTokenMint` and `NFTokenCreateOffer` goes through the same
`xrpl_ops` builders as single mint, which already stamp
`SourceTag = 2606160021` and the `mint` / `create-offer` provenance memos
(`memos.platform_for_surface(job.platform)`). No new signing path — nothing to
special-case — but the invariant tests are extended to assert it for the bulk
path.

## 7. Testing strategy (TDD)

**`tests/test_bulk_mint_flow.py` — job state machine (mocked, no network):**
- Quantity clamping: `N ≤ remaining` unchanged; `N > remaining` clamps K and
  `pay_amount = K × price`; `remaining == 0` → `collection_full` before any
  payload is built.
- Payment path detection at K× (parametrized LFGO / XRP).
- Unit progression `pending → minted → offered`; job `done` only when all units
  terminal.
- Failure taxonomy: transient-retry; minted-but-offer-failed re-offers on the
  existing `nft_id` (asserts **no** second `mint_nft`); cap-hit-mid-fulfillment
  → mint credit.
- Cancel legal in `awaiting_payment`, rejected once `paid`.

**`tests/test_bulk_mint_durability.py` — persistence & restart:**
- Job JSON round-trips; written after each unit transition.
- Startup sweep resumes a `fulfilling` job, mints only `pending` units, skips
  any past `minted` (no double-mint / no double-charge — `wait_for_payment` not
  re-invoked).
- Permanently-failed unit → `mint_credits` increment; redeemable-balance query.

**`tests/test_bulk_mint_supply_cap.py`:**
- `MAX_COLLECTION_SIZE` boundary: exact-fill, over-fill clamp, concurrent-race
  per-unit re-check → credit not loss.
- Burn entitlement stub raises `NotImplementedError`; cap-exemption asserted at
  the seam (burn source bypasses the request-time cap check).

**Service-level (`tests/test_bulk_mint_service.py` + `webapp/test_smoke.py`):**
- New routes registered and resolvable; the job appears in `/api/mint/active`
  as non-terminal `fulfilling`.
- Auth-gated; per-user active-job lock.

**SourceTag/memo invariant:** extend the existing invariant tests to cover the
bulk `NFTokenMint` + `NFTokenCreateOffer`.

## Open items deferred to follow-up issues

1. In-app "pending offers" tray (Phase B UX; also fixes single-mint QR-loss
   recovery).
2. XLS-56 Batch single-signature accept (once Xaman support confirmed).
3. Burn-to-mint full implementation (fills the `source: "burn"` seam; pairs
   with Batch).
