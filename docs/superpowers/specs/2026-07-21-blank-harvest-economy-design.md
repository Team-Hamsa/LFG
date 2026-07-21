# Blank-harvest economy (modify-in-place) + Assemble builder — design

**Date:** 2026-07-21
**Status:** approved (brainstorm session)

## Problem

Two user-reported problems with the current Harvest/Assemble economy:

1. **Assemble gives no choice.** The "Assemble new" tile calls
   `GET /api/assemble/prefill`, which walks the Closet's dead bodies in
   Closet order and returns the *first* fully-fillable edition with a
   first-match asset per slot. The user is locked into the lowest available
   edition and an arbitrary trait set — no edition picker, no trait builder.
2. **Harvest burns; Assemble mints.** Harvest issuer-burns the character and
   Assemble mints a fresh token + offer, forcing the user to re-accept the
   NFT in Xaman every cycle. The original product vision was that harvesting
   strips a character down while the NFT itself stays on-chain and in the
   user's wallet, and re-dressing edits it in place via `NFTokenModify`
   (Dynamic NFTs — which new mints already enable, `NFT_FLAGS = 25`).

## Decisions (user-confirmed)

- **Fully blank:** Harvest strips **all 9 slots: 8 non-body traits plus Body** into the
  Closet. The harvested NFT stays in the wallet as a *blank* — a shared
  silhouette image, metadata attributes all `None`. Bodies become loose
  Closet assets; consequently **any body can be assembled onto any blank**
  (bodies are effectively swappable; this supersedes the earlier
  "bodies never change" framing).
- **Edition = persistent identity.** An NFT keeps its edition number for
  life regardless of which body it wears. The `genesis.edition_bodies`
  edition↔body binding no longer gates Assemble.
- **Legacy flag-24 (non-mutable) characters:** Harvest performs a one-time
  upgrade — today's burn path, immediately reminted as a **mutable blank
  with the same edition number** and offered back (one accept). After that
  the NFT is modify-in-place forever. No character is excluded.
- **Assemble = dress one of YOUR blanks.** The user picks a blank NFT they
  own, a body from their Closet, and a full 8-slot trait set; one
  `NFTokenModify` dresses it in place. No mint, no offer, no accept. This
  also resolves problem 1: the "which one" choice is literally "which of my
  blanks", plus a full trait builder (Phase B).

## Model

### Blank characters

- A character is **blank** iff every trait attribute (including Body) is
  `None`. Derived from the on-chain index / metadata — no new DB state.
- Blank art: one shared silhouette PNG (1080×1080), uploaded once to
  BunnyCDN under the existing image folder; every blank's metadata `image`
  points at it. Metadata name keeps the edition (`… #2297`), attributes all
  `None`.
- Blanks render as the silhouette in the Activity roster, are excluded from
  rarity leaderboards, and are skipped by "pick default character". A blank
  is still an ordinary NFT — listable/tradeable on the marketplace; a buyer
  receives a blank and can dress it.

### Harvest (mutable char)

1. Verify ownership + active Closet (unchanged gates).
2. `NFTokenModify` the character's URI to blank metadata (chain first).
3. Credit all 8 assets + the body into the Closet (Closet token modify, then
   DB mirror — existing `_sync_then_persist` phase-aware discipline and
   `ClosetError`/`ClosetMirrorError`/`ClosetIndeterminateError` taxonomy
   apply unchanged).
4. **No burn. No `supply_changes` rows** — supply-neutral by construction.

Still two issuer-signed transactions (character modify + Closet modify), so
the fire-and-forget branch's issuer-submit lock and per-owner serialization
apply unchanged.

### Harvest (legacy non-mutable char) — one-time upgrade

1. Today's burn path (issuer burn) …
2. … immediately remint the same edition as a **blank** with
   `ECONOMY_NFT_FLAGS = 25` and offer it back to the owner (one accept).
3. Closet credit as above. Write the `supply_changes` `-1`/`+1` pair for
   audit clarity (net zero).
4. Failure between burn and remint journals for recovery exactly like
   today's assemble mint-failure paths.

### Assemble

- Inputs: `nft_id` (a blank the caller owns), `body` (a Closet body asset),
  `chosen` (all 8 non-body slots from Closet assets).
- Validation (`can_assemble` rework): caller owns `nft_id`; the NFT is
  mutable and blank; Closet holds the body and every chosen asset
  (count ≥ need); every value passes the same `resolve_layer` body-affinity
  gate `start_assemble` uses today. Edition-death and `edition_bodies`
  checks are removed.
- Execution: compose image (existing `swap_compose`) → upload image +
  metadata → `NFTokenModify` URI → debit Closet (body + 8 assets) →
  DB mirror. Supply-neutral, no `supply_changes` rows.

### Closet / bodies accounting

`closet_bodies` currently stores edition numbers (the body is implied by
`edition_bodies`). With bodies decoupled from editions, the Closet must hold
bodies **by value** (e.g. `Body=Milady` like any other asset). Migration:
existing `closet_bodies` rows are converted to body-value assets via the
frozen genesis mapping (one-off migration script, idempotent). The
`closet_bodies` table is retired after migration; `closet_assets` gains
`Body` as an ordinary slot (Closet token metadata schema versioned
accordingly, listener dual-reads old/new shapes during transition).

### Supply accounting / audit

- Harvest/Assemble stop writing `supply_changes`; historical rows remain
  valid (those burns/mints really happened). Conservation becomes: census
  (live dressed traits + closet assets + trait tokens) is invariant under
  harvest/assemble; only shop mints, extract/deposit (already neutral),
  legacy upgrades (net-zero pairs), and admin burns move it.
- `scripts/audit_trait_economy.py` updated: blanks contribute zero dressed
  traits; the legacy-upgrade pair nets out; mixed pre/post history accepted.

### Listener / index

`NFTokenModify` events already flow through the listener (swap path). New
work: recognize a modify-to-blank (owner keeps token, traits → all None) and
a modify-to-dressed, updating `onchain_nfts` attributes and the economy
mirror. The legacy-upgrade burn+remint reuses existing burn/mint handling.

## API & UI (Phase B — Assemble builder)

- `GET /api/assemble/options` (wallet-authed, Closet-active gate):

  ```json
  {
    "blanks":  [{"nft_id": "0008…", "edition": 2297}],
    "bodies":  ["Milady", "Skeleton"],
    "slots":   ["Background", "Clothing", "Eyes", "Eyebrows", "Mouth", "Head", "Accessory"],
    "options": {"milady": {"Head": ["Wizard Hat", "None"], …}, …}
  }
  ```

  `options` is keyed by body class, computed only for classes of bodies the
  user holds; each slot lists closet assets (count > 0) passing
  `resolve_layer` for that body. One round-trip; Closets are small.
- `POST /api/assemble` body becomes `{nft_id, body, chosen}` (edition-based
  form removed — single client, no external API consumers).
- `/api/assemble/prefill` is retired with the old flow.
- **Builder overlay** (replaces the confirm-only `openAssemble()`):
  1. blank picker (tiles: silhouette + `#edition`; auto-selected if one);
  2. body picker (from Closet bodies);
  3. per-slot pickers over `options[bodyClass]` with a live layered preview
     (reusing `layerSrc`/`layerMediaEl`); defaults via a pure
     `buildPure.defaultChosen(slots, options)` (first legal value per slot);
  4. Assemble → existing commit/poll path (minus the accept-QR step: success
     shows the new artwork, nothing to sign).
- Harvest UI copy: mutable chars lose the "permanently burns" scare copy
  ("Strip this character down to a blank? Its parts go to your Closet.");
  legacy chars keep a burn warning reworded as a one-time upgrade with one
  Xaman accept.
- Cache-buster `?v=` bumps on `app.js` / `build_pure.js` / `style.css` in
  the same PR.

## Surfaces / announce

Telegram/Discord announce lines update: harvest → "stripped a character
down to a blank"; assemble → "dressed a blank into #N". Memos: harvest's
modify tx keeps `action=harvest`; assemble's modify keeps
`action=assemble` (schema already covers both; initiator stays `backend`).

## Concurrency / coordination

A parallel branch (`claude/harvesting-mechanism-perf-a484ee`,
fire-and-forget stacked harvests) changes harvest *delivery*: non-blocking
client tracker, per-`(user, nft_id)` dedupe in `_economy_post`, global
issuer-submit lock. It does not change what harvest does on-chain, so the
two are compatible. **This work rebases over that branch when it lands**;
Phase A deliberately keeps `harvestActive()` changes to copy-only to
minimize the merge surface.

## Phasing

- **Phase A — backend model:** `economy_flow` (run_harvest/run_assemble
  rework + legacy branch), `trait_economy` (`can_assemble`, census),
  `economy_api` (start_assemble signature, economy state exposes blanks),
  closet bodies-by-value migration + script, blank art/metadata helper,
  listener modify-to-blank/dressed handling, auditor update, mock economy,
  tests.
- **Phase B — Activity builder UI:** `/api/assemble/options`, builder
  overlay, harvest copy, roster silhouette rendering, pure helpers + tests,
  cache-busters.

Each phase is a separate PR (normal review path, Team-Hamsa/LFG rules).

## Error handling

- Modify-to-blank fails → nothing changed on-chain, session fails clean.
- Blank modify lands but Closet credit fails → existing phase-aware
  taxonomy: mirror-only failure completes `complete_pending_mirror`;
  indeterminate journals and reconciles from chain. The character being
  already-blank on retry is idempotent (re-modify to same URI or skip).
- Assemble compose/upload fails → nothing on-chain, Closet untouched.
- Assemble modify lands but Closet debit fails → same taxonomy; listener
  rebuilds mirror from Closet token; auditor catches drift.
- Race (asset spent between options fetch and commit) → server-side
  `can_assemble` rejects; client shows the error. No locking beyond the
  existing per-owner serialization.

## Out of scope

- Equip/Extract/Deposit/Shop/marketplace mechanics (unchanged).
- Batch/multi-blank assemble.
- Any change to the swap flow or SourceTag/memo schema.
- Mainnet rollout sequencing beyond "Phase A behind the existing
  `ECONOMY_ENABLED` gates" (already live; deploy is a normal promote).

## Testing

- Unit: `can_assemble` rework matrix (ownership, blankness, mutability,
  affinity, counts); blank-detection helper; census invariance under
  harvest→assemble round-trip; legacy-upgrade net-zero supply pair;
  closet-bodies migration idempotence.
- Flow tests with fake deps (existing `economy_flow` test style): harvest
  modify path incl. each failure phase; legacy burn+remint path; assemble
  modify path incl. Closet-debit failure phases.
- Listener: modify-to-blank / modify-to-dressed event application.
- Web: `assemble_options` filtering (affinity, counts, closet gate);
  mock-economy parity; smoke test for the builder endpoints.
