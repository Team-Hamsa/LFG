# Dress-up Phase 4 — Hybrid tradeable trait NFTokens (Extract / Deposit)

**Date:** 2026-06-27
**Status:** Design (approved decisions; pending spec review)
**Issue:** #66 (part of epic #46, milestone "Dress-Up Trait Economy")
**Builds on:** Phase 1–3 (Closet/economy flows, listener, Activity UI) and the
Bucket→Closet rename (#105).

## Problem

Loose traits in a user's Closet are currently trapped — they can only be
re-assembled or equipped, never traded. Phase 4 is the **hybrid escape hatch**:

- **Extract** a loose Closet trait into a standalone, tradeable NFToken (removed
  from the Closet).
- **Deposit** a standalone trait NFToken back into a Closet (token burned, trait
  credited).

The dormant `trait_tokens` table (created empty in Phase 1) goes live, and
supply conservation must continue to hold.

## Core principle — supply-neutral

A trait has three possible homes, and `lfg_core/trait_economy.py::asset_census`
**already tallies all three**: on a live character, loose in a Closet, and as a
standalone trait NFToken (`asset_census` iterates `trait_tokens`). Therefore:

- **Extract** moves a trait Closet→token; **Deposit** moves it token→Closet.
- The census is invariant across both, so **no `supply_changes` rows are
  written** — identical to Equip. The auditor (`scripts/audit_trait_economy.py`,
  which already reads `read_trait_tokens`) keeps balancing with no changes.

Conservation is collection-wide: a trait token traded to a third party still
counts in the census under that owner; an owner's Closet shrinks but the trait
exists somewhere on-ledger.

## Decisions (locked in brainstorming, 2026-06-27)

| Question | Decision |
|---|---|
| Deposit custody | **Issuer-burn after verify** — verify on-ledger owner == depositor, then issuer `NFTokenBurn` (the token is burnable-by-issuer) |
| Trait token model | **Burnable + transferable** (`TRAIT_NFT_FLAGS = 9`), **not** mutable, new `TRAIT_TAXON`; royalty = the collection's `NFT_TRANSFER_FEE` (70%) |
| Op cost | **Free** (MVP, consistent with harvest/assemble/equip) |
| Surfaces | **Full vertical slice**: flows + CLI + service + Activity UI |
| Trait image | **Bare trait layer** (transparent PNG) |
| Plan shape | **Single plan → single branch → single PR** (like the Closet) |

## The trait NFToken

- New `config.TRAIT_TAXON` (distinct value), `config.TRAIT_NFT_FLAGS = 9`
  (burnable(1) + transferable(8); **not** mutable, **not** soulbound).
- **Royalty:** none added in config. `xrpl_ops.mint_nft` already stamps
  `config.NFT_TRANSFER_FEE` (7000 = 70%) on **any transferable** token, so a
  trait minted with flags 9 inherits the 70% transfer fee automatically. No
  `TRAIT_TRANSFER_FEE` constant is needed.
- **Metadata:** an `lfg_trait` block `{ "slot": <slot>, "value": <value> }`, plus
  `name` ("LFG Trait — <slot>: <value>"), `image`, `schema`, `external_link`,
  and a `collection` sub-name ("LFG Traits"). `parse_trait_metadata(meta) ->
  (slot, value) | None` mirrors `parse_closet_metadata` (tolerant of garbage).
- **Image:** the single trait layer rendered to a transparent PNG, uploaded to a
  `traits/` folder under `ECONOMY_CDN_FOLDER`. A `trait_compose_fn` resolves the
  one layer via `layer_store` and emits the PNG (no body composite).
- All mint/offer/accept/burn carry `SourceTag 2606160021` (via `xrpl_ops`).

## Extract flow (`lfg_core/extract_flow.py`)

An async state machine shaped like `run_assemble` (mint is reversible by
burn-back), with on-disk journaling to `ECONOMY_RECORDS_DIR`.

`ExtractSession(owner, slot, value)`. Order:
1. **Precheck:** `_require_active_closet(deps, owner)` (the shared gate from the
   Closet work) **and** the owner's Closet holds `(slot, value)` with count ≥ 1
   (read from `closet_assets`). Fail before any chain effect otherwise.
2. **Compose + upload** the bare trait image + trait metadata.
3. **Mint** the trait token under `TRAIT_TAXON`, flags 9 (reversible: burn-back).
4. **Decrement the Closet** `-1 (slot,value)` via `sync_closet` (token-then-DB),
   and `upsert_trait_token(nft_id, owner, slot, value)`.
   - If the Closet sync fails after the mint: **burn the mint back** (Closet
     untouched), journal `reverted_mint`. If the compensating burn also fails,
     keep the nft_id in the journal for an admin (mirrors `run_assemble`).
5. **Offer + Xaman accept** of the new token to the owner (self-issuer-skip for
   headless runs, per `_economy_deps`).

Result carries the new `nft_id` and the accept payload.

## Deposit flow (`lfg_core/deposit_flow.py`)

An async state machine shaped like `run_harvest` (burn is irreversible; credit
follows with journaled recovery).

`DepositSession(owner, nft_id)`. Order:
1. **Precheck:** `_require_active_closet(deps, owner)` (Closet to receive into);
   the token is **ours** — `nft_info(nft_id)` reports `taxon == TRAIT_TAXON` and
   `issuer == SWAP_ISSUER_ADDRESS`; its metadata yields a valid `(slot, value)`;
   and the **on-ledger owner == depositor** (fail-closed: `None`/mismatch refuses
   the burn — no asset loss). Reject foreign/garbage tokens with a clear error.
2. **IRREVERSIBLE: issuer burns** the trait token (`trait_burn_fn`).
3. **Credit the Closet** `+1 (slot,value)` via `sync_closet` (token-then-DB) and
   `delete_trait_token(nft_id)`.
   - If the credit fails after the burn: journal `deposited_pending_closet` with
     the burned nft_id + `(slot,value)` for recovery (the trait is never silently
     lost), mirroring harvest's burn-then-deposit-failed path.

## Listener — `trait_tokens` goes live

`lfg_core/nft_listener.py::apply_economy_tx` gains a `TRAIT_TAXON` branch and now
also processes the `burn` kind (today: mint/modify/accept):

- **Mint** of a `TRAIT_TAXON` token → `upsert_trait_token` from metadata
  `(owner, slot, value)`.
- **AcceptOffer** (transfer) → `upsert_trait_token` with the post-transfer owner
  (resolved via `nft_info`), updating ownership as the token trades.
- **Burn** → `delete_trait_token(nft_id)`.

New `economy_store` accessors: `upsert_trait_token(conn, nft_id, owner, slot,
value)` and `delete_trait_token(conn, nft_id)` (the `trait_tokens` table + its
`read_trait_tokens` already exist). The DB is the accounting mirror; the token's
on-chain metadata + ownership is the source of truth the listener rebuilds from.

The live `scripts/onchain_listener.py` dispatch filter must include `burn` (and
already includes `accept` after #105) so trait transfers/burns reach the handler.

## Service + economy state

- `POST /api/extract { slot, value }` → `start_extract`; `POST /api/deposit
  { nft_id }` → `start_deposit`. Both **gated on an active Closet** (surface 400
  `"Create and claim your Closet first."` before starting, plus the flow's own
  precondition), and routed through the existing `_economy_post` session
  machinery (poll to terminal, accept payload surfaced for extract).
- `GET /api/economy` gains `trait_tokens: [{ nft_id, slot, value }]` — the
  caller's standalone traits (from `read_trait_tokens` filtered to the wallet) so
  the UI can list deposit candidates.
- `scripts/_economy_deps.py` wires `trait_mint_fn` (mint TRAIT_TAXON, flags 9),
  `trait_burn_fn` (`xrpl_ops.burn_nft`), `trait_compose_fn` (bare layer), reusing
  `closet_owner_fn`/`nft_info` for ownership checks and the existing offer/accept
  callables. `EconomyDeps` gains these fields (Optional where it keeps existing
  test constructions valid).
- `webapp/mock_economy.py` mirrors `trait_tokens` state + extract/deposit for
  `WEBAPP_DEV_MODE`.

## Activity UI (Dressing Room)

- Each loose Closet trait tile gains an **Extract** action (→ `POST /api/extract`,
  then surface the Xaman accept QR via `showFlow`).
- A new **"Your tradeable traits"** strip lists the wallet's standalone trait
  tokens, each with a **Deposit** action (→ `POST /api/deposit`).
- Both are gated behind the active Closet (reuse the existing gate); the strip is
  populated from `economyState.trait_tokens`.

## CLI

- `scripts/economy_extract.py --network --owner --slot --value`
- `scripts/economy_deposit.py --network --owner --nft-id`

Both mirror `scripts/economy_harvest.py`'s structure (argparse, `_economy_deps`,
print state/error/accept).

## Testing (TDD)

- **Extract flow:** happy path (mint + Closet decrement + `trait_tokens` insert,
  census unchanged); Closet-sync-fails → mint burned back, Closet untouched;
  precheck rejects when no active Closet or the loose trait is absent.
- **Deposit flow:** happy path (verify → burn → Closet credit + row delete,
  census unchanged); burn-then-credit-fails → journaled `deposited_pending_closet`;
  rejects a foreign/garbage token (wrong taxon/issuer/metadata); fail-closed when
  on-ledger owner ≠ depositor (no burn).
- **Conservation:** an extract followed by a deposit round-trips the census to its
  original value; the auditor reports zero drift.
- **Listener:** trait-token mint→insert, accept→owner update, burn→delete; the
  `onchain_listener` dispatch includes `burn`.
- **Service:** extract/deposit gated 400 when Closet not active; economy state
  includes `trait_tokens`.
- **Mock parity** for `WEBAPP_DEV_MODE`; **CLI smoke** imports.

## Non-goals

- Paid extract/deposit (free MVP).
- A trait marketplace/DEX UI (trait tokens are tradeable on existing XRPL
  marketplaces; in-app marketplace is #44, separate).
- Mutable trait tokens (value is fixed at extract time).
- Changing the Closet soulbound model or genesis/supply accounting.

## Risks

- **Issuer-burn power:** the trait token is burnable-by-issuer, so Deposit's
  issuer-burn works for any holder. The flow verifies on-ledger ownership before
  burning; a holder could transfer the token in the gap between verify and burn
  (testnet-acceptable; the burn simply destroys whatever the verified holder
  presented). Fail-closed on any ownership uncertainty.
- **Listener ordering during transfers:** trait ownership must track AcceptOffer;
  a missed event leaves a stale owner in `trait_tokens` (rebuildable from chain).
- **70% transfer fee** is steep for a trait secondary market; it's inherited from
  `NFT_TRANSFER_FEE` per the locked decision and is env-configurable later.
