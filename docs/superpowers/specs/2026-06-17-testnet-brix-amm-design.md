# Testnet BRIX/XRP AMM Pool — Design

**Issue:** [#26 — Stand up testnet BRIX/XRP AMM pool for swap testing](https://github.com/Team-Hamsa/LFG/issues/26)
**Date:** 2026-06-17
**Status:** Approved

## Problem

The trait-swap fee flow has an XRP payment path: wallets that don't hold BRIX
pay the live AMM-quoted XRP equivalent, and the backend buys-and-burns the BRIX
off the DEX/AMM. On testnet this path is dead because **no XRP/BRIX AMM pool
exists**, so `xrpl_ops.get_amm_xrp_cost()` returns `None` and
`xrpl_ops.buy_and_burn(..., max_xrp=...)` has no liquidity to route through.
Without the pool, the swap flow cannot be tested end-to-end on testnet.

## Goal

Stand up an XRP/BRIX AMM pool on XRPL testnet with enough liquidity to quote and
clear the trait-swap fee, verify a token swap clears through it, and document the
pool for future reference. This unblocks (but does not itself perform) trait-swap
end-to-end testing.

## Scope

**In scope**
- Create the XRP/BRIX AMM on testnet from the issuer (SEED) account.
- Verify a swap clears through the pool using the production code path.
- Document the pool in `CLAUDE.md`.
- A reusable, idempotent setup script checked into the repo.

**Out of scope**
- Mainnet AMM (the real BRIX issuer `rLfgoBriX…` already has its own pool path).
- NFT trait-swap end-to-end run (needs a testnet NFT + owner wallet — separate
  follow-up).
- Any change to `main.py`, the webapp, or the swap flow code — they already read
  the pool via `AMMInfo` and need no modification.

## Background / current state (verified live, 2026-06-17)

- `XRPL_NETWORK=testnet`; RPC `https://s.altnet.rippletest.net:51234/`.
- BRIX issuer on testnet = the SEED minter account `rHb8SdDPAre5jmEQASWtZZt6PnBPtUpgTh`
  (on testnet `_default_brix_issuer` resolves to the SEED account address).
- BRIX currency hex: `4252495800000000000000000000000000000000` (`SWAP_OFFER_CURRENCY_HEX`).
- SEED account balance: ~110 testnet XRP, OwnerCount 14.
- **No XRP/BRIX AMM exists yet** (`AMMInfo` returns no pool).
- **Default Ripple is OFF** on the SEED account (`Flags = 0`) — must be enabled.
- Network reserves: base 1 XRP, increment 0.2 XRP → AMMCreate special fee = 0.2 XRP.
- The trait-swap fee is `SWAP_OFFER_AMOUNT = 10` BRIX; XRP-path fee =
  `get_amm_xrp_cost(10 BRIX) × SWAP_XRP_FEE_BUFFER (1.05)`.

## Approach (chosen)

**Issuer (SEED) account creates the AMM directly with its own BRIX.**

The SEED account is already the testnet BRIX issuer and the wallet the production
swap code signs with. It enables Default Ripple, then runs `AMMCreate` with XRP
plus self-issued BRIX. This is the standard "issuer bootstraps its own pool"
pattern: the issuer funds the token side by issuing it, so no pre-existing
trustline or balance is required, and no second account or inter-account funding
is needed.

Rejected alternative — **dedicated LP holder account**: spin up a second wallet,
fund it with XRP from SEED, set a BRIX trustline, send it BRIX, then create the
AMM from it. More moving parts and funding for zero benefit: an AMM is keyed by
its asset pair, not its creator, so the swap code's `AMMInfo(XRP, BRIX@issuer)`
lookup resolves to the same pool regardless of who created it.

## Pool parameters (chosen)

| Parameter | Value |
|---|---|
| Asset 1 | XRP, 50 XRP (50,000,000 drops) |
| Asset 2 | BRIX, 5,000 (issuer = SEED account) |
| Starting price | 0.01 XRP per BRIX |
| Trading fee | 500 (= 0.5%, in units of 1/100000) |
| 10-BRIX swap fee | ≈ 0.10 XRP (× 1.05 buffer ≈ 0.105 XRP) |

This locks 50 XRP permanently into the pool (redeemable later via `AMMWithdraw`
against the LP tokens), leaving ~55 XRP free in the SEED account for reserves and
transaction fees. BRIX is self-issued, so the token side costs nothing.

## Deliverable: `scripts/testnet_amm_setup.py`

An idempotent, re-runnable script. Safe after any testnet reset.

**Hard guard:** the script refuses to run unless `config.IS_TESTNET` is true, so
it can never touch mainnet.

**Steps:**

1. **Connect & sanity-check.** Load the SEED wallet, assert testnet, print the
   account address and XRP balance.
2. **Enable Default Ripple.** If `lsfDefaultRipple` (0x00800000) is not already
   set on the account, submit `AccountSet(SetFlag=asfDefaultRipple=8)` and wait
   for `tesSUCCESS`. Skip if already set.
3. **Check for an existing pool.** Query `AMMInfo(asset=XRP, asset2=BRIX@issuer)`.
   If a pool already exists, print its details and skip creation (idempotency).
4. **Create the pool.** Submit `AMMCreate(Amount=xrp_to_drops(50),
   Amount2=IssuedCurrencyAmount(BRIX, issuer=SEED, value="5000"),
   TradingFee=500)`. Set `Fee` explicitly to the special transaction cost —
   read `reserve_inc` from `ServerInfo` and use that many XRP (with a small
   safety margin) in drops, since autofill does not set the AMMCreate special
   fee. Wait for `tesSUCCESS`.
5. **Verify the pool.** Query `AMMInfo` again; confirm the reserves
   (≈50 XRP / 5,000 BRIX) and capture the **AMM account address** (the pool's
   on-ledger identity to document).
6. **Verify a swap end-to-end (AC #2).** Call the production path
   `xrpl_ops.get_amm_xrp_cost(BRIX, issuer, Decimal("10"))` to get a live quote,
   then `xrpl_ops.buy_and_burn(BRIX, issuer, "10", max_xrp=<quote×1.05>)`, which
   issues a cross-currency XRP→BRIX `Payment` routed through the AMM and burns
   the BRIX at the issuer. This is the exact code the trait-swap XRP-fee path
   runs in production, so success here proves the real flow. Confirm
   `tesSUCCESS` and that the pool moved / XRP was spent.
7. **Print a summary block** with the AMM account, asset pair, ratio, price, and
   trading fee — ready to paste into documentation.

**Error handling:** every transaction inspects
`meta.TransactionResult`; any non-`tesSUCCESS` aborts the script with the engine
result string. The Default-Ripple and existing-pool checks make every step safe
to re-run. The script performs no DB writes, no CDN uploads, and no edits to
application code.

## Documentation (AC #3)

Add a short "Testnet AMM" subsection to `CLAUDE.md` recording:
- AMM account address (pool ID).
- Asset pair (XRP / BRIX@`rHb8…pgTh`), starting ratio 50 XRP : 5,000 BRIX,
  price 0.01 XRP/BRIX, trading fee 0.5%.
- One-line recreate note: `python scripts/testnet_amm_setup.py`.

## Acceptance criteria mapping

- [ ] **AMM pool created on testnet with sufficient initial liquidity** →
  steps 4–5 (50 XRP : 5,000 BRIX).
- [ ] **Swap flow tested successfully end-to-end (BRIX↔XRP)** → step 6
  (`buy_and_burn` cross-currency Payment through the AMM, the production path).
- [ ] **AMM address/pool ID documented in .env or CLAUDE.md** → Documentation
  section (`CLAUDE.md`).

## Risks & notes

- **Issuer self-AMM:** issuers routinely create AMMs for their own tokens on
  XRPL; Default Ripple (step 2) is the documented prerequisite. If the ledger
  unexpectedly rejects it, the script aborts with the engine result for
  diagnosis (no silent failure).
- **`buy_and_burn` self-payment:** in this path sender = destination = issuer =
  SEED. This is intentional and mirrors production — a cross-currency payment to
  self is the canonical XRPL currency-exchange pattern, and delivering BRIX to
  its issuer burns it. If this fails, it surfaces a real production bug, which is
  exactly what we want the test to catch.
- **Liquidity is locked:** the 50 XRP is recoverable later via `AMMWithdraw`
  against the LP tokens held by the SEED account; not automated here.
