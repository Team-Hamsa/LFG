# DEX Integration (backend) — Implementation Plan

**Issue:** #45 · **Spec:** docs/superpowers/specs/2026-07-05-dex-backend-design.md
**Branch:** `feat/dex-backend` off `main`. **PR:** open as **draft**
(`gh pr create --draft`); flip ready (`gh pr ready`) only when settled, ≤4
ready-flips/hour; wait for CodeRabbit and resolve its findings before merge.
**Ordering note:** if the AMM PR (#47) is in flight, land it first — Task 4
reuses its payload-status registry if one exists (spec §3.4) and Task 3
reads its cached `PoolState` for `amm_price_xrp_per_brix` (spec §4; degrade
to `null` if AMM code is absent).

## Conventions (apply to every task)

- **TDD:** failing test first, watch it fail, implement, watch it pass.
- **Env-guard preamble** — every NEW test file importing `lfg_core` at module
  top MUST begin with this block, copied **verbatim** from
  `tests/test_seasons.py:1-18`:

  ```python
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
  ```

- All money: integer drops (`int`) for XRP, `Decimal` for BRIX. Grep-check no
  `float(` on any amount before each commit.
- SourceTag: no new code sets it manually — `xumm_ops._create_xumm_payload`
  (lfg_core/xumm_ops.py:146-149) stamps it; every payload test ASSERTS
  `SourceTag == 2606160021` anyway.
- Verify per task: `.venv/bin/python -m pytest tests/test_dex_ops.py
  tests/test_dex_api.py webapp/test_smoke.py -q`; full suite before PR.

## Task 1 — `lfg_core/dex_ops.py`: book + account offers (reads)

1. **Failing tests** `tests/test_dex_ops.py` (new; env-guard preamble). Fake
   `AsyncWebsocketClient` with canned responses:
   - `get_book(limit=50)` issues two `book_offers` (asks: gets=BRIX/pays=XRP;
     bids: gets=XRP/pays=BRIX) → aggregated levels; price recomputed from
     funded amounts (uses `taker_gets_funded` when present); drops are `int`,
     BRIX `Decimal`; levels sorted best-first; empty book → empty lists (not
     error); request error → `None`.
   - `get_account_offers(wallet)` filters to BRIX/XRP pairs only (canned
     response includes an unrelated USD offer — must be dropped); returns
     `seq`, side, remaining amounts, price, `funded`.
2. **Implement** per spec §2-§4 skeleton (requests via `config.WS_URL`,
   config.py:79). Price math: pure Decimal helpers, unit-tested directly.
3. Verify; ruff format.

## Task 2 — order/cancel txjson builders in `lfg_core/xumm_ops.py`

1. **Failing tests** (extend `tests/test_dex_ops.py` or new
   `tests/test_dex_payloads.py` with preamble): monkeypatch `requests.post`
   capturing posted json; assert per spec §3.1/§3.3:
   - `create_offer_payload(side="buy", brix, price)`: `TakerGets` = drops
     **string** (rounded DOWN — never pay above limit), `TakerPays` = BRIX
     IOU dict; `side="sell"` mirrored (`TakerPays` drops rounded UP — never
     receive below limit); **no `Flags` key** by default; `passive=True` →
     `Flags == 65536`; no `Account` key; `SourceTag == 2606160021`.
   - Rounding-direction unit tests on the pure drops-computation helper
     (**buy floor / sell ceil**, spec §3.1) with a non-dividing price:
     777 BRIX × 0.0123456789 ≈ 9,592,592.5 drops → buy leg 9,592,592,
     sell leg 9,592,593; also assert an exactly-dividing case (777 ×
     0.0123 = 9,557,100) is identical for both sides. Zero-drop legs
     rejected; >15 sig-digit BRIX rejected.
   - `create_offer_cancel_payload(seq)`: `OfferCancel` + `OfferSequence`,
     SourceTag present.
2. **Implement** via `_create_xumm_payload` (xumm_ops.py:142); drops math as
   pure helper.
3. Verify.

## Task 3 — `GET /api/dex/book` (public, single-key cache)

1. **Failing tests** `tests/test_dex_api.py` (new; preamble; aiohttp client
   pattern from webapp/test_smoke.py). Monkeypatch `dex_ops.get_book`:
   - JSON shape per spec §4 (string amounts, best_bid/ask/spread,
     `as_of_ledger`); `amm_price_xrp_per_brix` null when AMM state absent.
   - Second request within 15s TTL does NOT re-call (call counter) — cache
     is a bare module `(ts, book)` tuple, **one key**, no request-derived
     keying (assert two different-query requests share the cache).
   - Fetch fails, cache ≤60s → served `"stale": true`; no cache → 503
     `{"error": "dex_unavailable"}`; empty book → 200 with empty arrays.
2. **Implement** `handle_dex_book` + route in `lfg_service/app.py`
   (register in the app.py:1254-1289 block).
3. Verify incl. full `webapp/test_smoke.py` (route table changed).

## Task 4 — authed order endpoints + payload status

1. **Failing tests** (`tests/test_dex_api.py`):
   - `POST /api/dex/order`, `GET /api/dex/orders`, `POST /api/dex/cancel`
     without session token → 401 (`require_wallet`, app.py:308).
   - Order buy, no BRIX trustline (mock `get_trustline_balance` → None,
     xrpl_ops.py:255) → 200 `{"error": "no_trustline", "trustset": {...}}`
     (same shape as AMM spec — one surface handler).
   - Order sell, balance < brix → 400 `insufficient_brix`.
   - Happy path → `{uuid, xumm_url, qr_url}`; captured txjson matches Task 2
     expectations.
   - `GET /api/dex/orders` → spec §3.2 shape; NOT cached (two calls hit the
     mock twice).
   - Cancel with seq not in mocked `account_offers` → 404
     `offer_not_found`; present → OfferCancel payload triple.
   - `GET /api/dex/payload/{uuid}`: signed-by-other-wallet → not success
     (account check); unknown uuid → 404; expired prunes.
2. **Implement**: handlers + routes; payload registry = reuse the AMM PR's
   dict if merged, else module dict `dex_payloads` pruned by age (spec §3.4).
3. Verify.

## Task 5 — trade-history derivation delta + endpoint

1. **Failing tests** (extend an existing history-events test file or new
   with preamble):
   - `derive_brix_events` on a canned OfferCreate tx with BRIX RippleState
     diffs → rows with `kind == "dex_trade"` (currently falls through to
     `"amm_swap"`, history_events.py:283-293 — test must fail first).
   - **OfferCancel tx → NO `dex_trade` row** (spec §5: a cancel is not a
     trade; it moves no BRIX so `_brix_deltas` yields nothing — assert the
     result is `[]`, and that the classifier matches `OfferCreate` only).
     Payment tx unchanged (`payment`/`airdrop` untouched).
   - `GET /api/dex/history` (authed): seeded temp history DB → returns the
     wallet's `dex_trade` rows newest-first, limit 50; other wallets/kinds
     excluded. Query uses the **verified** `brix_events` columns `account`
     (wallet) and `ts` (integer unix timestamp) —
     lfg_core/history_store.py:42-51 (CREATE TABLE + idx_brixev_ts),
     `_BRIX_EV_COLS` history_store.py:138.
2. **Implement**: one-branch classifier addition (`ttype == "OfferCreate"`)
   in `lfg_core/history_events.py` (before the amm fallthrough; also update
   the `kind` schema comment at history_store.py:47) + handler reading via
   `lfg_core/history_store.py`.
3. Verify; note in PR body: post-merge op —
   `.venv/bin/python scripts/derive_history_events.py --network testnet`
   (and mainnet) to re-label historical fills; no chain re-scrape needed.

## Task 6 — Discord `/dex` embed

1. **Failing test** (surfaces test pattern): `LFGServiceClient.dex_book()`
   hits `GET /api/dex/book` (mock transport); command builds an embed with
   best bid/ask, spread, AMM price ("—" when null), top 3 levels/side,
   network footer; empty book renders "No open orders" not an error.
2. **Implement** in `surfaces/_client/client.py` + 
   `surfaces/discord_bot/commands.py` (follow the `config()`/`nfts()`
   client pattern).
3. Verify.

## Task 7 — integration check (testnet), docs, PR

1. Manual testnet pass (record outputs in PR body): place a passive sell via
   XUMM QR, see it in `/api/dex/orders` **confirming the assumed
   `account_offers` fields `seq`/`taker_gets_funded`** (spec §8), see it in
   `/api/dex/book`, cancel it, confirm gone; cross a small buy against it
   from a second wallet and confirm a `dex_trade` row appears in
   `/api/dex/history`.
2. CLAUDE.md: short DEX section (endpoints, single-key book cache, market
   orders = `/api/amm/swap`, re-derive op).
3. Full suite green; open **draft** PR; wait for CodeRabbit; resolve
   findings; then ready + merge per global PR rules.
4. Post-merge ops: pm2 restart lfg-activity (post-merge hook covers it),
   run `derive_history_events.py` per network.

## Risks

- **AMM PR merge race:** payload-registry and AMM-price coupling — Task 4/3
  each state the fallback; rebase before flipping ready.
- **Thin/empty testnet book:** all read paths treat empty as valid (tested).
- **Owner reserve per resting offer (0.2 XRP):** left to ledger (spec §3.1);
  surface the `tec` result honestly in payload status.
