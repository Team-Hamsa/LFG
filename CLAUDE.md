# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**LFG Bot** is a Discord bot that allows users to mint NFTs on the XRP Ledger (XRPL) and trade tokens (LFGO) using the XUMM app. The bot dynamically generates NFT images by compositing trait layers, uploads them to BunnyCDN, and mints them on the XRPL.

## Feature Workflow: Brainstorming → Spec → Plan → Issue Link

**Rule:** Any brainstorming session that starts from a GitHub issue MUST end by linking that issue's spec and plan markdown files back to the issue on GitHub.

Specs and plans live under `docs/superpowers/`:
- Specs: `docs/superpowers/specs/<YYYY-MM-DD>-<feature>-design.md`
- Plans: `docs/superpowers/plans/<YYYY-MM-DD>-<feature>.md`

When a brainstorming session begins with an issue (e.g., "let's spec out #41"):

1. Produce the spec (design doc) in `docs/superpowers/specs/`.
2. Produce the plan in `docs/superpowers/plans/`.
3. **Before the session is considered done, link both files to the issue** by posting a comment on the issue with permalinks (blob URLs at the current commit SHA, not branch-relative paths) to the spec and plan. Use:
   ```bash
   gh issue comment <number> --repo Team-Hamsa/LFG --body "Spec: <url>
   Plan: <url>"
   ```
   Commit the spec/plan files first so the permalinks resolve.

A brainstorming-from-issue session is not complete until the issue carries links to its spec and plan.

## Setup & Installation

### Dependencies
Install all dependencies with:
```bash
pip install -r requirements.txt
```

Key dependencies:
- **discord.py** (2.0+): Discord bot framework with slash commands and UI components
- **xrpl-py**: XRP Ledger client libraries for NFT minting and transactions
- **xumm-sdk-py**: XUMM SDK for secure transaction signing via QR codes
- **bunnycdn-storage**: BunnyCDN client for uploading images and metadata
- **ffmpeg-python**: FFmpeg bindings for layering/compositing trait images
- **python-dotenv**: Environment variable management
- **aiohttp**: Async HTTP client for uploads

### Environment Variables
Create a `.env` file with:
```
DISCORD_BOT_TOKEN=<bot-token>
XUMM_API_KEY=<xumm-key>
XUMM_API_SECRET=<xumm-secret>
BUNNY_CDN_ACCESS_KEY=<bunny-access-key>
BUNNY_CDN_STORAGE_ZONE=<bunny-zone>
BUNNY_CDN_BASE_URL=https://storage.bunnycdn.com
BUNNY_CDN_FOLDER=minttest
SEED=<xrpl-seed>
TOKEN_ISSUER_ADDRESS=<xrpl-token-issuer>
TOKEN_CURRENCY_HEX=<hex-currency-code>
ADMIN_LOG_CHANNEL_ID=<discord-channel-id>
NFT_TAXON=0
NFT_COLLECTION_NAME=Let's Effing Go!
NFT_COLLECTION_FAMILY=Test
NFT_DESCRIPTION=Test
NFT_TRANSFER_FEE=7000
NFT_FLAGS=25
CLOSET_TAXON=1762                                           # optional; Closet soulbound taxon (default 1762)
NFT_SCHEMA_URL=ipfs://QmNpi8rcXEkohca8iXu7zysKKSJYqCvBJn3xJwga8jXqWU
EXTERNAL_WEBSITE_URL=https://letseffinggo.com
RETRY_MAX_ATTEMPTS=5
RETRY_BASE_DELAY=1.0
SESSION_TIMEOUT_TOTAL=60
VIEW_TIMEOUT=600
LFG_SERVICE_URL=http://localhost:8000
SERVICE_TOKEN_DISCORD=<discord-surface-token>
DISCORD_GUILD_ID=<your-server-id>   # optional; makes slash commands appear instantly in that guild (global sync still runs)
TELEGRAM_BOT_TOKEN=<telegram-bot-token>
SERVICE_TOKEN_TELEGRAM=<telegram-surface-token>
TELEGRAM_ANNOUNCE_CHAT_ID=<telegram-channel-id>
TELEGRAM_MINI_APP_URL=<public-https-url-of-the-mini-app>   # optional (#89); unset = launch button omitted
TELEGRAM_INITDATA_MAX_AGE=3600                              # optional (#89); initData replay window in seconds
```

> **Telegram Mini App (#89):** the Mini App serves the same vanilla-JS Activity
> inside Telegram. It is feature-flagged OFF by default: with
> `TELEGRAM_MINI_APP_URL` unset, no launch/menu button appears; with
> `TELEGRAM_BOT_TOKEN` unset on the service side, `POST /api/telegram/auth`
> returns 503. `TELEGRAM_BOT_TOKEN` doubles as the service-side HMAC secret used
> to validate Telegram's signed `initData`. Going live (Part B) is an ops step:
> expose `:8176` over public HTTPS, set `TELEGRAM_MINI_APP_URL` to that host,
> and confirm BotFather accepts the URL.

### Running the Bot
```bash
python main.py
```

The Telegram surface runs as pm2 process `lfg-telegram` → `.venv/bin/python run_telegram.py`.
Launch via the `run_telegram.py` shim, **not** `python -m surfaces.telegram_bot.bot`: running `bot.py`
as `__main__` makes it load a second time under its canonical name when `commands.py` imports `svc`,
creating two `LFGServiceClient` instances — the events task enters one while the command handlers use
the other, whose aiohttp session is never opened, so `/register` and `/mint` fail. The shim imports
`bot` canonically once.

## Directory Structure

```
/LFG MINT BOT/
├── main.py                 # Discord bot entry point; handles slash commands and UI interactions
├── db_helpers.py           # Database helpers for NFT minting records (LFG table)
├── user_db.py              # User registration and wallet management (Users table)
├── ts_helpers.py           # XRPL transaction helpers and utility functions
├── init_db.py              # Database initialization script
├── trait_layers/           # NFT trait image layers organized by type
│   ├── 1 background/
│   ├── 2 body/
│   ├── 3 clothing/
│   ├── 4 mouth/
│   ├── 5 eyebrows/
│   ├── 6 eyes/
│   ├── 7 hat:hair/
│   └── ...
├── requirements.txt        # Python dependencies
├── lfg_nfts.db            # SQLite database (auto-created)
├── users.json             # Legacy user storage (deprecated, use SQLite Users table)
└── backup/                # Historical bot versions

Database Tables:
- LFG: Minted NFT records with metadata, traits, and URLs
- Users: Registered users with wallet addresses
- burned_nfts: Audit log of burned NFTs
```

## Architecture & Key Concepts

### Bot Command Structure
- **`/letsgo`**: Main slash command that displays the NFT minting interface with buttons
- **`/register <wallet>`**: User wallet registration for receiving NFT offers
- **`/admin`**: Admin control panel (requires administrator permissions)

### NFT Minting Flow

1. **User clicks "Mint NFT" button** → Initiates payment request
2. **Token Payment Request** → Creates XUMM QR code for user to scan with XUMM app (sends 1 token to TOKEN_ISSUER_ADDRESS)
3. **Payment Verification** → Bot polls XUMM API to check if payment was signed/confirmed
4. **Trait Selection** → Randomly selects one trait from each layer directory
5. **Image Composition** → Uses FFmpeg to overlay trait layers into a single PNG
6. **BunnyCDN Upload** → Uploads both image and metadata JSON to BunnyCDN
7. **NFT Minting** → Creates NFTokenMint transaction on XRPL using the wallet seed
8. **NFT Offer Creation** → Creates an NFTokenCreateOffer to send the minted NFT to user's wallet
9. **Offer QR Code** → Generates XUMM QR for user to accept the NFT offer

### Key Data Structures

**NFT Record** (in LFG table):
- `nft_number`: Sequential ID starting from 3536
- `nft_id`: XRPL NFToken ID (hex string)
- `discord_id`: Discord user ID who minted it
- `owner_address`: User's XRPL wallet address
- `metadata_url`: CDN URL to metadata.json
- `image_url`: CDN URL to NFT image
- `traits`: Trait columns for each layer (Background, Body, Clothing, Eyes, Eyebrows, Mouth, Hat, Accessory)

**User Record** (in Users table):
- `discord_id`: Unique Discord user ID
- `discord_name`: Discord username
- `wallet`: XRPL wallet address

### Trait Layer System

Traits are organized in numbered directories (e.g., `1 background`, `2 body`, `3 clothing`). The numeric prefix determines the layering order when compositing:

```
trait_layers/
├── 1 background/       (rendered first, at bottom)
├── 2 body/
├── 3 clothing/
├── 4 mouth/
├── 5 eyebrows/
├── 6 eyes/
├── 7 hat:hair/         (rendered last, at top)
└── ...
```

The sorting logic in `get_sorted_trait_layers()` (main.py:273) automatically handles this based on numeric prefixes.

### Database Schema

**LFG Table:**
```sql
CREATE TABLE LFG (
    nft_number INTEGER PRIMARY KEY,
    nft_id TEXT,
    discord_id TEXT,
    owner_address TEXT,
    metadata_url TEXT,
    image_url TEXT,
    Background TEXT, Body TEXT, Clothing TEXT, Eyes TEXT,
    Eyebrows TEXT, Mouth TEXT, Hat TEXT, Accessory TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
```

**Users Table:**
```sql
CREATE TABLE Users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    discord_id TEXT NOT NULL UNIQUE,
    discord_name TEXT NOT NULL,
    wallet TEXT NOT NULL
)
```

**burned_nfts Table:**
```sql
CREATE TABLE burned_nfts (
    nft_number INTEGER PRIMARY KEY,
    nft_id TEXT,
    discord_id TEXT,
    burned_by TEXT,
    reason TEXT,
    burned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    original_mint_time TIMESTAMP
)
```

## Common Development Tasks

### Adding a New Trait Layer
1. Create a numbered folder in `trait_layers/` (e.g., `8 new_trait/`)
2. Add PNG images with descriptive names
3. The trait will automatically be detected and included in NFT generation
4. Add corresponding column to LFG table if storing trait data

### Minting an NFT (Manual Testing)
1. Run `/letsgo` command in Discord
2. Click "Mint NFT" button
3. Scan the payment QR code with XUMM app
4. Approve the token payment
5. Wait for payment confirmation
6. Scan the NFT offer QR code to accept the offer
7. NFT will appear in your XRPL wallet

### Admin Operations
1. `/admin` command opens the admin panel
2. **View Stats**: Shows total mints, unique users, and recent mints
3. **Lookup NFT**: Search for an NFT by number to view details
4. **Burn NFT**: Burn an NFT by number (requires reason; creates audit log)

## Key Functions & Modules

### main.py
- `mint_nft_for_user()`: Async wrapper for XRPL NFT minting with retry logic (main.py:315)
- `create_payment_request()`: Creates XUMM payment QR for token payment (main.py:491)
- `check_payment_status()`: Polls XUMM API to verify payment (main.py:557)
- `create_nft_offer()`: Creates an offer to transfer NFT to user (main.py:414)
- `generate_xumm_qr()`: Generates XUMM QR for NFT acceptance (main.py:452)
- `MintView`: UI class with mint, trustline, and buy buttons (main.py:620)
- `get_sorted_trait_layers()`: Returns trait folders sorted by numeric prefix (main.py:273)
- `get_random_trait()`: Randomly selects a trait from a layer (main.py:263)

### db_helpers.py
- `get_next_nft_number()`: Returns next available NFT number from LFG table (db_helpers.py:6)
- `record_nft_mint()`: Records a newly minted NFT in the database (db_helpers.py:57)
- `get_nft_data()`: Retrieves all data for a specific NFT (db_helpers.py:129)

### user_db.py
- `create_users_table()`: Initializes Users table (user_db.py:9)
- `register_user()`: Registers a new user with wallet address (user_db.py:32)
- `get_all_registered_users()`: Retrieves all registered users (user_db.py:65)

### ts_helpers.py
- `mint_nft()`: Dummy NFT minting function placeholder (ts_helpers.py:743)
- `makeNft()`: Creates composite NFT image from trait layers using FFmpeg (ts_helpers.py:160)
- `burn_nft()`: Burns an NFT on XRPL using issuer seed (ts_helpers.py:636)
- `create_nft_offer()`: Creates NFTokenCreateOffer transaction (ts_helpers.py:793)

## XRPL Integration

### Make Waves Hackathon — required SourceTag `2606160021`

This project is accepted into the **XRPL Make Waves Hackathon**. Per the rules,
**transaction volume only counts when every transaction carries our unique
source tag**. **ALL** XRPL transactions and XUMM/Xaman signing payloads built or
submitted by the app MUST set:

```
SourceTag = 2606160021
```

This applies to every transaction type without exception — `NFTokenMint`,
`NFTokenCreateOffer`, `NFTokenAcceptOffer`, `NFTokenBurn`, `NFTokenModify`,
`Payment` (token + XRP), `TrustSet`, AMM trades (`buy_and_burn`), and any XUMM
payload `txjson`. When adding a new transaction path, set `SourceTag` on the tx
dict / payload before signing or submitting, or hackathon volume credit is lost.

- **Testnet URL**: `https://s.altnet.rippletest.net:51234/` (main.py:198)
- **Mainnet URL**: `https://s1.ripple.com:51234/` (ts_helpers.py:40)
- Wallet is initialized from SEED environment variable
- All NFT minting uses `NFTokenMint` with transfer fees (7000 basis points = 70% secondary sales fee)
- NFT flags = 25 (burnable + transferable + mutable — Dynamic NFTs amendment).
  New mints ARE burnable so the dress-up trait economy can harvest (issuer-burn)
  them. Trait swaps still update them in place via NFTokenModify — the swap path
  is selected by mutability, not burnability (lfg_core/swap_flow.py). Legacy
  non-mutable NFTs are still burned and reminted (now as burnable+mutable, per
  NFT_FLAGS). NFTs minted before this change at flag 24 remain non-harvestable.

### Testnet AMM (BRIX/XRP)

- **AMM account (pool ID):** `rLUnD5mskBnHfwFxCjakDA3RVgK584XQXG`
- **Pair / ratio:** 50 XRP : 5,000 BRIX (BRIX issuer = SEED account on testnet)
- **Starting price:** 0.01 XRP/BRIX · **Trading fee:** 0.5%
- **Purpose:** lets the trait-swap XRP-fee path (`get_amm_xrp_cost` / `buy_and_burn`) quote and clear on testnet.
- **Recreate after a testnet reset:** `.venv/bin/python scripts/testnet_amm_setup.py` (idempotent).

### On-chain NFT index (per-`nft_id`, listener-fresh)

The chain holds **multiple NFTokens per edition number** (duplicates from
trait-swaps / reminting). The app's `lfg_nfts.db` `LFG` table is keyed one row
per edition and **cannot** represent those duplicates, so swap tooling reads a
dedicated per-`nft_id` index instead.

- **Store:** per-network SQLite files `onchain_testnet.db` / `onchain_mainnet.db`
  (gitignored, regenerable), one `onchain_nfts` table keyed by `nft_id`. Built by
  `lfg_core/nft_index.py`; kept fresh by `lfg_core/nft_listener.py`.
- **Backfill (one-time / after a reset):**
  `.venv/bin/python scripts/backfill_onchain.py --network testnet|mainnet`
  (or `onchain_listener.py … snapshot`). Idempotent. Mainnet metadata is on IPFS
  (slow/flaky); unreadable tokens are recorded with empty attributes, not dropped.
- **Preferred mainnet source — Bithomp CSV:** clio+IPFS backfill leaves ~20% of
  mainnet unreadable. A Bithomp export (CDN-cached, metadata pre-parsed) is far
  more complete: `scripts/import_bithomp_csv.py --network mainnet --csv LFGOdata.csv`
  for the live set, and `--csv LFGOburned.csv --burned` for the burned history
  (separate burned-only export has no flag column). This cut unreadable-live from
  1174 → 1 and captured full per-edition history (718 editions with multiple
  tokens). CSVs are gitignored (`LFGO*.csv`).
- **Live sync (pm2):** `lfg-index-testnet` + `lfg-index-mainnet` run
  `scripts/onchain_listener.py --network <net> listen` — subscribe to the clio tx
  stream and apply NFTokenMint / AcceptOffer / Burn / **Modify** (in-place trait
  changes from swaps) to the index, resolving post-transfer owners via `nft_info`.
- **Consumer:** `scripts/audit_layer_coverage.py` reads this index by default
  (instant, offline, complete); pass `--live` to bypass it and scrape the chain.
- clio endpoints: mainnet `wss://s2-clio.ripple.com`, testnet
  `wss://clio.altnet.rippletest.net:51233`.

### Dress-up trait economy — Phase 2 (testnet, on-ledger ops)

Phase 2 (#64) makes the three trait-economy ops real on-chain, mirroring
`lfg_core/swap_flow.py` (fail-safe ordering, on-disk journaling to
`ECONOMY_RECORDS_DIR`, partial-failure recovery). MVP ops are **free**.

Harvest, Assemble, and Equip all require an **active Closet** (see below).

- **Harvest** (`scripts/economy_harvest.py`): burn a live character → its 8
  assets + body drop into the owner's Closet (collection size ↓).
- **Assemble** (`scripts/economy_assemble.py`): a body + a full asset set from
  the Closet → mint that edition + offer it back (collection size ↑, rebirth).
- **Equip** (`scripts/economy_equip.py`): `NFTokenModify` a loose Closet asset
  onto a live character; the displaced asset returns to the Closet (size =).

Model:
- **Economy characters are minted burnable + transferable + mutable**
  (`ECONOMY_NFT_FLAGS = 25`) so the issuer can harvest-burn / assemble-mint /
  equip-modify them. (A character already swapped to mutable-only can't be
  issuer-burned, so it is equip-only until re-minted — surfaced as a precondition
  error.)
- **The per-user Closet** is a soulbound mutable NFToken (`CLOSET_NFT_FLAGS = 16`,
  `CLOSET_TAXON = 1762`). Unlike the legacy Bucket, issuance is a **standalone,
  up-front step** — the user must explicitly accept the Closet offer in Xaman
  before Harvest or Assemble unlock.
  - Lifecycle: `none → pending_accept → active`. `ensure_closet` mints the token
    and creates an on-chain offer, recording status `pending_accept`. The listener
    promotes the record to `active` when it observes `NFTokenAcceptOffer` with
    `owner != issuer`. Harvest/Assemble gate on `status == active`; an offer
    payload is returned to the caller while status is `pending_accept` so the
    user can be prompted to accept.
- **Taxon transition:** `CLOSET_TAXON = 1762` (new, default).
  `LEGACY_BUCKET_TAXON = 1761` (old; read from `BUCKET_TAXON` env var, default
  1761). The listener dual-reads both `lfg_closet` and `lfg_bucket` metadata
  keys and matches both taxons so existing Bucket holders keep working during
  the transition.
- **DB tables are authoritative for accounting; the Closet NFToken mirrors them**
  (its metadata `lfg_closet` block is the on-chain truth the listener rebuilds
  the DB from). Each flow modifies the token *before* the DB so a crash leaves
  the DB rebuildable from the chain.
- **Supply accounting** (`lfg_core/trait_economy.py`): genesis stays frozen; an
  append-only `supply_changes` ledger records intentional growth/shrinkage
  (new-edition mint / permanent burn). Conservation:
  `census == genesis + Σ supply_changes`; `max_edition` is dynamic. The auditor
  (`scripts/audit_trait_economy.py`) flags any unlogged delta as drift.
- Core modules: `lfg_core/economy_flow.py` (flows + `EconomyDeps`),
  `lfg_core/closet_token.py` (Closet metadata + lifecycle — `ensure_closet`,
  `confirm_accept`, `sync_closet`, `ClosetRef`),
  `lfg_core/economy_store.py` (`closet_tokens`/`closet_assets`/`closet_bodies`/`supply_changes`);
  the listener applies closet/supply events via `nft_listener.apply_economy_tx`.
- **Migration** (legacy Bucket → Closet):
  `.venv/bin/python scripts/migrate_bucket_to_closet.py --network testnet|mainnet [--owner rXXX]`
  Idempotent: owners already on `CLOSET_TAXON` are skipped. The old soulbound
  Bucket (flags 16, non-burnable) is abandoned in place — it cannot be
  issuer-burned, so tracking is simply stopped. **Crash-recovery caveat:** if
  the process dies between the `closet_tokens` delete and the new mint, re-run
  with `--owner <addr>` for the affected address; contents in `closet_assets` /
  `closet_bodies` are safe — only the token pointer is transiently lost.

## Important Notes

1. **Token Trustline Required**: Users must set up a trustline for LFGO tokens before receiving payment instructions. The `/letsgo` command provides a "Set LFGO Trustline" button.

2. **XUMM Flow**: All signing is handled by XUMM (no private keys in bot). Users scan QR codes to approve transactions in their XUMM wallet app.

3. **Metadata URL Encoding**: Metadata CDN URLs are converted to hex before being stored on-chain (main.py:304).

4. **Retry Logic**: NFT minting includes exponential backoff retry mechanism (max 5 attempts) to handle network issues.

5. **Admin Channel Logging**: Burns are logged to the ADMIN_LOG_CHANNEL_ID for audit purposes (main.py:1239).

6. **Legacy User Storage**: The `users.json` file is deprecated; the SQLite Users table is the authoritative user store.

## Testing Checklist

- [ ] Bot connects and syncs slash commands
- [ ] `/letsgo` command displays embed with buttons
- [ ] `/register <wallet>` saves user to Users table
- [ ] "Mint NFT" button generates XUMM payment QR
- [ ] Payment confirmation triggers NFT generation
- [ ] FFmpeg successfully composes trait layers
- [ ] Image and metadata upload to BunnyCDN
- [ ] NFT mints on XRPL without errors
- [ ] Offer creation succeeds and generates QR
- [ ] `/admin` command accessible to admins only
- [ ] NFT lookup returns correct metadata
- [ ] NFT burn records in burned_nfts table and logs to admin channel
