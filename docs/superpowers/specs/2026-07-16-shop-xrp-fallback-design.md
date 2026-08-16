# Trait Shop XRP Payment Fallback via AMM — Design (#238)

**Issue:** [#238](https://github.com/Team-Hamsa/LFG/issues/238)
**Status:** Spec
**Depends on:** Trait Shop #217 (merged). Independent of #239 (marketplace trait BRIX), but shares the AMM-quote helper.

## Problem

The Trait Shop is BRIX-only. `start_shop_buy` (`lfg_core/shop_flow.py`) mints the trait
token and creates a **destination-locked BRIX sell offer** (`brix_amount(price_brix)`);
the buyer pays by accepting it. A wallet holding no BRIX cannot buy at all — there is no
XRP path and no error explaining why the accept will fail.

The trait-swap fee path already solves this exact problem
(`swap_flow.detect_swap_payment` + `xrpl_ops.buy_and_burn`): auto-select BRIX vs XRP by
wallet balance; on the XRP path, collect XRP and route it through the BRIX/XRP AMM with a
cross-currency Payment (`send_max` = collected XRP, deliver BRIX to the issuer = burned).
The buyback is silent — never surfaced to the user.

## Design

### Payment-path detection (reuse the swap pattern)

At `POST /api/shop/buy`, after pricing, detect the path exactly like
`detect_swap_payment` but against the shop's BRIX price:

- Buyer holds `>= price_brix` BRIX (`xrpl_ops.get_trustline_balance` on
  `TOKEN_CURRENCY_HEX`/`TOKEN_ISSUER_ADDRESS`) → **BRIX path** (today's flow, unchanged).
- Otherwise → **XRP path**: quote
  `get_amm_xrp_cost(TOKEN_CURRENCY_HEX, TOKEN_ISSUER_ADDRESS, price_brix)` and multiply by
  `SWAP_XRP_FEE_BUFFER` (reuse the existing buffer config; no new env var), rounding up to
  6 decimals. If the AMM can't quote, fail the session before minting anything
  ("pricing unavailable"), mirroring the swap path's RuntimeError.

Extract the shared detection into a helper (e.g. `xrpl_ops.detect_brix_or_xrp(wallet,
brix_amount)` or a small `lfg_core/brix_payment.py`) so swap and shop use one
implementation; swap's behavior must not change.

### XRP path mechanics — offer stays the payment vehicle

Keep the one-signature UX: on the XRP path the destination-locked sell offer is
denominated in **XRP drops** (`xrp_to_drops(quoted_xrp)`) instead of the BRIX
`IssuedCurrencyAmount`. Everything else — mint, supply row, `Expiration`
(`SHOP_OFFER_TTL_SECONDS`), destination lock, XUMM accept, signer==buyer check,
settlement via `run_deposit` — is identical.

After settlement succeeds (order flips to `settled`), fire
`xrpl_ops.buy_and_burn(price_brix, max_xrp=quoted_xrp)` **best-effort** (log-only on
failure, never blocks the order — same posture as the swap path). This converts the
collected XRP into BRIX through the AMM and burns it at the issuer, so the economics stay
BRIX-denominated regardless of what the buyer paid with. On testnet where the app wallet
is the issuer, `buy_and_burn` already no-ops (`self-issuer-noop`).

Buyback trigger placement: on the *settlement* transition (both the poll path in
`advance_shop_buy` and the sweep's retry path in `sweep_shop_orders`), guarded so it
fires at most once per order (see the `buyback_done` flag below).

### Store changes (`shop_store` / `shop_orders`)

New columns (self-migrating `ALTER TABLE`, like `identities.user_token`):

- `pay_with TEXT` — `"BRIX"` | `"XRP"`; existing rows read as BRIX.
- `price_xrp TEXT` — the quoted XRP amount (NULL on the BRIX path). Audit + buyback cap.
- `buyback_done INTEGER DEFAULT 0` — set to 1 after a successful (or attempted-and-
  logged) buy_and_burn, so poll + sweep can't double-fire it.

`price_brix` keeps recording the canonical BRIX price on both paths — pricing,
`shop_count` feedback, and `supply_changes` are unchanged.

### Session / API surface

`ShopBuySession` gains `pay_with` and `price_xrp`. The buy-start response and status
polls include both, so the Activity can show "Price: 12 BRIX (~0.14 XRP)" on the XRP
path. No new endpoints.

### Sweep interaction

The expiry sweep (`sweep_shop_orders`) needs no structural change: an expired XRP-path
order is cancelled/burned/supply-reversed identically (no BRIX ever moved). The rescue
branch (accept landed despite local timeout) settles as usual and then owes the buyback,
same as the poll path.

### Invariants

- `SourceTag=2606160021` + memos on every tx — automatic via the existing offer/burn/
  payment builders; the buyback Payment already stamps `ACTION_BUY_AND_BURN`.
- Supply accounting unchanged: shop mint = `+1` row, revert/expiry burn = `-1`,
  conservation `census == genesis + Σ supply_changes` holds on both paths.
- Buyback is best-effort and silent; a failed buyback leaves collected XRP in the app
  wallet (logged) — never a user-visible failure.

## Out of scope

- Letting BRIX holders *choose* XRP (detection is silent/automatic, like swaps).
- Marketplace trait listings (#239).
- Partial-BRIX payment (holds some BRIX but < price): treated as no-BRIX → XRP path.
