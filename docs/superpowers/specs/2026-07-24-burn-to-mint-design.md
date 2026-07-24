# Burn-to-mint — design

**Date:** 2026-07-24
**Status:** draft (triage — needs maintainer review)
**Issue:** #220

## Problem

Bulk minting (#215) shipped an **entitlement seam** so the fulfillment loop
mints `quantity` units without caring *why* the user is owed them. Two
entitlement sources were designed:

- `PaymentEntitlement` (`source="payment"`, `cap_exempt=False`) — the user paid
  K× the mint price; every mint counts against `MAX_COLLECTION_SIZE`.
- `BurnEntitlement` (`source="burn"`, `cap_exempt=True`) — the user burns M live
  NFTs to earn M fresh mints; net-zero supply, so it is **exempt from the
  10,000 cap**.

Today `lfg_core/entitlement.py::build_burn_entitlement` is a stub that raises
`NotImplementedError("burn-to-mint is not implemented yet (#220)")`, and no code
constructs a `BurnEntitlement`. The whole cap-exempt fulfillment machinery
already exists and is exercised by the payment path — `bulk_mint_flow`'s
`clamp_to_headroom` / `_fulfill_unit` / `headroom_snapshot` /
`release_job_headroom` all special-case `cap_exempt` (skip the reservation
entirely) — but there is no way to *reach* it: `clamp_to_headroom` only ever
builds a `PaymentEntitlement`, and there is no burn-first preflight.

This issue fills the stub: a user selects M live LFG characters they own, the
issuer burns them (user-initiated, respecting the **no-forced-burns**
principle), and the user is owed M cap-exempt mints, fulfilled by the same
`mint_flow.mint_one_unit` path bulk mint uses.

The PR #225 CodeRabbit gate (issue comment) is a hard constraint:
`BurnEntitlement.cap_exempt=True` must be reachable **only after on-chain burn
verification exists** — a publicly-constructed or deserialized burn entitlement
must never bypass `MAX_COLLECTION_SIZE` without a verified burn.

## Constraints discovered

- **SourceTag `2606160021` + provenance memos on every tx.** The burn is
  `xrpl_ops.burn_nft` (already stamps `source_tag=config.SOURCE_TAG` and
  `memos.build_memo_models(INITIATOR_BACKEND, platform, ACTION_BURN)`); the mint
  is `xrpl_ops.mint_nft` via `mint_flow.mint_one_unit` (already stamps
  `ACTION_MINT`). Both actions already exist in `lfg_core/memos.py`
  (`ACTION_BURN`, `ACTION_MINT`). The originating surface must be threaded via
  `memos.platform_for_surface(job.platform)`, exactly as bulk mint does.
- **No-forced-burns principle** (`memory/lfg-no-forced-burns-principle.md`):
  never issuer-burn an NFT from a user wallet without user authorization, and
  the ledger is the source of truth. Authorization here = the caller invoking
  the authed `POST /api/mint/burn` endpoint with their own wallet resolved by
  `@require_auth` + `identity.resolve`. Every target NFT's ownership,
  issuer, and burnable flag are re-verified **on-ledger, fail-closed**,
  immediately before the burn.
- **Burnability is a hard eligibility gate.** `NFT_FLAGS=25` (burnable) is the
  current mint flag, but legacy pre-change flag-24 characters are
  **non-burnable** and can't be issuer-burned (`xrpl_ops.NFT_FLAG_BURNABLE =
  0x0001`). Those are ineligible and must be rejected up front, never
  half-burned.
- **Cap-exemption must trail a real burn.** `MAX_COLLECTION_SIZE` is enforced by
  `headroom.try_reserve`; cap-exempt jobs skip it. So the exemption is only sound
  if M mints are backed by M *confirmed* burns. The flow burns **first**, then
  mints — so the collection never transiently exceeds the cap, and the
  entitlement's `quantity` equals the count of confirmed burns.
- **Burn is irreversible; fulfillment must be crash-safe.** The user has already
  given up M NFTs, so they must end up with exactly M fresh NFTs **or** M durable
  `mint_credits` (redeemable with no payment). This reuses the existing
  `mint_credits` tail in `bulk_mint_flow._fulfill_unit` and the durable
  persist/resume model.
- **Characters, not the trait economy.** Burn-to-mint operates on the character
  collection (`config.XRPL_NETWORK`, `config.SWAP_ISSUER_ADDRESS`,
  `config.NFT_TAXON`), so the `ECONOMY_NETWORK` seam does **not** apply — no
  `ECONOMY_ENABLED` gate.
- **No custody.** The burn is an issuer-authority `NFTokenBurn` (the app never
  holds the user's token); minted NFTs are delivered via the same gift
  sell-offer (`create_nft_offer`, amount `0`, `Destination=wallet`) the bulk
  path uses, accepted by the user in Xaman.

## Design

Three independent seams: **verify+burn preflight**, **cap-exempt fulfillment**
(reuses the mint path), and a **service endpoint + client**.

### 1. Entitlement gate (`lfg_core/entitlement.py`)

Fill `build_burn_entitlement` so it constructs a `BurnEntitlement` **only** from
a non-empty, confirmed-burn id list, asserting the invariant the CodeRabbit gate
demands:

```python
def build_burn_entitlement(quantity: int, burn_nft_ids: list[str]) -> BurnEntitlement:
    if quantity < 1 or quantity != len(burn_nft_ids) or not all(burn_nft_ids):
        raise ValueError("burn entitlement requires quantity == confirmed burn count")
    return BurnEntitlement(quantity=quantity, burn_nft_ids=list(burn_nft_ids))
```

`from_dict` stays as-is (used by resume of an already-burned, durable record).
The public API never accepts a caller-supplied entitlement — the only path to a
`BurnEntitlement` is the flow below, *after* it has burned the tokens.

### 2. Burn-to-mint flow (`lfg_core/burn_mint_flow.py`, new)

A dedicated durable job, deliberately kept **out** of the payment-critical
`BulkMintJob` (lower regression risk on the money path). It reuses the genuinely
shared, stable pieces:

- `mint_flow.mint_one_unit` / `mint_flow._allocate_nft_number` — the shared mint
  code path (the reuse #220 explicitly calls for).
- `bulk_mint_flow.Unit` and its `PENDING/MINTED/OFFERED/UNIT_FAILED` states.
- `mint_credits.add_credit` — the "owed but unmintable" durable tail.
- `xrpl_ops.burn_nft` / `get_account_nfts` / `nft_info` — burn + on-ledger verify.

`BurnMintJob` shape (mirrors `BulkMintJob`'s durability discipline, own record
dir `BURN_MINT_JOBS_DIR`, atomic tmp-file + `os.replace` persist):

```python
@dataclass
class BurnTarget:
    nft_id: str
    state: str = "pending"        # pending -> burned | failed
    burn_tx: str | None = None

class BurnMintJob:
    id, discord_id, wallet_address, platform, push_user_token, return_url
    network = config.XRPL_NETWORK
    created_at
    state: VERIFYING | BURNING | FULFILLING | DONE | FAILED
    targets: list[BurnTarget]     # the M requested nft_ids
    units: list[Unit]             # built once burns confirmed; len == confirmed burns
    entitlement: BurnEntitlement | None
```

**States & fail-safe ordering:**

1. **`VERIFYING` (synchronous, in the start handler, before any durable
   record).** For every requested `nft_id`, verify on-ledger, fail-closed:
   owner == caller wallet, issuer == `config.SWAP_ISSUER_ADDRESS`, burnable flag
   set, not already burned. `nft_info` (clio) is authoritative; an indeterminate
   lookup (`None`) rejects the whole request. **All-or-nothing:** if any target
   fails verification the request is refused with `400 ineligible_nfts` **before
   a single burn** — a bad request can never leave the user with partial burns.
   Also enforce `1 <= M <= BURN_MINT_MAX` and dedupe the id list.

2. **`BURNING`.** Persist the record (targets all `pending`) **before** the
   first burn so a crash is recoverable. Then, per target: re-verify ownership
   on-ledger immediately before the burn (double-spend guard — a token
   transferred/burned since step 1 is skipped as `failed`), call
   `xrpl_ops.burn_nft(nft_id, owner=wallet, platform=platform_for_surface(...))`,
   set `state="burned"`/`burn_tx`, and **persist after each burn**. On resume,
   a target already gone from the owner (or `is_burned`) is treated as
   `burned` (never re-burned) — burns are naturally idempotent (a burned token
   can't be burned twice).

3. Once all targets are resolved: `quantity = count(state=="burned")`; build
   `units = [Unit(index=i) for i in range(quantity)]`; set
   `entitlement = entitlement.build_burn_entitlement(quantity, burned_ids)`
   (the ONLY construction site → cap-exemption now trails a verified burn);
   transition **`FULFILLING`** and persist.

4. **`FULFILLING`.** A trimmed, cap-exempt-only copy of `bulk_mint_flow`'s
   fulfillment loop: for each `PENDING` unit call `mint_flow.mint_one_unit`
   (`on_mint` persists `MINTED` before the offer step, closing the resume
   double-mint window exactly as bulk does). A unit that mints but whose gift
   offer fails stays `MINTED` and is re-offered (`_ensure_offer`, mint-free) on
   the final pass / on resume. A unit that never mints after `_UNIT_MAX_ATTEMPTS`
   converts to a durable `mint_credits` row — the user keeps a redeemable mint,
   never a loss. **No headroom calls at all** (cap-exempt). Completion is
   conditional (all units `OFFERED`/`UNIT_FAILED`) → `DONE`; otherwise stay
   `FULFILLING` (resumable), same rule bulk uses so a minted-but-unoffered NFT is
   never stranded.

**Resume:** `burn_mint_flow.load_all_resumable()` returns `BURNING`/`FULFILLING`
records; wired into service startup next to `resume_bulk_jobs`
(`_start_bulk_resume` sibling). A `BURNING` resume continues burning
not-yet-burned targets (idempotent); a `FULFILLING` resume re-offers/mints owed
units, never re-burns. `DONE`/`FAILED` are terminal and never resumed.

**Fail-safe summary (issue's explicit ask):**
- *Burn ok, mint fails:* after retries → `mint_credits` (owed mint preserved).
- *Some burns fail (transfer/tec):* those targets are `failed`, `quantity` =
  confirmed burns, user is owed exactly what they actually burned.
- *Crash mid-burn:* per-burn persist + on-resume chain re-derivation → no
  re-burn, no lost entitlement.
- *Crash post-payment analog:* there is no payment; the durable record + credits
  tail guarantee M-in ⇒ M-out.

### 3. Service endpoint (`lfg_service/app.py`)

- `POST /api/mint/burn` `{ "nft_ids": ["...", ...] }` — `@require_auth`, wallet
  resolved like `handle_bulk_mint_start`. One-active-job-per-user guard
  (its own `burn_sessions` registry mirroring `bulk_sessions`, or a shared
  active check keyed on the user) prevents concurrent burn jobs / re-submitting
  the same ids. Runs `VERIFYING` synchronously (returns `400 ineligible_nfts`
  with per-id reasons, or `400 invalid_quantity` / `409 already in progress`),
  registers the job, launches `run_burn_mint_job` as a background task, returns
  `job.to_dict()`.
- `GET /api/mint/burn/{session_id}` — status poll (`to_dict`: state, targets,
  units, `minted`/`offered` counts).
- `GET /api/mint/burn/active` — the caller's live burn job or null.
- `POST /api/mint/burn/{session_id}/units/{index}/accept` — build the XUMM accept
  payload for one offered unit **on click** (never M payloads up front — the
  open-payload cap, #260), reusing the `handle_bulk_mint_unit_accept` pattern
  verbatim.

Kept **separate** from `/api/mint/bulk` on purpose: the job shapes differ
(`targets[]` vs a payment), and merging would break the existing bulk client.

### 4. Client (`webapp/client/`)

An Activity affordance to pick M owned live characters (roster already available
via `/api/market/mine` `unlisted_characters` / the index), POST them, and poll
`/api/mint/burn/{id}` rendering burn-then-mint progress and per-unit accept
buttons. Feature-flag it behind an env-gated UI toggle mirroring
`BULK_MINT_UI_ENABLED` (server endpoints stay live regardless). **Any `app.js`
change bumps the `?v=` cache-buster in `webapp/client/index.html` in the same
commit.**

### On-ledger tx shapes

- **Burn:** `NFTokenBurn { Account: SIGNING_ACCOUNT, NFTokenID, Owner: wallet,
  SourceTag: 2606160021, Memos: [initiator=backend, platform=<surface>,
  action=burn] }` — exactly what `xrpl_ops.burn_nft` already emits.
- **Mint + gift offer:** unchanged from `mint_flow.mint_one_unit` /
  `xrpl_ops.mint_nft` (`SourceTag` + `action=mint` memo) and
  `xrpl_ops.create_nft_offer` (amount `0`, `Destination=wallet`,
  `action=create-offer`).

## Out of scope

- **XLS-56 Batch** (burn-M + accept-M under one Xaman signature) — noted in the
  issue as a future pairing once Xaman supports Batch; MVP delivers via the
  existing per-unit gift-offer accept.
- **Choosing *which* traits the fresh mints get** — burn-to-mint re-rolls random
  attributes via `traits.select_random_attributes` (the standard mint path). A
  "burn to re-roll this exact character" UX is a separate feature.
- **Trait-token / economy burns** — MVP is character→character only.
- **`BurnEntitlement.cap_exempt` in the `PaymentEntitlement` bulk path** — the
  two flows stay separate.

## Open questions / decisions for maintainer

1. **Separate module vs extend `BulkMintJob`.** This design proposes a dedicated
   `burn_mint_flow.py` to keep the money-critical bulk path untouched, at the
   cost of a trimmed fulfillment-loop copy. Alternative: thread burn through
   `BulkMintJob` (add a `BURNING` pre-state and a burn preflight replacing
   `prepare_payment`), maximizing reuse but touching the payment path. Which does
   the maintainer prefer?
2. **Per-request M cap.** Reuse `BULK_MINT_MAX` (default 10) or add a distinct
   `BURN_MINT_MAX`? Burn-to-mint is free, so a higher cap may be desirable.
3. **Eligibility of *other* app-held states.** Should a character currently
   **listed** on the marketplace (has a live sell offer) or a **blank/harvested**
   character be burnable-to-mint, or excluded? (A live sell offer survives a burn
   as a dangling object; leaning toward excluding listed/blanked characters.)
4. **Feature-flag / rollout.** Gate behind a new env flag (e.g.
   `BURN_MINT_ENABLED`) for staging-first rollout, matching the economy/bulk-UI
   posture?
5. **Rarity accounting.** Fresh mints re-roll traits and go through the normal
   rarity feedback (`lfg_core/rarity.py`); the burned editions' trait counts are
   not decremented. Acceptable, or should burns feed back into rarity shares?

## Testing

- **Unit — verification (`tests/test_burn_mint_flow.py`, env-guard preamble):**
  reject a target with wrong owner / wrong issuer / non-burnable flag /
  already-burned / indeterminate `nft_info` (fail-closed); all-or-nothing (one
  bad id refuses the whole request, zero burns issued — assert `burn_nft` not
  called). Dedupe + `1..BURN_MINT_MAX` bound.
- **Unit — entitlement gate:** `build_burn_entitlement` raises on empty /
  quantity-mismatch / falsy id; succeeds on a matched confirmed list and yields
  `cap_exempt=True`. `from_dict` round-trips a burn entitlement.
- **Unit — burn ordering & double-spend:** with fakes for `burn_nft`/`nft_info`,
  a target transferred between the up-front verify and the per-target burn is
  skipped as `failed` and `quantity` reflects only confirmed burns.
- **Unit — fulfillment fail-safe:** mock `mint_one_unit` to fail all attempts →
  unit becomes a `mint_credits` row (assert credit written, no headroom touched);
  mint-ok/offer-fail leaves `MINTED` and the final pass re-offers to `OFFERED`.
- **Unit — cap-exemption:** patch `headroom.try_reserve`/`reserved_for` to raise
  and assert the burn-mint fulfillment never calls them (cap-exempt path).
- **Unit — resume:** a `BURNING` record with 2/5 burned resumes without
  re-burning the 2 (chain shows them gone → counted); a `FULFILLING` record with
  a `MINTED` unit resumes to `_ensure_offer`, never re-mints.
- **Integration (`webapp` smoke / service):** `POST /api/mint/burn` happy path
  end-to-end against fakes → `DONE` with M offered units; `409` on a second
  concurrent burn; `400` on ineligible ids; unit accept builds a XUMM payload.
- **Manual smoke (testnet):** register wallet, mint (or already own) 2 burnable
  characters, run burn-to-mint via the Activity, confirm 2 `NFTokenBurn` +
  2 `NFTokenMint` on-ledger each carrying `SourceTag 2606160021` and the
  burn/mint memos, accept both gift offers in Xaman, and verify live-edition
  count is unchanged (net-zero) and `MAX_COLLECTION_SIZE` was never consulted.
