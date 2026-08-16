# Runbook — Mainnet MVP Launch (Minter + Trait Swapper, Closet OFF)

_Last reviewed: 2026-07-10 against main @ a0bfb27. Full test suite: 1317 passed, 1 skipped._

Scope of the MVP: **Minter** and **Trait Swapper** on mainnet. The **Closet /
trait economy ships later** and must be gated off (Blocker 4). The **in-app
marketplace also ships later** and must be gated off via `MARKET_ENABLED=0`
(Blocker 4b) — with it set, every `/api/market/*` route answers 403
`market_disabled` and the client hides the 🛒 Marketplace button, so no
money-touching list/buy/cancel path is reachable. **Status: ✅ RESOLVED — PR
#143 (`MARKET_ENABLED`) merged 2026-07-09; gate deep-reviewed leak-proof.**

Review verdict: both flows are logically sound end-to-end (fail-safe ordering,
journaling, replay-safe payment verification) and SourceTag `2606160021` is
enforced on every transaction and XUMM payload (`lfg_core/xrpl_ops.py`,
`lfg_core/xumm_ops.py:148`). The blockers below are what stand between the
current deployment (testnet) and a mainnet launch.

---

## Merged since the 2026-07-02 review (all on main, launch-relevant)

- **#143 `MARKET_ENABLED`** — marketplace gated off for launch (Blocker 4b).
- **#144 provenance Memos (#54)** — every transaction and XUMM payload now
  carries `Memos` (initiator/platform/action) alongside SourceTag; see
  `lfg_core/memos.py`. Explorer spot-checks below cover both.
- **#145 ape face traits (#38)** — apes mint real Eyes/Eyebrows/Mouth; the
  affinity allow-lists that zeroed ape Eyebrows (crashing every ape mint) are
  fixed. #146/#147 guard the 200 legacy `None`-faced apes: swaps involving an
  empty/None slot are rejected, so a faceless ape can never delete a
  counterparty's trait (1,217 live NFTs encode empty Accessory as `""`).
- **#139 Xaman push delivery (#135)** — returning registered users get sign
  requests pushed to Xaman instead of re-scanning a QR; QR/deep-link fallback
  always present. Real-device smoke still pending (checklist item 8).
- **#140 trait-file audit (#137)** — `scripts/audit_trait_files.py`
  reconciles every stored trait value against the local `layers/` tree using
  the real compose resolution. Added after a CDN→local sync silently dropped
  the two ape body-root files (`layers/ape/Nose.png`, `Ape Mask.png`),
  blocking every ape swap. Run it pre-launch (checklist item 3).
- **#148 mint pay-screen cancel (#141)** — `POST /api/mint/{id}/cancel`
  releases the per-user mint lock immediately; a confirmed payment can never
  be cancelled. Known follow-up: the cancelled XUMM payload stays signable in
  Xaman until it expires (#152).
- **#149 marketplace buy-error surfacing (#133)** — malformed listing prices
  toast an error instead of a dead card (moot at launch while
  `MARKET_ENABLED=0`, but removes the known live trigger).
- **#114 Season 3 mint exclusion** — CLOSED: `layers/seasons.json` sidecar +
  per-network `enabled=0` keep S3 traits unmintable at launch.

**Still open (merge before cutover if bots clear them; neither blocks launch):**
PR #150 (marketplace hardening minors — marketplace is gated off) and PR #151
(phase-aware economy journaling — economy is gated off).

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

**Status:** ✅ RESOLVED — PR #112 merged to main 2026-07-02 (b7d2182).

## Blocker 2 — BunnyCDN out of credit (OPS) — ✅ RESOLVED 2026-07-02

Credit restored and verified: storage-zone PUT (201) + pull-zone GET (200)
both succeed. Architecture decision confirmed: keep `LAYER_SOURCE=local`
permanently — trait-layer reads stay local (latency + Bunny usage), Bunny is
used only for the final NFT image/metadata uploads and legacy metadata reads.

Original problem, for reference — `LAYER_SOURCE=local` only covers trait-layer
**reads**. Still hard-dependent on Bunny:

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

## Blocker 3 — No mainnet BRIX/XRP AMM (OPS + DECISION) — ✅ RESOLVED 2026-07-02

Pool created and verified on-ledger: AMM account
`rn6TaseGA12G2Lyf5BL5MjrKS4MYb9bGrc`, 104 XRP / 19,238 BRIX, 1% trading fee
(~0.0054 XRP/BRIX). `get_amm_xrp_cost` quotes the 10-BRIX swap fee at
~0.055 XRP, so the non-BRIX-holder XRP fee path now clears.

Original problem, for reference:

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
only).

**Decision 2026-07-02: option 1 — a small mainnet BRIX/XRP pool will be
created** (user-side capital op). Notes for that op: `AMMCreate` must be signed
by an account holding both XRP and BRIX (not the BRIX issuer itself — an
issuer cannot hold its own IOU); AMMCreate burns the special AMMCreate fee
(one incremental owner reserve, currently 0.2 XRP). After creation, verify
with `amm_info` for XRP/BRIX (`rLfgoBriX5ZaMP32mtc7RUZJcjnisKh2Px`) on
`s1.ripple.com`, then confirm `get_amm_xrp_cost` returns a quote. Until the
pool exists, swaps work only for wallets holding ≥ 20 BRIX.

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
**Status:** ✅ RESOLVED — PR #113 merged to main 2026-07-02 (9b420f1).

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
| `MARKET_ENABLED` | — (new) | `0` (marketplace off) |
| `TOKEN_ISSUER_ADDRESS` | testnet issuer | mainnet LFGO issuer |
| `TOKEN_CURRENCY_HEX` | testnet value | mainnet LFGO hex |
| `NFT_TAXON` | `1760` | confirm intended mainnet mint taxon (1760 matches SWAP_TAXON) |
| `NFT_COLLECTION_FAMILY` / `NFT_DESCRIPTION` | `Test` / `Test` | production values |
| `BUNNY_CDN_FOLDER` | `minttest` | production folder (e.g. `LFGO`) |
| `SWAP_MAX_NFT_NUMBER` | `10000` | remove override (config default 3535) |
| `LAYER_SOURCE` | `local` | keep `local` |
| `ADMIN_LOG_CHANNEL_ID`, `TELEGRAM_ANNOUNCE_CHAT_ID` | verify | production channels |
| `BRIX_AMM_ACCOUNT` (optional) | — | `rn6TaseGA12G2Lyf5BL5MjrKS4MYb9bGrc` (nightly `snapshot_balances` tracks the pool) |
| `BRIX_DISTRIBUTOR_ADDRESS` (optional) | verify | airdrop distributor wallet (excluded from BRIX leaderboards) |

**Step 0 before editing `.env`:** the deployed checkout (`/home/hamsa/LFG`)
lags main — `git pull` there first so the cutover restarts run the merged
launch fixes (#143–#149). The post-merge hook auto-restarts `lfg-activity`;
still run the full `pm2 restart lfg-bot lfg-activity lfg-telegram` after the
env edit so all three re-freeze config together.

Defaults in `lfg_core/config.py:48–60` (RPC/WS/clio URLs, issuer addresses)
flip correctly on `XRPL_NETWORK=mainnet` — clio stays `wss://s2-clio.ripple.com`
(nft_info/nft_exists are clio-only; do not point at a plain rippled WS).

## Blocker 6 — Issuer XRP reserves (OPS) — ✅ RESOLVED 2026-07-02

Issuer topped up with 100 XRP. Keep watching for `tecINSUFFICIENT_RESERVE`
during mint rushes (each pending offer locks 0.2 XRP owner reserve).

Original problem, for reference — issuer held ~11.4 XRP. Each pending `NFTokenCreateOffer` and each NFTokenPage
locks 0.2 XRP owner reserve (5 offers already outstanding from the census
reconciliation). ~11 XRP ≈ ~50 concurrent objects — fine for a soft launch, a
mint rush of unaccepted offers hits `tecINSUFFICIENT_RESERVE`. **Top up the
issuer** (suggest ≥ 50 XRP) and watch for that error in logs.

---

## Pre-launch verification checklist

1. `.venv/bin/python -m pytest -q` — all green.
2. Bunny: test upload to storage zone + fetch via `lfgo.b-cdn.net`.
3. `scripts/audit_trait_files.py --network mainnet` exits 0 against the
   DEPLOYED tree (`LAYERS_DIR=/home/hamsa/LFG/layers …`) — catches dropped
   layer files, incl. the ape body-root pair that once blocked all ape swaps.
4. `SELECT MAX(nft_number) FROM LFG` — testnet mints share the table and
   inflate the next edition number (`lfg_core/db_helpers.py` ignores the `network`
   column); confirm the next mainnet number before first mint.
5. One real mainnet mint with a team wallet: payment → mint → offer → accept;
   confirm SourceTag 2606160021 AND the provenance Memo on the tx in an
   explorer, confirm the listener (`lfg-index-mainnet`) records it. If the
   team wallet draws an ape body, confirm it has real Eyes/Eyebrows/Mouth
   (#38); otherwise eyeball one ape mint separately.
6. One real swap on a mutable NFT and one on a legacy burnable NFT. Optionally
   confirm a swap offering an empty slot is rejected with the #146 message.
7. Confirm the Dress Up button is gone and `/api/closet` returns
   feature-disabled. Confirm the 🛒 Marketplace button is gone and that the
   marketplace surface is gated across its route classes — a read
   (`GET /api/market/listings`), a money-touching write (`POST /api/market/buy`),
   and a status poll (`GET /api/market/buy/{session_id}`) each return 403
   `market_disabled` (every `/api/market/*` handler is wrapped by
   `require_market`, so these three spot-checks stand in for the whole surface).
8. Xaman push smoke (#135/#139): sign in once with a team wallet, then start a
   second mint — the sign request should arrive as a Xaman push notification
   (QR still shown as fallback). This is the still-pending real-device check.
9. Confirm which XUMM app the current `XUMM_API_KEY` belongs to — two legacy
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
- A cancelled mint's XUMM payload stays signable in Xaman until it expires —
  a late signature pays outside any session (#152; `xumm_ops` has no
  payload-cancel helper yet).
