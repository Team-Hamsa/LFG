# LFG — XRPL NFT Minting Bot & Discord Activity

![Discord](https://img.shields.io/badge/Discord-Bot%20%2B%20Activity-blue) ![XRPL](https://img.shields.io/badge/XRPL-NFT-green) ![Xaman](https://img.shields.io/badge/Xaman%20(XUMM)-Integration-orange)

**LFG** lets users mint NFTs on the XRP Ledger (XRPL) and swap traits between NFTs they own, paying with the `LFGO` token via the Xaman (XUMM) app. NFT images are composed dynamically from trait layers with ffmpeg, uploaded to BunnyCDN, and minted on the XRPL.

Three front ends share one backend (`lfg_service`) and one pipeline (`lfg_core/`):

- **Discord Activity webapp** (`python -m lfg_service.app`) — embedded app running inside Discord: wallet registration, LFGO trustline setup, NFT minting, the Trait Swapper, the Marketplace, Leaderboards, and the Dressing Room (economy features currently disabled — see below). Setup: [docs/ACTIVITY_SETUP.md](docs/ACTIVITY_SETUP.md).
- **Telegram bot** (`python run_telegram.py`) — chat-style mint + trait swapper via inline keyboards, plus a feature-flagged Mini App that serves the same Activity inside Telegram.
- **Classic Discord bot** (`python main.py`) — slash-command/button interface for the same mint flow.

All surfaces run side by side against the shared `lfg_service` backend. The collection is **live on XRPL mainnet** (cutover 2026-07-10; 3,535 editions reconciled).

> **XRPL Make Waves Hackathon:** every XRPL transaction and Xaman signing payload the app builds carries `SourceTag 2606160021`.

---

## What's Built

| Feature | Status |
|---|---|
| Dynamic NFT generation (trait selection + ffmpeg compositing) | ✅ |
| Animated NFT support (`.gif`/`.mp4` layers → video NFT + PNG thumbnail) | ✅ |
| Unified CDN/local trait layer store | ✅ |
| Xaman QR signing (payment, trustline, offer acceptance) | ✅ |
| Trait Swapper — in-place swap via `NFTokenModify` (mutable NFTs) | ✅ |
| Replay-safe payment watching over XRPL websocket | ✅ |
| BunnyCDN image + metadata hosting | ✅ |
| Discord Activity (embedded webapp) | ✅ |
| Variable rarity engine (mainnet-seeded weights, network-scoped) | ✅ |
| BRIX trustline setup button | ✅ |
| Admin panel (stats, NFT lookup, burn with audit log) | ✅ |
| Shared-services spine — one `lfg_service` backend, thin surface clients | ✅ |
| Telegram surface (bot + trait swapper + Mini App) | ✅ |
| Dress-up trait economy (Closet, harvest/assemble/equip, tradeable trait tokens) | ⏸ built, currently disabled — see note below |
| In-app NFT marketplace (list / browse / buy via Xaman, XRP-denominated) | ✅ |
| Xaman push delivery (sign requests pushed to the app, QR fallback) | ✅ |
| Ledger history database + Activity leaderboards (incl. BRIX richlist) | ✅ |
| On-chain NFT index with live listeners | ✅ |
| Seasonal trait manifest (Season 3 mint exclusion) | ✅ |
| Mainnet launch hardening (regular-key signing, feature flags, live BRIX/XRP AMM) | ✅ |
| Declarative trait rules engine (`trait_config.yaml`: z-order, body affinity, validation CLI) | ✅ |
| Body-affinity matrix derived from full mint history (3,535 editions audited) | ✅ |
| Cross-body trait swapping (compatibility matrix, API-enforced + UI-filtered) | ✅ |
| Shared trait layers (`layers/shared/` with verify-then-move migration) | ✅ |
| Fifth body type (milady) live in the mint pool | ✅ |
| Animated trait layers (transparent GIF bodies → video NFTs, gifski pipeline) | ✅ |
| Mainnet launch — live collection, network-aware databases, post-cutover hardening | ✅ |

> **Trait economy status:** all four phases are implemented and were live on
> testnet, but the economy is **currently disabled in production**
> (`ECONOMY_ENABLED=0`) — an adversarial review surfaced a batch of bugs
> (issues [#178](../../issues/178)–[#184](../../issues/184)) that are being
> worked through. Go-live checklist: [#185](../../issues/185).

---

## Shipped During the Hackathon (since June 21)

Everything below was designed, built, and merged during the Make Waves sprint. PR numbers link the work.

### Lines of Code

<!-- hackathon-loc:start -->
<img src="assets/hackathon_loc.svg" alt="Hackathon code growth bar" width="728">

*Hand-written code merged since the hackathon baseline (`e296308`, 2026-06-19 — last commit before June 21, 12,080 lines), measured by `git diff --numstat`. Counts `.py`/`.js`/`.css`/`.html` only; docs, markdown, data files (CSV/JSON manifests), dependency lists, and the legacy/backup trees are excluded. Updated automatically on every push to `main`.*

| Category | Lines added | Lines removed | Net |
|---|---:|---:|---:|
| Application code | +25,268 | −2,555 | 22,713 |
| Tests | +27,104 | −8 | 27,096 |
| **Total** | **+52,372** | **−2,563** | **49,809** |
<!-- hackathon-loc:end -->

### Shared-Services Spine 
**Issues[#43](../../issues/43) / [#53](../../issues/53)**. 
**PRs [#76](https://github.com/Team-Hamsa/LFG/pull/76#), [#78](https://github.com/Team-Hamsa/LFG/pull/76#), [#77](https://github.com/Team-Hamsa/LFG/pull/77), [#78](https://github.com/Team-Hamsa/LFG/pull/78), [#79](https://github.com/Team-Hamsa/LFG/pull/79), [#80](https://github.com/Team-Hamsa/LFG/pull/80), [#81](https://github.com/Team-Hamsa/LFG/pull/81)**.  
One `lfg_service` backend now serves every surface through a shared Surface SDK: the REST/WS backend (Plan 1), the `LFGServiceClient` SDK (Plan 2), the Discord bot migration (Plan 3), and the new Telegram surface (Plan 4).

### Telegram Integration — #81–#83, #92–#98
- Full Telegram bot: registration, minting, and a chat-style **trait swapper via inline keyboards** (#96).
- **Telegram Mini App** (feature-flagged) serving the Activity inside Telegram with signed-`initData` auth (#98).
- Xaman-verified `/register` on both Discord and Telegram (#83).
- Unified wallet-keyed **cross-surface accounts** with display handles (#94), minted-artwork announcements (#92, #95), and a cross-surface event **firehose** announcing swaps and economy actions everywhere (#97).

### Dress-up Trait Economy ([#46](../../issues/46)) — #62, #67, #71, #105, #106
A full on-ledger trait economy in four phases:
- **Phase 1** — supply model, genesis reconciliation, conservation auditor [#62](https://github.com/Team-Hamsa/LFG/pull/62).
- **Phase 2** — on-ledger ops: **Harvest** (burn a character → its traits drop into your Closet), **Assemble** (body + full trait set → re-mint), **Equip** (`NFTokenModify` a loose trait onto a live character) [#67](https://github.com/Team-Hamsa/LFG/pull/67).
- **Phase 3** — **Dressing Room UI** in the Discord Activity: visual composer with canvas + roster [#71](https://github.com/Team-Hamsa/LFG/pull/71),.
- **Phase 4** — **tradeable trait tokens**: Extract a Closet trait as a standalone transferable NFToken (7% royalty) and Deposit it back, creating a secondary market for individual traits [#106](https://github.com/Team-Hamsa/LFG/pull/106).
- The per-user **Closet** is a soulbound mutable NFToken with standalone issuance [#105](https://github.com/Team-Hamsa/LFG/pull/105).

> Currently **feature-flagged off** in production while review findings
> ([#178](../../issues/178)–[#184](../../issues/184)) are fixed;
> go-live tracked in [#185](../../issues/185).

### In-app Marketplace ([#44](../../issues/44)) — #129, #132, #134, #139
- XRP-denominated marketplace for characters and trait tokens, built entirely on
  native `NFTokenOffer` sell offers — no escrow, no custodial holding ([#129](https://github.com/Team-Hamsa/LFG/pull/129)).
- Derived `market_listings` index kept current three ways: the live tx listener,
  finalize-writes from the List/Buy/Cancel session state machines, and an
  idempotent backfill sweep.
- Fail-closed buys: the sell offer is re-verified on-ledger immediately before
  the Xaman payload is built, and the signer is checked against the session wallet.
- Sold traits settle automatically back into the buyer's Closet, with a retry
  sweep backstopping restarts.
- **Xaman push delivery** ([#135](../../issues/135), [#139](https://github.com/Team-Hamsa/LFG/pull/139)) —
  returning users get sign requests pushed straight to the Xaman app instead of
  rescanning a QR (QR/deep link always returned as fallback).

### Milady Body + Animated Layers — #171, #174
- Fifth body type (**milady**) registered end-to-end: art, trait config,
  affinity matrix, swap matrix ([#171](https://github.com/Team-Hamsa/LFG/pull/171)).
- **Animated trait layers**: transparent-GIF body values compose into video
  NFTs (MP4 + PNG thumbnail); `scripts/make_animated_layer.py` (ffmpeg → gifski)
  produces compliant 1080×1080 alpha-preserving GIFs ([#174](https://github.com/Team-Hamsa/LFG/pull/174)).

### Ledger History + Leaderboards (not in original scope) — #118–#121
- Per-network **ledger history database**: raw `account_tx` archive with derived NFT and BRIX events, resumable backfill (95k+ mainnet txs), and live dual-write from the index listeners (#118, #119).
- Public `GET /api/leaderboard` with **8 boards** — most NFTs held, most swaps, most builds, most-swapped NFTs, **BRIX richlist**, LP richlist, BRIX earned, and NFT rarity — with rolling time windows and a "me" rank lookup.
- Activity **Leaderboard UI** with a two-tier category/board selector (#120, #121).
- Nightly BRIX/LP balance snapshots for trend charts.

### On-chain NFT Index (not in original scope) — #59, #60
Per-network SQLite index of every live NFToken (the chain holds multiple tokens per edition), kept fresh by pm2 listeners on the clio tx stream, plus a layer-coverage auditor and Bithomp CSV importer.

### NFT Generation & Rules
- **Ape face compose rule** — nose injection + melt-ape masking, fixing face traits on ape bodies ([#38](../../issues/38)) (#110).
- **Seasonal trait manifest** — sidecar `layers/seasons.json` (1,167 traits across 3 seasons) with Season 3 excluded from minting (#115–#117).

### Trait Rules Engine + Body Affinity ([#40](../../issues/40), [#28](../../issues/28), [#30](../../issues/30)) — #122, #123, #126–#128
Trait legality is no longer an accident of directory layout — a single validated `trait_config.yaml` drives mint, swap, and economy:
- **Body-affinity audit** — derived the per-value body-compatibility matrix from the full 3,535-edition mint history (per-edition deduped, burned included), with a human-review report gate ([#122](https://github.com/Team-Hamsa/LFG/pull/122)). Closed #28 by proving the "legacy exclusion rules" never existed in code.
- **Rules engine** — declarative `trait_config.yaml` (layer z-order, per-value z-overrides absorbing TOP_TRAITS, owner-confirmed affinity, swap matrix, exclusion machinery) with strict load-time validation and a pre-commit/CI validation CLI ([#123](https://github.com/Team-Hamsa/LFG/pull/123)).
- **Mint + compose integration** — affinity-filtered selection that fails loud on over-constrained layers; compose ordering flows through config z-values; 200-mint property test ([#127](https://github.com/Team-Hamsa/LFG/pull/127)).
- **Cross-body trait swapping** — Ape↔Skeleton headwear/clothing, Straight↔Curved everything-but-clothing, universal Accessory/Back; enforced per-trait at the API, mirrored in the UI, and applied identically to economy equip/assemble ([#128](https://github.com/Team-Hamsa/LFG/pull/128)). Closes #30.
- **Shared trait layers** — byte-identical universal art (52 Backgrounds + 4 Backs) physically deduplicated into `layers/shared/` via an idempotent verify-then-move migration with atomic seasons-manifest rewrite ([#126](https://github.com/Team-Hamsa/LFG/pull/126)).

### Mainnet Launch Hardening
- Regular-key signing for the issuer (`SIGNING_ACCOUNT` override) (#112).
- `ECONOMY_ENABLED` flag to launch with the trait economy off (#113).
- Bithomp import filtered by collection issuer; census reconciled to 3,535 clean editions (#111).
- **Mainnet BRIX/XRP AMM pool live** and quoting for the trait-swap fee path; testnet pool tooling (`scripts/testnet_amm_setup.py`) closes [#26](../../issues/26).
- **Mainnet cutover executed 2026-07-10** — audits passing (3,535 live editions,
  zero drift), local-first image archive, network-aware app database
  ([#167](../../issues/167)) so testnet mints can't poison the mainnet edition counter.

---

## Roadmap — Remaining

- [ ] [#42 Web UI (standalone browser-based mint + collection viewer)](../../issues/42)
- [ ] [#41 X (Twitter) integration (OAuth2, auto-post on mint)](../../issues/41)
- [ ] Trait economy re-enable — fix review findings [#178](../../issues/178)–[#184](../../issues/184), go-live checklist [#185](../../issues/185)
- [ ] [#45 DEX integration — backend (OfferCreate/Cancel, order book)](../../issues/45)
- [ ] [#47 AMM integration — backend (deposit/withdraw/swap, pool stats)](../../issues/47)
- [ ] [#48 BRIX daily distribution (1/day per unlisted NFT, claim flow)](../../issues/48) — leaderboard/history groundwork shipped in #118
- [ ] [#39 Admin UI for authoring `trait_config.yaml`](../../issues/39)
- [ ] [#27 QR callback routing for mobile (UA-aware deep-link)](../../issues/27)

### Completed
- [x] [#26 Testnet BRIX/XRP AMM pool](../../issues/26)
- [x] [#28 Port generation rules and exclusions from legacy scripts](../../issues/28) — reframed: body-affinity matrix derived from mint history
- [x] [#29 NFT rarity logic (tiers, weights, metadata scoring)](../../issues/29)
- [x] [#30 Cross-body-type trait layer swapping rules](../../issues/30)
- [x] [#38 Ape bodies incorrectly assigned face traits](../../issues/38)
- [x] [#40 Trait selection rules engine (declarative `trait_config.yaml`)](../../issues/40)
- [x] [#43 Telegram integration](../../issues/43)
- [x] [#44 In-app collection Marketplace (list, browse, buy via Xaman)](../../issues/44)
- [x] [#46 Dress-up game](../../issues/46) — built; currently disabled pending bug fixes (see [#185](../../issues/185))
- [x] [#49 Explore: AI agent integration via XRPL Payments skill](../../issues/49)

---

## Repository Layout

```
LFG/
├── main.py                  # Classic Discord bot entry point
├── lfg_core/                # Shared pipeline (used by both front ends)
│   ├── config.py            # All environment configuration
│   ├── xrpl_ops.py          # Mint, burn, offers, payment watching
│   ├── xumm_ops.py          # Xaman payloads + QR generation
│   ├── mint_flow.py         # Mint session state machine
│   ├── swap_flow.py         # Trait-swap state machine (modify-in-place / mint-before-burn)
│   ├── swap_meta.py         # Wallet NFT + metadata fetching
│   ├── swap_compose.py      # ffmpeg compositing + output upload
│   ├── layer_store.py       # Unified CDN/local trait layer store
│   ├── traits.py            # Random trait selection
│   └── cdn.py               # BunnyCDN upload helper
├── webapp/
│   ├── server.py            # aiohttp backend for the Discord Activity
│   ├── client/              # No-build frontend (index.html, app.js, style.css)
│   └── test_smoke.py        # Smoke tests
├── lfg_service/             # Shared REST/WS backend serving all surfaces
├── surfaces/telegram_bot/   # Telegram surface (bot + Mini App)
├── run_telegram.py          # Telegram launch shim
├── db_helpers.py            # LFG (mint records) table helpers
├── user_db.py               # Users table (wallet registration)
├── init_db.py               # Database initialization
├── scripts/                 # Ops tooling: backfills, listeners, audits, economy CLIs
└── docs/ACTIVITY_SETUP.md   # Discord Activity setup guide
```

---

## Prerequisites

- Python 3.10+
- `ffmpeg` on the system path
- Discord application — bot token + Client ID/Secret. Privileged gateway intents required (classic bot); Activities enabled (webapp). Full steps in [docs/ACTIVITY_SETUP.md](docs/ACTIVITY_SETUP.md).
- [Xaman (XUMM) API credentials](https://apps.xumm.dev/)
- BunnyCDN storage zone credentials
- Funded XRPL account ([testnet faucet](https://xrpl.org/xrp-testnet-faucet.html) for testing)

---

## Installation

```bash
git clone https://github.com/Team-Hamsa/LFG.git
cd LFG
sudo apt-get update && sudo apt-get install -y ffmpeg
pip install -r requirements.txt
```

### Environment Variables

Create a `.env` in the repo root:

```plaintext
DISCORD_BOT_TOKEN=...        # classic bot only
XUMM_API_KEY=...
XUMM_API_SECRET=...
SEED=...                     # XRPL wallet seed used for minting
TOKEN_ISSUER_ADDRESS=...
TOKEN_CURRENCY_HEX=...
BUNNY_CDN_ACCESS_KEY=...
BUNNY_CDN_STORAGE_ZONE=...
```

Discord Activity additionally needs:

```plaintext
DISCORD_CLIENT_ID=...
DISCORD_CLIENT_SECRET=...
WEBAPP_SESSION_SECRET=...
WEBAPP_PORT=8080
```

Full list with defaults in `lfg_core/config.py`. Defaults point at **testnet**; set `XRPL_JSON_RPC_URL` / `XRPL_WS_URL` for mainnet.

### Trait Layers

Trait art is served from the local `layers/` tree (`LAYER_SOURCE=local`, the production setting); BunnyCDN is still used for minted image/metadata uploads:

```
layers/
├── shared/                  # universal art every body pulls from (Background, Back)
│   ├── Background/<Value>.png|.gif|.mp4
│   └── Back/
├── male/
│   ├── Body/ Clothing/ Mouth/ Eyebrows/ Eyes/ Head/ Accessory/
├── female/
├── ape/
├── milady/
└── skeleton/
```

Trait legality (layer order, per-value body affinity, cross-body swap matrix) lives in `trait_config.yaml` at the repo root, validated by `scripts/validate_trait_config.py` (runs in pre-commit and CI).

Use `scripts/upload_layers_cdn.py` to push a local `layers/` tree.

---

## Running

```bash
# Discord Activity
python -m lfg_service.app

# Classic bot
python main.py

# Tests
python3 -m pytest webapp/test_smoke.py
```

---

## Usage

1. Launch the Activity from a voice channel or App Launcher (or run `/letsgo` in the classic bot).
2. Register your XRPL wallet (first time only).
3. Optionally set the LFGO trustline via QR / Xaman deep link.
4. **Mint** — pay 1 LFGO, accept the NFT offer in Xaman.
5. **Trait Swapper** — pick two of your NFTs, choose traits to exchange, confirm; pay BRIX and accept via Xaman QR.

---

## Contributing

1. Fork the repo.
2. Create a branch (`git checkout -b feature/your-feature`).
3. Commit and push.
4. Open a pull request.

---

## License

MIT — see [LICENSE](LICENSE).

---

## Acknowledgments

- [xrpl-py](https://github.com/XRPLF/xrpl-py)
- [Xaman (XUMM) SDK](https://github.com/XRPL-Labs/XUMM-SDK)
- [Discord Embedded App SDK](https://github.com/discord/embedded-app-sdk)
- [BunnyCDN](https://bunny.net/)
- [FFmpeg](https://ffmpeg.org/)
