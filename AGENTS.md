# AGENTS.md — fork LFG and launch your own XRPL NFT collection

You are an AI coding assistant. Your operator wants to **fork this repository
and run it as their own NFT project** on the XRP Ledger: their art, their
wallets and tokens, their branding, their deployment. This file is your
runbook. It was written from a line-by-line audit of `main`. Where it and
the code disagree, the code wins; tell your operator.

LFG, the project you are forking, is a live mainnet collection. It has one
Python backend (`lfg_service`, aiohttp) with five clients: a Discord bot, a
Discord Activity, a Telegram bot, a Telegram Mini App, and a web app on
GitHub Pages. NFT art is composed at mint time from trait layers. Traits
swap in place (`NFTokenModify`), and an optional economy lets traits be
harvested into a per-user on-ledger "Closet" and traded. Users sign in their
own wallet (Xaman, or Joey Wallet over WalletConnect on the web). The
backend signs the project's own transactions with a hot regular key.

**The prime directive: rehearse everything on testnet first.** The code
default network is **mainnet**, and on mainnet several defaults point at
LFG's own wallets, CDN and SourceTag. A fork that skips the edits in §3 will
mint under LFG's identity, index LFG's collection, or silently fail to
index its own mints.

References below are `file` + symbol. Grep for them; line numbers drift.

---

## 1. Interview your operator first

Get these decisions before touching code. Several are hard to change after
launch.

| Decision | Why it matters |
|---|---|
| Collection name, website, logo, description | Written into every NFT's on-chain metadata |
| **Body types** and **trait slots** | LFG's five bodies and eight slots are partly hardcoded (§3.4). Reusing the model is cheap; a different one means code changes |
| **Tokens:** two (mint token + economy token, like LFGO + BRIX), one, or XRP-only minting | Drives accounts, trustlines and the AMM (§5). Trait swaps always charge an economy token (§5.2) |
| Mint price (token and XRP), royalty (`NFT_TRANSFER_FEE`, units of 1/100,000: 5000 = 5%), max collection size | |
| **SourceTag**: their own UInt32, or one assigned by a program they're in | Stamped on every transaction. **Never ship LFG's `2606160021`**: it's LFG's hackathon attribution |
| Surfaces: web app, Discord, Telegram, X auto-poster | Each needs an external account (§6) |
| Features: market, trait economy, Closet Market, daily token drip, bulk mint | §7. Start minimal |
| Hosting: a Linux box, a domain, a TLS proxy or tunnel | §8 |

Then work through §2 → §9 in order. Commit after each phase. Keep the test
suite green throughout (`scripts/run-tests -n auto --dist loadfile`).

---

## 2. Phase 0 — fork, install, green tests

```bash
git clone https://github.com/<operator>/<fork>.git && cd <fork>
./setup.sh               # Linux: ffmpeg, .venv, deps, pre-push gate
# macOS: brew install ffmpeg; python3 -m venv .venv
#        .venv/bin/pip install -r requirements.txt -r requirements-dev.txt
#        .venv/bin/pre-commit install --hook-type pre-push
source .venv/bin/activate
scripts/run-tests -n auto --dist loadfile     # needs no .env, no network
```

Requirements: Python 3.10+, `ffmpeg` (built with `libvpx` if you'll use
`.webm` layers). Tests are hermetic: the root `conftest.py` supplies fake env
and sends every SQLite store to a temp dir. Get to green **before** editing,
so any later failure is yours.

**Disable LFG's repo automation now.** Two workflows commit to `main` and
pull data from `Team-Hamsa/LFG`:

- `readme-sync.yml`
- `roadmap-sync.yml`

Delete them. Keep:

- `ci.yml` (the same gate as pre-push)
- `pages.yml` (edit it in §8)
- `dependabot.yml`

Actions stay disabled in a fork until the operator enables them.

---

## 3. Phase 1 — remove LFG's identity (code + config)

### 3.1 Must change, or the fork is broken or misattributed

- **SourceTag.**
  - Change the default in `lfg_core/config.py` (`SOURCE_TAG = int(os.getenv("SOURCE_TAG", …))`). Setting it only in `.env` isn't enough: `scripts/readme_badges.py` parses the default.
  - It is load-bearing, not cosmetic: WalletConnect result verification (`lfg_core/signing/provenance.py`, `signing/proof.py`) rejects a transaction carrying a different tag. Sponsored-mint eligibility and archive certification (`sponsored_mint.py`, `archive_reverify.py`, `history_store.py`) are defined in terms of it.
  - About 13 test files assert `2606160021`. Update them in the same commit:

    ```bash
    grep -rl 2606160021 tests webapp
    ```

  - Choose it before launch. Changing it on a live deployment invalidates any certified history archive.
- **Mainnet issuer hardcoded in scripts.** `scripts/backfill_onchain.py`, `NETWORKS["mainnet"]["issuer"]`, is `rLfgoMint…` and **takes precedence over config**:
  - `scripts/onchain_listener.py` uses `args.issuer or net["issuer"] or config…`;
  - `scripts/backfill_history.py` uses `net["issuer"] or issuer`.

  Set it to `None` (like testnet's entry) so `SWAP_ISSUER_ADDRESS` is used. Otherwise your mainnet listener indexes **LFG's** NFTs.

  The same literal appears in:
  - `scripts/derive_history_events.py` (NFT and BRIX issuers);
  - `scripts/audit_layer_coverage.py`;
  - `scripts/rebuild_collection_db/01_enumerate_onchain.py`;
  - `scripts/sourcetag_metrics.py`.

  ```bash
  grep -rn 'rLfgo' --include='*.py' .
  ```

  List every hit and decide on each.
- **`lfg_core/system_wallets.py`.** `HISTORICAL_DISTRIBUTORS` lists LFG's distributor wallets; empty it. The set is append-only: add your own distributor there when you have one. `HISTORICAL_HOUSE_WALLETS` must list your house wallet before `scripts/house_closet.py` will run (Closet Market only).
- **Edition numbering starts at #3536.** In `lfg_core/db_helpers.py`, `get_next_nft_number` treats an empty table as max `3535`, and `scripts/init_db.py` inserts 3535 placeholder rows. Change both, e.g. to `0` so numbering starts at #1.
- **`SWAP_MAX_NFT_NUMBER` defaults to 3535.** `lfg_core/swap_meta.py` drops every NFT numbered above it from swapping and Dress Up. Set it ≥ your largest possible edition (LFG prod uses 7777).
- **Taxons: one nonzero value for all three.** Mints use `NFT_TAXON`, but the listener and backfill enumerate `ASSEMBLE_TAXON`, and swap remints use `SWAP_TAXON`. The listener's membership test, `int(token.get("taxon") or -1) == taxon`, can never match taxon `0`, which is the shipped `NFT_TAXON` default.
  - Set `NFT_TAXON = ASSEMBLE_TAXON = SWAP_TAXON = <nonzero>`.
  - Pick your own `CLOSET_TAXON` and `TRAIT_TAXON`.

  Otherwise new mints are never indexed. The collection-size cap, market, leaderboards and economy all read that index.
- **`scripts/sourcetag_metrics.py`** hardcodes `OPERATOR_WALLETS` and `HISTORICAL_SIGNING_ADDRESSES`. Replace both with your own wallets before trusting its counts.
- **Deployment paths:**
  - `ecosystem.*.config.js`: `CWD` is `/home/hamsa/…`, and every app has `--network` baked in.
  - `scripts/deployer.py`: `HOME`, and the `STACKS` checkouts, branches, ports and pm2 names.
  - `.github/workflows/pages.yml`: `WEB_API_BASE` is `https://api.letseffinggo.com`.

  See §8.

### 3.2 Configure, don't edit: set these explicitly in `.env`

On mainnet these default to LFG's values:

| Variable | LFG default | Set to |
|---|---|---|
| `SWAP_ISSUER_ADDRESS` | `rLfgoMint…` | your NFT issuer. **Must equal `SIGNING_ACCOUNT`**: if they differ, `xrpl_ops.mint_nft` adds an `Issuer` field (the authorized-minter path) and mints fail |
| `SIGNING_ACCOUNT` | the address derived from `SEED` | your issuer address, when `SEED` is the issuer's regular key |
| `SWAP_OFFER_ISSUER`, `BRIX_ISSUER`, `SWAP_OFFER_CURRENCY_HEX`, `BRIX_CURRENCY_HEX` | LFG's BRIX (`rLfgoBriX…`, `BRIX`) | your economy token |
| `BUNNY_CDN_PUBLIC_BASE` | `https://lfgo.b-cdn.net` | your pull zone. It is **written into on-chain metadata URIs**, which are permanent |
| `BUNNY_PULL_ZONE`, `BUNNY_CDN_FOLDER`, `SWAP_CDN_FOLDER` | "", `minttest`, `LFGO` | your zone and folders. Staging and prod need **different** folders (edition numbers overlap) |
| `NFT_COLLECTION_NAME` | `Let's Effing Go!` | your name. Metadata `name` becomes `"<name> #<n>"` and `swap_meta` parses the `#`, so keep it |
| `EXTERNAL_WEBSITE_URL`, `NFT_COLLECTION_LOGO`, `NFT_SCHEMA_URL` | LFG's site, logo, IPFS schema | yours. `EXTERNAL_WEBSITE_URL` is also read in `surfaces/discord_bot/config.py` |
| `NFT_TRANSFER_FEE` | `7000` (7%) | your royalty |
| `ECONOMY_NETWORK` | `testnet` | same as `XRPL_NETWORK` if `ECONOMY_ENABLED=1`, or boot refuses |

`NFT_COLLECTION_FAMILY` and `NFT_DESCRIPTION` appear in old docs but **no
code reads them**. The metadata builders hardcode family/description as
`f"Season {season}"` (`swap_flow.py`, `scripts/_economy_deps.py`). Edit those
if it matters to the operator.

### 3.3 Branding (should change)

- **Client (`webapp/client/`):**
  - `assets/` (logo, mascot, og-card, favicons, icons, maskable icons). Keep the `xaman-`/`joey-app-icon.png` files.
  - `site.webmanifest`; `index.html` title/meta/og tags and welcome copy.
  - `app.js` user-facing strings: grep `LFG`. `WC_APP_URL` and the WalletConnect `name`/`description` sit next to each other.
  - The CSS palette `:root` tokens in `style.v*.css`.
- **Cache-busters:** when you edit client files, bump the versioned names (`style.vNN.css`, `app.js?v=NN` in `index.html`). Discord's webview caches aggressively.
- **Share text and the X poster:**
  - The tweet templates live in **two places**: `lfg_service/app.py` and `webapp/client/app.js`. Grep `@letseffinggo`.
  - Also `surfaces/x_bot/poster.py`, the OG share page title in `lfg_service/app.py`, and `scripts/share_card/` (logo, domain, colours, tagline).
- **Bots:**
  - Discord's `/letsgo` command and embed copy (`surfaces/discord_bot/commands.py`, `views.py`, `render.py`, `claim_view.py`, `trustline.py` — the latter also has a hardcoded Xaman `return_url`).
  - Telegram copy (`surfaces/telegram_bot/`).
- **Token display names.** `"LFGO"`/`"BRIX"` are literals throughout, and most occurrences are identifiers. Rename only user-visible copy. **Keep** these, because they are persisted and part of the API contract:
  - the `pay_with` values;
  - `*_brix` API fields;
  - `/api/brix/*` routes;
  - `brix_*` tables.
- **Trait and Closet token names:** `lfg_core/trait_token.py` (`"LFG Trait — …"`, `"LFG Traits"`) and `lfg_core/closet_token.py` (`"LFG Closet — …"`).
- **Docs:**
  - Replace `README.md`.
  - Replace `CLAUDE.md`: it's LFG's operator runbook.
  - Delete `docs/HACKATHON.md`, `docs/UNIQUE_USERS.md`, `metrics/sourcetag.json` and the README SVG generators you don't want.
  - Update the repo slug in `CONTRIBUTING.md`, `SECURITY.md` and `.github/ISSUE_TEMPLATE/config.yml`.
- **Update the tests that pin brand strings:**

  ```bash
  grep -rln "Let's Effing Go\|lfgo.b-cdn\|letseffinggo\|rLfgo" tests webapp
  ```

  `webapp/test_smoke.py` asserts the mainnet issuer defaults.
- **Keep these internal names.** Renaming them is a big refactor that users never see:
  - the `lfg_core`/`lfg_service` packages;
  - the `LFG` SQL table and `lfg_nfts.db`;
  - `window.LFG_WEB`, `LFG_SERVICE_URL`.
- **Memo and metadata markers.** These are read back during recovery: `lfg:brix_claim:`, `lfg:fee_cover:`, `lfg:closet_<phase>:`, `lfg/nonce`, and the `lfg_closet`/`lfg_trait` metadata keys. Keep them, or rename them all consistently **before your first mainnet transaction**. Renaming later strands in-flight claims, refunds and Closet fills.

### 3.4 Bodies and trait slots (only if the operator's model differs)

LFG's bodies are `ape, female, male, milady, skeleton`, with eight slots.
Hardcoded in:

- `lfg_core/trait_config.py` `VALID_BODIES`; `lfg_core/seasons.py` `BODIES`.
- `lfg_core/swap_meta.py`:
  - `detect_body`: picks the body **by substring of the Body trait value** ("Milady", "Straight", "Curved", "Ape"). **Anything else becomes `skeleton`**.
  - `TRAIT_ORDER` (a parity test ties it to `trait_config.yaml`'s `layers:`).
  - `season_for_number` (LFG's 707 / 2121 edition cutoffs).
- `lfg_core/rarity.py` `LFG_COLUMN_FOR_CATEGORY`; `webapp/client/build_pure.js`.
- Ape special case: `lfg_core/ape_face.py` and `traits.py` need `layers/ape/Nose.png` and `layers/ape/Ape Mask.png` at the **body root**.

Easiest path: map the operator's bodies onto LFG's five names. For a
different model, rewrite `detect_body` first (make it data-driven from
`trait_config.yaml`), then fix the tests it breaks.

---

## 4. Phase 2 — art and trait rules

- **Layout:** `layers/<body>/<TraitType>/<Value>.png` (also `.gif`, `.webm`, `.mp4`). Art shared by every body goes in `layers/shared/<TraitType>/`. The file stem is the trait value. `layers/` is gitignored, so the art lives on the box, not in git.
- **Every file must be exactly 1080×1080 with alpha.** Compose does no scaling: undersized art renders as a tiny top-left sprite, and an opaque GIF paints over every layer below it. The pre-push hook `scripts/audit_layer_dimensions.py` enforces the size. Use `scripts/make_animated_layer.py` for animated layers (needs `gifski`).
- **`trait_config.yaml`:** defines `layers` (z-order), `z_overrides`, `affinity` (which bodies a value may appear on), `exclusions` / `inclusions`, and `swap_matrix`. Validate with `scripts/validate_trait_config.py` (also a pre-push hook).
- **Serving and thumbnails:** `LAYER_SOURCE=local` + `LAYERS_DIR` (the code default is `cdn`). Then run `scripts/make_layer_thumbs.py` (the 512px preview tier).
- **Rarity** needs no seeding: `weighted_pick` inserts floor rows on first use. Tune it later with `scripts/rarity_admin.py` or the loopback-only `scripts/trait_dashboard.py`.
- **Economy only:** a 1080×1080 blank silhouette via `scripts/upload_blank_art.py`, which feeds `BLANK_IMAGE_URL`.

---

## 5. Phase 3 — XRPL accounts, tokens, AMM (testnet first)

Set `XRPL_NETWORK=testnet` and `ECONOMY_NETWORK=testnet`. On testnet the
`SEED` account is, by default, NFT issuer, economy-token issuer and app wallet
all at once. That's the fastest rehearsal.

### 5.1 Accounts

| Role | Env | Notes |
|---|---|---|
| NFT issuer = backend signer | `SEED`, `SIGNING_ACCOUNT`, `SWAP_ISSUER_ADDRESS` | On mainnet, use the regular-key model (below). XRP mint payments and swap fees are paid **to this account** (`xrpl_ops.bot_wallet_address`) |
| Mint-token issuer | `TOKEN_ISSUER_ADDRESS`, `TOKEN_CURRENCY_HEX` | **Required at import.** Token mint payments go to the issuer and are thereby burned |
| Economy-token issuer | `SWAP_OFFER_*` / `BRIX_*` | May be the same account as above |
| Distributor (daily drip) | `BRIX_DISTRIBUTOR_ADDRESS`, `BRIX_DISTRIBUTOR_SEED` | Pre-fund it with the economy token. Never pay claims from the issuer: that mints new supply |
| House wallet (Closet Market) | `CLOSET_HOUSE_WALLET`, `CLOSET_HOUSE_SEED` | Optional |

**Regular key.** No script does this, so the operator signs it themselves.
On the issuer:

1. `SetRegularKey` to a new key.
2. Put that key's seed in `SEED` and the issuer address in `SIGNING_ACCOUNT`.
3. Optionally `AccountSet SetFlag=4` (`asfDisableMaster`). Understand that the hot key then becomes the account's only authority. See the README's "What `SEED` actually is on mainnet".

Keep the issuer funded well above reserve: each NFT page and offer locks
owner reserve.

**Assumed account settings:**
- **DefaultRipple** on each token issuer, set *before* anyone opens a trustline.
- A trustline from the NFT issuer to the economy token when the two issuers differ (otherwise token-priced offers fail `tecNO_LINE`).
- For the Closet Market only: `asfAllowTrustLineLocking` on the economy-token issuer, plus a large app-wallet limit, both via `scripts/closet_market_setup.py`.

### 5.2 Token models

- **Two tokens (LFG's model):** `TOKEN_*` = mint token, `BRIX_*` = economy token.
- **One token:** point `TOKEN_*` at the same pair as `BRIX_*`. Raise `TOKEN_TRUSTLINE_LIMIT` (default 1000), because the Discord trustline button uses it.
- **XRP-only minting** has no flag. Setting `TOKEN_ISSUER_ADDRESS` to `SIGNING_ACCOUNT` and choosing a currency nobody holds makes everyone pay `MINT_PRICE_XRP`; the post-mint buy-and-burn then no-ops. *Verify on testnet.*
- **Trait swaps always charge `SWAP_OFFER_AMOUNT` of the economy token per NFT** (default 10). Non-holders pay XRP via an AMM quote; with no AMM, only token holders can swap. There is no free or XRP-only swap mode without a code change.

### 5.3 AMM

- `scripts/testnet_amm_setup.py` is **testnet only**. It enables DefaultRipple, creates a 50 XRP : 5000 token pool, and verifies both quoting and buy-and-burn. It's idempotent; rerun it after a testnet reset.
- On mainnet there's no script: `AMMCreate` by hand from an account holding both assets, then check with `amm_info`. The repo's docs disagree on whether the issuer itself can create the pool, so rehearse this exact step on testnet.
- **What needs the AMM:**
  - the swap fee for non-holders;
  - the trait-buy XRP→token on-ramp;
  - the Shop's XRP fallback;
  - the token buy-and-burns.

  All are best-effort or return `503 pricing_unavailable` without it.
- The post-XRP-mint buyback of the mint token crosses the DEX order book capped at the XRP paid. It does nothing when the book has no asks that low, and bulk XRP mints never attempt it.

---

## 6. Phase 4 — external accounts

| Service | Gives you | Setup notes |
|---|---|---|
| **Xaman** (apps.xumm.dev) | `XUMM_API_KEY`, `XUMM_API_SECRET` | No webhook or redirect needed (the backend polls). Push grants are tied to the wallet that *created* the app. Mind the rate limits (HTTP 429) |
| **BunnyCDN** | storage zone → `BUNNY_CDN_STORAGE_ZONE`, `BUNNY_CDN_ACCESS_KEY` (the storage password); pull zone → `BUNNY_CDN_PUBLIC_BASE` (+ optional `BUNNY_PULL_ZONE` hostname) | Mint images and metadata upload here. An upload failure after payment strands the mint, so test it first |
| **Discord** app (optional) | `DISCORD_BOT_TOKEN`, `DISCORD_CLIENT_ID`, `DISCORD_CLIENT_SECRET` | Turn on **all three privileged intents**, or the bot crashes. OAuth redirect = your HTTPS backend. Activities: URL Mapping `/` → backend host. Scopes: `bot` + `applications.commands`. `ADMIN_LOG_CHANNEL_ID` is required; `DISCORD_GUILD_ID` gives instant command sync in one guild. Full walkthrough: `docs/ACTIVITY_SETUP.md`. **`lfg_service` refuses to start without `DISCORD_CLIENT_ID`/`SECRET`** — a web-only fork must still set non-empty placeholders |
| **Telegram** (optional) | `TELEGRAM_BOT_TOKEN` (BotFather) | The **service** needs it too: it's the HMAC key for Mini App `initData`. The bot also needs `SERVICE_TOKEN_TELEGRAM`, `LFG_SERVICE_URL`, `TELEGRAM_ANNOUNCE_CHAT_ID`. `TELEGRAM_MINI_APP_URL` must be https. Launch via `run_telegram.py` only |
| **Reown / WalletConnect** (optional) | `REOWN_PROJECT_ID`, `WC_SURFACES` | Add every enabled origin to the Reown dashboard. Joey sign-in and signing are web-only |
| **X** developer app (optional) | `X_CONSUMER_KEY/SECRET`, `X_ACCESS_TOKEN/SECRET`, `SERVICE_TOKEN_X`, `X_ENABLED=1` | Posting is pay-per-use; cap it with `X_MONTHLY_POST_BUDGET` |
| **Domain + public HTTPS** | | Needed for the Discord Activity, Mini App, share cards (`PUBLIC_SHARE_BASE_URL`, `SHARE_FORWARD_URL`), the X OAuth callback and the web app's API. Any TLS reverse proxy or named tunnel works |

Shared secrets: every `SERVICE_TOKEN_<SURFACE>` in the service's env becomes a
trusted surface (`lfg_service/auth.py`), with the same value on both sides.
Use long random values, and remove stale ones.

---

## 7. Phase 5 — pick features

Start with the **minimal fork: mint + swap + market**. Add the rest once it
works on testnet.

| Feature | Switch | Prerequisites |
|---|---|---|
| Mint | always on | listener running with the right issuer/taxon; Bunny |
| Swap | always on | economy token (+ AMM for non-holders); `SWAP_MAX_NFT_NUMBER` |
| Market (characters in XRP) | `MARKET_ENABLED` (default **on**) | listener; nightly `scripts/backfill_market.py --report` |
| Trait economy (Closet, harvest/assemble/equip/extract/deposit, trait listings) | `ECONOMY_ENABLED` | `ECONOMY_NETWORK == XRPL_NETWORK`; blank art; genesis frozen (below); nightly reconcile + audit |
| Closet Market | `CLOSET_MARKET_ENABLED` | economy; `CLOSET_MARKET_ENC_KEY` (Fernet — **back it up**; losing it strands open bids); issuer flag + app limit (`closet_market_setup.py`); optional house wallet |
| Daily token drip | `BRIX_DISTRIBUTOR_SEED` (unset = claims return 503) | funded distributor; a **certified** history archive (see `docs/ops/sponsored-free-mint.md` for the certification run); nightly `scripts/accrue_brix.py` |
| Bulk-mint UI | `BULK_MINT_UI_ENABLED` | none |
| Trait Shop, burn-to-mint | `SHOP_ENABLED`, `BURN_TO_MINT_ENABLED` | off in LFG prod; leave them off |
| Sponsored free mint, fee cover | Discord `/admin` campaigns (off by default) | archive certification, `SPONSORED_MINT_EXCLUDED_WALLETS`, the readiness audit. Leave them off |
| X auto-poster | `X_ENABLED` | §6 |
| Joey Wallet | `REOWN_PROJECT_ID` | §6 |

**Economy genesis.** Run `scripts/freeze_genesis.py --network <net>
--max-edition <N>` exactly once. After that the listener records every new
mint as a supply-growth row, and the nightly audit checks
`census == genesis + Σ supply_changes`. For a brand-new collection, freeze
before the first mint. *(This order is inferred from the code, not
exercised; rehearse it on testnet.)* Never re-freeze to "fix" drift. The
script's default `COLLECTION_SIZE=3535` is LFG's, so always pass
`--max-edition`.

---

## 8. Phase 6 — deploy (single box, single stack)

LFG runs two pm2 stacks (`main` = staging, `deploy` = prod) with a polling
deployer. **A fork doesn't need that.** The simplest viable setup:

1. **Box.** Linux, Python 3.10+, ffmpeg. Set the timezone to **UTC**: pm2 cron uses host-local time.
2. **Install and configure.** `./setup.sh`, then `cp .env.example .env` (a testnet template) and fill in §3.2, §5 and §6. Set:
   - `WEBAPP_SESSION_SECRET` (long random);
   - `WEBAPP_PORT=8176`;
   - `LFG_SERVICE_URL=http://localhost:8176`. The X bot's own default is `:8000`, which is wrong.

   `chmod 600 .env`.
3. **Backend.** `python -m lfg_service.app` (pm2 runs the equivalent, `python -m webapp.server`). Tables are created on first use. Check `curl localhost:8176/api/health` and `/api/config`.
4. **Network exposure.** The app binds **all interfaces**, and `_client_ip` trusts the rightmost `X-Forwarded-For`. **Firewall 8176** and expose it only through your TLS proxy or tunnel, or clients can spoof IPs past the rate limits.
5. **Listener.** Run it from the first mint onward:

   ```bash
   python scripts/onchain_listener.py --network <net> --issuer <issuer> --taxon <taxon> listen
   ```

   It needs a **clio** WebSocket (`XRPL_CLIO_WS_URL`; the default is Ripple's public clio), because `nft_info` exists only on clio.
6. **Surfaces.** `python main.py` (Discord), `python run_telegram.py` (Telegram), `python run_x.py` (X; exits 0 when off).
7. **pm2.** Copy `ecosystem.prod.config.js` and change `CWD` and the `--network` args. Delete the LFG-only app `lfg-funnel-health`. Keep the crons for the features you enabled (market sweep; economy reconcile → audit; Closet Market audit; drip accrual; balance snapshot). Then `pm2 start <file> && pm2 save && pm2 startup`.
8. **Updates.** `git pull && pip install -r requirements.txt && pm2 restart <apps>`. Check `/api/health` shows no active sessions first: in-flight mint and swap sessions live in memory. `scripts/deployer.py` automates this, but it hardcodes LFG's paths, branches and ports (`HOME`, `STACKS`).
9. **Web app on GitHub Pages** (`.github/workflows/pages.yml`):
   1. Set `WEB_API_BASE` to your public API origin.
   2. Pick the trigger branch (LFG uses `deploy`).
   3. Settings → Pages → Source: GitHub Actions. Allow that branch in the `github-pages` environment rules.
   4. Set a custom domain + DNS `CNAME` (no CNAME file is used).
   5. On the backend, set `WEB_ALLOWED_ORIGINS` to the Pages origin(s) (exact scheme + host).
   6. Edit the hardcoded domains in `webapp/client/index.html` and `app.js` (`WC_APP_URL`).

---

## 9. Phase 7 — testnet rehearsal, then mainnet

**Testnet checklist** (with a funded testnet `SEED`):

1. Web sign-in with Xaman.
2. Mint, paying in the token *and* in XRP.
3. The NFT arrives in the wallet: the mint and its delivery offer are one `NFTokenMint` (XLS-52); accept it.
4. The listener indexes it: `/api/nfts` shows it.
5. Swap a trait between two NFTs.
6. List, buy and cancel on the market.
7. The metadata/image URL is on **your** CDN.
8. An explorer shows **your** SourceTag and memos on every tx.
9. Any enabled feature: run its flow once (e.g. `scripts/closet_market_e2e.py` for the Closet Market).

**Mainnet cutover:**
- Switch to `XRPL_NETWORK=mainnet` / `ECONOMY_NETWORK=mainnet`.
- Use new mainnet accounts, regular key, tokens and AMM (§5).
- Point at a fresh Bunny folder.
- Set every §3.2 variable explicitly.
- Rerun the §3.1 grep to confirm no `rLfgo` or `2606160021` remains.
- Start the listener before announcing.
- Mint #1 yourself and repeat the checklist.

---

## 10. Invariants — keep these when changing code

1. **Every XRPL transaction and Xaman payload carries `SourceTag` + provenance `Memos`** (`lfg_core/memos.py`; the only exception is Xaman's `SignIn`).
2. **Never retry a mint, and never treat "unknown" as "failed".** Backend transactions are signed once and confirmed by hash. `IndeterminateResultError` means *maybe committed*: reconcile from the ledger.
3. **Payments count only with a validated `tesSUCCESS` meta and its `delivered_amount`**, never the requested `Amount` (partial payments).
4. **Wallets are bound only by proof**: a signed Xaman `SignIn`, or a proof-signed WalletConnect link. Never take an address from a request body as identity.
5. **No user keys.** Backend keys sign only project-side operations. `WEBAPP_DEV_MODE` skips auth and is refused on any network but testnet.
6. **Supply is audited** (`census == genesis + Σ supply_changes`). A flow that creates or destroys a trait writes a `supply_changes` row. Never delete rows from `supply_changes`, `closet_orders`/`closet_fills` or `free_mint_*`: correct them with inverse rows.
7. **Tests stay hermetic.** No bare `load_dotenv()` (use `lfg_core.envload.load_dotenv_unless_skipped()`). Pin new env/global state in the root `conftest.py`.
8. **Never bypass the gate** (`--no-verify`).
