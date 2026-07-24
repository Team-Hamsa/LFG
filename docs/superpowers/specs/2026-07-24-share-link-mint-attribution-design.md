# Share-Link Mint Attribution — design

**Date:** 2026-07-24
**Status:** draft (triage — needs maintainer review)
**Issue:** #273

## Problem

The X share-card forwarding work (`docs/superpowers/specs/2026-07-17-x-share-forwarding-design.md`)
shipped the *front half* of the funnel:

- The Activity share button appends `?ref=<sharer wallet>` (`shareUrlFor` in
  `webapp/client/app.js`).
- `handle_nft_card` (`lfg_service/app.py`) validates the `ref` shape and logs a
  row to the `share_clicks` table (`lfg_core/share_clicks.py`: `nft_number`,
  `ref_wallet`, `is_bot`, `user_agent`, `clicked_at`) — one row per card hit.
- The webapp client stashes a valid-shaped ref in `localStorage['lfg_ref']` on
  load (`main()` in `webapp/client/app.js:3751`).

The *back half* is missing. The stashed `lfg_ref` is **never sent at mint
time**, so there is no join between a click (visit) and a mint (conversion). The
mint POST body (`startMint` → `POST /api/mint` with `discordCtx()` only,
`webapp/client/app.js:1215`) carries no referrer; `handle_mint_start`
(`lfg_service/app.py:3330`) never reads the request JSON body; `MintSession`
(`lfg_core/mint_flow.py:59`), `mint_one_unit` (`:352`), and `record_nft_mint`
(`lfg_core/db_helpers.py:61`, INSERT into the `LFG` table) have no referrer
field. The result: `share_clicks` shows who *drove clicks* but nothing shows
whose shares *converted to mints* — the whole point of attribution.

The parent spec §3 explicitly deferred this ("Full mint attribution … deserves
its own review") — this is that review.

## Constraints discovered

- **No on-ledger transaction is added.** Attribution is pure app-DB
  bookkeeping — a nullable column on the mint record plus a read-only metrics
  query. The `SourceTag = 2606160021` + provenance-memo requirement therefore
  does **not** apply here (there is no new tx to stamp); the existing mint tx is
  untouched.
- **The ref is user-supplied and unauthenticated.** It is a public XRPL wallet
  string typed into a URL by whoever shared the link; nothing proves the named
  wallet actually referred anyone. The design must treat it as an untrusted
  hint, validate its checksum server-side (`is_valid_classic_address`, not just
  the client's `XRPL_ADDR_RE` shape regex), and must never let a bad/hostile ref
  break or block a mint (best-effort, exactly like `share_clicks.record_click`
  swallows every error).
- **Self-referral is trivially spoofable** (mint from the same wallet you
  "shared" as). Reject `referrer == minter wallet` at the service. Wash-sharing
  across two wallets one person controls is *not* on-chain-preventable without
  identity — this is why the issue flags reward-abuse: the store here is
  attribution-only, and any *reward* payout must live behind a separate,
  sybil-resistant policy pass (out of scope).
- **Network-aware DB.** Both `share_clicks` and the `LFG` table live in the
  per-network app DB (`db_path.app_db_path(network)` / `config.DB_PATH`), so the
  metrics query must resolve the same network the mints were recorded under —
  never hard-code mainnet.
- **Self-migrating schema convention.** `record_nft_mint` already ALTERs missing
  columns onto `LFG` at write time (`new_columns` dict, `db_helpers.py:83`); a
  new `referrer` column follows that exact pattern — no migration script.
- **Vanilla no-build client.** Any `app.js` change requires bumping the
  cache-buster query on the `app.js` script tag in `webapp/client/index.html` in
  the same commit (repo convention — Discord caches `app.js` aggressively).
- **Both mint surfaces share `mint_one_unit`.** Single mint
  (`run_mint_session`) and bulk mint (`run_bulk_mint_job` →
  `bulk_mint_flow.py`) both funnel through `mint_one_unit`, so a `referrer`
  kwarg there covers both paths uniformly. MVP wires single mint end-to-end;
  bulk passes it through the same kwarg (see Out of scope for the durability
  nuance).

## Design

Four independent seams: **client send → service validate → thread + persist →
metrics readout**.

### 1. Client: send the stashed ref (`webapp/client/app.js`)

- Add a tiny helper `stashedRef()` that reads `localStorage['lfg_ref']` inside a
  try/catch (private-mode safe, mirroring the existing `WEB_SESSION_KEY`
  reads) and returns the value only if it passes `XRPL_ADDR_RE`, else `null`.
- In `startMint()` (`app.js:1213`), include it in the POST body:
  `JSON.stringify({ ...discordCtx(), referrer: stashedRef() })`. `discordCtx()`
  stays unchanged; the referrer rides alongside it. When there is no ref the
  field is `null` and the server treats it as absent.
- Bump the `app.js` cache-buster in `webapp/client/index.html` in the same
  commit.
- No change to `shareUrlFor` / the stash-on-load logic — that half already
  works.

### 2. Service: read + validate (`lfg_service/app.py::handle_mint_start`)

`handle_mint_start` currently never reads the body. Add a guarded read at the
top (the pattern used by every other POST handler — `body = await
request.json()` wrapped so a malformed/empty body degrades to `{}` rather than
500ing an otherwise-valid mint):

```python
try:
    body = await request.json()
except Exception:
    body = {}
referrer = _clean_referrer(body.get("referrer"), request["wallet"])
```

`_clean_referrer(raw, minter_wallet)` (new small helper in `app.py`, or in a new
`lfg_core/referral.py` for testability) returns a normalized referrer or
`None`:

- `None`/empty/non-str → `None`.
- Not `is_valid_classic_address(raw)` (imported from `xrpl.core.addresscodec`,
  already used at `config.py:84` and `history_events.py:14`) → `None` (checksum,
  not just shape).
- `raw == minter_wallet` (self-referral) → `None`.
- Otherwise → `raw`.

Pass `referrer=referrer` into the `MintSession(...)` constructor. This is
resolved *before* the await-free one-active-session guard window, so it adds no
new race.

### 3. Thread + persist (`mint_flow.py`, `db_helpers.py`)

- `MintSession.__init__` (`mint_flow.py:59`) gains `referrer: str | None = None`
  → `self.referrer = referrer`. Serialize it in `to_dict()` only if it's already
  surfaced there for debugging (optional; not required for correctness).
- `mint_one_unit` (`mint_flow.py:352`) gains a keyword-only `referrer: str |
  None = None`; add `"referrer": referrer` to the `record` dict built at
  `mint_flow.py:486` (guarded by the same "on-chain, DB failure must not block
  the offer" try/except that already wraps `record_nft_mint`).
- `run_mint_session` (`mint_flow.py:727`) passes
  `referrer=session.referrer` into the `mint_one_unit(...)` call.
- `record_nft_mint` (`db_helpers.py:61`) gains a `referrer: str | None = None`
  param, adds `"referrer": "TEXT"` to the `new_columns` self-migration dict
  (`:83`), and includes `referrer` in the INSERT column list + values tuple
  (`:112`). The `LFG.referrer` column *is* the durable attribution store — one
  row per mint already exists there; we annotate it with who (claims to have)
  referred it. Nullable, defaults to nothing when absent.

No new table for the mint side is needed — the `LFG` row is the record of "which
mint, who referred." `share_clicks` remains the record of "who drove visits."

### 4. Metrics readout (`lfg_core/referral.py` + `scripts/share_metrics.py`)

Conversion is a two-table aggregate in the per-network app DB:

- **Visits** per referrer: `SELECT ref_wallet, COUNT(*) FROM share_clicks WHERE
  ref_wallet IS NOT NULL AND is_bot = 0 GROUP BY ref_wallet`.
- **Mints** per referrer: `SELECT referrer, COUNT(*) FROM LFG WHERE referrer IS
  NOT NULL AND network = ? GROUP BY referrer`.
- Full-outer-join in Python (sqlite has no FULL OUTER) keyed on wallet →
  `{referrer, human_clicks, mints, conversion_rate = mints / human_clicks}`.

Ship this as an **ops CLI** `scripts/share_metrics.py --network testnet|mainnet
[--min-clicks N] [--json]` (loopback ops tool, same posture as
`scripts/rarity_admin.py` / the audit scripts), printing a ranked table
(referrer, clicks, mints, conversion). The aggregation logic lives in a testable
`lfg_core/referral.py::referrer_conversion(app_db_path, network)` the CLI wraps.

A *public* rewards board is intentionally **not** built (see Out of scope) —
exposing conversion counts as a leaderboard is exactly what a wash-sharer would
farm; that gate belongs to the rewards phase, not to attribution recording.

## Out of scope

- **Any reward/payout** for referrals — that needs a sybil-resistance policy
  (minimum distinct-holder threshold, wallet-age/holding checks, manual review)
  and its own issue. This spec only *records* attribution and *reports* raw
  conversion for ops.
- **A public/Activity-facing referrer leaderboard.** The metrics are an ops CLI
  only until abuse controls exist.
- **Bulk-mint durable referrer persistence.** `mint_one_unit`'s new kwarg means
  bulk mints *can* carry a referrer, but persisting it across a
  `bulk_mint_jobs/<id>.json` crash/resume (adding a field to `BulkMintJob`) is a
  follow-up; MVP wires single mint end-to-end and lets bulk pass `None` (or a
  best-effort non-durable ref) without changing the job record shape.
- **Opaque referral codes** (indirection over the raw wallet). Wallets are
  public on-chain; the parent spec already chose raw-wallet refs.
- **Attribution for Discord-bot native mints** — there is no bot-native mint
  path (it funnels through `lfg_service`); the only referrer source is the web
  Activity's `localStorage`, which the bot surface lacks.

## Open questions / decisions for maintainer

1. **Metrics surface:** ops CLI only (proposed), or also a
   token-gated `GET /api/share/metrics` for a future admin panel? CLI is the
   lower-abuse-surface default.
2. **Referrer eligibility:** record *any* checksum-valid non-self wallet
   (proposed), or additionally require the referrer to be a *known* wallet
   (exists in `Users`/`identities` or has minted before) to cut obvious junk?
   The latter reduces noise but complicates the (best-effort, fast) mint path.
3. **Ref freshness / TTL:** `lfg_ref` currently persists indefinitely in
   `localStorage`. Should attribution only count if the mint happens within N
   days of the click, and should a mint *clear* `lfg_ref` after crediting (so
   one click ≠ credit for every future mint)? Proposed: clear `lfg_ref` on a
   successful mint start; no server-side TTL in MVP.
4. **First-touch vs last-touch:** last stashed ref wins (current behavior).
   Acceptable?
5. **Bulk mint:** attribute all N units to the referrer, or only 1? (Affects
   how easily volume is farmed once rewards exist.)

## Testing

**Unit (`tests/`, with the env-guard preamble — `os.environ.setdefault`
`BUNNY_PULL_ZONE` / `LAYER_SOURCE` at module top):**

- `_clean_referrer` / `referral.clean_referrer`: valid classic address passes;
  checksum-invalid string → `None`; self-referral (== minter) → `None`;
  `None`/empty/non-str → `None`.
- `record_nft_mint(..., referrer=...)`: column self-migrates onto a fresh `LFG`
  table; the value round-trips via `get_nft_data`; `referrer=None` stores NULL.
- `referral.referrer_conversion`: seed `share_clicks` + `LFG` rows across two
  referrers (incl. a bot click that must be excluded and a referrer with clicks
  but zero mints), assert per-referrer clicks/mints/rate and network filtering.

**Integration (service, aiohttp test client):**

- `POST /api/mint` with a valid `referrer` → the created `MintSession` carries
  it (assert via `/api/mint/{id}` or a mint_flow-level stub of the mint), and a
  simulated successful unit writes `LFG.referrer`.
- `POST /api/mint` with a garbage referrer, a self-referral, and no referrer
  each start a normal mint with `referrer` NULL — the mint never fails on the
  referrer.

**Manual smoke:**

- Visit `PUBLIC_SHARE_BASE_URL/nft/<n>?ref=<walletB>` in a browser → confirm
  `lfg_ref` set. Mint from walletA in the Activity → `LFG` row for the new
  edition has `referrer = walletB`. `scripts/share_metrics.py --network testnet`
  shows walletB with 1 mint. Repeat with `?ref=<walletA>` then mint from walletA
  → `referrer` NULL (self-referral rejected).
