# Build panel: batched Save instead of per-click equip

**Date:** 2026-07-21
**Status:** Design approved, ready for planning

## Problem

In the Build (Dressing Room) panel, clicking a compatible Closet tile fires an
equip immediately: `renderCloset()` wires the tile straight to `equipTrait()`
(`webapp/client/app.js`), which POSTs `/api/equip` and drives one
`NFTokenModify` plus one Closet swap per click (`lfg_core/economy_flow.run_equip`).

Dressing a character therefore costs one on-ledger transaction per trait. A user
trying four traits pays four modifies, four Closet syncs, and sits through four
poll cycles — and each intermediate state is permanently on-chain even though
only the final look was ever wanted.

## Goal

Clicking a tile stages a change locally. A **Save** button surfaces while
changes are pending. Saving commits every staged change in **one**
`NFTokenModify` and **one** Closet sync.

## Scope

In scope: N slot swaps on the **currently-selected character**, where each swap
replaces that slot's value with a loose Closet asset — exactly what a tile click
does today, deferred.

Out of scope (YAGNI):

- Unequip-to-`None` (`trait_economy.can_equip` has no `None` path today).
- Batching across multiple characters (needs one `NFTokenModify` per character
  regardless — no transaction saved, much more state).
- Drag-and-drop, and any persisted "draft" of pending changes. Pending state is
  in-memory only.

Only the Activity / web client drives equip. Discord and Telegram merely render
`equip.completed` / `equip.failed` announcements, and the `_client` SDK's
`equip_start` has no surface caller — so no surface work is required.

## Approach

Generalize the existing `EquipSession` / `run_equip` to carry a **list** of
changes, keeping the same endpoint, session kind, and fail-safe ordering.

Rejected alternatives:

- **New `/api/build/save` + `BuildSession`.** Cleaner naming, but duplicates the
  most safety-critical ~150 lines in the repo (partial-failure ordering,
  journaling, the `#107` phase-aware sync taxonomy) and leaves a second live
  path free to drift from the first.
- **Client loops `/api/equip` on Save.** No server change, but it is still N
  transactions — the thing being removed.

## Server

### Wire format

`POST /api/equip` accepts:

```json
{"nft_id": "0008...", "changes": [{"slot": "Hat", "value": "Wizard Hat"},
                                  {"slot": "Eyes", "value": "Laser"}]}
```

The legacy `{"nft_id": ..., "slot": ..., "value": ...}` shape is normalized to a
one-element list by the handler, so the `_client` SDK's `equip_start(user_id,
body)` and any in-flight client keep working across the deploy.

Compatibility is **wire-level only**. In Python there is exactly one shape:
`EquipSession(changes=[...])` and `start_equip(..., changes)`. Callers inside the
repo (`scripts/economy_equip.py` and the existing tests) are migrated to it
rather than carrying a dual-signature shim, so no second code path exists to
drift.

Rejected with 400:

- empty `changes`
- a `slot` appearing more than once in one batch (the client sends net changes,
  one entry per slot)
- more entries than there are non-body slots (`len(trait_economy.NON_BODY_SLOTS)`)

Session kind, the `/api/equip/{session_id}` poll, the 409 "an economy action is
already in progress" guard, push-token wiring, and the `equip.completed` /
`equip.failed` announcements are all unchanged.

### `EquipSession`

`lfg_core/economy_flow.EquipSession` replaces the scalars
`slot` / `incoming_value` / `displaced_value` with:

- `changes: list[tuple[str, str]]` — ordered `(slot, incoming_value)` pairs
- `displaced: dict[str, str]` — slot → the value that change pushed out

The journal record replaces those three scalars with
`"changes": [{"slot": ..., "incoming": ..., "displaced": ...}, ...]`. `op`,
`modify_hash`, `sync_tx_hash`, `mirror_pending`, and every status string are
unchanged, so the operator recovery table in the `economy_flow` module docstring
continues to apply verbatim.

`economy_api.economy_session_dict` emits `displaced` as
`[{"slot": ..., "value": ...}, ...]`. No client code reads the field today.

### `run_equip`

The ordering is unchanged. Three steps simply loop:

1. **Precheck** every change against one accumulating copy of the owner's
   assets: `trait_economy.can_equip(rec, slot, value, working, mutable=...)`,
   then apply `−incoming / +displaced` to `working` before the next change. Any
   change failing its precheck aborts the whole batch before the character is
   touched.

   Closet assets are keyed `(slot, value)` and a slot may appear at most once
   per batch (duplicates are rejected), so the changes are independent — the
   accumulation exists to build the single asset dict handed to the one Closet
   sync in step 4, not to catch cross-change interference.
2. **`new_attrs`** applies every change at once → **one** `char_compose_fn` call,
   one metadata URL.
3. **One `NFTokenModify`** via `char_modify_fn` — still the single reversible
   point of no return.
4. **One `_sync_then_persist`** carrying every `−incoming / +displaced` delta.

Failure branches are untouched:

- `bt.ClosetMirrorError` → `complete_pending_mirror`, no on-chain compensation
- `bt.ClosetIndeterminateError` → `equip_sync_indeterminate`, fail-closed
- ledger-failed → a **single** modify-back to `_raw_uri(rec.uri_hex)`, which
  restores every slot at once because it is a whole-URI revert. Batching makes
  this branch simpler, not harder.
- a falsy revert hash still yields `failed_revert`

### Other server touchpoints

- `webapp/economy_api.start_equip` mirrors the precheck loop over a working
  asset copy and calls `_require_body_affinity(rec.body, slot, value)` once per
  change.
- `webapp/mock_economy.equip` applies the list, for `WEBAPP_DEV_MODE`.
- `lfg_service/app.py`'s `handle_equip_start` lambda and its mock lambda pass the
  normalized body through.
- `scripts/economy_equip.py` gains a repeatable `--set Slot=Value` flag while
  keeping `--slot/--value` for the single-change case.

## Client

### Pending state

Two module-level vars in `webapp/client/app.js`:

- `pendingEquips` — `{slot: incomingValue}`
- `pendingFor` — the `nft_id` the batch belongs to, so a stale batch can never
  be attributed to the wrong character

Both are cleared on save, on discard, and on character switch.

### Pure logic in `build_pure.js`

Node-testable, same split as `mint_pure.js` / `market_pure.js`:

- `applyPending(attributes, pending)` → the attribute list `renderCanvas` draws.
- `effectiveAssets(closetAssets, character, pending)` → Closet counts with
  `−incoming / +displaced` applied per pending slot: entries reaching 0 are
  dropped, and a displaced value the Closet did not already hold is synthesized
  as a new entry so it can be clicked back on. `None` is never materialized as a
  tile.
- `netChanges(character, pending)` → the `changes` array for the POST, omitting
  any slot whose pending value equals the character's current on-chain value.

`netChanges` gives undo for free: re-clicking the character's original trait for
a dirty slot nets to zero and that slot leaves the batch.

### Interaction

A compatible tile click calls `stagePendingEquip(slot, value)`: record the
change, `renderCanvas(applyPending(...))`, `renderCloset()` — no network call.
The optimistic-update-then-revert dance inside `equipTrait` is deleted; the
canvas simply renders pending state.

Closet counts are live-optimistic while dirty (chosen over static counts): a
staged asset's `×count` drops by 1, the trait it displaced appears immediately,
and a user holding `×1` of a trait cannot appear to equip it into two slots.

### Save bar

Lives in `.dressup-stage` under the canvas, `hidden` unless `netChanges` is
non-empty. Two controls: `💾 Save changes (N)` and `Discard`.

Save POSTs the whole batch to `/api/equip` and polls via the existing
`pollEconomyOp('equip', …)`. Save is disabled while a save is in flight.

- On `done`: clear pending, refetch `/api/economy`, re-select the character.
- On `failed`: **also** clear pending and resync from `/api/economy` before
  showing the error. The indeterminate and mirror-pending branches can leave the
  character genuinely changed, so silently re-offering the same batch for a retry
  would risk a double-apply. The user re-stages from authoritative truth.

### Guards while dirty

- Character switch, Back, Assemble, and Harvest each route through the existing
  in-app `confirmDialog`: "You have unsaved changes to #1234. Discard them?"
  Cancel stays put; OK discards and proceeds. Native `window.confirm` is a
  silent no-op inside Discord's sandboxed iframe and must not be used.
- Extract and Deposit buttons are disabled while dirty, titled "Save or discard
  your changes first" — both mutate the very Closet counts the batch is computed
  against.

## Testing

Server (`tests/test_economy_flow_equip.py`, which already fakes `EconomyDeps`):

- multi-change happy path asserts **exactly one** `char_compose_fn`, **one**
  `char_modify_fn`, and **one** `_sync_then_persist` call, and that the synced
  asset dict carries every delta
- a batch whose second change is not in the Closet fails precheck with the
  character untouched and the Closet unchanged
- duplicate slot / empty list / oversized list are rejected
- the existing single-change failure-path tests pass unmodified via the legacy
  shape — the regression net for the fail-safe ordering

Also: `webapp/test_economy_api.py` for per-change affinity rejection;
`webapp/test_smoke.py` for both request shapes through `handle_equip_start`;
`webapp/test_mock_economy.py` for the dev-mode mock applying a list.

Client (`tests/test_build_pure_js.py`): `applyPending`; `effectiveAssets`
(decrement, zero-drop, synthesized displaced tile, `None` never materialized);
`netChanges` (undo-to-empty, multi-slot).

## Rollout

No schema change, no migration, no feature flag — client and server ship
together.

`index.html`'s `app.js?v=` **and** the `build_pure.js?v=` import inside `app.js`
must both be bumped in the same commit. A stale cached `build_pure.js` paired
with a new `app.js` is exactly the failure mode that broke the bulk-mint quantity
stepper on 2026-07-21.
