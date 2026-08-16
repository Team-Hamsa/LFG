# AMM Integration (backend) — Design

**Issue:** #47 · **Date:** 2026-07-05 · **Status:** Draft

## 0. Naming drift — LFGO → BRIX

Issue #47 was written when the economy token was called "LFGO" and speculates
about an "LFGO/XRP or LFGO/BRIX pair". Neither exists. The live economy token
is **BRIX** (currency hex `4252495800000000000000000000000000000000`,
`config.SWAP_OFFER_CURRENCY_HEX`, lfg_core/config.py:162-165), and the pool is
**BRIX/XRP**:

- **Testnet pool:** `rLUnD5mskBnHfwFxCjakDA3RVgK584XQXG` — 50 XRP : 5,000 BRIX,
  0.5% fee, created by `scripts/testnet_amm_setup.py`.
- **Mainnet pool:** `rn6TaseGA12G2W1oJyN7Vpx6crQVXhuRZY` — 104 XRP : 19,238 BRIX,
  1% fee (launch notes).

This spec targets **BRIX/XRP only**. LFGO (the legacy mint-payment token) has
no pool and is out of scope.

## 1. Inventory — what already exists

| Issue scope item | Status | Where |
|---|---|---|
| Query AMM pool state | **Partial** | `xrpl_ops.get_amm_xrp_cost` (lfg_core/xrpl_ops.py:278-298) calls `AMMInfo` but only returns an exact-output XRP quote; pool reserves / LP supply / fee are read then discarded. `scripts/testnet_amm_setup.py:116,148` also queries `AMMInfo` (setup-time only). |
| Deposit/withdraw via XUMM | **Missing** | No `AMMDeposit`/`AMMWithdraw` anywhere in the repo. |
| Swap via AMM via XUMM | **Partial (bot-signed only)** | `xrpl_ops.buy_and_burn` (xrpl_ops.py:301-340) does a cross-currency Payment with `send_max` from the **bot wallet** — the swap-fee buyback path (consumed by lfg_core/swap_flow.py:326, mint_flow.py:231, quoted at swap_flow.py:66-74). No **user-signed** AMM swap exists. |
| Pool stats API / embed | **Missing** | No `/api/amm*` route (lfg_service/app.py:1254-1289). `scripts/snapshot_balances.py:98-106` tracks `BRIX_AMM_ACCOUNT` balances daily but is offline tooling, not an API. |
| Discord `/amm` + Activity widget hooks | **Missing** | surfaces/discord_bot/commands.py has register/link/letsgo only. |
| Per-network pool config | **Partial** | `config.BRIX_AMM_ACCOUNT` (config.py:176) exists but is optional env, only consumed by snapshot script and leaderboard system-account filter (app.py:484-494). |
| SourceTag | **Done (infra)** | `xumm_ops._create_xumm_payload` stamps `SourceTag` on every non-SignIn txjson (lfg_core/xumm_ops.py:146-149); bot-signed txs set `source_tag=config.SOURCE_TAG` (xrpl_ops.py:327 etc.). New payloads inherit this automatically. |

**Explicit non-build (Q6):** the swap-fee/mint-fee XRP path is fully covered by
`get_amm_xrp_cost` + `buy_and_burn` and is NOT touched. We also do NOT build a
24h-volume indexer from scratch — see §3.1.

## 2. New module: `lfg_core/amm_ops.py`

Small, self-contained; keeps `xrpl_ops.py` from growing another concern.

### 2.1 `get_pool_state() -> PoolState | None`

```python
@dataclass(frozen=True)
class PoolState:
    account: str            # AMM pseudo-account
    xrp_drops: int          # INTEGER drops (never float)
    brix_value: Decimal     # IOU amounts are decimal strings on-ledger
    lp_currency: str        # 40-hex LP token code
    lp_issuer: str          # == account
    lp_supply: Decimal
    trading_fee: int        # units of 1/100000 (raw ledger value)
    ledger_index: int
```

- Request: `AMMInfo(asset=XRP(), asset2=IssuedCurrency(currency=config.SWAP_OFFER_CURRENCY_HEX, issuer=config.SWAP_OFFER_ISSUER))` — by **asset pair**, not by
  pool account, exactly like xrpl_ops.py:286. This makes `BRIX_AMM_ACCOUNT`
  env optional for reads: the response's `amm["account"]` is authoritative and
  we log a WARNING if it disagrees with the configured value (config drift).
- **Endpoint: `config.WS_URL`** (Q3). `amm_info` is a standard rippled method,
  not clio-only — the existing production quote path already uses `WS_URL`
  (xrpl_ops.py:284) and works on both networks. The clio-only caveat
  (CLAUDE.md) applies to `nft_info`/`nft_exists` only.
- Money discipline: XRP kept as **integer drops** end-to-end; converted to XRP
  Decimal only at the presentation edge. BRIX/LP amounts are `Decimal` from
  the ledger's string values — no float ever.
- Derived (computed by callers, not stored): spot price
  `drops / 1_000_000 / brix_value` XRP-per-BRIX; TVL ≈ `2 * xrp_drops` (XRP
  side doubled — standard constant-product approximation, documented in the
  API response).
- Returns `None` on any error, including `actNotFound` (no pool). **Fail
  closed:** callers surface "pool unavailable", never a stale/zero price.

### 2.2 24h volume (Q on "24h volume" in the issue)

We do **not** scrape the chain. `history_<net>.db` (lfg_core/history_store.py)
already archives raw `xrpl_txs` for the BRIX issuer, and every AMM swap
against the pool is a Payment/OfferCreate touching the pool account. Phase 1
ships **without** 24h volume (field `volume_24h: null` reserved in the API
shape). Phase 2 (optional follow-up issue): add the AMM account as a backfill
source in `scripts/backfill_history.py` and derive a `amm_trades` view. This
keeps #47 shippable without a listener change.

## 3. Service API (lfg_service/app.py)

### 3.1 `GET /api/amm` — public pool stats

Follows the leaderboard pattern: public (no auth), module-level TTL cache.

```json
{
  "network": "testnet",
  "pool_account": "rLUnD5...",
  "xrp": "50.000000",          // string, 6dp — from integer drops
  "brix": "5000",              // string decimal
  "price_xrp_per_brix": "0.01",
  "tvl_xrp": "100.000000",     // 2 * xrp side (approximation, documented)
  "lp_supply": "500000",
  "lp_token": {"currency": "03AB...", "issuer": "rLUnD5..."},
  "trading_fee_bps": 50,       // trading_fee/10 → basis points for humans
  "volume_24h": null,          // reserved (Phase 2)
  "as_of_ledger": 12345678
}
```

- **Caching (Q3):** simple `(monotonic_ts, PoolState)` module global with
  **15s TTL** — one key per process (single pool), so the leaderboard's
  keyed-eviction machinery (app.py:463-481) is overkill; a bare tuple suffices.
  15s not 60s because price feeds a signing quote (deposit/swap previews) and
  a minute-stale price on a thin pool is worse than one WS round-trip per 15s.
  On fetch failure with a live cache entry ≤60s old, serve it with
  `"stale": true`; beyond that, 503 `{"error": "amm_unavailable"}`.
- **No pool** (fresh testnet reset, `actNotFound`): 503 with
  `{"error": "amm_unavailable", "reason": "no_pool"}` — surfaces render
  "Pool not available on this network". No feature flag needed (Q5): absence
  of the pool IS the flag, and reads are pair-derived so no env is required.

### 3.2 `POST /api/amm/deposit` · `POST /api/amm/withdraw` · `POST /api/amm/swap` — authed, XUMM-signed

All three are `@require_wallet` (app.py:308) and return the standard payload
triple `{uuid, xumm_url, qr_url}` from `xumm_ops._create_xumm_payload`
(xumm_ops.py:142-165) — which **already stamps `SourceTag=2606160021`**
(xumm_ops.py:148-149) on every txjson. New helpers in `xumm_ops.py`:
`create_amm_deposit_payload`, `create_amm_withdraw_payload`,
`create_amm_swap_payload`. `Account` is intentionally omitted from txjson —
XUMM fills the signer's account (matches existing Payment payload,
xumm_ops.py:186-190); after signing we verify `response.account ==
request["wallet"]` before reporting success (mirrors sign-in handling,
app.py:979-1010).

**Status polling (Q4):** reuse the exact sign-in pattern (app.py:928-1010):
a module dict `amm_payloads[uuid] = {wallet, kind, created_at}` pruned by age,
plus `GET /api/amm/payload/{uuid}` calling `xumm_ops.get_payload_status`
(xumm_ops.py:224-245) → `{opened, signed, expired}`. A payload never signed
simply expires (XUMM `options.expire`, we set 10 min) — server state is a
prunable dict entry, nothing on-ledger, nothing to roll back. **No
server-side journaling is needed** (unlike economy flows): every op here is a
single user-signed atomic tx; the ledger either applied it or didn't.

#### AMMDeposit txjson (Q2)

Request body: `{"mode": "double" | "single_xrp" | "single_brix", "xrp": "10.5"?, "brix": "1000"?, "slippage_bps": 100?}`

MVP supports two modes (both documented in xrpl.org AMMDeposit):

*Double-sided (default, recommended — no price impact):*
```json
{
  "TransactionType": "AMMDeposit",
  "Asset":  {"currency": "XRP"},
  "Asset2": {"currency": "<BRIX hex>", "issuer": "<BRIX issuer>"},
  "Amount":  "<drops>",                       // integer drops string
  "Amount2": {"currency": "...", "issuer": "...", "value": "<brix>"},
  "Flags": 1048576,                           // tfTwoAsset
  "SourceTag": 2606160021
}
```
The server computes the paired amount from the cached pool ratio when the
caller supplies only one side, then inflates the *other* side by
`slippage_bps` (default 100 = 1%) so the tx clears if the pool moves —
`tfTwoAsset` semantics are "up to Amount/Amount2 at the current ratio", so
overshoot is safe (the ledger takes the ratio-correct portion).

*Single-sided:* same skeleton with only `Amount` (XRP drops) or only `Amount2`
(BRIX), `Flags: 524288` (`tfSingleAsset`). Response includes a warning field
`"price_impact_warning": true` when the deposit exceeds 1% of its pool side.

**Deposit preconditions (fail-closed — same posture as withdraw/swap):** any
mode contributing BRIX (`double`, `single_brix`) checks the caller's BRIX
trustline first via `xrpl_ops.get_trustline_balance(wallet,
config.SWAP_OFFER_CURRENCY_HEX, config.SWAP_OFFER_ISSUER)`
(xrpl_ops.py:255-275):
- No trustline (`None`) → 200 `{"error": "no_trustline", "trustset":
  {payload triple}}` — identical shape to the swap buy_brix response, so
  surfaces reuse one handler.
- Trustline present but the BRIX contribution (explicit or server-paired,
  **including** the slippage overshoot) exceeds the balance → 400
  `{"error": "insufficient_brix", "balance": "<Decimal string>"}`.
- `single_xrp` skips the BRIX checks; XRP funding/reserve math is left to
  the ledger (a rare `tec` there is honest; duplicating reserve accounting
  server-side is not worth it).

This prevents sending the user to Xaman for a guaranteed `tec` failure.

We do **not** use `tfLPToken` / `LPTokenOut` modes in MVP — specifying exact
LP-out requires client-side LP math that duplicates ledger rounding; amount-in
modes are strictly simpler and sufficient. Listed as a rejected alternative.

#### AMMWithdraw txjson

Request body: `{"mode": "all" | "double" | "single_xrp" | "single_brix", "lp_tokens": "..."?, "xrp": ...?, "brix": ...?}`

- `all`: `Flags: 131072` (`tfWithdrawAll`) — no amounts, withdraws the user's
  entire LP position proportionally. Zero-math, zero-slippage; the default UI
  action.
- `double`: `Flags: 65536` (`tfLPToken`) + `LPTokenIn` (an
  IssuedCurrencyAmount of the pool's LP token, from `PoolState.lp_currency/
  lp_issuer`) — proportional withdrawal of a chosen LP amount.
- single-sided: `Flags: 524288` + `Amount` or `Amount2` (price impact warning
  as above).

Precondition check (fail-closed): before creating the payload, read the
caller's LP trustline balance via `xrpl_ops.get_trustline_balance(wallet,
lp_currency, lp_issuer)` (xrpl_ops.py:255-275) and 400 `{"error":
"no_lp_position"}` if zero/None — avoids sending the user to Xaman for a
guaranteed `tecAMM_BALANCE` failure.

#### Swap Payment txjson (Q2 — slippage)

Request body: `{"direction": "buy_brix" | "sell_brix", "amount": "...", "slippage_bps": 100?}`

Self-Payment with AMM pathing (Destination = signer; XUMM injects Account, and
we set Destination from the authenticated `request["wallet"]`):

*buy_brix* (exact-output, mirrors `buy_and_burn`'s SendMax semantics,
xrpl_ops.py:329-330):
```json
{
  "TransactionType": "Payment",
  "Destination": "<user wallet>",
  "Amount": {"currency": "<BRIX>", "issuer": "<issuer>", "value": "1000"},
  "SendMax": "<quote_drops * (1 + slippage_bps/10000)>",
  "SourceTag": 2606160021
}
```
Quote from `get_amm_xrp_cost` (xrpl_ops.py:278) — already fee-inclusive
exact-output math; **reused, not reimplemented**.

*sell_brix* (exact-input): `Amount` = XRP drops of the quoted proceeds shaved
by slippage, `SendMax` = the BRIX being sold, **no Flags set** — a plain
all-or-nothing Payment. If the pool moves beyond the slippage haircut the tx
fails `tecPATH_PARTIAL`: fail closed, user retries with a fresh quote.
(`tfPartialPayment` (0x00020000 = 131072) + `DeliverMin` is the explicitly
rejected/future alternative — it converts a stale quote into a partial fill
instead of a clean failure, a worse default; revisit only if all-or-nothing
proves brittle in testing.)

**Sell quote — new pure function (Q1 gap):** `get_amm_xrp_cost`
(xrpl_ops.py:278-298) is exact-OUTPUT math for the XRP→BRIX direction only
and cannot quote BRIX→XRP proceeds. Add to `amm_ops` a pure function computed
from `PoolState` reserves — no extra network call; the endpoint already holds
a ≤15s-fresh PoolState:

```python
def quote_sell_brix(pool: PoolState, brix_in: Decimal) -> int:
    """Drops of XRP received for selling `brix_in` BRIX into the pool
    (constant-product exact-input; trading fee charged on the input side,
    mirroring the fee convention in get_amm_xrp_cost).
    Returns integer drops, floored — never over-promise. Decimal/int only."""
    fee = Decimal(pool.trading_fee) / 100_000          # 1/100000 units
    dx = brix_in * (1 - fee)                           # fee-adjusted input
    out = Decimal(pool.xrp_drops) * dx / (pool.brix_value + dx)
    return int(out)                                    # floor to drops
```

The payload's `Amount` is then
`quote_sell_brix(pool, brix_in) * (10000 - slippage_bps) // 10000`
(pure integer arithmetic on drops).

Precondition: buy_brix requires a BRIX trustline (else `tecPATH_DRY`); the
endpoint checks `get_trustline_balance` returning non-None and, if absent,
responds `{"error": "no_trustline", "trustset": {payload triple}}` offering a
`TrustSet` XUMM payload first (TrustSet also gets SourceTag via xumm_ops).

## 4. Surfaces (minimal, per issue "backend only")

- **Discord `/amm`** (surfaces/discord_bot/commands.py): public embed built
  from `client.amm_stats()` (new `LFGServiceClient.amm_stats()` →
  `GET /api/amm`, following the `config()`/`nfts()` pattern,
  surfaces/_client/client.py:145,336). Fields: price, XRP/BRIX reserves, TVL,
  fee, LP supply; footer shows network + `as_of_ledger`. Buttons/deposit UX
  deferred — the embed plus a link into the Activity is the MVP.
- **Activity widget hooks** (webapp): the API shapes above are the contract;
  a minimal stats card fetching `/api/amm` on load may ship, but
  deposit/withdraw/swap UI is a follow-up issue. No UI redesign.

## 5. Per-network behavior (Q5)

| | testnet | mainnet |
|---|---|---|
| Pool discovery | pair-based `amm_info` (no env needed) | same |
| `BRIX_AMM_ACCOUNT` | **no default — env-only** (currently `rLUnD5mskBnHfwFxCjakDA3RVgK584XQXG`; the account CHANGES on every testnet reset when `testnet_amm_setup.py` recreates the pool, so a hardcoded default would silently rot) | default `rn6TaseGA12G2W1oJyN7Vpx6crQVXhuRZY` (stable; cross-check + snapshots) |
| No pool (post-reset) | `/api/amm` → 503 `no_pool`; write endpoints → 503 too (checked before payload creation) | shouldn't happen; same handling |

Config addition: **mainnet-only** default for `BRIX_AMM_ACCOUNT` in config.py
(following the `_default_swap_issuer` per-network pattern,
config.py:157-165); testnet stays `None` unless set via env. Reads never need
the value (pool discovery is pair-based and `amm["account"]` from the
response is authoritative); the §2.1 drift WARNING fires **only when a value
is configured** and disagrees.

## 6. Failure ordering summary

1. **Reads:** cache → live fetch → stale-≤60s fallback → 503. Never invent.
2. **Writes:** precondition checks (trustline/LP balance/pool exists) →
   payload creation (SourceTag stamped centrally) → user signs or payload
   expires → status poll verifies `signed && account == wallet`. No partial
   state exists server-side at any step.
3. **buy_and_burn path:** untouched.

## 7. Risks & alternatives

- **Risk:** thin testnet pool → big slippage on modest amounts. Mitigation:
  price-impact warning field; server-side slippage default 1%, cap request
  values at 5000 bps.
- **Risk:** XUMM fills `Account` after we compute paired amounts — user could
  sign from a different wallet than authed. Mitigation: post-sign account
  check (same as sign-in flow); the tx itself is still safe (it's their funds).
- **Alternative rejected:** LPTokenOut deposit mode (duplicate LP math).
- **Alternative rejected:** clio for `amm_info` — no benefit; standard rippled
  method, existing code path proves WS_URL works.
- **Alternative rejected:** 24h volume in MVP (needs history-source addition;
  split to follow-up).

## 8. Non-goals

- No UI redesign; no deposit/withdraw Activity screens (widget = stats card).
- No AMMCreate/AMMBid/AMMVote/AMMDelete endpoints.
- No LFGO pool. No changes to swap/mint fee paths.
- No 24h volume in MVP (`volume_24h: null` reserved).
