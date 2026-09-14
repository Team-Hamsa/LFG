# Marketplace cross-listing — list your external listings on LFG too, in batches

**Status:** design only. **Blocked on XLS-56 Batch (#219).** Do not start
building until the #219 feasibility gate can be flipped on mainnet.
**Date:** 2026-09-14
**Depends on:** #219 / `2026-07-24-xls56-batch-accept-offers-design.md`
(its §4.0 feasibility gate and batch payload builder), #131 external
listing rows
**Companion:** `2026-09-14-marketplace-fee-cover-design.md`, which ships
first and holds the efficacy probe this builds on

## Problem

LFG's marketplace has no supply. On 2026-09-13 there was **1** plain LFG
character listing against **370** on xrp.cafe. 76 of cafe's 108 sellers are
already LFG wallets, and they hold 283 of those listings. Six of the top eight
cafe sellers are LFG users with 17–32 cafe listings each.

The mismatch comes from price and habit together:

- Sellers net 93% of the ask on both venues.
- An LFG buyer pays the ask; a cafe buyer pays ~101.6% of it.

So LFG is already the better venue for any listing on it. Nobody lists here
because the buyers are on cafe.

**Cross-listing** lets a seller keep their cafe listing and *add* a plain LFG
listing for the same NFT at the same ask. An NFT can carry several sell offers
at once. Sellers lose nothing, buyers here pay less, and nothing is taken from
cafe.

## Why Batch, and why it blocks

A per-NFT flow means one signature per listing, and that is explicitly
rejected: sellers with 17–32 listings will not sign 32 times. XLS-56 `Batch`
bundles 2–8 inner transactions under one signature. A 32-listing seller signs
4 times, not 32.

Batch is not usable yet. Per the #219 dependency check (2026-08-18), the
original `Batch` amendment is unsupported since rippled 3.1.1, and
`BatchV1_1` is still in mainnet voting. Xaman support for signing a `Batch`,
including inner `Sequence` autofill, is also unverified. This feature therefore
sits behind **the same feasibility gate as #219**. There is no separate flag,
and nothing ships until that gate can be flipped on mainnet.

## Design

### Who can cross-list what

`GET /api/market/mine` gains a `cross_listable` group. It holds the caller's
live `market_listings` rows where:

- `destination` resolves via `brokers.resolve` to **any** allowlisted broker.
  A measured rate is *not* required here; the rate only matters for Buy-now.
- `amount_drops > 0`, and `seller == wallet == current owner-of-record`.
- **No** live plain (`destination IS NULL`) listing by the same seller exists
  for that `nft_id`, or the only one has expired (then the item shows as
  "Refresh").

Each item carries `{nft_id, nft_number, image, marketplace, ask_drops,
ask_xrp, lfg_buyer_saves_xrp}`.

### Batch payload

`POST /api/market/cross-list` with body
`{items: [{nft_id, price_xrp?}], guild_id?, channel_id?}`, where the item count
is between 1 and `BATCH_ACCEPT_MAX_INNER` (8). That constant comes from the
#219 spec; rename it to a shared `BATCH_MAX_INNER` when both features land.

- **Validation.** Every item must be in the caller's `cross_listable` set
  (recomputed server-side, never trusted from the client). `price_xrp` defaults
  to the external ask and goes through `xrp_to_drops_str` like the List flow.
  One active market session per user, as today.
- **Exactly 1 item.** Use today's single `NFTokenCreateOffer` payload, since
  Batch needs at least 2 inner transactions. It still counts as "cross-list"
  in the UI: the seller picked one listing, nobody forced a one-at-a-time loop.
- **2–8 items.** One user-signed `Batch`:
  - Outer: `Account = wallet`, `Flags = tfIndependent`, so each listing
    succeeds or fails on its own. One NFT that moved must not sink the other
    seven.
  - Inner: `NFTokenCreateOffer`, each with `Flags = tfSellNFToken |
    tfInnerBatchTxn`, `Amount`, **`Expiration = now +
    MARKET_CROSS_LIST_TTL_SECONDS`** (new env, default 2592000 = 30 days), and
    no `Destination`.
  - Built by the #219 spec's batch builder. Inner `Sequence` and `Fee` handling
    follow whatever that spec's §3 resolution turns out to be.
- **SourceTag and memos on the outer *and* every inner transaction.** Memos
  are `build_memos_json(USER, platform, ACTION_LIST)`. The inner transactions
  are what the ledger records as listings, so each must carry its own
  hackathon tag.
- **More than 8 selected.** The client splits the selection into consecutive
  batches of ≤ 8. It shows "Batch 1 of 4 — sign in Xaman" and starts the next
  session only after the previous one is terminal. The server enforces the cap
  per request.

### Session and finalize

`CrossListSession` (`lfg_core/market_flow.py`) mirrors `ListSession`:
`awaiting_signature → submitted → done | failed`, with signer == session wallet
enforced as in every market session.

At finalize the service fetches the outer `Batch` tx. Inner transactions carry
their own metadata with a `ParentBatchID` pointer back to the outer hash. Only
committed inner transactions appear on the ledger (XLS-56; re-confirm under
`BatchV1_1`). For each selected item the service records one of two outcomes:

- A committed `tesSUCCESS` inner exists: extract the created offer
  (`extract_created_sell_offer`, `expect="xrp"`) and write it via
  `record_listing_creation`, including the new `expiration` column.
- Anything else: add it to `session.failed_items` with the inner result code,
  or `not_committed` when no inner record exists.

The session reports `{listed: [...], failed: [{nft_id, result}]}`. The
listener and `backfill_market.py` already index plain sell offers, so a missed
finalize self-heals.

### Required changes to existing marketplace code

1. **The List dedup guard blocks cross-listing today.**
   `_has_live_listing` → `market_store.live_listing_for_nft` returns 409 on
   *any* live row, including the seller's own broker listing and stale rows
   left by previous owners.
   - The new rule blocks only a live, **unexpired, plain** listing whose
     `seller == wallet`.
   - The plan audits every other `live_listing_for_nft` caller, since two
     live rows per `nft_id` becomes normal.
2. **`market_listings.expiration`** (INTEGER, Ripple epoch, self-migrating).
   - Written by `record_listing_creation`, the listener's `offer_create`
     upsert, and the backfill.
   - Browse, `mine`, and the cross-listable check treat `expiration <= now`
     as not live.
   - Buy is already safe: `market_ops.verify_sell_offer` rejects an expired
     offer on-ledger.
3. **Browse dedup per `nft_id`.** When one NFT has a live buyable LFG row and
   a live external row, browse shows **one** card: the LFG row, with
   `also_listed_on: [{marketplace, external_url, clearing_xrp}]` (price
   comparison copy: "5.08 XRP here · 5.16 XRP on xrp.cafe").
   - This runs post-cache, so the cached superset is unchanged, like the
     #131 opt-in filter.
   - The external row is suppressed only while the LFG row is live and
     unexpired.

### Cleanup tray — leftover offers

After a cross-listed NFT sells on one venue, the other offer stays on-ledger:

- It holds 0.2 XRP of the seller's owner reserve.
- It would become fillable again at the old price if the NFT ever returned to
  that wallet. The ledger only checks ownership at accept time.

The 30-day `Expiration` bounds the resurrection risk. The tray returns the
reserve sooner.

- `GET /api/market/leftover-offers` (authed). Lists the wallet's own
  `NFTokenOffer` objects (sell and buy), using
  `xrpl_ops.get_account_nft_offers(wallet)`, which is `account_objects`-based.
  An offer is included when either holds:
  - **Sell offer:** the NFT's current owner-of-record is not the wallet,
    confirmed on-ledger with `nft_info`, clio. A failed lookup excludes the
    offer (fail closed: never offer to cancel a live listing).
  - **Any offer:** its `Expiration` has passed.
  Rows carry `{offer_index, nft_id, nft_number, image, venue (LFG | broker
  name | direct), amount_xrp, reason: sold_elsewhere | expired}`.
- `POST /api/market/leftover-offers/cancel` with body `{offer_indexes: [...]}`.
  - The server re-checks every index against the rule above.
  - It builds **one** `NFTokenCancelOffer` with `NFTokenOffers` = the
    array. This is a single signature and needs no Batch; the ledger does not
    error on entries that no longer exist.
  - The plan confirms the protocol's per-tx array limit and chunks if a tray
    ever exceeds it.
  - Memo action `cancel-offer`; reuses `CancelSession` with a new
    `target = "leftovers"`.
- **Surfaced** as a "Clean up N old listings (frees X XRP)" banner on My
  listings when N > 0. It ships **with** cross-listing, per the 2026-09-14
  decision, even though it has no Batch dependency.

### Client

- **My listings** gains a "Also list on LFG" section when `cross_listable` is
  non-empty:
  - Copy: "Buyers here pay your ask, 1.6% less than on xrp.cafe. You still
    receive 93%."
  - Checkboxes, default all selected, and a per-item price field defaulting to
    the external ask.
  - One **Cross-list** button, which drives the ≤ 8 batching stepper above.
- An expired LFG cross-listing whose external listing is still live
  reappears in `cross_listable` as **Refresh**.
- The cleanup tray banner and sheet.
- Pure helpers (`market_pure.js`): `chunkForBatch(items, max)`,
  `crossListCopy(item)`, `leftoverOfferRow(row)`. The JS pure tests cover them.

### Seller incentive

None. The seller refund (the seller half of #428 / the roadmap item) is
reconsidered once this ships and there are LFG-listed sales to refund. See
the fee-cover spec, "Why not the seller refund".

## What this does not do

- No per-NFT one-signature-each flow, apart from the single-item case, which
  is the seller's choice.
- No automatic cross-listing and no mirroring of cafe price changes. If the
  seller re-lists on cafe at a new price, the LFG listing keeps its price until
  they refresh it. The comparison copy shows both prices.
- No cancellation of the external listing: the two coexist by design.
- No LFG broker or broker fee.

## Risks

- **Batch never activates, or Xaman never signs it.** Then this never ships,
  which is by decision.
- **BatchV1_1 wire shape differs from `Batch`.** The #219 spec flags that
  xrpl-py models the original amendment. Both features share one builder, so
  one rework covers both.
- **A price diverges across venues.** A cafe re-list leaves the LFG price
  stale.
  - Stale-low: the seller sells cheaper than intended, on the price they set.
  - Stale-high: the LFG listing simply won't sell.
  - The comparison copy and the Refresh affordance mitigate it.
- **The cleanup tray cancels a live listing.** Prevented by the on-ledger
  owner check, which fails closed, and a server-side re-check on cancel.

## Testing

- **Cross-listable derivation.** Broker vs plain vs other-seller stale rows,
  expired LFG row → Refresh, unmeasured broker still eligible.
- **List guard.** Own broker listing no longer blocks; own live plain listing
  still 409s; another seller's stale row doesn't block.
- **Payload.** 1 item → plain `NFTokenCreateOffer`; 2–8 → `Batch` with
  `tfIndependent`; each inner carries its flags, `Expiration`, `SourceTag`
  and memos; 9 → 400.
- **Finalize.** Mixed inner results record only the successes; `failed_items`
  carries the result codes.
- **Expiration.** Column migration; browse, mine and cross-listable ignore
  expired rows.
- **Browse dedup.** One card with `also_listed_on`; the external row returns
  when the LFG row expires or sells.
- **Leftover tray.**
  - Owned-elsewhere and expired offers listed.
  - A still-owned, unexpired offer is never listed.
  - A failed lookup excludes the offer.
  - Cancel re-check rejects a tampered index.
  - One `NFTokenCancelOffer` carries all indexes.
- **JS pure.** `chunkForBatch` boundaries (1, 8, 9, 32), copy helpers.
- **Manual.** Testnet or devnet once `BatchV1_1` is enabled there and Xaman
  signs a Batch, following the #219 spec's rollout order.
