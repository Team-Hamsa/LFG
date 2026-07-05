# In-App P2P Marketplace — Implementation Plan

**Issue:** #44 · **Spec:** `docs/superpowers/specs/2026-07-05-marketplace-design.md`
**Date:** 2026-07-05

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
- Full suite (`.venv/bin/python -m pytest`) green after every task; run
  ruff format before push (pre-push hook enforces it).

## Task list

### Task 1 — `market_ops`: offer-meta extraction + money edge
Test: `tests/test_market_ops.py` (env-guard preamble). Fixtures: a real-shaped
`NFTokenCreateOffer` tx-meta dict with a `CreatedNode` of
`LedgerEntryType=NFTokenOffer` (adapt shape from Baysed
`_market_extract_created_nft_offers`, `~/Baysed-Lab/.../market.py:263-297`).
- red: `extract_created_sell_offer(meta, nft_id)` returns
  `{offer_index, amount_drops, destination, flags}`; returns `None` for
  buy-side flags, wrong nft_id, missing CreatedNode.
- red: `xrp_to_drops_str("1.5") == "1500000"`; rejects float input, >6 dp,
  ≤0; `drops_to_xrp_str` round-trips.
- green: implement `lfg_core/market_ops.py`.

### Task 2 — `xrpl_ops.get_nft_sell_offers` + fail-closed verify
Test: `tests/test_market_verify.py` (preamble). Mock the JSON-RPC call.
- red: `get_nft_sell_offers(nft_id)` parses offers accepting
  `nft_offer_index` with `index` fallback (drift guard).
- red: `market_ops.verify_sell_offer(nft_id, offer_index, expected_drops)` —
  True only on present + amount match + destination is None; **False on RPC
  exception, absent offer, amount mismatch, foreign destination** (fail-closed
  matrix, one assert each).
- green: implement in `lfg_core/xrpl_ops.py` (JSON_RPC_URL, standard method)
  and `market_ops`.

### Task 3 — `market_store`: schema + upsert/close/browse query
Test: `tests/test_market_store.py` (preamble), tmp sqlite path.
- red: DDL idempotent (`init_db` twice); upsert live listing; close with
  reason; browse query joins a seeded `onchain_nfts` table and enforces
  `seller == owner`, `is_burned=0`, `destination IS NULL`; trait filter
  (AND across slots, OR within slot), min/max drops, three sorts,
  limit/offset.
- green: implement `lfg_core/market_store.py` on `onchain_<net>.db`.

### Task 4 — listener: classify + `apply_market_tx`
Test: `tests/test_market_listener.py` (preamble). Fixture txs: sell
offer_create (ours + foreign-issuer + IOU-amount), offer_cancel (DeletedNode),
accept (sold).
- red: `classify_tx` returns `offer_create`/`offer_cancel` for the new types
  (extending `_TYPE_TO_KIND`, nft_listener.py:24-29) without changing
  existing kinds (regression asserts for mint/accept/burn/modify).
- red: **"ours" filtering is collection membership, NOT taxon-from-ID** (the
  NFTokenID taxon field is scrambled by XLS-20 obfuscation — never decode it):
  `apply_market_tx` upserts a live row (offer index from meta) only when
  `nft_id IN onchain_nfts`; a foreign-issuer NFTokenID (issuer bytes, hex
  chars 8-48, unscrambled — cheap pre-filter) → ignored; an nft_id with our
  issuer bytes but absent from `onchain_nfts` → ignored; IOU amounts →
  ignored; our indexed nft_id → row created; cancel closes `cancelled`;
  accept closes
  `sold` and closes other live rows for the same nft_id when owner changed.
- green: implement in `lfg_core/nft_listener.py`; wire into
  `scripts/onchain_listener.py` next to `apply_tx`/`apply_economy_tx`.

### Task 5 — `scripts/backfill_market.py`
Test: `tests/test_backfill_market.py` (preamble), mocked `get_nft_sell_offers`.
- red: sweep over seeded `onchain_nfts` live tokens rebuilds
  `market_listings`; re-run idempotent; previously-live rows absent on-ledger
  get closed `stale`. (Membership filtering is structural here — the sweep
  iterates `onchain_nfts` rows, so only our collection is ever queried; no
  taxon decoding anywhere.)
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
- red: `GET /api/market/listings` filters/sorts/paginates; **cache holds only
  the canonical unfiltered per-network join (one key per network), with
  trait/price filter + sort + pagination applied in-process post-cache** —
  tests: (i) two calls with *different* filters within TTL hit the store only
  once (monkeypatch time); (ii) cache dict size stays ≤ number of networks
  after many distinct filter combinations (cardinality-abuse guard); bad
  params → 400.
- red: `GET /api/market/mine` requires wallet (401 unauth), returns caller's
  listings + unlisted tokens.
- red: `GET /api/market/history?nft_id=` returns seeded `nft_events`
  sale/offer rows, excludes `transfer`.
- green: implement handlers in `lfg_service/app.py` (executor-thread sqlite
  pattern app.py:544-582; cache pattern :464-481); register routes
  (:1260-1289).

### Task 8 — service endpoints: list / cancel / buy sessions
Test: extend `tests/test_market_api.py`; mock xumm_ops + verify.
- red: `POST /api/market/list` — 409 if not owner or already listed; returns
  QR/deeplink.
- red: **list finalize (spec §Q4 "Finalize")** — XUMM status yields txid only,
  so the status endpoint fetches the tx by hash (`tx` method, mocked) and:
  (i) signed but tx not yet `validated` → status `pending`, no row written;
  (ii) `validated: true` + `tesSUCCESS` → offer index extracted from
  `CreatedNode` meta, row upserted (keyed on `offer_index` PK — a subsequent
  listener `apply_market_tx` of the same tx is a no-op, idempotency assert);
  (iii) `tx` lookup raises → no row, status `unknown`, no crash;
  (iv) poll bound: after 10 pending polls (~30s) status flips to `unknown`
  (listener/backfill self-heals the row later).
- red: `POST /api/market/cancel` — 403 on foreign listing; on signed closes
  row.
- red: `POST /api/market/buy` — 410 when row dead, when on-ledger verify
  fails, **and when verify raises** (fail-closed); happy path returns accept
  payload built with existing `create_accept_offer_payload`; status endpoint
  maps `tecOBJECT_NOT_FOUND`-style failure to
  `{"state":"failed","reason":"listing_unavailable"}` and marks row stale.
- green: implement.

### Task 9 — Activity frontend (manual-verify task; no-build vanilla JS)
- Add `market-panel` section to `webapp/client/index.html`; loader +
  grid renderer in `app.js` cloned from swap picker (:629-674) /
  `renderCloset` (:1087-1120); filter bar (trait selects from attributes,
  price min/max, sort); list/cancel from "mine" view; buy modal shows price,
  **"Seller receives 30% (70% collection royalty)"** disclosure, QR +
  deeplink, poll status, in-app overlay for confirmations (never
  `window.confirm` — silent no-op in Discord's sandboxed iframe).
- Verify in `WEBAPP_DEV_MODE=1` mock harness; extend `webapp` mock economy
  with mock market endpoints if the harness pattern requires it.

### Task 10 — end-to-end testnet smoke + ops
- Run `scripts/backfill_market.py --network testnet`; restart
  `lfg-index-testnet` (picks up `apply_market_tx`); pm2 restart
  `lfg-activity`.
- Manual smoke: list a testnet token via Xaman → row appears; browse filter;
  buy from a second wallet → sold + history row (`nft_events` sale via
  existing derivation); cancel path; race case (cancel after buy QR issued →
  buyer sees `listing_unavailable`).
- Update CLAUDE.md (marketplace section: store, backfill, endpoints) in the
  same PR.

### Task 11 — finish
- Full suite green, ruff clean; `gh pr create --draft` with spec/plan links;
  after settle, `gh pr ready`; address CodeRabbit findings; **do not merge
  before CodeRabbit resolution**.
- Post issue #44 comment with commit-SHA permalinks to spec + plan (repo
  workflow rule).
- Separately surface the **70%-fee product question** (spec §Q2) to the user —
  not a code task, requires an explicit decision.
