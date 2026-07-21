# Fire-and-forget stacked harvests — design

**Date:** 2026-07-21
**Status:** approved (brainstorm session)

## Problem

Harvest in the Activity is slow and blocking:

1. `harvestActive()` (`webapp/client/app.js`) awaits `pollEconomyOp` to
   completion (~10–20 s of on-chain time), then unconditionally
   `showPanel('dressup-panel')` + re-selects a character — if the user
   navigated elsewhere (e.g. into a mint), they are yanked back.
2. The server's `_economy_post` gate (`lfg_service/app.py`) 409s
   ("an economy action is already in progress") while **any** prior economy
   session for the user is non-terminal, so consecutive harvests cannot stack.

Harvest should be fire-and-forget: start it, keep using the app, get a
non-blocking notification when it lands.

## Constraints discovered

- Every harvest performs **two issuer-signed transactions**: the character
  burn and an `NFTokenModify` of the owner's single Closet token.
- `xrpl_ops._submit_and_confirm` does `autofill_and_sign` with **no lock**:
  two concurrent backend-signed txs autofill the same account sequence and one
  fails (`tefPAST_SEQ`). This is a latent cross-user race today; intra-user
  stacking makes it likely.
- The Closet token metadata update is a read-modify-write on one shared
  token per owner; unserialized concurrent harvests could drop each other's
  harvested assets.

## Design

### Client (`webapp/client/app.js`)

- `harvestActive()`: after the confirm dialog and the `POST /api/harvest`,
  register the session in a small **background-ops tracker** and return
  immediately. No `await pollEconomyOp`, no `showPanel`, no forced
  `selectCharacter`.
- The harvested character's tile is greyed / marked "harvesting…" and removed
  from the selectable set; the Harvest button re-enables immediately so
  another character can be harvested right away.
- The tracker polls each live op in the background (reusing
  `pollEconomyOp`). On terminal state it:
  - shows a non-blocking **toast** — success ("🔥 #1234 harvested — parts
    added to your Closet") or failure (error message);
  - silently refreshes `economyState`;
  - re-renders dress-up/Closet panels **only if currently visible** — it
    never calls `showPanel()`.

### Server (`lfg_service/app.py`)

`_economy_post` gains a per-kind concurrency policy:

- **harvest**: dedupe per `(user, nft_id)` — 409 only if *that* NFT already
  has a live harvest session. Multiple harvests per user stack freely.
- **equip / assemble / extract / deposit**: unchanged per-user exclusivity
  among themselves, and additionally refuse to start while any harvest is in
  flight for the user (cheap rule that avoids cross-op Closet reasoning).
  Harvests refuse to start while one of these non-harvest ops is live
  (symmetric).

### Concurrency safety (`lfg_core`)

- **Global issuer-submit lock**: an `asyncio.Lock` in `xrpl_ops` held across
  `autofill_and_sign` + `submit_and_wait` for backend-wallet submissions.
  Eliminates the sequence-collision race (fixes the latent cross-user bug
  too).
- **Per-owner Closet lock**: an `asyncio.Lock` keyed by owner around the
  Closet sync-then-persist step in the harvest flow, so concurrent harvests'
  Closet metadata read-modify-writes serialize and both asset sets land.
- Net behavior: stacked harvests **pipeline** through the single issuer
  wallet rather than run truly simultaneously on-chain (unavoidable with one
  signing account); the user never waits on any of it.

## Out of scope

- Batching multiple burns into one transaction.
- Making equip/assemble/extract/deposit fire-and-forget (they involve user
  signatures / distinct UX; can follow later).
- Multi-signing-account throughput scaling.

## Testing

- Per-kind 409 policy: same-NFT harvest 409s; different-NFT harvest stacks;
  non-harvest op blocked while a harvest is live and vice versa.
- Two concurrent `run_harvest`s for one owner: both asset sets present in the
  Closet afterwards (per-owner lock ordering).
- Issuer-submit lock: concurrent `_submit_and_confirm` calls serialize.
- Client: harvest start returns immediately, no panel switch; completion
  toast fires; visible-panel-only re-render.
