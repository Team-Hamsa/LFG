# DEX Integration (order-book, backend) — Design

**Issue:** #45 · **Date:** 2026-07-05 · **Status:** Draft
**Sibling:** docs/superpowers/specs/2026-07-05-amm-backend-design.md (issue #47)

## 0. Naming drift — LFGO → BRIX

Issue #45 says "LFGO token trading" / "LFGO pairs". Same drift the AMM spec
(§0) reconciled: the live economy token is **BRIX**
(`config.SWAP_OFFER_CURRENCY_HEX = 4252495800000000000000000000000000000000`,
lfg_core/config.py:162-165; issuer `config.SWAP_OFFER_ISSUER`,
config.py:165). LFGO is the legacy mint-payment token with no market. This
spec targets the **BRIX/XRP** pair only.

## 1. Critical framing — one routing engine, one sibling spec

On XRPL, the AMM and the order-book DEX are **not two venues**. A single
payment/offer-crossing engine routes every trade across both AMM pools and
resting book offers, taking whichever gives the better rate at each step:

- The AMM spec's swap (self-Payment with `SendMax`, AMM-spec §3.2) **already
  executes against book liquidity** when the book beats the pool. It is,
  functionally, a market order.
- An `OfferCreate` that crosses the spread also consumes AMM liquidity via
  the same engine.

So "market orders" from issue #45's scope are **already shipped by the AMM
spec**. Building a second market-order path (`OfferCreate` +
`tfImmediateOrCancel`) would duplicate the swap endpoint with near-identical
semantics and a worse UX (IoC can partially fill then vanish, whereas the
swap Payment is quote-bounded all-or-nothing). **Decision: no separate
market-order path.** `/api/amm/swap` is the market order; this spec's UI/docs
just say so.

### 1.1 Overlap table (design question 1)

| #45 scope item | Disposition | Where |
|---|---|---|
| Market orders via XUMM | **AMM-spec-covered — drop from this spec** | `POST /api/amm/swap` (AMM spec §3.2): quote-bounded self-Payment, routes across book+AMM. `tfImmediateOrCancel` OfferCreate rejected as duplicate (§1). |
| Limit orders (resting `OfferCreate`) | **DEX delta — build** | §3.1 |
| Cancel open offers (`OfferCancel`) | **DEX delta — build** | §3.3 |
| List my open offers (`account_offers`) | **DEX delta — build** (prereq for cancel + order-state UI) | §3.2 |
| Order book depth (`book_offers`) | **DEX delta — build** | §4 |
| Trade history per wallet | **Partial exists; thin delta + honest deferral** | §5 |
| XUMM payload machinery, SourceTag, status polling | **AMM-spec-covered — reuse** | `xumm_ops._create_xumm_payload` stamps `SourceTag=2606160021` on every non-SignIn txjson (lfg_core/xumm_ops.py:146-149); payload-status polling per AMM spec §3.2 (sign-in pattern, app.py:928-1010 vintage). |
| BRIX trustline precondition + TrustSet offer | **AMM-spec-covered — reuse** | `xrpl_ops.get_trustline_balance` (lfg_core/xrpl_ops.py:255) + the AMM spec's `{"error": "no_trustline", "trustset": {...}}` response shape — surfaces reuse one handler. |
| Slippage bounds / price quoting | **AMM-spec-covered** for market path; limit orders need none (price IS the limit). | AMM spec §3.2, `quote_sell_brix`. |
| Pool/pair stats | **AMM-spec-covered** | `GET /api/amm`. Book depth (§4) complements it; no duplication. |

Everything below is the **true DEX delta** only.

## 2. New module: `lfg_core/dex_ops.py`

Read-side XRPL calls, mirroring `amm_ops` style. All requests go to
`config.WS_URL` (lfg_core/config.py:79) — `book_offers`, `account_offers`,
and `tx` are standard rippled methods, not clio-only (the clio-only caveat
covers `nft_info`/`nft_exists` per CLAUDE.md). Money discipline throughout:
XRP as **integer drops** (`int`), BRIX as **`Decimal`** from ledger strings,
never `float` — same rule as AMM spec §2.1.

## 3. Orders (write path, user-signed via XUMM)

### 3.1 Limit orders — `POST /api/dex/order` (authed, `require_wallet` app.py:308)

Request: `{"side": "buy" | "sell", "brix": "1000", "price_xrp_per_brix": "0.012", "passive": false?}`

The server computes the XRP leg as integer drops from
`Decimal(brix) * Decimal(price) * 1_000_000`, rounding so the user's limit
is **never violated**:

- **buy → round the XRP leg DOWN** (floor). The XRP leg is `TakerGets` —
  what I give. Worked example: buy 1000 BRIX at limit 0.012 → the leg must
  be **at most** 12,000,000 drops (effective price ≤ 0.012 XRP/BRIX).
  Rounding up to 12,000,001 would pay > 0.012/BRIX — above the limit.
- **sell → round the XRP leg UP** (ceil). The XRP leg is `TakerPays` — what
  I receive. Sell 1000 BRIX at 0.012 → the leg must be **at least**
  12,000,000 drops (effective price ≥ 0.012); rounding down would
  under-charge below the limit.

Non-dividing example: 777 BRIX × 0.0123 XRP/BRIX = 9,557,100 drops exactly,
but e.g. × 0.0123456789 ≈ 9,592,592.5 drops → buy floors to 9,592,592, sell
ceils to 9,592,593. Pure Decimal/int; no float.

**TakerGets/TakerPays orientation** (design question 2) — from the *offer
creator's* perspective: `TakerGets` = what the taker receives = what **I
give**; `TakerPays` = what the taker pays = what **I receive**.

*Buy BRIX with XRP* (I give XRP, I receive BRIX):

```json
{
  "TransactionType": "OfferCreate",
  "TakerGets": "12000000",
  "TakerPays": {"currency": "4252495800000000000000000000000000000000",
                "issuer": "<BRIX issuer>", "value": "1000"},
  "SourceTag": 2606160021
}
```

*Sell BRIX for XRP* (I give BRIX, I receive XRP):

```json
{
  "TransactionType": "OfferCreate",
  "TakerGets": {"currency": "<BRIX hex>", "issuer": "<BRIX issuer>", "value": "1000"},
  "TakerPays": "12000000",
  "SourceTag": 2606160021
}
```

`Account` omitted (XUMM injects the signer, per AMM spec §3.2); after
signing, verify `response.account == request["wallet"]`. `SourceTag` is
stamped centrally by `_create_xumm_payload` (xumm_ops.py:146-149) — new code
never sets it manually; tests assert its presence anyway.

**Flags:**
- Default: **no flags** (0). A plain OfferCreate crosses anything at-or-better
  (including AMM liquidity) and rests the remainder — this is the normal
  limit-order semantic and the right default.
- `passive: true` → `tfPassive` (0x00010000 = 65536): rests without consuming
  equal-priced offers; exposed because it's free to support and useful for
  market-making, but off by default.
- `tfImmediateOrCancel` / `tfFillOrKill`: **not exposed** (§1 — the AMM swap
  endpoint is the market-order path). `tfSell`: not exposed in MVP (exact-in
  sell semantics; adds a mode without user demand).

**Preconditions (fail-closed, before any XUMM call, AMM-spec posture §3.2):**
- `side=buy` with no BRIX trustline → 200
  `{"error": "no_trustline", "trustset": {payload triple}}` (identical shape
  to AMM buy_brix — surfaces reuse the handler). A buy offer can't deliver
  BRIX to you without a line.
- `side=sell` → trustline balance must be ≥ `brix`
  (`get_trustline_balance`, xrpl_ops.py:255); else 400
  `{"error": "insufficient_brix", "balance": "<Decimal string>"}`.
- Price/amount validation: both > 0; `brix` ≤ 15 significant digits (IOU
  precision); reject drops legs that round to 0.
- XRP funding/reserve math (each resting offer consumes one owner-reserve
  increment, currently 0.2 XRP) is **left to the ledger** — same rationale
  as AMM deposit `single_xrp` (AMM spec §3.2): duplicating reserve
  accounting server-side is not worth it; a rare `tec` is honest.

Response: standard `{uuid, xumm_url, qr_url}` payload triple; polled via the
shared payload-status endpoint (§3.4).

### 3.2 Open offers — `GET /api/dex/orders` (authed)

Design question 3, listing half. `account_offers(account=wallet)` via
`WS_URL`, filtered to offers where the pair is BRIX/XRP in either
orientation (drop the user's unrelated offers on other pairs). Response per
offer:

```json
{
  "seq": 812345,
  "side": "sell",
  "brix_remaining": "640",
  "drops_remaining": "7680000",
  "price_xrp_per_brix": "0.012",
  "brix_original": null,
  "funded": true,
  "flags": {"passive": false, "sell": false}
}
```

- **`seq` is the cancel handle** (§3.3): `account_offers` returns each
  offer's `seq` (the sequence of the OfferCreate that made it) directly —
  no tx-meta archaeology needed for the list-then-cancel flow. (For
  immediate post-placement display, the signed payload's tx can also be
  fetched once via the `tx` method and the `Sequence` + meta read — but the
  authoritative path is always a fresh `account_offers`.)
- `*_remaining` come from the offer's current `taker_gets`/`taker_pays` —
  the **ledger decrements these in place on partial fills**, which is how
  order state is derived (§6).
- `funded`: from `taker_gets_funded` when rippled includes it (unfunded
  offers); `brix_original` is null unless we later join history (§5) —
  honest field, not fabricated.

No cache: per-wallet, authed, low traffic, and must be fresh right after a
cancel/place. (Caching per-wallet is exactly the cache-cardinality trap —
unbounded keys — that the leaderboard needed eviction machinery for,
app.py:463-481. Skip it.)

### 3.3 Cancel — `POST /api/dex/cancel` (authed)

Request: `{"seq": 812345}`. Precondition: the seq must appear in a fresh
`account_offers` for the authed wallet (fail-closed: don't send the user to
Xaman to cancel an offer that's already gone — 404
`{"error": "offer_not_found"}`; it may have just filled, which the UI reads
as good news).

```json
{
  "TransactionType": "OfferCancel",
  "OfferSequence": 812345,
  "SourceTag": 2606160021
}
```

Cancelling a nonexistent offer is a no-op success on-ledger (`tesSUCCESS`),
so even a race past the precondition check is harmless.

### 3.4 Payload status — shared

`GET /api/dex/payload/{uuid}`: same pattern as the AMM spec §3.2 / sign-in
flow — module dict `dex_payloads[uuid] = {wallet, kind, seq?, created_at}`
pruned by age, `xumm_ops.get_payload_status` → `{opened, signed, expired}`,
post-sign account check. If the AMM PR lands first with a generic payload
registry, **reuse it instead of adding a parallel dict** (implementation
plan flags this merge point).

**No journaling** (design question 6): the AMM spec's argument applies
verbatim — "every op here is a single user-signed atomic tx; the ledger
either applied it or didn't" (AMM spec §3.2). OfferCreate and OfferCancel
are exactly that. Unsigned payloads expire (`options.expire`, 10 min);
nothing server-side to roll back. This is unlike the economy flows'
multi-step journaling and deliberately so.

## 4. Book depth — `GET /api/dex/book` (public)

Design question 4. Two `book_offers` calls per refresh over `WS_URL`:

- **asks** (people selling BRIX): `taker_gets = BRIX`, `taker_pays = XRP`
- **bids** (people buying BRIX): `taker_gets = XRP`, `taker_pays = BRIX`

`limit` 50 each; use `taker_gets_funded`/`taker_pays_funded` when present
(unfunded offers otherwise overstate depth). Aggregate into price levels
(quality rounded to 6 dp of XRP-per-BRIX; amounts summed as Decimal/int):

```json
{
  "network": "testnet",
  "pair": "BRIX/XRP",
  "bids": [{"price_xrp_per_brix": "0.0098", "brix": "1500", "drops": "14700000", "offers": 2}],
  "asks": [{"price_xrp_per_brix": "0.0102", "brix": "800",  "drops": "8160000",  "offers": 1}],
  "best_bid": "0.0098", "best_ask": "0.0102", "spread": "0.0004",
  "amm_price_xrp_per_brix": "0.0100",
  "as_of_ledger": 12345678,
  "stale": false
}
```

`amm_price_xrp_per_brix` is read from the AMM endpoint's cached `PoolState`
when available (null otherwise) — the book alone understates real depth
because the AMM fills between the visible quotes; surfacing the pool price
beside the book is the honest presentation.

**Caching:** module-level `(monotonic_ts, book)` tuple, **15s TTL** —
literally one key per process, since there is exactly one pair and both
sides are fetched together. This follows the AMM spec's single-key-cache
lesson (§3.1: keyed-eviction machinery is overkill for a single pool) and
avoids the cache-cardinality trap outright: **do not** key by side, depth
param, or anything request-derived. Staleness: on fetch failure serve a ≤60s
cache with `"stale": true`; beyond that 503 `{"error": "dex_unavailable"}`.
Empty book is NOT an error — `{bids: [], asks: []}` is a valid, honest
response (likely state on fresh testnet).

## 5. Trade history per wallet (design question 5) — honest assessment

**What exists (verified in code):**
`derive_brix_events` (lfg_core/history_events.py:273) derives per-holder
BRIX balance deltas from RippleState node diffs in tx metadata
(`_brix_deltas`, history_events.py:240) — it is **transaction-type-blind on
the balance side**, so a DEX fill that moves BRIX **is captured** for every
affected holder, not just issuer-adjacent wallets: any tx touching a BRIX
RippleState appears in the BRIX issuer's `account_tx`, which is a backfill
source and is dual-written by the live listeners (CLAUDE.md, ledger-history
section). **But** the `kind` classifier (history_events.py:283-293) has no
OfferCreate branch — DEX fills fall into the `else` bucket and are
**mislabeled `"amm_swap"`**. So: captured, yes; queryable as DEX trades, no.

**Thin delta (build):**
1. In `derive_brix_events`, classify `ttype == "OfferCreate"` →
   `kind = "dex_trade"` before the amm fallthrough. **`OfferCancel` is
   deliberately excluded**: a cancel is not a trade, and since it moves no
   BRIX it produces no RippleState delta and therefore no row at all —
   classifying it would be dead code at best, a mislabel at worst if a
   bundled tx ever surfaced a delta. (Note: a Payment that
   routes through the book still books as `"payment"`/`"amm_swap"` — the
   engine is unified and per-leg venue attribution from meta is out of
   scope; documented, not hidden.)
2. Re-run `scripts/derive_history_events.py --network <net>` (rebuilds
   `nft_events`/`brix_events` from raw `xrpl_txs` — no chain re-scrape,
   CLAUDE.md).
3. `GET /api/dex/history` (authed): reads `brix_events WHERE account = wallet
   AND kind = 'dex_trade' ORDER BY ts DESC LIMIT 50` via
   `lfg_core/history_store.py` — same DB the leaderboards already read.
   **Column names verified** against the actual schema
   (lfg_core/history_store.py:42-50 CREATE TABLE, columns `tx_hash, account,
   counterparty, delta, kind, ts`, PK `(tx_hash, account)`; insert column
   list `_BRIX_EV_COLS`, history_store.py:138) — the wallet column IS
   `account` and the timestamp IS `ts` (integer unix), indexed by
   `idx_brixev_ts` (history_store.py:51). The `kind` column's schema comment
   lists the existing enum; `dex_trade` is a new value — TEXT column, no
   CHECK constraint, so no migration needed (update the comment).

**Deferred with rationale:** per-fill detail (price per fill, counterparty
offer, fee legs) requires parsing Offer-node diffs out of each tx's meta —
a meaningful derivation project with its own edge cases (multi-path fills,
autobridging through other IOUs). The delta above gives signed BRIX amounts,
counterparty, and timestamps per fill, which satisfies "trade history per
wallet" for MVP; fill-price derivation is a follow-up issue, mirroring the
AMM spec deferring 24h volume (§2.2).

**Known wart (pre-existing, not worsened):** `_brix_deltas` uses `float` for
deltas (history_events.py:262-266) — an existing convention in the derived
events table. This spec does not introduce new float money anywhere; fixing
the historical float is out of scope.

## 6. Offer lifecycle & failure posture (design question 6)

Partial fills are **normal** for resting offers — not failures, no recovery
needed. State is derived, never stored server-side:

| UI state | Derivation |
|---|---|
| `pending_sign` | payload created, `signed` false, not expired |
| `open` | seq present in `account_offers`, remaining == placed amounts (when known) |
| `partially_filled` | seq present, remaining < placed (ledger decrements `taker_gets`/`taker_pays` in place; without the placed amount, the UI shows remaining and labels it "open — N remaining", which is always truthful) |
| `filled` or `cancelled` | seq absent from `account_offers`. Disambiguate best-effort from `brix_events` history (a `dex_trade` row near the disappearance ⇒ filled); otherwise show "closed". Honest > precise. |
| `unfunded` | `taker_gets_funded` == 0 — offer exists but can't execute (owner spent the funds elsewhere) |

Failure ordering (mirrors AMM spec §6): reads = cache → live → stale-≤60s →
503, never invent; writes = preconditions → payload (SourceTag central) →
sign-or-expire → status poll verifies `signed && account == wallet`. Zero
server-side partial state.

## 7. Surfaces (minimal — issue says backend only)

- **Discord `/dex`** (surfaces/discord_bot/commands.py): public embed from a
  new `LFGServiceClient.dex_book()` → `GET /api/dex/book` — best bid/ask,
  spread, AMM price, top 3 levels each side, footer network+ledger. Placing
  orders from Discord is a follow-up (the QR-in-channel UX needs design);
  the embed links into the Activity.
- **Activity widget hooks:** the API shapes above are the contract
  (`/api/dex/book` public; `order`/`orders`/`cancel`/`history` authed).
  Trading UI is a follow-up issue; a read-only book card may ship.

## 8. Verified vs assumed

**Verified (read in repo):** SourceTag stamping (xumm_ops.py:146-149);
`get_trustline_balance` (xrpl_ops.py:255); `derive_brix_events` kind gap
(history_events.py:283-293); `_brix_deltas` type-blind capture + float wart
(history_events.py:240-268); `brix_events` schema columns
(history_store.py:42-51, 138); config constants (config.py:79, 162-165, 176,
193); route/`require_wallet` patterns (app.py:308, 1254-1289); AMM spec+plan
content on origin/main.

**Assumed — verify during implementation:**
- `account_offers` includes per-offer `seq` and `taker_gets_funded` fields
  as described (rippled-documented; confirm against a live testnet response
  in Task 2's integration check).
- OfferCancel of a missing seq is `tesSUCCESS` (rippled-documented; harmless
  either way given the precondition check).
- Book `quality` field semantics vs recomputing price from
  gets/pays — plan says recompute from funded amounts (self-contained,
  avoids per-direction quality-orientation mistakes).

## 9. Non-goals

- No market-order endpoint (that's `/api/amm/swap` — §1).
- No pairs other than BRIX/XRP; no LFGO market.
- No tfSell/IoC/FoK flags exposed; no order-placement UI in Discord.
- No per-fill price derivation or venue attribution (follow-up).
- No changes to AMM endpoints, buy_and_burn, or economy flows.
