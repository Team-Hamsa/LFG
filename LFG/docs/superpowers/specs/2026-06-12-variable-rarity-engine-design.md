# Variable Rarity Generation Engine — Design

**Date:** 2026-06-12
**Status:** Approved design, pending implementation plan

## Goal

Replace uniform-random trait selection in both mint paths with a rarity engine
whose per-trait mint probability tracks the live collection's current trait
distribution (proportional-with-floor), recalculated on every collection
change (mint, burn, trait swap, external listener update). Adds a
"dormant boost" mechanic for newly introduced traits and makes the body/base
type itself a weighted rarity category.

## Decisions (from brainstorming)

- **Weighting:** proportional with floor. `weight = max(live_share, floor) × boost_multiplier`.
- **Scope:** both mint paths — webapp (`lfg_core/traits.py`) and legacy bot (`main.py get_random_trait`).
- **Source of truth:** the DB (LFG minus burned). On-chain reconciliation is
  handled externally by the XRPL-NFT-Listener repo
  (https://github.com/joshuahamsa/XRPL-NFT-Listener), which updates the DB and
  triggers a recalc.
- **Floor:** fixed configurable % (default 0.5% of category weight) so every
  trait, including 0-occurrence ones, is always mintable.
- **New traits:** hybrid introduction — auto-detected from the layer store at
  floor weight; boost is an explicit admin opt-in.
- **Boost decay:** dormant-then-stepped. A boosted trait sits at floor weight
  until its first organic mint; that mint starts the clock. Multiplier then
  jumps to `boost_initial` (default 7×) and steps down by 1 every
  `boost_step_hours` (default 24h) until it reaches 1×. Rewards the users who
  are actively minting when a trait is discovered.
- **Body, not gender:** the layer store's top-level grouping
  (`female|male|skeleton|ape`) is a body/base type, not a gender. The rarity
  table uses `body`; the store API is renamed (`gender` → `body`,
  `list_genders` → `list_bodies`) as an included cleanup. Body selection
  itself becomes a weighted rarity category (reserved category `'Body Type'`,
  `body = '*'`), replacing the current uniform `random.choice(genders)`.
- **Network scoping:** all counts and weights are scoped by `XRPL_NETWORK`
  (testnet|mainnet) so testnet mints never pollute mainnet rarity.
- **Out of scope (separate effort):** a trait-management dashboard (upload
  image + name + category → CDN upload + DB row). This design leaves the seam:
  the dashboard inserts a `trait_rarity` row and uploads to the store; the
  engine picks it up automatically.

## Data model

```sql
CREATE TABLE trait_rarity (
    network          TEXT NOT NULL DEFAULT 'mainnet',
    body             TEXT NOT NULL,      -- 'female'|'male'|'skeleton'|'ape'; sentinel '*' for legacy path and 'Body Type' rows (SQLite does not enforce PK uniqueness on NULLs)
    category         TEXT NOT NULL,      -- 'Background', 'Hat', ..., reserved 'Body Type'
    trait            TEXT NOT NULL,
    live_count       INTEGER NOT NULL DEFAULT 0,
    floor_weight     REAL NOT NULL DEFAULT 0.005,
    boost_initial    REAL,               -- e.g. 7.0; NULL = no boost configured
    boost_step_hours INTEGER DEFAULT 24,
    boost_started_at TIMESTAMP,          -- set at first organic mint; NULL = dormant
    enabled          INTEGER NOT NULL DEFAULT 1,
    first_seen_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (network, body, category, trait)
)
```

- **Effective weight is computed at read time** (boost is a function of
  wall-clock time; a stored column would be stale by definition).
  `live_count` is the cached part, maintained by recalc.
- `LFG` table gains `network TEXT NOT NULL DEFAULT 'mainnet'`; migration
  backfills existing rows as mainnet (seed CLI offers
  `--mark-testnet <nft_numbers>` for known test mints). All write paths stamp
  `network = XRPL_NETWORK`; all reads filter by it.
- Global defaults (floor %, boost initial, step hours) live in env/config.

## Selection math (`lfg_core/rarity.py`)

`weighted_pick(conn, body, category, available) -> trait`

1. Load enabled `trait_rarity` rows for `(network, body, category)`,
   intersected with `available` — the trait values that actually exist in the
   layer store right now. The store stays the authority on what's mintable;
   the DB only supplies weights.
2. Per trait:
   - `share = live_count / category_live_total` (0 when total is 0)
   - `base = max(share, floor_weight)`
   - `multiplier`: if `boost_started_at` set,
     `max(1, boost_initial − floor(hours_since(boost_started_at) / boost_step_hours))`;
     if boost configured but clock unstarted, 1 (dormant = floor weight only).
   - `weight = base × multiplier`
3. One draw via `random.choices`.
4. **Boost trigger:** if the picked trait has `boost_initial` set and
   `boost_started_at IS NULL`, set `boost_started_at = now` in the same
   transaction as the mint record — clock starts on completed mint, not on
   selection, so abandoned mints don't burn the window.

Weights are relative (not normalized probabilities), so floor sums exceeding
1 across many 0-count traits are naturally fine.

**Auto-detect:** any value in `available` with no row is inserted on the fly
at default floor, no boost.

**Body selection:** same function with `(body='*', category='Body Type')`,
`available = store.list_bodies()`.

**Recalc:** `recalculate_rarity(network, body=None, category=None)` recounts
`live_count` from LFG minus burned (grouped by trait). Called from
`record_nft_mint()`, burn, trait-swap completion; exposed as a
function/CLI for the XRPL-NFT-Listener. Staleness guard: at mint time, if
`SUM(live_count)` for the category disagrees with the live NFT count,
recalc before picking.

## Integration points

- **Webapp:** `select_random_attributes()` keeps its shape (param renamed
  `gender` → `body`); each uniform `random.choice` becomes `weighted_pick`.
- **Legacy bot:** `get_random_trait(trait_layer_dir)` derives the category
  from the folder name (strip numeric prefix, match LFG column
  normalization), calls `weighted_pick(body='*', available=file stems)`.
  Falls back to uniform random if the rarity table is missing/empty so the
  bot never bricks on a fresh DB.

## Edge cases

- Empty category (all zero counts) → all-floor → effectively uniform.
- All traits disabled → raise, surfaced as admin error.
- Fresh DB / missing table → legacy path falls back to uniform.
- Boost mathematically self-terminates at 1× after `boost_initial − 1`
  windows; no cleanup job needed. Re-arming = admin resets
  `boost_started_at` to NULL.

## Admin surface

Discord `/admin` panel additions:
- **View Odds** — body + category → embed of trait, live count, share %,
  effective weight, boost status (dormant / active N× with time left / —).
- **Boost Trait** — set `boost_initial` (default 7) + step hours (default
  24). Confirmation required if the trait already has occurrences
  ("comeback event" is legitimate but shouldn't be a fat-finger).
- **Disable/Enable Trait** — flips `enabled`; disabled traits can't mint but
  remain on existing NFTs.

CLI (`rarity_admin.py`): same ops plus `refresh` (full recount, also for
listener/cron), `set-floor` (global or per-trait), and `seed` (bootstrap:
scan layer stores, insert all traits, full recount from existing mints).

Admin actions (boost armed, trait disabled, floor changed) log to
`ADMIN_LOG_CHANNEL_ID` — they change mint economics, audit trail required.

New-trait workflow today (pre-dashboard): upload PNG to store (mintable at
floor immediately) → optionally Boost Trait to arm the dormant mechanic →
clock starts when the community finds it.

## Testing

TDD throughout:
- Unit: proportional shares, floor clamping, dormant vs active boost,
  step-decay at window boundaries (injected clock, no sleeps), boost
  triggers only on completed mint, multiplier never < 1.
- Distribution sanity: 10k draws over a fixed table, observed frequencies
  within tolerance of expected weights.
- Integration: recalc matches ground-truth GROUP BY after mint/burn/swap;
  staleness guard fires on mismatch; auto-detect inserts unknown traits;
  network isolation (testnet mint doesn't move mainnet counts); legacy
  uniform fallback.

## Rollout

1. Migration + `seed` on testnet; verify View Odds against known counts.
2. Testnet mint batch; watch weights move; arm a boost on a dummy trait with
   short step-hours and verify dormant → 7× → decay lifecycle.
3. Mainnet migration + `seed`, deploy. Rollback = revert selection functions
   to uniform; no mint-flow-critical schema is touched.
