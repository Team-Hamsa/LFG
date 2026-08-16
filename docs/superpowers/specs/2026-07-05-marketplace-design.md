# In-App P2P Marketplace — Design (Rev 2)

**Issue:** #44 · **Date:** 2026-07-05 (Rev 2) · **Status:** Draft

## Rev 2 — what changed and why

1. **Baysed-Lab is the explicit baseline** (user direction). §0 is now a full
   survey of the Baysed marketplace — backend, data model, broker settlement,
   and **frontend** — with a wider reuse verdict. The UI's information
   architecture is adopted; its React/Tailwind code is not (the Activity is
   no-build vanilla JS themed by the LFG brand kit, §Q8).
2. **Trait marketplace added (§Q7).** P2P sale of traits between users: a
   trait is listed out of a user's Closet, **minted on-chain as a trait
   NFToken for the sale process**, sold with a **marketplace fee**, and **at
   sale time burned and credited into the buyer's Closet**. One collection,
   two listing kinds: `character` and `trait` (Baysed's multi-collection
   `collection` column generalizes to our `kind`).
3. **Fee arithmetic corrected.** XRPL `TransferFee` is in units of 1/100,000
   (0.001% steps, max 50000 = 50% — xrpl-py `nftoken_mint.py:94-100`,
   `_MAX_TRANSFER_FEE = 50000`). `NFT_TRANSFER_FEE = 7000` is therefore
   **7%**, not the 70% long claimed by CLAUDE.md and inherited by Rev 1.
   Sellers net **93%**; Rev 1's "sellers net 30% — listing supply may die"
   open product question dissolves. CLAUDE.md/README corrections ship with
   this rev.

## 0. Baseline: the Baysed-Lab marketplace

Issue #44 says to "pull and adapt existing code from the Mutation-Pieces
repo". That repo was renamed; the live checkout is `~/Baysed-Lab` (see the
consolidation audit,
`~/Baysed-Lab/docs/archive/2026-05-consolidation-audit/audit/00-overview.md`).
Its marketplace is **mature and production-deployed** (lab.baysed.club):
~4,600 LoC of feature code, ~4,000 LoC of tests, zero TODOs, heavily
issue-driven hardening.

| Layer | Where in Baysed | What it does |
|---|---|---|
| API | `services/api/app/routers/market.py` (2,659 LoC, FastAPI) | All `/api/market/*` routes; two-phase `start`/`finalize` per action |
| Data | `libs/baysed-common/baysed_common/database.py:386-566` | 6 SQLite tables: `market_listings`, `market_buy_offers`, `market_sales`, `market_fee_events`, `market_baynana_jobs`, `market_cooldown_reductions` |
| Settlement | `services/xrpl-engine/engine/signing.py:222` + `node/xrpl_ravager.js:169` | Broker-signed `NFTokenAcceptOffer` with `NFTokenBrokerFee` |
| Fees | `services/api/app/baynana_jobs.py` + worker | Broker fees accrue → AMM buyback of BAYNANA ("fee → token-buyback" loop) |
| Frontend | `services/frontend/UI/app/market/page.tsx` (1,711 LoC) + `lib/market.ts` | Next.js 15/React/Tailwind; 3 tabs (Browse / Inventory / Offers); pure-function mappers/reducers unit-tested separately |

Its sale mechanics: seller's sell offer is **destination-locked to a broker
hot wallet** with `Amount = seller_min` (list price minus 2.5% fee,
`MARKET_BROKER_FEE_BPS`, config.py:377); buyer signs a gross buy offer; the
**server-held broker key** settles via brokered `NFTokenAcceptOffer`, fee =
`min(buy − seller_min, list − seller_min)` (market.py:2536). Reconciliation
is lazy — on-action with `check_ledger=True`, on-browse without — and
settlement re-verifies everything on-ledger; idempotency comes from
`accept_txid UNIQUE` / `source_txid UNIQUE`.

### 0.1 Reuse verdict

**Adopt (as patterns, re-implemented in `lfg_core` / vanilla JS):**

| Baysed source | What it does | Reuse |
|---|---|---|
| `_market_extract_created_nft_offers` / `_market_pick_created_offer` (market.py:263-313) | Pulls the created `NFTokenOffer` LedgerIndex, amount, flags, destination out of tx `meta.AffectedNodes` | Port into `lfg_core/market_ops.py` — canonical way to learn a new listing's offer index |
| `_market_offer_exists` (market.py:366-425) | Verifies an offer index is still live via `nft_sell_offers`, with retries | Port — our fail-closed pre-buy / reconcile check |
| `_market_reconcile_listing_state` (market.py:447-489) | On-read reconcile: expired/vanished listings flip `expired`/`stale` | Adopt simplified (our listener already streams cancels/accepts) |
| start → sign → finalize two-phase (market.py:1281-1470) | Server builds txjson, user signs in Xaman, server verifies the **validated** tx before activating | Adopt as the listing/cancel/buy flow shape, using LFG's existing XUMM status-polling |
| Idempotent settlement keys (`accept_txid UNIQUE`, database.py:441) | Settlement/fee writes can't double-apply | Adopt: `market_listings.offer_index` PK; trait settlement keyed on sale tx hash |
| Frontend IA (page.tsx:99 `browse\|inventory\|offers`) + pure `lib/market.ts` + single `startXummFlow` action helper | One page, tabbed; logic extracted into tested pure functions; every action drives the same start→QR→poll helper | Adopt IA (Browse / Mine; Offers omitted — no bids in MVP) and both code patterns, in vanilla JS (§Q8) |
| `market_e2e_harness.py` | End-to-end harness polling real XUMM/ledger | Adopt the idea for the testnet smoke task |
| Fee → buyback loop (Baynana) | Accrued fees auto-buy the project token via AMM | **Future hook only**: LFG already has `buy_and_burn`; wiring issuer fee income → BRIX buy-and-burn is a natural fast-follow, not MVP |

**Not ported:**

- **Broker-key settlement / `NFTokenBrokerFee`.** Baysed needs a broker to
  capture its fee because its NFTs carry no `TransferFee`. Ours do — the
  ledger itself routes the fee to the issuer on every secondary sale (§Q2,
  §Q7), with no hot broker wallet, no settlement-liveness dependency, and no
  `temMALFORMED` broker≠seller guard (Baysed issue #366). Same fee outcome,
  strictly less machinery and custody.
- **Buy offers / bids** (Baysed's largest subsystem, market.py:1612-2131) —
  fast-follow at most; note bids would *not* force brokered mode either
  (a seller accepting a buy offer still pays TransferFee to the issuer).
- **React/Tailwind frontend code.** LFG's Activity is single-page, no-build
  vanilla JS with a strict brand kit (§Q8). We port the IA and the
  pure-function discipline, not the components.
- **Multi-collection curation** (`MARKET_ALLOWED_COLLECTIONS`, config.py:385)
  — LFG has one collection; the analogous axis is `kind ∈ {character, trait}`.
- Cooldown coupling, Ravager claims, Baynana job tables — Baysed-game
  specific.

## 1. Inventory — what LFG already has

| Need | Status | Where |
|---|---|---|
| Live token set with owner + traits | **Done** | `onchain_nfts` (`lfg_core/nft_index.py:66-82`): `nft_id PK, nft_number, owner, is_burned, attributes_json`. Helpers: `live_nfts` (:171-175), `owner_live_nfts` (:178-185). |
| Live **trait tokens** with owner + slot/value | **Done** | `trait_tokens` (Phase 4, `lfg_core/economy_store.py`; listener-maintained per CLAUDE.md): `nft_id, owner, slot, value`. This is the membership set for `kind='trait'` listings. |
| Listener freshness | **Partial** | `lfg_core/nft_listener.py` `_TYPE_TO_KIND` (:24-29) handles Mint/AcceptOffer/Burn/Modify. `NFTokenCreateOffer`/`NFTokenCancelOffer` classify to `None` → no-op. **Must extend** for listing sync. |
| Mint-for-sale (closet → on-chain token) | **Done** | `run_extract` (`lfg_core/economy_flow.py:517-591`): compose → mint (`TRAIT_TAXON=1763`, flags 9, TransferFee applied by `xrpl_ops.mint_nft:60-63`) → Closet decrement (fail-safe revert) → offer → owner accepts in Xaman. Supply-neutral. |
| Burn-to-closet (token → buyer's Closet) | **Done** | `run_deposit` (`economy_flow.py:626-689`): fail-closed on-ledger owner verify → issuer burn → Closet credit; journals `deposited_pending_closet` on partial failure. Supply-neutral. |
| Conservation accounting | **Done** | `asset_census` (`lfg_core/trait_economy.py:107-125`) already tallies `trait_tokens` alongside Closet contents — a listed (extracted) trait stays inside the census with **no auditor change**. |
| Sales history | **Done** | `nft_events` (`lfg_core/history_store.py:26-40`); derivation resolves buyer/seller incl. brokered accepts (`history_events.py:162-202`). |
| XUMM payloads + SourceTag | **Done (infra)** | `_create_xumm_payload` stamps `SourceTag` on every txjson (`lfg_core/xumm_ops.py:142-149`); `create_accept_offer_payload` (:195-205) — buy flow reuses it verbatim. No user-signed CreateOffer/CancelOffer builders yet. |
| `nft_sell_offers` usage | **Missing** | zero matches repo-wide. New helper needed (standard rippled method → `JSON_RPC_URL`). |
| Service patterns | **Done** | `require_wallet` (lfg_service/app.py:308-322), 60s cache (:464-481), executor-thread sqlite (:544-582), route table (:1260-1289). Economy sessions (extract/deposit) already run in this service with `EconomyDeps`. |
| Frontend | **Done (pattern)** | Vanilla JS panels (`showPanel`, webapp/client/app.js:159-164), `api()` wrapper (:66-73), grid renderers (swap picker :629-674, `renderCloset` :1087-1120), QR/poll flows from swap + economy ops. Brand kit: `webapp/client/style.css` (v3 "sticker" theme). |

House rigor bar (per the AMM/BRIX specs on `main`): money is INTEGER drops /
`Decimal` at edges, never float; fail-closed on unknown ledger state;
standard rippled methods → `config.WS_URL`/`JSON_RPC_URL`, clio-only methods
(`nft_info`) → `CLIO_WS_URL`. `nft_sell_offers` is standard — no clio
dependency.

## 2. Design decisions

### Q1 — Listings source of truth: **on-ledger sell offers, DB as index**

Unchanged from Rev 1, extended with `kind`. The ledger is authoritative; a
`market_listings` table (in `onchain_<net>.db`) is a **derived, droppable,
rebuildable index** — same posture as `nft_events`. No listing exists unless
a live `NFTokenOffer` ledger object backs it. **This now covers trait
listings too**: because a listed trait is first extracted to a real on-chain
token (§Q7), both kinds share one table, one listener path, one backfill,
and one on-ledger-truth posture. (Rev 1's alternative of DB-authoritative
trait listings was considered and dropped — it would have created a second,
weaker source-of-truth regime and an auditor change; extraction makes it
unnecessary.)

```sql
CREATE TABLE IF NOT EXISTS market_listings (
    offer_index   TEXT PRIMARY KEY,   -- NFTokenOffer LedgerIndex (64-hex)
    nft_id        TEXT NOT NULL,
    kind          TEXT NOT NULL,      -- 'character' | 'trait'
    seller        TEXT NOT NULL,      -- offer Owner
    amount_drops  INTEGER NOT NULL,   -- XRP-denominated only in MVP
    destination   TEXT,               -- non-NULL ⇒ hidden from browse
    slot          TEXT,               -- trait kind only (denormalized)
    value         TEXT,               -- trait kind only (denormalized)
    created_ledger INTEGER,
    created_ts    INTEGER,
    is_live       INTEGER NOT NULL DEFAULT 1,
    closed_reason TEXT,               -- sold | cancelled | stale
    settled       INTEGER,            -- trait kind: 0=burn-back pending, 1=done; NULL for characters
    buyer         TEXT                 -- sold kind: durable buyer-of-record for settlement recovery; NULL otherwise
);
CREATE INDEX IF NOT EXISTS idx_market_live ON market_listings(is_live, kind, nft_id);
```

**How rows are discovered/synced (three layers):**

1. **Listener (primary, streaming).** Extend `nft_listener._TYPE_TO_KIND`
   (:24-29) with `NFTokenCreateOffer → "offer_create"` and
   `NFTokenCancelOffer → "offer_cancel"`, plus a new
   `apply_market_tx(conn_onchain, tx)` applied by `scripts/onchain_listener.py`
   next to `apply_tx`/`apply_economy_tx`:
   - `offer_create` (sell flag, XRP `Amount`, membership check below):
     extract the **offer index** from `meta.AffectedNodes[].CreatedNode`
     where `LedgerEntryType == "NFTokenOffer"` → `LedgerIndex` (ported
     `_market_extract_created_nft_offers`, Baysed market.py:263-297) and
     upsert a live row (`kind` + slot/value from the membership lookup).
   - `offer_cancel`: `DeletedNode`s of type `NFTokenOffer` → mark those
     offer indexes `is_live=0, closed_reason='cancelled'`.
   - `accept`: the deleted sell-offer node's `LedgerIndex` →
     `is_live=0, closed_reason='sold'` (+ `settled=0` when `kind='trait'` —
     §Q7 settlement). Also delist any other live rows for that `nft_id`
     whose seller no longer owns it.

   **"Ours" filtering — collection membership, NOT taxon-from-ID.** The
   taxon in an NFTokenID is scrambled (XLS-20 obfuscation) — never decode
   it. Membership is:
   - **(a) Authoritative:** `nft_id IN onchain_nfts` → `kind='character'`;
     else `nft_id IN trait_tokens` → `kind='trait'` (slot/value read from
     that row). Neither ⇒ not ours, dropped.
   - **(b) Issuer pre-filter (cheap short-circuit):** the issuer account
     bytes in the NFTokenID (hex chars 8-48) are unscrambled — compare to
     our issuer to skip DB lookups for the overwhelmingly-foreign firehose.
     (a) remains the gate.
2. **Backfill / rebuild.** `scripts/backfill_market.py --network <net>`:
   for every live token in `onchain_nfts` **and every row in
   `trait_tokens`**, call `nft_sell_offers` (new
   `xrpl_ops.get_nft_sell_offers(nft_id)`, JSON-RPC) and rebuild the table.
   Response carries `nft_offer_index` per offer (accept `index` fallback —
   drift guard per Baysed market.py:386-390), plus amount/owner/destination/
   flags. Idempotent, same convention as `backfill_onchain.py`.
3. **Fail-closed point reconcile (money paths).** Before issuing a buy
   payload, and in `GET /api/market/listing/<offer_index>`, re-verify via
   `nft_sell_offers` that the offer is present and unchanged (ported
   `_market_offer_exists`). RPC error or ambiguity ⇒ **not available** (buy
   refused). Browse may serve slightly stale rows; buy may not.

**Seller sanity check:** a listing row is browsable only if
`market_listings.seller` equals the current owner — `onchain_nfts.owner`
for characters, `trait_tokens.owner` for traits (join condition). Offers
lingering from a previous owner are hidden before reconcile marks them.

**Why not on-ledger-only (no DB)?** Browse-with-filters needs traits ×
price × liveness over ~3.5k tokens; `nft_sell_offers` is per-NFT, so DB-free
browse is O(collection) RPCs per page. The index makes browse instant and
offline-capable.

### Q2 — Sale mode: **direct sale, user-signed; fee capture via TransferFee**

Listing txjson (user-signed via XUMM), identical for both kinds:

```json
{
  "TransactionType": "NFTokenCreateOffer",
  "Account": "<seller>",
  "NFTokenID": "<nft_id>",
  "Amount": "<drops>",          // INTEGER drops as string; XRP only in MVP
  "Flags": 1                    // tfSellNFToken; NO Destination
}
```

- **Direct**: anyone may `NFTokenAcceptOffer` the sell offer; buy is a single
  user-signed accept — atomic on-ledger, no server key on the settlement
  path. Baysed's brokered mode exists to capture a fee its tokens can't
  carry; ours carry `TransferFee`, so the ledger already pays the issuer on
  every secondary sale (below). Brokered mode's remaining benefits (bid
  matching, curation) are not MVP goals — see §0.1.
- Trade-off accepted: a direct listing is fillable by any ledger user, so
  third-party marketplaces/bots can fill our listings. The offer *is* the
  market. Our accepts carry `SourceTag 2606160021` (stamped centrally,
  xumm_ops.py:148-149); third-party fills don't and simply don't count for
  hackathon volume. (For trait listings this has a settlement consequence —
  §Q7.)
- **No `Expiration` in MVP.** Listings persist until cancelled or filled.

**Fee economics — corrected.** `TransferFee` is in units of 1/100,000
(0.001% steps), legal range 0–50000 = 0%–50% (xrpl-py
`nftoken_mint.py:94-100`; `_MAX_TRANSFER_FEE = 50000`). So
`NFT_TRANSFER_FEE = 7000` (config.py:88, applied to every transferable mint
by `xrpl_ops.mint_nft:60-63`) is **7.000%** — the "70%" in CLAUDE.md (and
Rev 1 of this spec, which inherited it and raised a seller-nets-30% product
alarm) misread the units; 70% is not even encodable (cap is 50%). On any
non-zero secondary sale where neither party is the issuer: buyer pays the
listed Amount, **issuer receives 7%, seller nets 93%**. That is a normal
royalty; no product question remains. The UI shows it transparently:
"You receive X XRP (93% — 7% collection royalty)". This spec changes no
minting config; the CLAUDE.md/README corrections land with this rev.

**This 7% TransferFee is also the marketplace's fee engine** — every sale of
every LFG token (character or trait, on our UI or any third-party venue)
routes 7% to the issuer with zero settlement infrastructure. A future
fast-follow can sweep accrued fee income into `buy_and_burn` (BRIX), which
is exactly Baysed's Baynana loop re-based onto our AMM.

### Q3 — Browse API

`GET /api/market/listings` — public (no auth), like `/api/leaderboard`.

Query params:
- `kind=character|trait` (default `character`).
- `trait=<Slot>:<Value>` — repeatable, AND across slots, OR within a slot.
  For `kind=character` this matches `attributes_json`; for `kind=trait` it
  matches the row's own `slot`/`value`.
- `min_xrp`, `max_xrp` — converted to INTEGER drops at the edge
  (`Decimal`, reject >6 dp; floats never touch money).
- `sort=price_asc|price_desc|newest` (default `price_asc`), `limit` (≤100,
  default 24), `offset`.

Implementation: executor-thread sqlite joining `market_listings (is_live=1,
destination IS NULL)` × owner table for its kind (owner==seller,
`is_burned=0` for characters); response rows carry `{nft_id, kind,
nft_number?, slot?, value?, image, attributes?, amount_drops, amount_xrp
(string), seller, offer_index}`.

**Cache — one key per (network, kind), filters applied post-cache.** Browse
filters are user-controlled; keying the cache on them is a memory-abuse
vector on a public endpoint. The cache holds only the canonical unfiltered
live join per (network, kind), TTL 60s; trait/price filter + sort +
pagination run in-process on the cached rows (a few hundred listings,
trivially cheap). Cardinality ≤ 2 networks × 2 kinds by construction.

Also: `GET /api/market/mine` (`require_wallet`) — the caller's live listings
(both kinds) + unlisted live characters (`owner_live_nfts`) + unlisted
wallet trait tokens (`trait_tokens` by owner) + **loose Closet traits**
(from economy state) so the UI can offer list/cancel/sell-from-closet per
item.

### Q4 — List / cancel / buy flows, race handling

All ops follow the swap/mint session shape: `POST` start → XUMM QR/deeplink
→ `GET …/status` polling (xumm_ops.py:224-245; pattern app.py:905).

- **List** `POST /api/market/list {nft_id, price_xrp}` (`require_wallet`):
  verify the caller owns `nft_id` (in `onchain_nfts` **or** `trait_tokens`)
  and no live listing row exists (409 otherwise); build the Q2 txjson via
  new `xumm_ops.create_sell_offer_payload(...)` (SourceTag inherited).

  **Finalize (offer-index capture).** XUMM's payload status yields a txid
  only, not meta. On `signed=true`, the status handler fetches the tx by
  hash (`tx` method on `JSON_RPC_URL`; shape per Baysed
  `_market_wait_for_validated_tx`, market.py:315-330) and:
  1. requires `"validated": true` and `meta.TransactionResult ==
     "tesSUCCESS"` before extracting the `CreatedNode` offer index
     (`extract_created_sell_offer`) and upserting the row;
  2. if not yet validated, status stays `pending`; bounded at **10 polls
     (~30s)**, then `unknown` with no row written — the listener/backfill
     self-heals once the tx validates (the listing is on-ledger truth
     either way);
  3. a `tx` lookup error writes nothing and returns `unknown` (fail-closed
     on writes).

  **Idempotency vs the listener echo:** upsert keyed on `offer_index`
  (PRIMARY KEY) — finalize write and listener write converge on one row.
- **Cancel** `POST /api/market/cancel {offer_index}`: verify the live row
  belongs to caller; payload `{"TransactionType": "NFTokenCancelOffer",
  "NFTokenOffers": [offer_index]}` via new
  `xumm_ops.create_cancel_offer_payload(...)`. On signed: mark row closed.
  For a trait listing, the extracted token simply stays in the seller's
  wallet — relistable, or depositable back into the Closet via the existing
  Deposit flow (no new machinery).
- **Buy** `POST /api/market/buy {offer_index}` (`require_wallet`):
  1. Load the live row; 404/410 if unknown or dead.
  2. **Trait gate:** if `kind='trait'`, require the buyer's Closet is
     `active` (403 `closet_required` otherwise) — the §Q7 burn-back needs a
     Closet to credit, and requiring it up front keeps the sale → Closet
     pipeline deterministic for in-app buyers.
  3. **Fail-closed on-ledger verify**: `nft_sell_offers(nft_id)` must
     contain `offer_index` with the same `amount` and no foreign
     `destination`; any mismatch, absence, or RPC failure ⇒ `410
     {"error": "listing_unavailable"}` and the row is marked stale.
  4. Issue `create_accept_offer_payload(offer_index)` (exists,
     xumm_ops.py:195-205) with price echoed in the instruction text.
  - **Race between verify and sign**: if the offer is filled/cancelled after
    the QR is issued, the buyer's accept fails on-ledger
    (`tecOBJECT_NOT_FOUND`) — no funds move. The status endpoint surfaces
    `{"state": "failed", "reason": "listing_unavailable"}` and marks the row
    stale. No retry loop, no server-side queuing.
  - On a **confirmed trait purchase**, the status handler additionally
    kicks off settlement (§Q7).

### Q5 — Sales history

`GET /api/market/history?nft_id=…` (public) reads `history_<net>.db`
`nft_events` `WHERE nft_id=? AND event IN
('sale','offer_create','offer_cancel') ORDER BY ledger_index DESC LIMIT 50`.
Derivation already resolves buyer/seller and prices, and excludes zero-price
transfers (history_events.py:82-202). No schema change.

For traits, per-`nft_id` history is near-useless (each listing is a fresh
token), so `GET /api/market/history?slot=…&value=…` serves **sold trait
listings** from `market_listings (kind='trait', closed_reason='sold')` —
the slot/value denormalization exists for exactly this.

### Q6 — What we do NOT build

- **No escrow of user assets, no broker wallet, no server key on the
  settlement path** — list/cancel/accept are user-signed; the issuer key
  acts only in the already-issuer-operated trait economy ops (extract mint,
  settlement burn — §Q7), exactly as it does today for
  Harvest/Assemble/Equip/Extract/Deposit.
- **No buy offers / bids / counter-offers** (Baysed's largest subsystem) —
  fast-follow at most.
- **No brokered mode / `NFTokenBrokerFee`** — fee capture is TransferFee
  (§Q2); revisit only if a fee *above* the mint-time TransferFee is ever
  wanted on in-app sales.
- **No fee→buyback sweep** (Baynana analogue) in MVP — future hook onto
  `buy_and_burn`.
- **No BRIX/IOU pricing** — XRP drops only (INTEGER end-to-end).
- **No listing expirations, no `nft_events` schema change, no minting-fee
  change.**

### Q7 — Trait marketplace: Closet → mint-for-sale → burn-to-Closet

The user-facing loop: *list a trait out of your Closet; it is minted
on-chain as an NFT for the sale process (carrying the marketplace fee); when
it sells, it is burned and the trait lands in the buyer's Closet.*

The entire loop composes from shipped Phase-4 machinery plus the Q1–Q4
marketplace core; the on-chain trait token is a **transient settlement
vehicle** whose lifetime is the listing's lifetime.

**1. List from Closet** (`POST /api/market/trait/list {slot, value,
price_xrp}`, `require_wallet`): a two-step wizard over existing flows —
1. **Extract** — run the existing `ExtractSession`/`run_extract`
   (economy_flow.py:517): verifies the caller's active Closet holds a loose
   `(slot, value)`, composes + mints the trait token (`TRAIT_TAXON=1763`,
   flags 9 = burnable+transferable, `TransferFee = NFT_TRANSFER_FEE` = 7%),
   decrements the Closet (fail-safe revert on failure), and offers the token
   to the seller — **Xaman signature 1** (accept). Supply-neutral; the token
   immediately appears in `trait_tokens`, keeping `asset_census`
   conservation intact with **zero auditor changes**
   (trait_economy.py:107-125).
2. **List** — the standard Q4 list flow on the freshly-owned token —
   **Xaman signature 2** (sell offer). The listener/finalize row lands with
   `kind='trait'` + slot/value.

The wizard presents both QRs in sequence as one "Sell trait" action; each
step is independently recoverable (an extracted-but-unlisted token is just a
normal Phase-4 trait token — listable or depositable later, surfaced under
`mine`). An **already-extracted wallet trait token lists in one signature**
via the plain Q4 flow.

**2. The marketplace fee = TransferFee.** The sale token carries the same
7% `TransferFee` every LFG token gets at mint (xrpl_ops.py:60-63). On
purchase the ledger routes 7% of the price to the issuer and 93% to the
seller — atomically, inside the buyer's single `NFTokenAcceptOffer`. This is
the "marketplace broker fee" with the broker deleted: no
`MARKET_BROKER_SEED`, no settlement liveness, no fee-bypass hole (the fee is
baked into the token, so even an off-market resale of a listed-then-cancelled
token pays the same 7%). If a *different* trait-market fee is ever wanted,
`mint_nft` grows an optional `transfer_fee` override (3 lines) and the
extract-for-market path passes it — deliberately not MVP.

**3. Buy** — the plain Q4 buy flow + the active-Closet gate (Q4 step 2).

**4. Settlement — burn + Closet credit.** On a validated trait sale the
token must leave the buyer's wallet and become a Closet asset. This is
**exactly `run_deposit`** (economy_flow.py:626) executed *on behalf of the
buyer*: fail-closed on-ledger owner verify (owner must now be the buyer) →
issuer burn (tokens are burnable, flags 9) → Closet credit; partial failure
journals `deposited_pending_closet` for recovery, and the DB
`trait_tokens`/`closet_assets` moves keep the census conserved.

Triggers, in order of preference:
- **Primary — buy status handler:** when the buyer's accept validates
  (`tesSUCCESS`), the service marks the row `closed_reason='sold',
  settled=0` and immediately runs the deposit session; on success
  `settled=1`. The buyer sees "Trait added to your Closet" in the same
  polling UI.
- **Backstop — settlement sweep:** a small periodic service task (asyncio,
  every ~2 min) scans `market_listings (kind='trait', closed_reason='sold',
  settled=0)` and retries the deposit. This heals service restarts
  mid-settlement **and third-party fills** (a direct offer is fillable by
  any ledger user who never touches our API; the listener still marks the
  row sold/unsettled).
- **Degraded case:** the buyer has no active Closet (possible only for
  third-party/off-app buyers — in-app buys gate on it). The sweep's deposit
  precondition fails cleanly and leaves the token in the buyer's wallet as
  a perfectly ordinary Phase-4 trait token; they can register, accept a
  Closet, and Deposit manually. `settled` stays 0 and the sweep marks the
  row after N attempts (journaled) rather than retrying forever.

**5. Cancel / relist** — Q4 cancel; the token stays in the seller's wallet
(relist = one signature; return-to-Closet = existing Deposit).

**Why not mint-at-purchase / DB-authoritative listings** (considered): a
sell offer needs an existing token, so a DB-only listing would force the
*issuer* to hold the token and the sale proceeds (custody of funds, payout
liveness, a Payment leg to the seller) — strictly worse than extraction on
every axis this repo cares about, and it would need a new census location
for "escrowed" traits. Extraction keeps the ledger authoritative, the census
untouched, and the seller paid atomically by the ledger itself.

### Q8 — UI: Baysed IA, LFG brand kit

No React/Tailwind port — the Activity is single-page no-build vanilla JS.
What we take from Baysed's frontend is its *shape*, restyled entirely by the
existing brand kit (`webapp/client/style.css`, v3 "sticker" theme: `--sticker`
paper-frame shadows, Fredoka headings, Inter body, JetBrains Mono numerics,
the sampled logo accent palette, dark shell + restrained sticker treatment
on cards/CTAs/QRs/NFT art).

- **IA (from page.tsx:99):** one `market-panel` with two tabs — **Browse**
  (Characters | Traits kind toggle, trait/price filters, price-sorted
  sticker-card grid) and **Mine** (my listings with cancel; my unlisted
  characters + wallet trait tokens with "List"; my loose Closet traits with
  "Sell" → the Q7 wizard). Baysed's Offers tab is omitted (no bids).
- **Pure-function pattern (from lib/market.ts):** browse-row mapping,
  filter/sort reducers, and badge logic as plain functions separate from DOM
  code, unit-testable under the existing webapp test harness.
- **Single action helper (from `startXummFlow`):** one `marketFlow(kind,
  startPath, body)` driving start → QR render → status poll for
  list/cancel/buy/trait-sell, reusing the swap/economy QR machinery.
- Grid renderers cloned from the swap picker (app.js:629-674) /
  `renderCloset` (:1087-1120); in-app overlay for confirmations (never
  `window.confirm` — silent no-op in Discord's sandboxed iframe).
- Disclosures: list screens show "You receive X XRP (93% — 7% collection
  royalty)"; the trait-sell wizard labels its two signatures ("1 of 2:
  claim your trait token · 2 of 2: post your listing").

## 3. Module layout

| File | Contents |
|---|---|
| `lfg_core/market_ops.py` (new) | `extract_created_sell_offer` (meta → offer index), `verify_sell_offer(nft_id, offer_index, expected_drops)` fail-closed check, drops/XRP edge conversion (Decimal) |
| `lfg_core/market_store.py` (new) | `market_listings` DDL (incl. `kind`/`slot`/`value`/`settled`) + upsert/close/settle/browse-query helpers on `onchain_<net>.db` |
| `lfg_core/xrpl_ops.py` | `get_nft_sell_offers(nft_id)` (standard method, `JSON_RPC_URL`) |
| `lfg_core/xumm_ops.py` | `create_sell_offer_payload`, `create_cancel_offer_payload` (SourceTag automatic) |
| `lfg_core/nft_listener.py` | classify + `apply_market_tx` for offer_create/offer_cancel/accept, membership via `onchain_nfts` ∪ `trait_tokens` |
| `scripts/backfill_market.py` (new) | rebuild `market_listings` from `nft_sell_offers` sweep over characters + trait tokens; idempotent |
| `lfg_service/app.py` | routes: listings, mine, history, list(+status), cancel(+status), buy(+status incl. trait settlement), trait/list wizard; settlement sweep task |
| `webapp/client` | `market-panel` (Browse/Mine tabs, kind toggle, filters), trait-sell wizard, pure-function helpers, `marketFlow` action driver — all on the sticker brand kit |

## 4. Risks

- **Listener gap → phantom listing**: mitigated by browse-time owner join
  and mandatory pre-buy on-ledger verify; worst case a 410 at buy.
- **Trait settlement is post-sale, not atomic**: the burn+credit runs after
  the ledger sale. Mitigations: journaled `run_deposit` (existing recovery
  posture), `settled` flag + sweep retries, buyer-side Closet gate for
  in-app buys, and the degraded outcome is safe (buyer holds a real trait
  token, manually depositable). Conservation holds in every intermediate
  state because `asset_census` counts wallet trait tokens.
- **Third-party fills of trait listings**: settle via the sweep when the
  buyer has an active Closet; otherwise degrade as above. Documented, not
  prevented (destination-locking to a broker would reintroduce everything
  §0.1 rejects).
- **Lingering foreign offers** (pre-existing sell offers): backfill imports
  them — correct, they're real listings; the seller==owner join hides dead
  ones.
- **Fee-units regression**: the 7000 = 7% correction must land everywhere
  (CLAUDE.md ×2, README ×1, this spec, plan) or a future doc reader
  re-imports the 70% myth; a plan task covers the sweep.
- **`nft_sell_offers` response-shape drift**: accept `nft_offer_index` with
  `index` fallback; fixture test.
