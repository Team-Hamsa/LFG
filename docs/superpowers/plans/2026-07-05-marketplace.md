# In-App P2P Marketplace — Implementation Plan (Rev 2)

**Issue:** #44 · **Spec:** `docs/superpowers/specs/2026-07-05-marketplace-design.md` (Rev 2)
**Date:** 2026-07-05

Rev 2 deltas vs Rev 1: trait marketplace (spec §Q7 — extract-to-list, buy
gate, burn-to-Closet settlement + sweep), `kind`/`slot`/`value`/`settled`
columns, Baysed-IA frontend on the brand kit (§Q8), and the TransferFee
units correction (7000 = **7%**, not 70%) propagated to all docs and UI
copy.

## Process constraints

- **TDD throughout**: every task writes the failing test first, sees it fail,
  then implements to green. No implementation before a red test.
- **Branch + draft PR**: work on `feat/marketplace` off `main`. Open the PR as
  **draft** (`gh pr create --draft`); flip ready (`gh pr ready`) only when the
  branch is settled — that is the deliberate CodeRabbit trigger. Do not merge,
  and do not call the feature done, until CodeRabbit has reviewed and its
  actionable comments are handled. Respect the ~4 ready-for-review/hour cap.
- **Env-guard preamble rule**: every NEW test file that imports `lfg_core` at
  module top MUST begin with this preamble, copied verbatim from
  `tests/test_seasons.py:1-18` (omitting it strands frozen config constants
  and breaks `webapp/test_smoke` in full-suite order):

  ```python
  # tests/test_<name>.py
  # Env-guard preamble: importing lfg_core.config freezes its constants (e.g.
  # IMG_PROXY_ALLOWED_BASES, LAYER_SOURCE) at import time; set the same defaults
  # test_smoke.py uses so collection order can't strand them. (Copy the block
  # verbatim from tests/test_server_identity_wiring.py — same keys/values.)
  import os

  os.environ.setdefault("XUMM_API_KEY", "test")
  os.environ.setdefault("XUMM_API_SECRET", "test")
  os.environ.setdefault("SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2")
  os.environ.setdefault("TOKEN_ISSUER_ADDRESS", "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
  os.environ.setdefault("TOKEN_CURRENCY_HEX", "4C46474F00000000000000000000000000000000")
  os.environ.setdefault("BUNNY_CDN_ACCESS_KEY", "test")
  os.environ.setdefault("BUNNY_CDN_STORAGE_ZONE", "test")
  os.environ.setdefault("LAYER_SOURCE", "local")
  os.environ.setdefault("BUNNY_PULL_ZONE", "nft.pullzone.example")

  import json  # noqa: E402
  ```

- **Money discipline**: prices are INTEGER drops everywhere internal;
  `Decimal` only at the API edge for XRP↔drops; a test asserts floats are
  rejected/never produced.
- **SourceTag**: no marketplace code sets SourceTag manually — tests assert
  the payload txjson carries `2606160021` via the central stamp
  (`lfg_core/xumm_ops.py:142-149`).
- **Fee copy**: any UI/doc string about the royalty says **7% / seller nets
  93%** — never 70%/30% (spec Rev 2 correction).
- Full suite (`.venv/bin/python -m pytest`) green after every task; run
  ruff format before push (pre-push hook enforces it).

## Task list

### Task 0 — docs: TransferFee units correction (no code)
Fix the 70% claims: CLAUDE.md (XRPL section "7000 basis points = 70%", Phase
4 "The 70% royalty"), README.md Phase-4 bullet. All become "7% (TransferFee
7000, units of 1/100,000)". May land directly on `main` as a docs commit
before the feature branch (repo docs policy) — listed here so the sweep is
tracked.

### Task 1 — `market_ops`: offer-meta extraction + money edge
Test: `tests/test_market_ops.py` (env-guard preamble). Fixtures: a real-shaped
`NFTokenCreateOffer` tx-meta dict with a `CreatedNode` of
`LedgerEntryType=NFTokenOffer` (adapt shape from Baysed
`_market_extract_created_nft_offers`, `~/Baysed-Lab/services/api/app/routers/market.py:263-297`).
- red: `extract_created_sell_offer(meta, nft_id)` returns
  `{offer_index, amount_drops, destination, flags}`; returns `None` for
  buy-side flags, wrong nft_id, missing CreatedNode.
- red: `xrp_to_drops_str("1.5") == "1500000"`; rejects float input, >6 dp,
  ≤0; `drops_to_xrp_str` round-trips.
- green: implement `lfg_core/market_ops.py`.

### Task 2 — `xrpl_ops.get_nft_sell_offers` + fail-closed verify
Test: `tests/test_market_verify.py` (preamble). Mock the JSON-RPC call.
- red: `get_nft_sell_offers(nft_id)` parses offers accepting
  `nft_offer_index` with `index` fallback (drift guard, Baysed
  market.py:386-390).
- red: `market_ops.verify_sell_offer(nft_id, offer_index, expected_drops)` —
  True only on present + amount match + destination is None; **False on RPC
  exception, absent offer, amount mismatch, foreign destination** (fail-closed
  matrix, one assert each).
- green: implement in `lfg_core/xrpl_ops.py` (JSON_RPC_URL, standard method)
  and `market_ops`.

### Task 3 — `market_store`: schema + upsert/close/settle/browse
Test: `tests/test_market_store.py` (preamble), tmp sqlite path.
- red: DDL idempotent (`init_db` twice) incl. `kind`, `slot`, `value`,
  `settled`; upsert live listing (character and trait shapes); close with
  reason (`settled=0` auto-set on trait sold); `mark_settled`; browse query
  joins seeded `onchain_nfts` (characters: seller==owner, is_burned=0) and
  seeded `trait_tokens` (traits: seller==owner), `destination IS NULL`;
  `kind` filter; trait filter (characters: attributes AND-across/OR-within;
  traits: slot/value match); min/max drops; three sorts; limit/offset.
- red: `unsettled_trait_sales()` returns sold+settled=0 rows only.
- green: implement `lfg_core/market_store.py` on `onchain_<net>.db`.

### Task 4 — listener: classify + `apply_market_tx`
Test: `tests/test_market_listener.py` (preamble). Fixture txs: sell
offer_create (character, trait, foreign-issuer, IOU-amount), offer_cancel
(DeletedNode), accept (character sold; trait sold).
- red: `classify_tx` returns `offer_create`/`offer_cancel` for the new types
  (extending `_TYPE_TO_KIND`, nft_listener.py:24-29) without changing
  existing kinds (regression asserts for mint/accept/burn/modify).
- red: **membership is `onchain_nfts` ∪ `trait_tokens`, NOT taxon-from-ID**
  (NFTokenID taxon is scrambled by XLS-20 obfuscation — never decode it):
  character nft_id → row `kind='character'`; trait-token nft_id → row
  `kind='trait'` with slot/value copied from `trait_tokens`; issuer-bytes
  pre-filter (hex chars 8-48, unscrambled) short-circuits foreign IDs;
  our-issuer-but-unindexed → ignored; IOU amounts → ignored.
- red: cancel closes `cancelled`; character accept closes `sold`
  (+ delists other live rows for the nft_id on owner change); **trait accept
  closes `sold` with `settled=0`**.
- green: implement in `lfg_core/nft_listener.py`; wire into
  `scripts/onchain_listener.py` next to `apply_tx`/`apply_economy_tx`.

### Task 5 — `scripts/backfill_market.py`
Test: `tests/test_backfill_market.py` (preamble), mocked `get_nft_sell_offers`.
- red: sweep over seeded `onchain_nfts` live tokens **and `trait_tokens`
  rows** rebuilds `market_listings` with correct `kind`/slot/value; re-run
  idempotent; previously-live rows absent on-ledger get closed `stale`;
  `settled` preserved across re-runs (a sold-unsettled trait row is not
  resurrected).
- green: implement script (argparse `--network`, same conventions as
  `backfill_onchain.py`).

### Task 6 — XUMM payload builders
Test: `tests/test_market_payloads.py` (preamble), mock XUMM SDK like existing
xumm tests.
- red: `create_sell_offer_payload(account, nft_id, drops)` txjson has
  `Flags=1`, string drops Amount, **no Destination**, and
  `SourceTag == 2606160021`; `create_cancel_offer_payload(account,
  offer_index)` has `NFTokenOffers=[offer_index]` + SourceTag.
- green: implement in `lfg_core/xumm_ops.py` (thin wrappers over
  `_create_xumm_payload`).

### Task 7 — service endpoints: browse + mine + history
Test: `tests/test_market_api.py` (preamble), aiohttp test client against
`create_app()` with tmp DBs (follow existing app tests' setup).
- red: `GET /api/market/listings` — `kind` param (default character),
  filters/sorts/paginates; **cache holds only the canonical unfiltered
  per-(network, kind) join, with filter/sort/pagination applied in-process
  post-cache** — tests: (i) two calls with different filters within TTL hit
  the store once (monkeypatch time); (ii) cache dict stays ≤ networks×kinds
  after many distinct filter combos (cardinality-abuse guard); bad params →
  400.
- red: `GET /api/market/mine` requires wallet (401 unauth); returns caller's
  listings (both kinds) + unlisted characters + unlisted wallet trait tokens
  + loose Closet traits (mocked economy state).
- red: `GET /api/market/history?nft_id=` returns seeded `nft_events`
  sale/offer rows, excludes `transfer`; `?slot=&value=` returns sold trait
  listings from `market_listings`.
- green: implement handlers in `lfg_service/app.py` (executor-thread sqlite
  pattern app.py:544-582; cache pattern :464-481); register routes
  (:1260-1289).

### Task 8 — service endpoints: list / cancel / buy sessions
Test: extend `tests/test_market_api.py`; mock xumm_ops + verify.
- red: `POST /api/market/list` — 409 if not owner (checks `onchain_nfts` ∪
  `trait_tokens`) or already listed; returns QR/deeplink.
- red: **list finalize (spec §Q4)** — XUMM status yields txid only, so the
  status endpoint fetches the tx by hash (`tx` method, mocked) and:
  (i) signed but not yet `validated` → `pending`, no row; (ii) validated +
  `tesSUCCESS` → offer index extracted, row upserted with correct `kind`
  (idempotent vs listener echo — PK assert); (iii) `tx` lookup raises → no
  row, `unknown`, no crash; (iv) after 10 pending polls status flips
  `unknown` (listener/backfill self-heals later).
- red: `POST /api/market/cancel` — 403 on foreign listing; on signed closes
  row.
- red: `POST /api/market/buy` — 410 when row dead, when on-ledger verify
  fails, **and when verify raises** (fail-closed); **403 `closet_required`
  when `kind='trait'` and buyer's Closet is not active**; happy path returns
  accept payload via existing `create_accept_offer_payload`; status endpoint
  maps failure to `{"state":"failed","reason":"listing_unavailable"}` and
  marks row stale.
- green: implement.

### Task 9 — trait sell wizard + settlement (burn-to-Closet)
Test: `tests/test_market_trait_flow.py` (preamble); mock EconomyDeps like
existing economy-flow tests.
- red: `POST /api/market/trait/list {slot, value, price_xrp}` — runs
  `run_extract` (existing session) then hands off to the Task-8 list flow;
  extract failure surfaces the session error and leaves no listing; an
  extracted-but-never-listed token appears under `mine` (recoverable).
- red: **settlement** — on trait buy status reaching validated `tesSUCCESS`:
  row → `sold, settled=0`, then `run_deposit(buyer, nft_id)` (mocked deps)
  runs; success → `settled=1`; deposit failure → row stays `settled=0` and
  the journal record is written (assert via records dir).
- red: **sweep** — `settle_pending_trait_sales()` retries unsettled rows;
  buyer-without-active-Closet → precondition failure leaves token in wallet,
  row marked after N attempts (no infinite retry); service startup registers
  the asyncio task (assert scheduled, pattern of existing background tasks).
- green: implement (`lfg_service/app.py` + small glue in
  `lfg_core/economy_flow.py` only if needed — prefer zero changes to the
  shipped flows).

### Task 10 — Activity frontend (manual-verify task; no-build vanilla JS)
- `market-panel` in `webapp/client/index.html` + app.js: **Browse / Mine
  tabs**, Characters|Traits kind toggle, filter bar (trait selects, price
  min/max, sort), sticker-card grid cloned from swap picker (:629-674) /
  `renderCloset` (:1087-1120) — all existing brand-kit classes (spec §Q8).
- Pure-function helpers (row mapping, filter/sort) separate from DOM code;
  single `marketFlow` start→QR→poll driver reused by list/cancel/buy/trait
  wizard.
- Mine: cancel per listing; "List" per unlisted character/trait token;
  "Sell" per loose Closet trait → two-step wizard ("1 of 2: claim your trait
  token · 2 of 2: post your listing").
- Disclosures: "You receive X XRP (93% — 7% collection royalty)"; buy modal
  shows price + royalty note; trait buy success shows "added to your
  Closet". In-app overlay for confirmations (never `window.confirm` —
  silent no-op in Discord's sandboxed iframe).
- Verify in `WEBAPP_DEV_MODE=1` mock harness; extend the mock economy with
  mock market endpoints if the harness pattern requires it.

### Task 11 — end-to-end testnet smoke + ops
- Run `scripts/backfill_market.py --network testnet`; restart
  `lfg-index-testnet` (picks up `apply_market_tx`); pm2 restart
  `lfg-activity`.
- Character smoke: list via Xaman → row appears; browse filter; buy from a
  second wallet → sold + history row; cancel path; race case (cancel after
  buy QR issued → buyer sees `listing_unavailable`).
- **Trait smoke (full §Q7 loop):** sell a loose Closet trait (2 QRs) →
  listing browsable under Traits; buy from a second registered wallet with
  an active Closet → sale settles → token burned on-ledger → trait appears
  in buyer's Closet; `audit_trait_economy` PASS (census conserved);
  cancel-then-deposit path returns a trait to the seller's Closet.
- Update CLAUDE.md (marketplace section: store, backfill, endpoints, trait
  loop, settlement sweep) in the same PR.

### Task 12 — finish
- Full suite green, ruff clean; `gh pr create --draft` with spec/plan links;
  after settle, `gh pr ready`; address CodeRabbit findings; **do not merge
  before CodeRabbit resolution**.
- Post issue #44 comment with commit-SHA permalinks to spec + plan (repo
  workflow rule).
