# AMM Integration (backend) — Implementation Plan

**Issue:** #47 · **Spec:** docs/superpowers/specs/2026-07-05-amm-backend-design.md
**Branch:** `feat/amm-backend` off `main`. **PR:** open as **draft**
(`gh pr create --draft`); flip ready (`gh pr ready`) only when settled, ≤4
ready-flips/hour; wait for CodeRabbit and resolve its findings before merge.

## Conventions (apply to every task)

- **TDD:** write the failing test first, watch it fail, implement, watch it pass.
- **Env-guard preamble** — every NEW test file that imports `lfg_core` at
  module top MUST begin with this block, copied **verbatim** from
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

- All money: integer drops for XRP, `Decimal` for BRIX/LP. Grep-check no
  `float(` on any amount before each commit.
- Verify per task: `.venv/bin/python -m pytest tests/test_amm_ops.py
  tests/test_amm_api.py webapp/test_smoke.py -q` plus full suite before PR.
- SourceTag: no new code sets it manually on XUMM payloads —
  `xumm_ops._create_xumm_payload` (lfg_core/xumm_ops.py:148-149) stamps it;
  each task's tests ASSERT it is present anyway.

## Task 1 — `lfg_core/amm_ops.py`: `PoolState` + `get_pool_state`

1. **Failing test** `tests/test_amm_ops.py` (new; env-guard preamble):
   - Fake `AsyncWebsocketClient` returning a canned `amm_info` result
     (amount drops string, amount2 value, lp_token, trading_fee 500, account)
     → assert `PoolState` fields, `xrp_drops` is `int`, `brix_value` is
     `Decimal`, no floats.
   - `actNotFound` error result → returns `None`.
   - Exception during request → returns `None` (and logs).
   - Configured `BRIX_AMM_ACCOUNT` mismatch → still returns state, logs
     WARNING; `BRIX_AMM_ACCOUNT` unset (`None`) → no warning (testnet's
     normal state).
   - `quote_sell_brix(pool, brix_in)` (pure function, spec §3.2): known
     reserves → exact expected integer drops. E.g. pool 50_000_000 drops /
     5000 BRIX, fee 500 (0.5%), sell 100 BRIX → dx = 99.5,
     out = 50_000_000 * 99.5 / 5099.5 = 975_585.84… → **975_585** (floored).
     Also: fee 0 sanity case; result type is `int`; no float anywhere.
2. **Implement** `lfg_core/amm_ops.py` per spec §2.1-§3.2 (request via
   `config.WS_URL`, pair-based `AMMInfo` mirroring xrpl_ops.py:284-287;
   `quote_sell_brix` as a pure Decimal/int function over `PoolState`).
3. Verify tests pass; ruff format.

## Task 2 — config: mainnet-only `BRIX_AMM_ACCOUNT` default

1. **Failing test** (in `tests/test_amm_ops.py`): mainnet default resolves to
   `rn6TaseGA12G2W1oJyN7Vpx6crQVXhuRZY` (pattern: `_default_swap_issuer`,
   lfg_core/config.py:157-165); **testnet default stays `None`** — the
   testnet pool account changes on every reset (`testnet_amm_setup.py`
   recreates it), so a baked-in testnet default would silently rot. Env
   override still wins on both networks.
2. **Implement** in `lfg_core/config.py` near line 176 (mainnet default only;
   testnet env-only).
3. Confirm `scripts/snapshot_balances.py` and app.py:484-494 still pass tests.

## Task 3 — `GET /api/amm` (stats + cache)

1. **Failing tests** `tests/test_amm_api.py` (new; env-guard preamble;
   aiohttp test client pattern from webapp/test_smoke.py):
   - Monkeypatch `amm_ops.get_pool_state` → JSON shape per spec §3.1
     (string amounts, `trading_fee_bps`, `volume_24h: null`, `as_of_ledger`).
   - Second request within TTL does NOT re-call `get_pool_state` (call counter).
   - Fetch fails with cache ≤60s old → served with `"stale": true`.
   - Fetch fails, no cache → 503 `{"error": "amm_unavailable"}`.
   - `get_pool_state` returns None (no pool) → 503 with `"reason": "no_pool"`.
2. **Implement** `handle_amm` in `lfg_service/app.py` + route registration
   (app.py:1254-1289 block); module-level `(ts, state)` cache, 15s TTL.
3. Verify; run full `webapp/test_smoke.py` (route table changed).

## Task 4 — `xumm_ops` payload builders

1. **Failing tests** (extend `tests/test_amm_ops.py` or new
   `tests/test_amm_payloads.py` with preamble): monkeypatch `requests.post`
   capturing the posted json; assert for each builder:
   - `create_amm_deposit_payload(mode=...)`: `TransactionType == "AMMDeposit"`,
     Asset/Asset2 pair, correct Flags (`tfTwoAsset` 1048576 / `tfSingleAsset`
     524288), `Amount` is a drops **string**, `SourceTag == 2606160021`.
   - `create_amm_withdraw_payload`: `tfWithdrawAll` 131072 with no amounts;
     `tfLPToken` 65536 with `LPTokenIn`; single-sided variants.
   - `create_amm_swap_payload`: buy_brix → IOU `Amount` + drops `SendMax`
     inflated by slippage; sell_brix → drops `Amount` =
     `quote_sell_brix(...) * (10000 - slippage_bps) // 10000` (integer math,
     quote from Task 1's pure function) + IOU `SendMax` = BRIX in, and
     **no `Flags` key in the txjson** (plain all-or-nothing Payment — assert
     `"Flags" not in txjson`; tfPartialPayment/DeliverMin is the rejected
     alternative per spec §3.2); `Destination` set; SourceTag present.
   - Slippage math uses `Decimal`/int only; `slippage_bps > 5000` raises.
2. **Implement** in `lfg_core/xumm_ops.py` via `_create_xumm_payload`
   (xumm_ops.py:142); slippage/pairing math as pure helper functions
   (unit-testable without XUMM).
3. Verify.

## Task 5 — authed write endpoints + payload status

1. **Failing tests** (`tests/test_amm_api.py`):
   - `POST /api/amm/deposit` without session token → 401 (require_wallet,
     app.py:308).
   - With auth + pool state mocked: returns `{uuid, xumm_url, qr_url}`;
     server computed the paired amount from pool ratio (assert captured txjson).
   - No pool → 503 before any XUMM call.
   - `deposit` mode `double`/`single_brix` with no BRIX trustline
     (`get_trustline_balance` → None) → 200 `{"error": "no_trustline",
     "trustset": {...}}` (same shape as swap); trustline present but
     requested BRIX (incl. slippage overshoot) > balance → 400
     `{"error": "insufficient_brix", "balance": ...}`; mode `single_xrp`
     skips BRIX checks (no `get_trustline_balance` call — assert via mock).
   - `withdraw` with `get_trustline_balance` → None → 400 `no_lp_position`.
   - `swap` buy_brix with no BRIX trustline → 200 with `trustset` payload.
   - `GET /api/amm/payload/{uuid}`: known uuid → proxied
     `get_payload_status` dict; unknown uuid → 404; expired entries pruned
     (mirror signin_payloads pruning, app.py:928-941).
2. **Implement** handlers + `amm_payloads` dict + routes.
3. Verify; full suite.

## Task 6 — surface client + Discord `/amm` embed

1. **Failing test**: `LFGServiceClient.amm_stats()` hits `GET /api/amm`
   (pattern: `client.config`, surfaces/_client/client.py:145). Discord embed
   builder as a pure function `build_amm_embed(stats) -> Embed` tested for
   field content (price, reserves, TVL, fee, network footer) without a live
   bot.
2. **Implement** `amm_stats` in `surfaces/_client/client.py`; `/amm` command
   in `surfaces/discord_bot/commands.py` (pattern of `letsgo`,
   commands.py:24-47) + registration alongside existing commands; graceful
   "Pool not available" message on 503.
3. Verify.

## Task 7 — docs + wrap-up

1. Update CLAUDE.md env/API notes (new endpoints, per-network AMM defaults).
2. Full suite: `.venv/bin/python -m pytest -q` (watch for env-guard
   collection-order failures; verify on clean checkout order too).
3. `/verify`-style manual pass on testnet: `curl :8000/api/amm`; create a
   deposit payload with a real session token; confirm SourceTag in the XUMM
   payload JSON; sign a tiny double-sided deposit in Xaman; `AMMWithdraw`
   `tfWithdrawAll` it back.
4. Commit spec+plan, open **draft** PR, then `gh pr ready` when settled;
   address CodeRabbit findings before merge. Post spec/plan permalinks
   (commit-SHA blob URLs) to issue #47 via `gh issue comment`.

## Explicitly not in this plan

- 24h volume derivation (follow-up issue; API field reserved as null).
- Activity deposit/withdraw/swap UI (follow-up; stats card optional).
- Any change to `get_amm_xrp_cost` / `buy_and_burn` / swap_flow / mint_flow.
