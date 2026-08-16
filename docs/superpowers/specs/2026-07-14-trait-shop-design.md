# Trait Shop — project-listed traits priced in BRIX (design)

**Issue:** [#217](https://github.com/Team-Hamsa/LFG/issues/217)
**Date:** 2026-07-14
**Status:** Approved design (brainstorm 2026-07-14)

## Summary

A project-run trait shop inside the marketplace: any rarity-enabled trait can be
bought for **BRIX**, minted on demand as a trait NFToken and settled into the
buyer's Closet. The seller is the **BRIX issuer account**, so the BRIX paid is
redeemed on-ledger — burned by XRPL construction, with zero extra transactions.
The existing user↔user marketplace stays **XRP-only and unchanged**.

Bundled taxon realignment: **traits → taxon 176** (default flip from 1763),
**Assemble-minted characters → taxon 1760** (regular mints stay taxon 0).

## Goals

- A BRIX sink whose strength scales with trait rarity.
- Project-owned trait supply without pre-minted inventory or manual drops.
- Honest scarcity: shop purchases feed back into the rarity engine.
- Admin control (exclude / price-override) from the existing rarity dashboard.

## Non-goals

- BRIX pricing for user↔user listings (stays XRP-only).
- Selling **bodies** through the shop (Assemble inputs stay harvest-sourced;
  possible v2).
- Migrating existing taxon-0 characters or reminting taxon-1763 trait tokens.
- Mainnet enablement — the whole feature is `ECONOMY_ENABLED`-gated like the
  rest of the trait economy.

## Catalog & pricing

**Every trait enabled in the rarity engine is purchasable by default.** There
is no standalone catalog of listed items — the catalog is *derived* from
`trait_rarity` (enabled rows) plus an overrides table.

**Price formula** (derived, computed live at quote time):

```
smoothed_share = (Σ_bodies live_count + shop_count + 1) / (Σ_bodies category_total + population_size)
price_brix     = clamp(round(SHOP_BASE_BRIX / smoothed_share), SHOP_MIN_BRIX, SHOP_MAX_BRIX)
```

- Same Laplace smoothing as `rarity.effective_weight` (prevents cold-start
  infinities and share=1.0 artifacts).
- Share is **aggregated across bodies** because trait tokens are body-agnostic
  (`slot`/`value` only — see `economy_flow` Extract). Per-body pricing is a
  possible refinement, not v1.
- `SHOP_BASE_BRIX`, `SHOP_MIN_BRIX`, `SHOP_MAX_BRIX` are config/env values
  tuned at launch (spec fixes the formula, not the numbers).
- Rarity-**disabled** traits are not purchasable (they're out of the derived
  catalog entirely).

**Overrides table** — `shop_overrides` in the app DB (same `db_path` /
`network`-column pattern as `trait_rarity`):

```sql
CREATE TABLE shop_overrides (
    network        TEXT NOT NULL,
    slot           TEXT NOT NULL,   -- category: Body-excluded 8 slots
    value          TEXT NOT NULL,
    excluded       INTEGER NOT NULL DEFAULT 0,
    price_override INTEGER,         -- BRIX; NULL = use derived price
    updated_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (network, slot, value)
)
```

Managed from the rarity admin dashboard (`scripts/trait_dashboard.py`): a shop
column/panel per trait showing derived price, with exclude toggle and price
override input, audit-logged like every other dashboard mutation.

## Purchase flow

State machine `ShopBuySession` in `lfg_core/market_flow.py`, mirroring
`BuySession` / `TraitSellSession` conventions (polled via session-status GET).

1. **Quote & gate.** `POST /api/shop/buy {slot, value}` — authed. Fail-closed
   preconditions: `ECONOMY_ENABLED` (403 `economy_disabled`), trait enabled and
   not excluded (404/403), buyer has an **active Closet** (403
   `closet_required`, same gate as trait market buys). Price computed now and
   frozen into the session.
2. **Mint on demand.** Service mints the trait token — taxon **176**,
   `TRAIT_NFT_FLAGS = 9` (burnable + transferable, not mutable) — via the
   existing extract-style mint path. Writes a `supply_changes` growth row
   (**shop mints are NOT supply-neutral**, unlike Extract/Deposit; without this
   row `scripts/audit_trait_economy.py` flags conservation drift).
3. **BRIX sell offer.** Issuer creates `NFTokenCreateOffer`: sell-flagged,
   `Amount = {currency: BRIX, issuer: <BRIX issuer>, value: <price>}`,
   `Destination = buyer`, on-ledger `Expiration = now + SHOP_OFFER_TTL`
   (~15 min). Private offer → **never** indexed into `market_listings` (the
   listener's XRP-only public-listing filter is untouched); tracked in
   `shop_orders` instead.
4. **Xaman accept.** Buyer signs `NFTokenAcceptOffer` (push-delivered via the
   buyer's `user_token` when available, QR/deep-link fallback — same
   `_push_token` resolution as marketplace buys). Signer-matches-session-wallet
   check as in `advance_buy_session`. On acceptance the BRIX flows to its
   issuer and is **redeemed/burned by construction**.
5. **Settle into Closet.** Reuse the sold-trait settlement path
   (`run_deposit`: fail-closed on-ledger owner verify → issuer burn →
   Closet credit). The Deposit leg is supply-neutral as today; net supply
   effect of a completed purchase is the step-2 growth row.
6. **Rarity feedback.** On settlement, increment
   `trait_rarity.shop_count` for the (slot, value) — see below.

### Expiry & cleanup

Unaccepted orders: the offer's on-ledger `Expiration` makes it unacceptable
after the TTL; the existing 2-minute settlement sweep grows a shop pass that
(a) cancels expired offers, (b) issuer-burns the orphaned trait token, and
(c) writes a `supply_changes` **reversal** row, closing the order `expired`.
No permanent inventory leaks, no conservation drift.

### `shop_orders` table

In the economy-network `onchain_<net>.db` (beside `trait_tokens` /
economy_store, resolved via `config.ECONOMY_NETWORK`):

```sql
CREATE TABLE shop_orders (
    session_id   TEXT PRIMARY KEY,
    buyer        TEXT NOT NULL,
    slot         TEXT NOT NULL,
    value        TEXT NOT NULL,
    price_brix   INTEGER NOT NULL,
    nft_id       TEXT,              -- set after mint
    offer_index  TEXT,              -- set after offer creation
    status       TEXT NOT NULL,     -- pending_mint | pending_accept | accepted
                                    -- | settled | expired | failed
    created_ts   INTEGER NOT NULL,
    updated_ts   INTEGER NOT NULL
)
```

Failure handling follows the phase-aware `_sync_then_persist` taxonomy from
`lfg_core/economy_flow.py` (ledger-not-committed → compensate on-chain;
mirror-only failure → `complete_pending_mirror`; indeterminate → fail closed
and reconcile from chain). Partial-failure journals go to
`ECONOMY_RECORDS_DIR` as today.

## Rarity feedback loop

`recalculate_rarity` counts live traits from the `LFG` table only; a purchased
trait lives in a Closet, not on a character, so it needs an explicit counter:

- New column `trait_rarity.shop_count INTEGER NOT NULL DEFAULT 0`
  (self-migrating `ensure_schema`, like existing columns).
- Incremented once per **settled** purchase; **preserved** by
  `recalculate_rarity` (which zeroes and recounts `live_count` only — same
  preservation contract as boost/floor/enabled).
- Consumed in **shop pricing only** (share numerator above). Mint-time
  `weighted_pick` / `effective_weight` are unchanged in v1 — buying a trait
  lowers its shop price but does not change mint odds. (Folding `shop_count`
  into mint weights is a deliberate follow-up decision, not an accident of
  implementation.)
- Trait tokens are body-agnostic, so the increment applies to the (slot,
  value) aggregated across bodies — matching how the price reads it.

## Taxon realignment

| Token class | Today | New | Migration |
|---|---|---|---|
| Trait tokens | `TRAIT_TAXON = 1763` | **176** | Flip the env default only. Existing 1763 tokens (testnet-only, a handful) are abandoned — no dual-read, no remint. |
| Assembled characters | `NFT_TAXON = 0` (all mints) | **1760** for Assemble-minted rebirths only | New `ASSEMBLE_TAXON = 1760` config; `run_assemble`'s mint uses it. Regular `/letsgo` mints stay taxon 0 — the 3,535-strong main collection is not split. |
| Closet | 1762 | unchanged | — |

Touchpoints to update for 176: `config.py` default, plus everything that
matches `config.TRAIT_TAXON` (`nft_listener.py` trait upsert, `economy_flow.py`
deposit ownership gate, `backfill_economy.py`, `_economy_deps.py`, docs/env
templates). For 1760: `economy_flow.run_assemble` mint site + listener/index
treatment of taxon-1760 tokens as collection characters (character membership
is issuer+membership-based, not taxon-based — verify no code path filters
characters by taxon 0; `backfill` enumeration by taxon must include 1760).

## Service API

- `GET /api/shop/catalog` — public. Derived catalog: enabled, non-excluded
  traits with live price, art URL, and (slot, value). Cached ~60s per network
  (same posture as `_MARKET_CACHE`); overrides/exclusions apply post-derivation.
- `POST /api/shop/buy` + `GET /api/shop/buy/{session_id}` — authed; drives
  `ShopBuySession` (quote-freeze → mint → offer → accept-poll → settle).
- Dashboard (localhost-only, not `lfg_service`): shop panel with exclude /
  price-override, writing `shop_overrides`, audit-logged.

## Memos & SourceTag

- New closed-enum memo action `shop-buy` in `lfg_core/memos.py`; the mint,
  offer, and settlement-burn legs carry it with the real originating surface
  via `platform_for_surface`.
- `SourceTag = 2606160021` on every leg — automatic via `_create_xumm_payload`
  (user-signed accept) and `build_memo_models` sites (backend-signed
  mint/offer/burn). Shop code never sets it by hand.

## UI

Shop section in the Activity marketplace (vanilla-JS, no-build): catalog grid
(art, name, slot, BRIX price), buy button → existing QR/push signing overlay,
Closet-required prompt reusing the marketplace's `closet_required` handling.

## Error handling summary

- All preconditions fail closed **before** any on-ledger action.
- Mint succeeded but offer creation definitively failed → issuer-burn the
  token + `supply_changes` reversal (revert, as Extract does).
- Accept landed but settlement fails → order stays `accepted`/unsettled; the
  sweep retries up to the existing max attempts, then journals a giveup record
  (token sits in the buyer's wallet for a manual Deposit — never lost).
- Offer expiry → sweep cancels, burns, reverses (above).

## Testing

- Unit: price formula (smoothing, clamp, override precedence, excluded),
  `shop_overrides` / `shop_orders` stores, `shop_count` preservation across
  `recalculate_rarity`.
- Flow: `ShopBuySession` happy path + each failure phase (mint-fail,
  offer-fail revert, accept-expiry sweep, settle-fail retry/giveup),
  fail-closed gates (economy disabled, no Closet, excluded trait,
  signer mismatch).
- Conservation: `audit_trait_economy` green across purchase, expiry-revert,
  and settle — every path's `supply_changes` accounting.
- Taxon: listener recognizes 176 trait tokens and 1760 characters; Assemble
  mints at 1760; regular mint still taxon 0.
- New test files copy the env-guard preamble (repo convention).

## Open follow-ups (out of scope)

- Fold `shop_count` into mint-time weights (odds feedback, not just price).
- Per-body pricing; selling bodies.
- BRIX pricing for user↔user listings.
- Mainnet enablement (rides the economy-go-mainnet gate, #185 lineage).
