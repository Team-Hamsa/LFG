# Closet — standalone issuance + Bucket→Closet rename

**Date:** 2026-06-27
**Status:** Design (approved decisions; pending spec review)
**Supersedes:** PR #104 (harvest band-aid) — merge #104 first for interim
protection, then this work refactors the same path.

## Problem

The per-wallet trait container (today called the **Bucket**) is issued lazily and
post-hoc:

- It is minted on first use *inside* `run_harvest` via `bucket_token.ensure_bucket`.
- The Xaman "Claim your Bucket" accept QR is shown **after** harvest completes
  (`webapp/client/app.js:887`) — i.e. *after* the character is already burned.
- The issuer mints the soulbound token and offers it to the user. The issuer can
  `NFTokenModify` its own token, so harvest "works" even if the user never
  accepts — but then the soulbound token sits unaccepted in the issuer wallet and
  the user never physically holds it.
- A stale DB record (e.g. after a testnet reset) made the post-burn modify hit
  `tecNO_ENTRY` → irreversible asset loss (issue #101; PR #104 hardened the
  staleness check but left the buried-issuance ordering in place).

We are also renaming the concept **Bucket → Closet** end to end (user-facing,
code, DB, and on-chain metadata).

## Goals

1. Lift container issuance out of harvest into a **standalone, up-front, verified**
   step ("Create your Closet"), so no irreversible economy op can run before the
   user actually holds an accepted Closet.
2. Gate Harvest/Assemble on the Closet being **accepted by the user** (owned by
   their wallet), not merely existing on-ledger.
3. Rename **Bucket → Closet** fully: user-facing strings, code identifiers, DB
   tables, and the on-chain metadata key — with a backward-compatible read path
   and a re-mint migration for existing testnet tokens.

## Non-goals

- Mainnet rollout of the economy (still testnet Phase 2).
- Changing the soulbound model (mutable-only, non-transferable, non-burnable),
  asset/body accounting, or supply-conservation ledger.
- Touching the swap (`lfg_core/swap_flow.py`) path.

## Decisions (locked in brainstorming, 2026-06-27)

| Question | Decision |
|---|---|
| Issuance model | **Standalone, up-front** — dedicated action + endpoint |
| Confirm semantics | **Accepted by user** — `none → pending_accept → active` |
| Rename depth | **Full** — strings + code + DB + on-chain metadata key |
| On-chain migration | **New `CLOSET_TAXON` + re-mint** existing buckets |
| Entry points | Dressing Room **and** `/register` (Closet-ready before harvest) |
| Ship vs PR #104 | **Merge #104 first**, build on top |
| Plan shape | **Single plan / single PR** (internally phased tasks) |

## Closet lifecycle (state machine)

```
none ──ensure_closet (mint + offer)──▶ pending_accept ──user accepts in Xaman──▶ active
                                              │                                     ▲
                                              └──── on-demand nft_info owner==user ──┘
```

- **none** — no closet token recorded for the wallet.
- **pending_accept** — issuer minted + offered the token; not yet accepted (token
  still owned by issuer). The outstanding accept payload is retained so the UI can
  re-show the QR. Issuance is **idempotent** in this state (re-issuing returns the
  same outstanding offer).
- **active** — `nft_info(closet_id).owner == owner_wallet` (offer accepted; user
  holds the soulbound token). Harvest/Assemble unlock here.

The renamed `closet_tokens` table gains a `status` column (`pending_accept` /
`active`) and retains the outstanding `offer_id` / accept payload reference while
pending. `nft_id` + `uri_hex` carry over from today's `bucket_tokens`.

## Components

### `lfg_core/closet_token.py` (renamed from `bucket_token.py`)
- `build_closet_metadata` / `parse_closet_metadata` — emit `lfg_closet`; the
  parser **dual-reads** `lfg_closet` then legacy `lfg_bucket` (tolerant, as today).
- `ensure_closet(conn, owner, *, upload_fn, mint_fn, offer_fn, accept_payload_fn,
  exists_fn=None) -> ClosetRef` — supersedes `ensure_bucket`. Mints + offers when
  `none`, records `pending_accept`; verifies an existing record on-ledger via
  `exists_fn` (the #104 logic) and re-mints if stale; idempotent while pending.
  Returns `{ nft_id, uri_hex, status, accept_payload }`.
- `confirm_accept(conn, owner, *, closet_owner_fn) -> bool` — on-demand check that
  flips `pending_accept → active` when `closet_owner_fn(closet_id) == owner`. In
  the issuer-as-owner / headless case (`_SELF_OFFER_SKIPPED` in
  `scripts/_economy_deps.py`, where owner == the issuer and no offer/accept
  happens) the closet is recorded `active` immediately at mint.
- `sync_closet` — unchanged behavior; recomposes metadata and `NFTokenModify`s the
  URI (requires an existing record).

### `lfg_core/economy_store.py`
- Rename tables `bucket_assets/bucket_bodies/bucket_tokens → closet_assets/
  closet_bodies/closet_tokens`; add `status` (+ optional `offer_id`) to
  `closet_tokens`. Rename funcs `set_bucket_*/read_bucket_*/get_bucket_token →
  set_closet_*/read_closet_*/get_closet_token`; add `set_closet_status` /
  `get_closet_status`.

### `lfg_core/economy_flow.py`
- `EconomyDeps.bucket_* → closet_*` field renames; keep `closet_exists_fn`
  (Optional) and add a `closet_owner_fn` for accept confirmation.
- **`run_harvest` / `run_assemble` precondition:** fail with
  `"Create and claim your Closet first"` **before** any irreversible step unless
  the Closet is `active`. Remove the inline `ensure_bucket` + post-burn accept.

### `lfg_service/app.py`
- `POST /api/closet` — `ensure_closet` for the session wallet; returns
  `{ status, nft_id, accept? }`. Idempotent.
- `GET /api/economy` — add `closet: { status, nft_id, accept? }`; runs an
  on-demand `confirm_accept` so a returning user auto-promotes to `active`.
- `POST /api/harvest` / `POST /api/assemble` — return 400 with the
  closet-precondition error when not `active`.
- `handle_register` (`app.py:458`) — after a successful (Xaman-verified)
  registration, call `ensure_closet` and include the accept link in the
  registration result so wallets are Closet-ready before they ever harvest.

### `lfg_core/nft_listener.py`
- `_apply_bucket → _apply_closet`; match **both** `CLOSET_TAXON` and legacy
  `BUCKET_TAXON` during transition. On mint (owner = issuer) record
  `pending_accept`; on `NFTokenAcceptOffer` (post-transfer owner = user) mark
  `active`. Parsing uses the dual-read parser.

### `lfg_core/config.py`
- `BUCKET_TAXON/BUCKET_NFT_FLAGS/BUCKET_IMAGE_URL → CLOSET_*`. `CLOSET_TAXON` is a
  **new distinct value** (env-overridable). Keep a `LEGACY_BUCKET_TAXON` constant
  for the listener/migration transition read path.

### Surfaces (`surfaces/discord_bot`, `surfaces/telegram_bot`)
- Announce strings "harvested … into their bucket" → "… into their Closet"; any
  register confirmation surfaces the Closet accept link.

### Frontend (`webapp/client/{app.js,index.html,style.css}`)
- `Bucket → Closet` labels; `dressup-bucket/bucket-grid/...` CSS + ids →
  `closet-*`.
- Dressing Room states: **[ Create your Closet ]** when `none`,
  **[ Finish claiming your Closet ]** (re-show accept QR) when `pending_accept`,
  **Harvest/Assemble** enabled only when `active`. Remove the post-harvest
  "Claim your Bucket" step (`app.js:887`).

### `webapp/mock_economy.py` / `webapp/economy_api.py`
- Mirror the `closet` state block + statuses so `WEBAPP_DEV_MODE` exercises the
  new states.

## On-chain migration

`scripts/migrate_bucket_to_closet.py` (idempotent, per-network):
- For each existing `bucket_tokens` row: read the old token's contents, mint a new
  Closet under `CLOSET_TAXON` with `lfg_closet` metadata, offer it to the owner,
  copy the contents into the new closet record (`pending_accept`), and record the
  abandoned old soulbound token (flags 16 → non-burnable, left in place).
- Testnet-only and likely near-empty after the recent reset; the script exists for
  completeness and mainnet-readiness.

## DB migration

The per-network index DBs (`onchain_testnet.db` / `onchain_mainnet.db`) are
gitignored + regenerable from chain. `init_economy_schema` creates the `closet_*`
tables; a one-time in-place copy moves any existing `bucket_*` rows, after which
the listener keeps them fresh. No genesis/supply change.

## Testing (TDD)

- State machine: `none/pending_accept/active` transitions; `ensure_closet`
  idempotency while pending; stale re-mint (reuse #104 fakes).
- `confirm_accept` flips pending→active only when owner == user.
- Harvest/Assemble **reject before any irreversible op** when closet not active;
  succeed when active.
- Listener: mint→`pending_accept`, AcceptOffer→`active`; matches both taxons.
- Dual-read parser: `lfg_closet` and legacy `lfg_bucket` both parse.
- Migration script: re-mint + content copy + abandoned-token record; idempotent.
- Mock economy parity for `WEBAPP_DEV_MODE`.
- Existing suites stay green after the rename (mechanical identifier changes).

## Relationship to PR #104

Merge #104 first (interim asset-loss protection). This feature refactors
`ensure_bucket → ensure_closet`, reuses its `nft_info`-based `exists_fn` verify
logic, and makes the standalone `active` gate the real fix — the buried
mint-on-first-use + post-burn accept is removed.

## Risks

- **Large diff** (single PR, ~40 files touched by the rename). Mitigation: the
  rename is mechanical and behavior-preserving; isolate it as the first set of
  tasks in the plan so the behavior changes review cleanly on top.
- **Listener transition window**: must match both taxons / both metadata keys
  until migration completes, else existing buckets vanish from the index.
- **Register-path issuance** adds a Xaman accept to the registration UX on two
  surfaces; keep it non-blocking (registration succeeds even if the user defers
  the accept — closet stays `pending_accept`).
