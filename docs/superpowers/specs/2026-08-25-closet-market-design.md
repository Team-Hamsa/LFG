# Closet Market — off-ledger trait asks & escrow-backed bids

**Status:** design approved in chat 2026-08-25, awaiting implementation plan.
**Supersedes for Closet assets:** the Extract → List → Accept trait-sell wizard
(`market_flow.TraitSellSession`, #44/#239). That path stays for tokens that are
already extracted.

## Problem

Selling a trait today costs the seller two Xaman signatures and an issuer
mint (Extract, then a native `NFTokenOffer`), the buyer one more signature
(plus an on-ramp), and the backend a burn-back into the buyer's Closet — all
to move one `closet_assets` row between two owners. There is also no way for
a user to say "I want a *Wizard Hat*, here is what I'll pay" and have another
holder fill it.

## Decisions (from the brainstorm)

| Question | Decision |
|---|---|
| Where does BRIX flow on an ask fill? | Buyer → **app wallet** → seller (option 2). The app holds funds only for the forwarding step; every hop is on-ledger and memo-linked. |
| Bids | **XRPL TokenEscrow** (XLS-85) of BRIX, `Destination` = app wallet, PREIMAGE-SHA-256 condition whose fulfillment only the backend holds. |
| Fee | `CLOSET_MARKET_FEE_BPS`, default 700 (parity with the 7 % TransferFee). Operator intends to run it at 0. |
| Matching | **Auto-cross**: resting order sets the price; bid overshoot is refunded. |
| On-chain visibility | Open orders mirrored into the Closet token's `lfg_closet` metadata by the existing async `NFTokenModify` sync. |

## Ledger prerequisites (verified on mainnet 2026-08-25)

- `Escrow`, `TokenEscrow`, `fixTokenEscrowV1` amendments: **enabled**.
- BRIX issuer `rLfgoMintj3KBcs4s2XKtquvDwEte2kYfJ` (= `config._default_brix_issuer`)
  has master disabled, regular key `rUi3o9XG…` (`.env MAINNET_REGKEY_SEED`),
  and **`lsfAllowTrustLineLocking` NOT set.** One-time ops step before
  enabling the feature: `AccountSet` `SetFlag=17` (asfAllowTrustLineLocking),
  signed with the regkey and `Account` set explicitly to the issuer. The flag
  cannot be cleared while any BRIX is locked in an escrow — treat as one-way.
- The app wallet must hold a BRIX trustline with limit ≥ the largest open
  bid (`EscrowFinish` fails otherwise). It already holds one for royalties;
  the audit script checks headroom.
- TokenEscrow moves the bidder's BRIX out of their trustline at
  `EscrowCreate` — a real lock, not a promise.

## Data model

All tables live in the per-network `onchain_<net>.db` (same file as
`closet_assets` / `trait_tokens` / `market_listings`) so a fill's asset move
and order close are one sqlite transaction. Resolved via `ECONOMY_NETWORK`.

### `closet_orders`

| column | notes |
|---|---|
| `id` TEXT PK | uuid hex |
| `side` | `ask` \| `bid` |
| `owner` | wallet |
| `slot`, `value` | Closet asset key |
| `price_brix` TEXT | `validate_brix_value` (>0, ≤6dp, cap 1e15) |
| `state` | ask: `open → filled \| cancelled`; bid: `pending_escrow → open → filled \| cancelled \| expired` |
| `created_ts`, `updated_ts` | |
| `escrow_owner_seq` INTEGER | bid only — `OfferSequence` for Finish/Cancel |
| `escrow_tx_hash` | bid only |
| `condition` TEXT | bid only, hex crypto-condition |
| `fulfillment_enc` TEXT | bid only, Fernet(`CLOSET_MARKET_ENC_KEY`) of the fulfillment |
| `cancel_after` INTEGER | bid only, ripple epoch |
| `platform` | for memos |

Indexes: `(state, side, slot, value, price_brix)` for the book;
partial unique `(owner, slot, value)` for open bids (one open bid per
key per wallet keeps the escrow bookkeeping simple).

### `closet_fills`

| column | notes |
|---|---|
| `id` TEXT PK | |
| `order_id` | the resting order |
| `taker_order_id` | NULL for a manual take/fill, else the crossing order |
| `seller`, `buyer` | wallets |
| `slot`, `value`, `price_brix`, `fee_brix`, `overshoot_brix` | |
| `payment_tx_hash` | ask taken by Payment |
| `escrow_finish_hash` | bid fill |
| `forward_tx_hash` | app → seller |
| `refund_tx_hash` | overshoot / failed-match refund |
| `state` | `funds_pending → funded → asset_moved → paid → mirrored` / `refund_pending → refunded` / `indeterminate` |
| `error`, `journal_path` | |

### Encumbrance

An open ask does **not** decrement `closet_assets.count`; it encumbers it:

```
available(owner, slot, value) = closet_assets.count − COUNT(open asks on that key)
```

`economy_store` gains `available_count()`; Equip, Assemble, Extract and the
existing trait-sell wizard read it instead of `count`, so a listed trait can
never be used or extracted out from under a buyer. Cancelling an ask is a
row update — instant, free, no signature.

## API (`lfg_service/app.py`, all gated `ECONOMY_ENABLED` + `CLOSET_MARKET_ENABLED`)

| endpoint | auth | sigs | notes |
|---|---|---|---|
| `GET /api/closet/book?slot=&value=` | public | – | best asks/bids per key, 60 s cache like `_MARKET_CACHE` |
| `GET /api/closet/orders/mine` | yes | – | own open orders + recent fills |
| `POST /api/closet/ask {slot,value,price_brix}` | yes | 0 | requires `available ≥ 1`; runs auto-cross |
| `DELETE /api/closet/ask/{id}` | yes | 0 | |
| `POST /api/closet/bid {slot,value,price_brix}` | yes | 1 | requires active Closet + BRIX trustline; returns `EscrowCreate` XUMM payload (push token threaded, 15-min expire) |
| `GET /api/closet/bid/{id}` | yes | – | poll; promotes `pending_escrow → open` once the escrow object is verified on-ledger, then auto-cross |
| `DELETE /api/closet/bid/{id}` | yes | 0 | see Cancel below |
| `POST /api/closet/ask/{id}/buy` | yes | 1 | Payment buyer→app, memo `lfg:closet_ask:<id>`; `detect_payment_path` (#239) supplies the XRP on-ramp for non-holders |
| `GET /api/closet/ask/{id}/buy/{fill_id}` | yes | – | poll |
| `POST /api/closet/bid/{id}/fill` | yes | 0 | any wallet with `available ≥ 1` |

Signer == session wallet is enforced on every payload (the #314 pin).
Every tx carries `SourceTag` + memos; new `memos.action` values `ask`, `bid`,
`fill`, `forward`, `refund`.

## Settlement — `lfg_core/closet_market_flow.py::settle_fill`

One routine for both sides, fail-safe ordered and journaled to
`ECONOMY_RECORDS_DIR` (same shape as `economy_flow`):

1. **Funds.** Ask: the buyer's Payment is validated `tesSUCCESS` and
   `meta.delivered_amount ≥ price` (partial payments rejected). Bid: submit
   `EscrowFinish(Owner=bidder, OfferSequence, Condition, Fulfillment)` from
   the app wallet with `PRESUBMIT_SIMULATE` and a `LastLedgerSequence`
   margin; validated `tesSUCCESS` → `funded`. Indeterminate outcome →
   `indeterminate`, never proceed; the sweep resolves it by `tx` lookup /
   ledger passing `LastLedgerSequence` (the `recover_brix_claims` rule:
   absence alone is not failure).
2. **Asset.** In one sqlite transaction: seller `closet_assets −1`, buyer
   `+1`, both orders closed, fill `asset_moved`. Buyer must have an
   **active Closet** (checked at order creation and re-checked here; if
   missing → `refund_pending`, nothing moved).
3. **Pay.** `Payment` app → seller of `price − fee` (memo
   `lfg:closet_fill:<id>`), then any bid overshoot back to the bidder.
   Fee stays in the app wallet. → `paid`.
4. **Mirror.** `closet_token.sync_closet` on both Closets (async, existing
   `ClosetError` / `ClosetMirrorError` / `ClosetIndeterminateError`
   taxonomy). The `lfg_closet` metadata block gains an `orders` list of the
   owner's open asks/bids. → `mirrored`.

Steps 3–4 are idempotent and retried by a 2-minute sweep in the existing
`_settlement_sweep_loop`; nothing after step 2 can be lost, only delayed.

### Auto-cross

Runs inside the transaction that opens an order (ask `open`, or bid
promoted to `open`): pick the best opposite order (lowest ask / highest bid,
FIFO on ties) whose price crosses; the **resting** order's price is the fill
price; bid overshoot is recorded on the fill and refunded in step 3. An
incoming order that crosses creates the fill immediately — for a bid hit by
a new ask, no human signs anything. Only a buyer taking an ask needs a
signature (the Payment).

### Races and refunds

- Payment lands after the ask was cancelled/filled → fill `refund_pending`,
  app refunds the full amount to the payer (memo `lfg:closet_refund:<id>`).
  Two buyers pay one ask → first validated Payment wins, second refunded.
- Bid cancel: before `CancelAfter` the backend runs `EscrowFinish` and
  refunds the full amount (instant from the user's view); after
  `CancelAfter` anyone may `EscrowCancel` — the sweep submits it and marks
  `expired`. Escrows that expire un-cancelled are cleaned up by the sweep.
- A bid whose `EscrowCreate` never validates within the payload expiry is
  `cancelled` at `pending_escrow` with nothing locked.

## Ops & config

- `CLOSET_MARKET_ENABLED=0` (default off), `CLOSET_MARKET_FEE_BPS=700`,
  `CLOSET_BID_TTL_SECONDS=604800`, `CLOSET_MARKET_ENC_KEY=<fernet>`
  (feature refuses to enable without it).
- `docs/ops/closet-market.md`: the `asfAllowTrustLineLocking` step, app
  trustline headroom, enc-key generation.
- `scripts/audit_closet_market.py`: per fill,
  `Σ in (payment | escrow finish) == forward + refund + fee`; every `open`
  bid's escrow object exists on-ledger with the recorded amount; app-wallet
  BRIX ≥ Σ unforwarded funds. Exit non-zero on drift; nightly cron like the
  other economy audits.
- Supply-neutral: no `supply_changes` rows; `audit_trait_economy` unchanged.

## Client (Activity)

Closet tile "Sell" → price prompt → ask (no wallet round-trip). New "Wanted"
tab/section: place a bid (one Xaman sign), and a "Fill" button on bids for
keys you hold. Book view per `(slot, value)` alongside the existing trait
listings. Cache-buster `?v=` bumps travel with the PR.

## Testing

- Pure state-machine tests for `settle_fill` with fake deps covering every
  phase failure (funds indeterminate, Closet missing at step 2, forward
  fails, mirror fails) and idempotent resume.
- Auto-cross: price rule, FIFO, overshoot, no self-cross (own bid vs own ask
  is refused).
- Encumbrance: Equip/Extract/Assemble refuse when `available == 0`.
- Condition/fulfillment round-trip with xrpl-py; `EscrowCreate` payload
  shape (IOU amount, Destination, CancelAfter, SourceTag, memos).
- `scripts/closet_market_e2e.py` against testnet (TokenEscrow needs a real
  ledger; testnet issuer flag set by `testnet_amm_setup.py`-style helper).

## Out of scope

Character asks/bids (characters keep native offers), partial fills,
multi-unit orders, price history charts.
