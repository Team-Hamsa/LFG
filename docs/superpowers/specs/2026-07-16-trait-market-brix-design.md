# BRIX-Denominated P2P Trait Listings + XRP On-Ramp — Design (#239)

**Issue:** [#239](https://github.com/Team-Hamsa/LFG/issues/239)
**Status:** Spec
**Depends on:** Marketplace #44 (merged). Independent of #238 (shop XRP fallback) but
shares the AMM-quote/detection helper. Trait side of the market is
`ECONOMY_ENABLED`-gated (testnet today), so this ships without touching the live
mainnet character market.

## Problem

Design intent: **all trait-economy payments are BRIX-denominated**; users without BRIX
pay XRP that buys the BRIX out of the AMM. The character market stays XRP — correct as
built. But P2P trait listings are XRP-only: `market_ops` rejects IOU-denominated offers
outright (`extract_created_sell_offer` returns None for dict Amounts;
`verify_sell_offer` treats a dict as a mismatch), and `market_flow`/`market_store`/the
listener/backfill all assume drops.

## Design

### Denomination rule

- `kind=character`: XRP drops, exactly as today. IOU offers remain rejected.
- `kind=trait`: **BRIX only** (`IssuedCurrencyAmount` on `TOKEN_CURRENCY_HEX` /
  `TOKEN_ISSUER_ADDRESS`). XRP-denominated trait offers are rejected by the same
  mechanism that rejects IOUs for characters — the rule inverts per kind, one code path
  parameterized by expected currency.

### Money edge (`market_ops`)

Generalize the amount layer around an expected-currency parameter:

- `extract_created_sell_offer(meta, expect)` — `expect="xrp"` keeps today's behavior;
  `expect="brix"` accepts only a dict Amount matching our currency+issuer and
  normalizes its `value` (Decimal, ≤ BRIX precision, > 0).
- `verify_sell_offer(...)` likewise: amount match is per-kind (drops string vs
  BRIX dict `{currency, issuer, value}`); the no-foreign-`Destination` check is
  unchanged.
- Validation bounds for BRIX values mirror the XRP ones (positive, capped, ≤ sane
  decimal places) — reuse the Trait Shop's price bounds (`SHOP_MIN_BRIX`/`SHOP_MAX_BRIX`
  are shop-pricing knobs, NOT listing bounds; listings get their own generous cap, e.g.
  1e15, precision 6dp).

### Store + API

- `market_listings`: keep `amount_drops` for characters; add `amount_brix TEXT`
  (self-migrating ALTER; exactly one of the two is non-NULL per row, checked at upsert).
- `GET /api/market/listings`: trait rows return `amount_brix`; `min_xrp`/`max_xrp`
  filters apply only to characters — trait filtering gains `min_brix`/`max_brix`.
  Sort by price sorts within kind (browse is already per-kind).
- `GET /api/market/history` (`?slot=&value=`): sold-trait rows expose the BRIX price.
- Activity UI: trait prices render as BRIX; list-a-trait wizard inputs BRIX.

### List / Cancel flows

`ListSession` for `kind=trait` carries `amount_brix` instead of `amount_drops`; the
XUMM `NFTokenCreateOffer` txjson `Amount` is the BRIX dict. Cancel is unchanged
(`NFTokenCancelOffer` is amount-agnostic). The two-signature trait-sell wizard
(`TraitSellSession`) inherits this via the List leg.

### Buy flow — one signature for holders, two for the on-ramp

1. `POST /api/market/buy` on a trait listing detects the buyer's BRIX balance vs the
   listing price (shared helper from #238 / `detect_swap_payment` — balance check +
   `get_amm_xrp_cost` quote × buffer).
2. **Holder path:** exactly today's shape — on-ledger `verify_sell_offer` (now
   BRIX-aware), XUMM `NFTokenAcceptOffer`, signer==buyer check, settle via
   `_settle_trait_sale`/`run_deposit`. One signature.
3. **On-ramp path (no/insufficient BRIX):** a native `NFTokenOffer` is single-currency —
   no cross-currency accept exists. Insert a **self-Payment pre-step**: the buyer signs
   a cross-currency Payment to *themselves* — `Amount` = the listing's BRIX
   `IssuedCurrencyAmount`, `SendMax` = quoted XRP × buffer, destination = their own
   wallet — which buys the BRIX out of the AMM into their own wallet. `BuySession` gains
   a preceding `AWAITING_ONRAMP` state: build the on-ramp payload (new
   `xumm_ops.create_onramp_payment_payload`, stamped with SourceTag + memos
   `action=payment` like every payload), poll it signed + validated `tesSUCCESS`,
   re-verify the (unchanged) sell offer, then proceed to the normal accept payload.
   Two signatures total. If the buyer abandons after the on-ramp, they keep the BRIX
   they bought — no custody, no stranded funds, listing untouched.
   - The buyer needs a BRIX trustline for the self-Payment to deliver; if
     `get_trustline_balance` returns None (no line), the on-ramp payload is preceded by
     the existing TrustSet flow (or the API returns 409 `trustline_required`, reusing
     the mint flow's pattern — plan decides which the Activity can drive most cleanly).
4. Buy-status responses expose `pay_with` + `price_xrp_quote` so the UI can render the
   extra step.

Note the buyer's BRIX goes to the **seller** (minus the 7% `TransferFee` royalty) — the
marketplace burns nothing; the AMM buy pressure comes from the on-ramp Payment itself.

### Listener + backfill

- `nft_listener.apply_market_tx` offer_create: accept BRIX-denominated offers for trait
  tokens (`trait_tokens` membership) and XRP for characters; store into the right amount
  column. Accept/cancel/stale logic is amount-agnostic and unchanged.
- `scripts/backfill_market.py`: same per-kind currency rule when sweeping
  `get_nft_sell_offers` results.
- **Migration/transition:** existing live XRP-denominated trait listings (testnet only)
  are closed `stale` by a one-shot pass in the backfill (`--restale-xrp-traits` or just
  documented as "run backfill after deploy"); sellers re-list in BRIX. No mainnet impact.

### Invariants

- Character market byte-identical.
- Trait buys remain `ECONOMY_ENABLED`-gated and Closet-gated (`closet_required`), and
  settlement (deposit-to-Closet, sweep, give-up journal) is untouched — only the offer's
  Amount type changes.
- `SourceTag=2606160021` + provenance memos on every payload, including the new on-ramp
  Payment.

## Out of scope

- Brokered/custodial cross-currency accepts.
- BRIX-denominated *character* listings.
- Shop fallback (#238).
