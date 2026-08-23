# External listing "Buy now" — auto-calculated clearing price

**Status:** implemented (#426)
**Date:** 2026-08-22
**Depends on:** #131 (external listing rows), #283 (native buy offers)

## Problem

`GET /api/market/listings?include_external=1` surfaces NFTs of ours listed on
other marketplaces (xrp.cafe, bidds, Art Dept) as read-only rows tagged
`buyable:false`. A user who wants one has to leave the app.

The reason those rows are unbuyable is real: the seller's `NFTokenOffer` is
destination-locked to the broker's account, so only that broker can accept it.
LFG cannot settle the listing.

What we missed is that **we do not have to settle it**. The brokers run bots
that watch the ledger for buy offers on tokens listed with them and submit the
brokered `NFTokenAcceptOffer` themselves — and they do not care where the buy
offer came from.

## Evidence

Measured against `history_mainnet.db` on 2026-08-22 (1,137 cafe-brokered sales
of our NFTs, 2023-2026):

- **Seven brokered sales were settled from buy offers carrying our own
  `SourceTag 2606160021`** — created by the #283 bid flow from wallets
  `rHaMsA…`, `rMRKx…`, `rHU8n…`, with no `Destination` set. Cafe's bot picked
  them up and settled six of the seven in **9-10 seconds**.
- Across all cafe accepts, **62% settle within 30 seconds** of the buy offer
  appearing. This is a bot, not a human seller.
- `NFTokenBrokerFee` **never exceeds 1.5890% of the buy amount**, in any year.
- `(buy_amount - broker_fee) >= sell_amount` holds in **all 1,137** cases, with
  the minimum exactly `1.0000` — the broker never dips the seller below the ask
  and never brokers a bid that fails to cover it.

The capability already works by accident. What is missing is the price.

### Verified deliberately, 2026-08-22

A bid placed from the LFG app on cafe-listed #4691 (ask 4,990,000 drops) at
5,075,000 drops was brokered by cafe **nine seconds later**:

- bid `2AF272B2…`, `SourceTag 2606160021`, **no `Destination`**
- accept `FED6256D…` by `rpx9JT…`, `NFTokenBrokerFee = 80642`, `SourceTag 101102979`

Predicted fee `5,075,000 * 0.015890 = 80,641.75`; actual **80,642**. The rate is
exact and the rounding is `ceil()`. The seller received 4,994,358 against their
4,990,000 ask — **the broker's take is capped at its rate on the bid, so the
excess above the clearing price reaches the seller (net of that rate), never
the broker as a lump**, which makes a small safety buffer cheap insurance
against rate drift.

This also holds across taxons: #4691 is taxon 1760 (Assemble-minted), not 0.

## The price

Two fee regimes appear in the data (the fee taken against the buy side vs. the
sell side), so the safe minimum is the worse of the two:

```
base_bid_drops = ceil(ask_drops / (1 - broker_rate))    # cafe: rate = 0.015890
               ~= ask_drops * 1.016146
min_bid_drops  = base_bid_drops + BROKER_CLEARING_BUFFER_DROPS   # env, default 0
```

`lfg_core.brokers.clearing_drops` implements exactly this: the integer-rounded
base plus the optional operator buffer (`BROKER_CLEARING_BUFFER_DROPS`, ignored
when unset/unparsable/negative).

cafe's rate is confirmed at `0.015890` against the buy amount (see above).
Both failure modes are silent and invisible to the user, which is precisely why
this must be computed rather than typed:

- **Bidding the displayed ask** is ~1.6% short. The bot ignores it. The bid
  simply sits there and the user thinks the feature is broken.
- **Bidding generously** does not overpay the broker — its fee stays capped at
  its percentage — so the entire excess is handed to the seller.

The 7% `TransferFee` does not enter the buyer's number; it comes out of the
seller's share after the broker fee.

## Design

### Per-broker rate as allowlist data

`broker_rate` becomes a field on the `lfg_core/brokers.py` entry, beside
`name` and `url_template`, and is overridable through `BROKER_ALLOWLIST_PATH`
without a code change — the same posture as the rest of that module. Only
cafe's rate is measured; **bidds and Art Dept ship with `broker_rate: None`
until someone runs the same query against their accepts**, and a `None` rate
means no Buy-now button on that broker's rows. Guessing a rate produces
offers that never fill.

Validation mirrors the existing `url_template` check: a rate must parse as a
float in `[0, 0.25)` or the whole overlay falls back to built-ins.

### Serialization

`_serialize_listing_row` gains, for external rows with a known rate:

```
"clearing_drops": <int>,          # ceil(amount_drops / (1 - rate))
"clearing_xrp":   "<decimal str>",
"broker_rate":    0.015890,
```

`buyable` stays `false` — the row is still not settleable by us, and the 409
`external_listing` guard on `/api/market/buy` stays exactly as it is. Buy-now
is a *bid*, and it goes through the bid endpoints.

### Client

External cards gain a primary action: **Buy now — <clearing_xrp> XRP via
<marketplace>**, with the ask shown underneath and the difference named
honestly as the marketplace's fee. The existing deep link stays as a secondary
action.

The button calls the existing `POST /api/market/bid` with
`price_xrp = clearing_xrp`. No new session type, no new endpoint.

### Settlement is somebody else's bot

This is the part the UI must not lie about. After the bid validates, the app
polls `GET /api/market/bids/{session_id}` as it does today, then watches for
the token's ownership to change. Copy states:

- **placed** — "Offer placed. <marketplace> usually settles these within a
  minute."
- **filled** (listener sees the accept, owner is now the bidder) — normal
  success.
- **unfilled after ~5 minutes** — "Still waiting on <marketplace>. Your offer
  stays live for <TTL> and will fill if they take it; you can cancel any time."

We never claim the purchase succeeded until the ledger says the token moved.

### Expiration

The bid carries the existing `MARKET_BID_TTL_SECONDS` on-ledger `Expiration`,
so an unfilled buy-now expires on its own. Nothing is escrowed; the buyer's
funds are never held.

## What this does not do

- It does not make external listings settleable in-app. That is impossible.
- It does not earn us the accept transaction — the broker signs that one with
  its own `SourceTag` (cafe uses `101102979`). We earn the `offer_create` only.
- It does not touch the trait side. Trait listings are BRIX-denominated and no
  broker indexes them; characters only.

## Risks

- **Rate drift.** If a broker changes its fee, computed bids stop filling. The
  rate is overlay-configurable for exactly this reason, and the failure is
  benign (the bid expires unfilled) rather than costly.
- **Race with a cafe-native buyer.** Two buyers can bid on one listing; one
  loses and their bid expires. Same as any open marketplace.
- **Stale ask.** Our `market_listings` row can lag a re-list at a new price. A
  bid computed from a stale ask under-clears and does not fill. Refresh the
  row's offer on-ledger immediately before building the payload, the way
  `verify_sell_offer` already does for our own listings.

## Testing

- Clearing-price math: exact drops, rounding up at the boundary, that the
  computed bid satisfies `bid - ceil(bid * rate) >= ask` for a spread of asks
  (the broker fee is integer-rounded UP); the buffer is additive on top.
- Allowlist: rate parsing, out-of-range rejection, `None` rate suppresses the
  field, malformed overlay falls back to built-ins.
- Serialization: external row with and without a known rate; our own rows
  unchanged.
- A regression asserting `/api/market/buy` still 409s on external rows.
