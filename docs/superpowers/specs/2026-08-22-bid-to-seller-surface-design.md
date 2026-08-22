# Bid-to-seller surface — routing external-listing offers to the owner

**Status:** proposed
**Date:** 2026-08-22
**Depends on:** #283 (native buy offers + accept flow), #135/#212 (Xaman push)
**Pairs with:** external buy-now (clearing price) and the royalty-refund campaign

## Problem

A buy offer on one of our NFTs can be accepted three ways, and they are not
equivalent to us:

| Path | Who signs the accept | Tagged txs we earn |
|---|---|---|
| Broker's bot settles it | the broker (cafe: `SourceTag 101102979`) | 1 — the `offer_create` |
| Seller accepts in Xaman | the seller, **untagged** | 1 — the `offer_create` |
| **Seller accepts through LFG** | the seller, via our payload | **2** — `offer_create` + accept |

Measured on `history_mainnet.db`: of all `NFTokenAcceptOffer` txs touching our
collection, 4,822 are untagged, 3,424 carry cafe's tag, and 1,414 carry ours —
the 1,414 being exactly the ones where an LFG surface built the payload.

The third row is the only path that adds transaction volume for us, and the
machinery for it already exists. What is missing is that **the seller never
finds out a bid is waiting.**

## What already exists

- `POST /api/market/bid/accept` + `BidAcceptSession` /
  `advance_bid_accept_session` (`lfg_core/market_flow.py`) — the owner-signed
  accept, fail-closed via `verify_buy_offer`.
- `GET /api/market/bids/mine` — returns `my_bids` and `bids_on_my_nfts`.
- The `buy_offers` index, kept live by the listener and swept nightly by
  `backfill_market.py`.
- Xaman push delivery (`identity.user_token_for`), which puts a signable
  payload directly in a user's app.

Everything needed to *accept* a bid is built. This spec is a notification
surface over it.

## Design

### Trigger

The listener already indexes buy-flagged `offer_create` for our character NFTs
(`nft_listener.apply_market_tx`). When it writes a new `buy_offers` row, it
enqueues a notify job for the token's current owner-of-record.

Resolution is best-effort and fails silently: `identity.resolve` the owner
wallet to an LFG identity; no identity means no notification, which is the
common case for a wallet that has never used the app.

### Delivery

One notification per new bid per owner, deduped on `offer_index` in a durable
`bid_notifications` table (same posture as every other derived table here:
per-network `onchain_<net>.db`, droppable, rebuildable from `buy_offers`).

Delivery is per-surface, best-effort, in this order of preference:

1. **Xaman push of the accept payload itself**, when the owner has a
   `user_token`. One tap in the app they already have open is the shortest
   path from bid to a tagged accept.
2. **Discord DM / Telegram message** with the amount and a deep link into the
   Activity's bids view, when the surface is known.
3. Nothing. The bid still shows in `bids_on_my_nfts` next time they open the
   app.

A failed push is not an error: the payload is rebuildable, the bid persists
on-ledger until its `Expiration`, and the in-app list is the durable surface.

### Anti-nuisance rules

These are requirements, not polish. A notification surface that annoys sellers
gets the app muted, which costs more than it earns.

- **Rate limit** per owner per rolling window; batch multiple bids that land
  together into one message.
- **Ignore dust.** A configurable floor below which a bid is indexed but not
  notified.
- **Never re-notify** a cancelled, expired, or already-accepted offer. The
  listener closes those rows; the notifier reads the live set only.
- **Honour an opt-out** stored on the identity.

### The interaction with buy-now

A bid at or above the broker's clearing price
(`ask / (1 - broker_rate)`) is swept by the broker's bot in about ten seconds.
A bid intended for the seller must therefore be **hard-capped strictly below**
that price in the UI, or it silently becomes a broker-settled purchase with
the broker's attribution on it — the opposite of this spec's purpose.

Where the token is externally listed and the rate is known, the "make an
offer" input caps at `clearing_drops - 1`. Where there is no external listing,
no cap applies.

### Copy

The seller-facing message states the amount, the NFT, and what accepting does.
It must not imply LFG is a party to the trade or guarantee anything about the
bidder. If the royalty-refund campaign is active, that message is where its
incentive gets stated — see that spec.

## What this does not do

- It does not auto-accept anything. Every accept is owner-signed.
- It does not create bids; that is the buy-now spec and the existing #283 flow.
- It does not reach sellers with no LFG identity. Those bids rely on the
  seller noticing the offer in their own wallet.

## Risks

- **Notification spam** as bid volume grows — mitigated by the rules above.
- **Push token staleness** — already handled: `_create_xumm_payload` falls back
  to QR/deep-link and re-captures tokens from every signed payload (#212).
- **A notified bid that expires before the seller acts** looks like a bug to
  the seller. State the expiry in the message.

## Testing

- Notify-once semantics: duplicate listener events for one `offer_index`
  produce one notification.
- No identity, no push token, opted out, below the dust floor: each produces
  no delivery and no crash.
- Cancelled / expired / accepted bids are never notified.
- The UI cap: an external row with a known rate caps the offer input below
  `clearing_drops`; a row without one does not cap.
- `bid_notifications` rebuilds cleanly from `buy_offers` after being dropped.
