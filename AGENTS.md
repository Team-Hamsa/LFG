# AGENTS.md — a guide for AI assistants working with LFG

You are an AI assistant (Claude, ChatGPT, Cursor, Copilot, …) helping someone
**understand, run, test, or use** LFG. Read this file first; it is written for
you. Everything here was checked against the code on `main`. When this file
and the code disagree, the code wins — say so.

**What LFG is:** an XRP Ledger NFT platform, live on XRPL **mainnet**. Art is
composed at mint time from trait layers; traits can be swapped in place
(`NFTokenModify`), harvested into a per-user on-ledger "Closet", and traded.
One Python backend (`lfg_service`, aiohttp) serves five clients: a Discord
bot, a Discord Activity, a Telegram bot, a Telegram Mini App, and a web app at
<https://build.letseffinggo.com>. Users sign in their own wallet (Xaman, or Joey
Wallet over WalletConnect on the web). The backend signs the project's own
transactions with hot keys.

---

## 1. First, work out what the person wants

| They want to… | Go to |
|---|---|
| Try the product | §2 — no setup at all |
| Read or evaluate the code (e.g. judging) | §3 + §7 |
| Run the test suite | §4 — needs Python 3.10+ and ffmpeg, no secrets |
| Run the backend locally | §5 — testnet, placeholder or real credentials |
| Change code / open a PR | §6 rules, then §4 |

Default to the lightest path that answers the question. Most "does X work?"
questions are answered by §2 (the live app), §4 (the tests), or reading the
module named in §3 — not by standing up the whole stack.

---

## 2. Use the live app (no install)

Open <https://build.letseffinggo.com> and sign in with **Xaman** (QR / push)
or **Joey Wallet** (WalletConnect). A logged-out visitor sees only the sign-in
buttons. Every transaction the user makes is signed in their own wallet; the
app never sees a user's key.

What a user can do, and what it costs on mainnet (production values):

- **Mint** — 1 LFGO, or 10 XRP for wallets without enough LFGO. Bulk mint
  (pay once for up to 10) is on.
- **Swap traits** between two NFTs you own — 10 BRIX per NFT (XRP fallback).
  Mutable NFTs change in place; older non-mutable ones are burned and
  reminted as mutable.
- **Trade** — characters in XRP, traits in BRIX, on native `NFTokenOffer`s;
  bids; "Buy now" on xrp.cafe listings.
- **Closet** — harvest a character's traits into a soulbound Closet NFT,
  re-dress characters, extract traits as tradeable tokens. The Closet Market
  sells Closet traits with no signature and takes a 7% fee, disclosed before
  you list or buy.
- **BRIX drip** — 1 BRIX per unlisted NFT per day, claimed in the app.
- A **BRIX trustline** is needed for claims and trait buys; the app offers to
  set one.

Public, read-only API (no auth) — handy for answering questions with data:

```
https://api.letseffinggo.com/api/config                            # live feature flags
https://api.letseffinggo.com/api/leaderboard?board=users_nfts&period=all
https://api.letseffinggo.com/api/market/listings?kind=character&limit=20
https://api.letseffinggo.com/api/rarity?body=male                  # male|female|ape|milady|skeleton
```

Other leaderboard `board` values: `users_swaps`, `users_builds`, `nft_swaps`,
`brix_rich`, `brix_lp`, `brix_earned`, `nft_rarity`.

On-ledger proof: every app transaction carries `SourceTag 2606160021` plus
provenance `Memos` (who signed, which surface, what action). The collection
issuer is `rLfgoMintj3KBcs4s2XKtquvDwEte2kYfJ`; look it up on any XRPL explorer.

---

## 3. Map of the code

```
lfg_service/app.py      the whole HTTP API: routes, auth, session state machines (large — grep, don't scroll)
lfg_service/identity.py (platform, user) → wallet resolution
lfg_core/config.py      every environment variable and its default — the source of truth
lfg_core/xrpl_ops.py    backend-signed transactions: sign once, confirm by hash, never blind-retry
lfg_core/xumm_ops.py    user-signed Xaman payloads (SourceTag + Memos stamped centrally)
lfg_core/memos.py       the provenance-memo schema (closed enums)
lfg_core/*_flow.py      one state machine per feature: mint, bulk_mint, swap, market, economy,
                        closet_market, shop, burn2mint
lfg_core/signing/       Xaman vs WalletConnect/Joey provider seam
lfg_core/traits.py + trait_config.yaml   rules-driven trait selection
surfaces/               thin clients: discord_bot/, telegram_bot/, x_bot/, _client/ (SDK)
webapp/client/          the no-build vanilla-JS front end (Activity, Mini App and web app)
scripts/                ops tools: listeners, backfills, audits, README generators
tests/, webapp/test_*   ~5,900 pytest tests
docs/                   HACKATHON.md (build log), ACTIVITY_SETUP.md, ops/, superpowers/ (design specs + plans)
```

`CLAUDE.md` at the repo root is the maintainers' operator handbook. It is long
and deployment-specific (paths on the maintainers' server), but it is the
deepest explanation of *why* each subsystem works the way it does. Search it
by topic; don't read it top to bottom.

---

## 4. Run the tests (no secrets needed)

Requirements: Python **3.10+** and `ffmpeg` on `PATH`.

```bash
git clone https://github.com/Team-Hamsa/LFG.git && cd LFG
./setup.sh                      # Linux: ffmpeg if missing, .venv, deps, pre-push hook
# macOS: setup.sh exits early — do it by hand:
#   brew install ffmpeg
#   python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt
#   .venv/bin/pre-commit install --hook-type pre-push
source .venv/bin/activate
scripts/run-tests -n auto --dist loadfile   # parallel; `python -m pytest` also works, slower
```

You do **not** need a `.env` for tests: the root `conftest.py` supplies every
required variable with fake values, and redirects every SQLite store into a
temp directory. Tests never touch the network or a real ledger. Useful
targeted runs:

```bash
python -m pytest tests/test_bulk_mint_flow.py -q
python -m pytest -k sourcetag -q          # the "every tx is tagged" invariants
```

The full pre-push gate (ruff, ruff-format, mypy, gitleaks, pytest, config and
layout checks) is `pre-commit run --hook-stage pre-push --all-files`.

---

## 5. Run the backend locally

**Always use testnet.** The code default is `XRPL_NETWORK=mainnet`.

```bash
cp .env.example .env        # then edit; the template is already set to testnet
```

`lfg_service` refuses to import without these, even if the value is a
placeholder: `XUMM_API_KEY`, `XUMM_API_SECRET`, `SEED`,
`TOKEN_ISSUER_ADDRESS`, `TOKEN_CURRENCY_HEX`, `BUNNY_CDN_ACCESS_KEY`,
`BUNNY_CDN_STORAGE_ZONE`, `DISCORD_CLIENT_ID`, `DISCORD_CLIENT_SECRET`.

- `SEED` must be a **well-formed** XRPL seed — a placeholder string fails at
  import. Generate a throwaway one:
  `python -c "from xrpl.wallet import Wallet; print(Wallet.create().seed)"`
  (fund it from the [testnet faucet](https://xrpl.org/resources/dev-tools/xrp-faucets)
  only if you intend to actually mint).
- Trait art is **not** in the repo (`layers/` is gitignored). Without your own
  `layers/` tree plus `LAYER_SOURCE=local`, the server runs but minting and
  compositing can't.

### Two levels

**a) API exploration, no real credentials.** Placeholders + a generated
`SEED` + `WEBAPP_DEV_MODE=1` + `XRPL_NETWORK=testnet`:

```bash
python -m lfg_service.app            # http://127.0.0.1:8176
curl -s localhost:8176/api/config    # dev_mode: true
curl -s localhost:8176/api/nfts      # authed routes answer as a fixed dev user
```

Dev mode skips authentication, so it is **refused on any network except
testnet** (the service won't start). Note that opening the client in a plain
browser in this mode stops at "Open this inside Telegram or Discord". The
client needs a real host (Discord Activity or Telegram) or the web-surface
build, so level (a) is for the API, not the UI.

**b) The real thing on testnet.** You need real
[Xaman developer credentials](https://apps.xumm.dev/), a Discord application
(for the Activity: see `docs/ACTIVITY_SETUP.md`), a BunnyCDN storage zone, a
funded testnet wallet as `SEED`, and trait art. Then run the surfaces:

```bash
python -m lfg_service.app     # hub + Activity host — start this first
python main.py                # Discord bot (DISCORD_BOT_TOKEN, SERVICE_TOKEN_DISCORD, LFG_SERVICE_URL, ADMIN_LOG_CHANNEL_ID)
python run_telegram.py        # Telegram bot (TELEGRAM_BOT_TOKEN, SERVICE_TOKEN_TELEGRAM, TELEGRAM_ANNOUNCE_CHAT_ID, LFG_SERVICE_URL)
```

Always launch Telegram via `run_telegram.py`, never
`python -m surfaces.telegram_bot.bot`. The latter double-imports the module
and breaks commands.

Feature flags (`ECONOMY_ENABLED`, `CLOSET_MARKET_ENABLED`,
`BULK_MINT_UI_ENABLED`, …) are listed in the README's feature-flags table,
with code defaults generated from `lfg_core/config.py`.

---

## 6. Rules when changing code

These are the project's invariants. Breaking one is a bug even if tests pass.

1. **Every XRPL transaction and every Xaman payload carries
   `SourceTag = 2606160021` and provenance `Memos`** (built by
   `lfg_core/memos.py`). The only exception is Xaman's `SignIn`
   pseudo-transaction. A new transaction path must set both.
2. **Never retry a mint, and never treat "unknown" as "failed".** Backend
   transactions are signed once and confirmed by hash. An indeterminate result
   (`IndeterminateResultError`) means *maybe committed*: reconcile from the
   ledger, never resubmit or compensate blindly.
3. **Incoming payments count only when a validated `tesSUCCESS` meta shows the
   `delivered_amount`.** Never match on the requested `Amount` (partial
   payments).
4. **Wallets are bound only by proof:** a signed Xaman `SignIn`, or a
   proof-signed WalletConnect link. Never accept a wallet address from a
   request body as identity.
5. **No user keys, ever.** Users sign in their own wallet. Backend keys
   (`SEED` = issuer regular key, `BRIX_DISTRIBUTOR_SEED`) sign only
   project-side operations.
6. **Trait-economy supply is audited:**
   `census == genesis + Σ supply_changes`. A flow that creates or destroys a
   trait must write a `supply_changes` row. Never re-freeze genesis.
7. **Tests must stay hermetic:** don't add a bare `load_dotenv()` (use
   `lfg_core.envload.load_dotenv_unless_skipped()`); pin new env/global state
   in the root `conftest.py`. Never write stores into the checkout.
8. **Never bypass the gate** (`git push --no-verify`). CI runs the same checks.
9. **Generated files:** the README blocks between `<!-- …:start/end -->`
   markers and `assets/*.svg` are regenerated by `scripts/readme_*.py` /
   `scripts/render_*.py` in CI. Edit the generator, not the output.

---

## 7. Evaluating LFG: honest boundaries

Useful context if you are reviewing or judging the project:

- **Trust model:** users never give up keys. The backend holds the issuer's
  regular key; the issuer's master key is disabled, so that key is the
  account's only signing authority. The Closet Market is backend-settled: bids
  are BRIX `TokenEscrow`s payable to the app wallet, and the backend holds the
  (encrypted) fulfillment. See `SECURITY.md`.
- **Best-effort, not guaranteed:** buying and burning LFGO after a mint paid in
  XRP (it has not filled since 2026-08-21, #594; bulk XRP mints don't attempt
  it) and the BRIX buy-and-burn on swap fees.
- **Built but off in production:** Trait Shop (`SHOP_ENABLED`), burn-to-mint,
  per-user "Share from my account" on X.
- **Known gaps** are tracked as open issues. The README's Roadmap section is
  synced from `roadmap`-labelled issues.

Where the XRPL depth is: `NFTokenModify` dynamic NFTs, XLS-52 mint-with-offer
(`mint_flow.py` / `xrpl_ops.mint_nft`), the AMM on-ramp and buy-and-burn,
`TokenEscrow` with PREIMAGE-SHA-256 conditions (`crypto_condition.py`,
`closet_market_flow.py`), pinned `LastLedgerSequence` + memo-based recovery,
and a pre-submit `simulate` check.
