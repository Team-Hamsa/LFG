# Runbook — Mainnet MVP Launch (Minter + Trait Swapper, Closet OFF)

_Last reviewed: 2026-07-02 against main @ 7df9ded. Full test suite: 633 passed._

Scope of the MVP: **Minter** and **Trait Swapper** on mainnet. The **Closet /
trait economy ships later** and must be gated off (Blocker 4).

Review verdict: both flows are logically sound end-to-end (fail-safe ordering,
journaling, replay-safe payment verification) and SourceTag `2606160021` is
enforced on every transaction and XUMM payload (`lfg_core/xrpl_ops.py`,
`lfg_core/xumm_ops.py:148`). The blockers below are what stand between the
current deployment (testnet) and a mainnet launch.

---

## Blocker 1 — Regular-key signing (CODE)

**Problem.** The mainnet issuer `rLfgoMintj3KBcs4s2XKtquvDwEte2kYfJ` signs via a
regular key whose seed lives in `.env` `MAINNET_REGKEY_SEED` — but **no code
reads that variable**. Every transaction builder in `lfg_core/xrpl_ops.py`
does `Wallet.from_seed(config.SEED)` and sets `account=wallet.classic_address`:

- `mint_nft` (xrpl_ops.py:49–54)
- `create_nft_offer` (xrpl_ops.py:112–116)
- `buy_and_burn` (xrpl_ops.py:310–324)
- `burn_nft` (xrpl_ops.py:348–350)
- `modify_nft` (xrpl_ops.py:399–402)
- `bot_wallet_address()` (xrpl_ops.py:444–446 — also used as the XRP payment
  destination in `mint_flow.py:78` and the swap fee destination)

With a regkey seed, `Wallet.from_seed` derives the regkey pair's *own* address,
not the issuer — every issuer-authority tx (mint, modify, issuer-burn, offer)
would sign for the wrong account.

**Fix.** Add an explicit issuer-account override:
- New config: `SIGNING_ACCOUNT` (default: address derived from `SEED`, i.e.
  current behavior; on mainnet set to `rLfgoMint…`).
- In each builder: sign with `Wallet.from_seed(config.SEED)` but set
  `Account=config.SIGNING_ACCOUNT` explicitly; `bot_wallet_address()` returns
  `SIGNING_ACCOUNT`.
- On mainnet cutover: `SEED=<MAINNET_REGKEY_SEED value>`,
  `SIGNING_ACCOUNT=rLfgoMintj3KBcs4s2XKtquvDwEte2kYfJ`.
- Note: with `Account == issuer`, the `Issuer` field branch in `mint_nft`
  (xrpl_ops.py:64) is skipped — correct; do NOT set `Issuer` (that would
  require `NFTokenMinter` authorization).

**Status:** PR in progress (see PR referenced from this runbook's commit).

## Blocker 2 — BunnyCDN out of credit (OPS)

`LAYER_SOURCE=local` only covers trait-layer **reads**. Still hard-dependent on
Bunny:

- **Mint uploads** — image + metadata PUT via `cdn.upload_to_bunny`
  (`lfg_core/cdn.py:9`, raises on non-2xx; called from `mint_flow.py:253/266`).
  Failure happens **after payment, before mint** → user pays and gets nothing.
- **Swap uploads** — new image/video/metadata (`swap_flow.py:372–384`). Fails
  safely (nothing on-chain yet) but the swapper is unusable.
- **Swap reads** — `swap_meta.load_wallet_nfts` fetches each NFT's metadata
  from its baked-in `lfgo.b-cdn.net` URL (`swap_meta.py:141–157`). If the pull
  zone is suspended, wallets list **zero swappable NFTs**.

**Fix (ops, user-side):** top up Bunny credit or migrate off the shared client
account before launch. There is no code fallback for uploads. Verify with a
test PUT to the storage zone and a GET through the pull zone.

## Blocker 3 — No mainnet BRIX/XRP AMM (OPS + DECISION)

`amm_info` for XRP/BRIX (`rLfgoBriX5ZaMP32mtc7RUZJcjnisKh2Px`) on mainnet
returns `actNotFound`. `detect_swap_payment` (`swap_flow.py:56–74`) runs
unconditionally at swap start and raises for any wallet holding < 20 BRIX →
**swaps only work for BRIX holders** until fixed. Options:

1. **Create the mainnet AMM** — `scripts/testnet_amm_setup.py` is
   testnet-only; a mainnet pool must be funded from the real BRIX issuer
   (capital decision — pool XRP + BRIX at the intended price).
2. **Code fallback** — fixed XRP swap price via env (e.g. `SWAP_XRP_PRICE`)
   used when the AMM quote fails, or launch swaps BRIX-holders-only.

The swapper has **no free mode** (the free-ops carve-out is Phase 2 economy
only). Decision needed before launch.

## Blocker 4 — Closet has no feature flag (CODE)

Launching Closet-off is currently impossible:

- Webapp shows "👗 Dress Up" unconditionally (`webapp/client/index.html:34`,
  `webapp/client/app.js:1189`), and a mainnet user can mint a real Closet
  token (taxon 1762) from the in-room gate.
- `lfg_service/app.py:462–506` registers `/api/economy`, `/api/closet`,
  harvest/assemble/equip/extract/deposit routes unconditionally.
- Leaving `ECONOMY_NETWORK=testnet` is **not** a safe gate: `start_closet`
  mints against `XRPL_NETWORK` (mainnet) while accounting writes to the
  testnet DB — split-brain.

**Fix.** `ECONOMY_ENABLED` flag (default on): when `0`, hide the Dress Up
button and return 403/feature-disabled from all economy routes.
**Status:** PR in progress.

## Blocker 5 — Env cutover + restart (OPS)

All of `lfg-bot`, `lfg-activity`, `lfg-telegram` share the repo `.env`
(currently testnet) and freeze config at import — **edit `.env`, then
`pm2 restart lfg-bot lfg-activity lfg-telegram`**. Index listeners take
`--network` from their CLI args and need no change.

| Key | Current | Mainnet value |
|---|---|---|
| `XRPL_NETWORK` | `testnet` | `mainnet` |
| `SEED` | testnet minter seed | mainnet regkey seed (Blocker 1) |
| `SIGNING_ACCOUNT` | — (new) | `rLfgoMintj3KBcs4s2XKtquvDwEte2kYfJ` |
| `ECONOMY_ENABLED` | — (new) | `0` (Closet off) |
| `TOKEN_ISSUER_ADDRESS` | testnet issuer | mainnet LFGO issuer |
| `TOKEN_CURRENCY_HEX` | testnet value | mainnet LFGO hex |
| `NFT_TAXON` | `1760` | confirm intended mainnet mint taxon (1760 matches SWAP_TAXON) |
| `NFT_COLLECTION_FAMILY` / `NFT_DESCRIPTION` | `Test` / `Test` | production values |
| `BUNNY_CDN_FOLDER` | `minttest` | production folder (e.g. `LFGO`) |
| `SWAP_MAX_NFT_NUMBER` | `10000` | remove override (config default 3535) |
| `LAYER_SOURCE` | `local` | keep `local` |
| `ADMIN_LOG_CHANNEL_ID`, `TELEGRAM_ANNOUNCE_CHAT_ID` | verify | production channels |

Defaults in `lfg_core/config.py:48–60` (RPC/WS/clio URLs, issuer addresses)
flip correctly on `XRPL_NETWORK=mainnet` — clio stays `wss://s2-clio.ripple.com`
(nft_info/nft_exists are clio-only; do not point at a plain rippled WS).

## Blocker 6 — Issuer XRP reserves (OPS)

Issuer holds ~11.4 XRP. Each pending `NFTokenCreateOffer` and each NFTokenPage
locks 0.2 XRP owner reserve (5 offers already outstanding from the census
reconciliation). ~11 XRP ≈ ~50 concurrent objects — fine for a soft launch, a
mint rush of unaccepted offers hits `tecINSUFFICIENT_RESERVE`. **Top up the
issuer** (suggest ≥ 50 XRP) and watch for that error in logs.

---

## Pre-launch verification checklist

1. `.venv/bin/python -m pytest -q` — all green.
2. Bunny: test upload to storage zone + fetch via `lfgo.b-cdn.net`.
3. `SELECT MAX(nft_number) FROM LFG` — testnet mints share the table and
   inflate the next edition number (`db_helpers.py:7` ignores the `network`
   column); confirm the next mainnet number before first mint.
4. One real mainnet mint with a team wallet: payment → mint → offer → accept;
   confirm SourceTag 2606160021 on the tx in an explorer, confirm the listener
   (`lfg-index-mainnet`) records it.
5. One real swap on a mutable NFT and one on a legacy burnable NFT.
6. Confirm the Dress Up button is gone and `/api/closet` returns
   feature-disabled.
7. Confirm which XUMM app the current `XUMM_API_KEY` belongs to — two legacy
   apps are pending retirement after the public-repo scrub; rotate if flagged.

## Known minor gaps (non-blocking)

- `generate_static_payment_link` (`xumm_ops.py:41–52`) — fallback xaman.app
  link lacks SourceTag; only used when the XUMM API is down.
- `create_nft_offer` result polling is 3 attempts (`xrpl_ops.py:126–137`) —
  can report FAILED after a successful mint if tx lookup lags; manual recovery.
- `buy_and_burn` on the XRP mint path is best-effort (`mint_flow.py:231`) —
  persistent DEX failure accumulates XRP without burning LFGO; watch logs.
- Users table holds only 2 (testnet-era) rows — mainnet users re-register via
  the Xaman-verified `/register` flow.
