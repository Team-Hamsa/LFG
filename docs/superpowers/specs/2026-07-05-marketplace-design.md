# In-App P2P Marketplace — Design

**Issue:** #44 · **Date:** 2026-07-05 · **Status:** Draft

## 0. Source-reference correction — "Mutation-Pieces"

Issue #44 says to "pull and adapt existing code from the Mutation-Pieces repo"
at `~/Mutation-Pieces`. That path does not exist. The repo was **renamed**: the
consolidation audit in the Baysed project identifies its origin as
`Team-Hamsa/Mutation-Pieces`
(`~/Baysed-Lab/docs/archive/2026-05-consolidation-audit/audit/00-overview.md`),
and the live checkout is `~/Baysed-Lab` (remote `Baysed-Lab/Baysed-Lab`). Its
marketplace is `services/api/app/routers/market.py` (2,659 lines, FastAPI) plus
`docs/MARKETPLACE_API.md`.

### 0.1 Reuse verdict: adapt *patterns*, do not port code

The Baysed marketplace is a **brokered, curated** marketplace:

- Every listing is destination-locked to a broker hot wallet
  (`Destination = MARKET_BROKER_ADDRESS`, market.py:1330-1338) and settlement
  is a **server-signed** brokered `NFTokenAcceptOffer` using
  `MARKET_BROKER_SEED` (`_market_run_broker_sale`, market.py:574-599;
  docs/MARKETPLACE_API.md "Broker settlement").
- It carries a large buy-offer/bid subsystem (market.py:1612-2131), a
  cooldown-reduction game mechanic, and an 8-state listing status machine
  wired into Baysed's `db_manager` and FastAPI dependency stack.

None of that transplants: LFG's service is aiohttp (`lfg_service/app.py`), its
posture for this feature is **everything user-signed, no custody, no hot
wallet**, and bids/cooldowns are out of scope. Porting the router would mean
rewriting ~90% of it anyway.

What **is** worth adapting (as patterns, re-implemented in `lfg_core`):

| Baysed source | What it does | Reuse |
|---|---|---|
| `_market_extract_created_nft_offers` / `_market_pick_created_offer` (market.py:263-313) | Pulls the created `NFTokenOffer` **LedgerIndex (offer index)**, amount, flags, destination out of tx `meta.AffectedNodes` | Port logic into `lfg_core/market_ops.py` — this is the canonical way to learn a new listing's offer index |
| `_market_offer_exists` (market.py:366-425) | Verifies an offer index is still live via `nft_sell_offers`, with not-found retries | Port — this is our fail-closed pre-buy / reconcile check |
| `_market_reconcile_listing_state` (market.py:447-489) | On-read reconcile: expired / vanished-on-ledger listings flip to `expired`/`stale` | Adopt the *on-read reconcile* idea (simplified: our listener already streams cancels/accepts) |
| start → sign → finalize two-step (market.py:1281-1470) | Server builds txjson, user signs in Xaman, server verifies validated tx before activating the listing | Adopt as the listing/cancel flow shape, using LFG's existing XUMM status-polling pattern instead of a finalize-by-payload-uuid endpoint |

Everything else (broker fee math, buy offers, curated-collection gating,
Baysed status model) is intentionally **not** carried over.

## 1. Inventory — what LFG already has

| Need | Status | Where |
|---|---|---|
| Live token set with owner + traits | **Done** | `onchain_nfts` (`lfg_core/nft_index.py:66-82`): `nft_id PK, nft_number, owner, is_burned, attributes_json` (JSON list of `{trait_type, value}`, written nft_index.py:125, read :140). Helpers: `live_nfts` (:171-175), `owner_live_nfts` (:178-185). No listing/offer columns — pure ownership state. |
| Listener freshness | **Partial** | `lfg_core/nft_listener.py` `_TYPE_TO_KIND` (:24-29) handles only Mint/AcceptOffer/Burn/Modify. `NFTokenCreateOffer`/`NFTokenCancelOffer` classify to `None` → `apply_tx` (:74-105) no-ops. **Must extend** for listing sync. |
| Sales history | **Done** | `nft_events` (`lfg_core/history_store.py:26-40`): `event ∈ {…, sale, offer_create, offer_cancel}`, `price_drops INTEGER`, `price_token` JSON. Derivation `derive_nft_events` (`lfg_core/history_events.py:99-234`): AcceptOffer → `sale` vs `transfer` by zero-price check (:162-202), buyer/seller from deleted offer nodes incl. brokered (:169-181). |
| Offer index in history | **Missing (known limitation)** | `offer_create` rows (:204-219) store only `NFTokenID/Amount/Destination` — **no offer index column exists in the schema**, and the `CreatedNode.LedgerIndex` is never read. Confirms the #48-review note. History is therefore *not* usable as the active-listings source. |
| XUMM payloads + SourceTag | **Done (infra)** | `_create_xumm_payload` stamps `SourceTag` on every non-SignIn txjson (`lfg_core/xumm_ops.py:142-149`, `config.SOURCE_TAG=2606160021`, config.py:193). `create_accept_offer_payload(offer_id)` exists (:195-205) — the buy flow reuses it verbatim. **No** user-signed CreateOffer/CancelOffer payload builders yet. |
| CreateOffer helper | **Bot-signed only** | `xrpl_ops.create_nft_offer` (lfg_core/xrpl_ops.py:107-142) signs with the bot wallet and reads `meta.offer_id` (:131). Marketplace listings are **user**-signed, so this is not reusable for listing — only as a reference for offer-id extraction. |
| `nft_sell_offers` / `ledger_entry` usage | **Missing** | zero matches repo-wide. New helper needed. |
| Service patterns | **Done** | `require_wallet` (lfg_service/app.py:308-322), leaderboard 60s cache (`_LB_CACHE_TTL` :465, put :472-481, read :530-535), sqlite in executor thread (:544-554, run_in_executor :582), route table (:1260-1289). |
| Frontend | **Done (pattern)** | Single-page vanilla JS: panels toggled by `showPanel` (webapp/client/app.js:159-164), `api()` fetch wrapper (:66-73), grid renderers to copy: swap picker (:629-674), `renderCloset` (:1087-1120). |

House rigor bar (per `docs/superpowers/specs/2026-07-05-amm-backend-design.md`
and `2026-07-05-brix-daily-distribution-design.md`, on `main`): **money is
INTEGER drops / Decimal for IOU, never float**; fail-closed on any unknown
ledger state; standard rippled methods go to `config.WS_URL` / `JSON_RPC_URL`,
clio-only methods (`nft_info`) to `CLIO_WS_URL` (config.py:78-84). Note
`nft_sell_offers` is a **standard** rippled method — no clio dependency.

## 2. Design decisions

### Q1 — Listings source of truth: **on-ledger sell offers, DB as index**

The ledger is authoritative; a `market_listings` table (in
`onchain_<net>.db`, alongside `onchain_nfts` and `trait_tokens`) is a
**derived, droppable, rebuildable index** — the same posture as `nft_events`
("derived, droppable, rebuildable", CLAUDE.md history section). No listing
exists unless a live `NFTokenOffer` ledger object backs it.

```sql
CREATE TABLE IF NOT EXISTS market_listings (
    offer_index   TEXT PRIMARY KEY,   -- NFTokenOffer LedgerIndex (64-hex)
    nft_id        TEXT NOT NULL,
    seller        TEXT NOT NULL,      -- offer Owner
    amount_drops  INTEGER NOT NULL,   -- XRP-denominated only in MVP
    destination   TEXT,               -- non-NULL ⇒ hidden from browse
    created_ledger INTEGER,
    created_ts    INTEGER,
    is_live       INTEGER NOT NULL DEFAULT 1,
    closed_reason TEXT               -- sold | cancelled | stale
);
CREATE INDEX IF NOT EXISTS idx_market_live ON market_listings(is_live, nft_id);
```

**How rows are discovered/synced (three layers):**

1. **Listener (primary, streaming).** Extend `nft_listener._TYPE_TO_KIND`
   (:24-29) with `NFTokenCreateOffer → "offer_create"` and
   `NFTokenCancelOffer → "offer_cancel"`, plus a new
   `apply_market_tx(conn_onchain, tx)` applied by `scripts/onchain_listener.py`
   next to `apply_tx`/`apply_economy_tx` (:74-105, :179-221):
   - `offer_create` (sell flag set, collection-membership check below, XRP
     `Amount`): extract the **offer index** from `meta.AffectedNodes[].CreatedNode`
     where `LedgerEntryType == "NFTokenOffer"` → `LedgerIndex` (ported
     `_market_extract_created_nft_offers`, Baysed market.py:263-297) and
     upsert a live row.
   - `offer_cancel`: `meta` `DeletedNode`s of type `NFTokenOffer` → mark
     those offer indexes `is_live=0, closed_reason='cancelled'`.
   - `accept` (already classified): the deleted sell-offer node's
     `LedgerIndex` → `is_live=0, closed_reason='sold'`. Also delist **any**
     other live rows for that `nft_id` whose seller no longer owns it (owner
     change already lands in `onchain_nfts` via `apply_tx` :95-103).

   **"Ours" filtering — collection membership, NOT taxon-from-ID.** The taxon
   field embedded in an NFTokenID is **scrambled** (XLS-20 taxon obfuscation
   mixes it with the mint sequence), so it cannot be read straight off the ID.
   The mechanism is:
   - **(a) Membership check (authoritative):** `nft_id IN onchain_nfts` — the
     index already holds exactly our collection, listener-fresh
     (`nft_index.py:66-82`); a CreateOffer for a token we don't index is by
     definition not ours and is dropped.
   - **(b) Issuer pre-filter (cheap, optional short-circuit):** the issuer
     account bytes in the NFTokenID (hex chars 8-48) are **not** scrambled —
     compare them to our issuer's account-ID hex to skip the DB lookup for
     the overwhelmingly-foreign firehose traffic. (a) remains the gate.
2. **Backfill / rebuild.** `scripts/backfill_market.py --network <net>`:
   for every live token in `onchain_nfts`, call `nft_sell_offers` (new
   `xrpl_ops.get_nft_sell_offers(nft_id)`, JSON-RPC on `config.JSON_RPC_URL`)
   and rebuild the table. The response carries the offer index as
   `nft_offer_index` per offer (field names per Baysed
   `_market_offer_exists`, market.py:376-390: `nft_offer_index` /
   fallback `index`), plus `amount`, `owner`, `destination`, `flags`. This is
   exactly how the offer index is obtained without tx meta.
3. **Fail-closed point reconcile (on the money paths).** Before issuing a buy
   payload, and in `GET /api/market/listing/<offer_index>`, re-verify via
   `nft_sell_offers` that the offer index is still present and unchanged
   (ported `_market_offer_exists`, Baysed market.py:366-425). RPC error or
   ambiguity ⇒ treat as **not available** (buy refused), never "probably fine".
   Browse may serve slightly stale rows (cheap, non-money); buy may not.

Staleness handling: browse rows carry `is_live` from the listener stream
(seconds-fresh, same freshness class as `onchain_nfts`); a listener gap is
healed by re-running the backfill (idempotent, same convention as
`backfill_onchain.py`). We deliberately do **not** add an offer-index column
to `nft_events` in this feature — history stays append-only-derived and the
marketplace index owns offer-index truth (avoids the #48-noted gap without a
history migration).

**Why not on-ledger-only (no DB)?** Browse-with-filters needs a join of
traits × price × liveness over ~3.5k tokens; `nft_sell_offers` is per-NFT, so
a DB-free browse is O(collection) RPCs per page load. The index makes browse
instant and offline-capable, matching `audit_layer_coverage.py`'s "index by
default, `--live` to bypass" convention.

**Seller sanity check:** a listing row is only browsable if
`market_listings.seller == onchain_nfts.owner` for that `nft_id` (join
condition). An offer left behind after an off-market transfer is thereby
hidden even before reconcile marks it stale (on XRPL, offers from a previous
owner become unfundable but can linger as objects).

### Q2 — Sale mode: **direct sale (Destination unset), user-signed; brokered rejected for MVP**

Listing txjson (user-signed via XUMM):

```json
{
  "TransactionType": "NFTokenCreateOffer",
  "Account": "<seller>",
  "NFTokenID": "<nft_id>",
  "Amount": "<drops>",          // INTEGER drops as string; XRP only in MVP
  "Flags": 1                    // tfSellNFToken; NO Destination
}
```

- **Direct**: anyone may `NFTokenAcceptOffer` the sell offer. Buy is a single
  user-signed accept — atomic on-ledger, no server key involved. This is the
  only mode compatible with the no-custody posture (§Q6): brokered mode
  requires a **server-held broker key to sign the settlement**
  (Baysed `MARKET_BROKER_SEED`, market.py:574-599) — that is a hot wallet and
  a settlement liveness dependency we explicitly do not want. Brokered mode's
  benefits (broker fee capture, sniping-resistant curation) are not MVP goals.
- Trade-off accepted: a direct listing is acceptable to *any* ledger user, so
  third-party marketplaces/bots can fill our listings. That is fine — the
  offer *is* the market. Our accepts still carry `SourceTag 2606160021`
  (stamped centrally, xumm_ops.py:148-149); third-party fills don't, and
  simply don't count for hackathon volume.
- **No `Expiration` in MVP.** Listings persist until cancelled or filled;
  cancel is one XUMM tx. (Expiration adds an expiry-sweep state with little
  MVP value; revisit with bids.)

**70% transfer-fee economics — spelled out.** `NFT_TRANSFER_FEE = 7000`
(config.py:88) is baked immutably into every minted token (applied in
`xrpl_ops.mint_nft`; `NFTokenModify` can change only the URI, not
`TransferFee`). On any non-zero-Amount secondary sale where neither party is
the issuer, the ledger routes **70% of Amount to the issuer**; the seller
nets **30%**. Buyer pays the full listed Amount. Concretely: list at 100 XRP →
buyer pays 100, issuer receives 70, seller receives 30.

That is severe enough to plausibly kill organic listing supply — sellers must
price ~3.3× their target net just to break even, which inflates sticker
prices and depresses buys. **This is an open product question for the user,
not a decision this spec makes:**

- **(a) Ship as-is**, with the UI always showing "You receive: X XRP (30% —
  70% royalty to the collection)" on the list screen and "Seller receives
  30%" context on buy. Zero code beyond honest display. Fee revenue accrues
  to the issuer account.
- **(b) Lower `NFT_TRANSFER_FEE` for future mints/re-mints** — does nothing
  for the existing ~3.5k tokens (fee is immutable per token), creating a
  two-tier market.
- **(c) Issuer-side rebate** of some fee share back to sellers — new money
  flow, out of MVP scope, and reintroduces a trust component.

MVP implements **(a) transparent display**; (b)/(c) need an explicit user
call. The spec's economics do not change any minting config.

### Q3 — Browse API

`GET /api/market/listings` — public (no auth), like `/api/leaderboard`.

Query params:
- `trait=<Slot>:<Value>` — repeatable, AND-combined across slots, OR within a
  repeated slot (matches `attributes_json` entries, parsed in Python like the
  Baysed traits filter, market.py:221-261 — SQLite JSON1 not assumed).
- `min_xrp`, `max_xrp` — converted to INTEGER drops at the edge
  (`Decimal(str(x)) * 1_000_000`, rejecting >6 decimal places; floats never
  touch money).
- `sort=price_asc|price_desc|newest` (default `price_asc`), `limit` (≤100,
  default 24), `offset`.

Implementation: executor-thread sqlite (pattern app.py:544-582) joining
`market_listings (is_live=1, destination IS NULL)` × `onchain_nfts
(is_burned=0 AND owner = seller)` on `nft_id`; response rows carry
`{nft_id, nft_number, image, attributes, amount_drops, amount_xrp (string),
seller, offer_index}`.

**Cache — one key per network, filters applied post-cache.** Unlike the
leaderboard, whose cache key space is a small enum (app.py:464-481), browse
filters are user-controlled: keying on `(network, filters, sort)` would let
arbitrary query-string combinations each pin a full result set for 60s — a
memory-abuse vector on a public endpoint. Instead the cache holds **only the
canonical unfiltered live-listings join** (listings × owner-checked
`onchain_nfts`, traits pre-parsed), one entry per network, TTL 60s. Trait/
price filtering, sorting, and pagination run in-process on the cached rows
per request — a few hundred listings at most, trivially cheap. Cardinality is
bounded by construction (≤2 keys), so no eviction machinery is needed.

Also: `GET /api/market/mine` (`require_wallet`, app.py:308-322) — the caller's
live listings + their unlisted live tokens (`owner_live_nfts`,
nft_index.py:178-185) so the UI can offer list/cancel per token.

### Q4 — Buy flow (and list/cancel), race handling

All three ops follow the swap/mint session shape: `POST` start → XUMM QR/
deeplink → `GET …/status` polling (`get_payload_status`, xumm_ops.py:224-245;
service polling pattern per `handle_swap_status`, app.py:905).

- **List** `POST /api/market/list {nft_id, price_xrp}` (`require_wallet`):
  verify caller owns `nft_id` in `onchain_nfts` **and** no live listing row
  exists (409 otherwise); build the Q2 txjson via a new
  `xumm_ops.create_sell_offer_payload(...)` (SourceTag inherited).

  **Finalize (offer-index capture) — explicit design.** XUMM's payload status
  (`get_payload_status`, xumm_ops.py:224-245) yields a **txid only, not tx
  meta**. On `signed=true`, the status handler fetches the tx by hash via the
  standard `tx` method (`{"transaction": txid, "binary": false}` on
  `config.JSON_RPC_URL`; same shape as Baysed `_market_wait_for_validated_tx`,
  market.py:315-330) and:
  1. requires `"validated": true` **and** `meta.TransactionResult ==
     "tesSUCCESS"` before extracting the `CreatedNode` offer index
     (`extract_created_sell_offer`) and upserting the row;
  2. if not yet validated, the status stays `pending` and the client poller
     retries — bounded at **10 polls (~30s)**, after which the status returns
     `unknown` and no row is written; the listener stream / backfill sweep
     self-heals the row once the tx validates (the listing is on-ledger truth
     either way);
  3. a `tx` lookup error writes nothing and returns `unknown` (fail-closed on
     writes, never a crash or a phantom row).

  **Idempotency vs the listener echo:** the upsert is keyed on
  `offer_index` (PRIMARY KEY), so the finalize write and the listener's
  `apply_market_tx` for the same tx converge on one identical row regardless
  of arrival order.
- **Cancel** `POST /api/market/cancel {offer_index}`: verify the live row
  belongs to caller; payload
  `{"TransactionType": "NFTokenCancelOffer", "NFTokenOffers": [offer_index]}`
  via new `xumm_ops.create_cancel_offer_payload(...)`. On signed: mark row
  closed. (Owners can cancel their own offers with no further checks needed —
  the ledger enforces authority.)
- **Buy** `POST /api/market/buy {offer_index}` (`require_wallet`):
  1. Load the live row; 404/410 if unknown or `is_live=0`.
  2. **Fail-closed on-ledger verify**: `nft_sell_offers(nft_id)` must contain
     `offer_index` with the **same `amount`** and no foreign `destination`;
     any mismatch, absence, or RPC failure ⇒ `410 {"error":
     "listing_unavailable"}` and the row is marked stale — the buyer never
     gets a payload for a dead/altered listing (mirrors the fail-closed
     Deposit posture, CLAUDE.md Phase 4).
  3. Issue `create_accept_offer_payload(offer_index)` (xumm_ops.py:195-205,
     already exists) with the price echoed in the payload instruction text.
  - **Race between verify and sign**: unavoidable window; if the offer is
    filled/cancelled after the QR is issued, the buyer's accept fails
    on-ledger with `tecOBJECT_NOT_FOUND` — **no funds move** (atomicity is
    the ledger's, not ours). The status endpoint surfaces this as
    `{"state": "failed", "reason": "listing_unavailable"}` by checking the
    validated tx's `TransactionResult`, and marks the row stale. The buyer
    sees "This listing was just sold or cancelled." No retry loop, no
    server-side queuing.

### Q5 — Per-NFT sales history

`GET /api/market/history?nft_id=…` (public) reads `history_<net>.db`
`nft_events` `WHERE nft_id=? AND event IN ('sale','offer_create','offer_cancel')
ORDER BY ledger_index DESC LIMIT 50`. Derivation verified: `sale` rows carry
`price_drops` (or `price_token` JSON for IOU sales), buyer (`to_addr`) and
seller (`from_addr`) resolved from the deleted offer nodes — including
brokered accepts (history_events.py:162-202); zero-price accepts are
`transfer`, correctly excluded from price history (`_is_zero_price`, :82-96).
No schema change needed. (offer_create rows lack the offer index — fine here,
history display doesn't need it.)

### Q6 — What we do NOT build

- **No escrow, no custody, no server-side settlement key** — every tx
  (list/cancel/accept) is user-signed via XUMM; the service only builds
  payloads and indexes ledger state.
- **No buy offers / bids / counter-offers** (Baysed's largest subsystem) — a
  fast-follow at most.
- **No brokered mode / marketplace fee capture** (see Q2).
- **No BRIX/IOU pricing** in MVP — XRP drops only (INTEGER end-to-end); the
  schema's `amount_drops` stays honest and browse sorting stays trivial.
  (`nft_events.price_token` already future-proofs history.)
- **No listing expirations, no `nft_events` schema change, no minting-fee
  change** (Q2 product question pending).

## 3. Module layout

| File | Contents |
|---|---|
| `lfg_core/market_ops.py` (new) | offer-meta extractor (`extract_created_sell_offer`), `verify_sell_offer(nft_id, offer_index, expected_drops)` fail-closed check, drops/XRP edge conversion (Decimal) |
| `lfg_core/market_store.py` (new) | `market_listings` DDL + upsert/close/browse-query helpers on `onchain_<net>.db` |
| `lfg_core/xrpl_ops.py` | `get_nft_sell_offers(nft_id)` (standard method, `JSON_RPC_URL`) |
| `lfg_core/xumm_ops.py` | `create_sell_offer_payload`, `create_cancel_offer_payload` (SourceTag automatic) |
| `lfg_core/nft_listener.py` | classify + `apply_market_tx` for offer_create/offer_cancel/accept |
| `scripts/backfill_market.py` (new) | rebuild `market_listings` from `nft_sell_offers` sweep; idempotent |
| `lfg_service/app.py` | 6 routes: listings, mine, history, list(+status), cancel(+status), buy(+status) |
| `webapp/client` | new `market-panel` section + grid renderer (clone of swap picker :629-674 / `renderCloset` :1087-1120), list/cancel/buy modals over `api()` |

## 4. Risks

- **Listener gap → phantom listing**: mitigated by the browse-time owner join
  and the mandatory pre-buy on-ledger verify; worst case is a 410 at buy.
- **Lingering foreign offers** (tokens with pre-existing sell offers from
  before the marketplace): backfill imports them too — correct behavior,
  they're real listings; the seller==owner join filters dead ones.
- **70% fee suppresses volume**: product question surfaced in Q2; MVP ships
  transparent display and data to inform the decision.
- **`nft_sell_offers` response-shape drift**: accept `nft_offer_index` with
  `index` fallback (as Baysed does, market.py:386-390) and cover with a
  fixture test.
