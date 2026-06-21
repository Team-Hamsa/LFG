# LFG — XRPL NFT Minting Bot & Discord Activity

![Discord](https://img.shields.io/badge/Discord-Bot%20%2B%20Activity-blue) ![XRPL](https://img.shields.io/badge/XRPL-NFT-green) ![Xaman](https://img.shields.io/badge/Xaman%20(XUMM)-Integration-orange)

**LFG** lets users mint NFTs on the XRP Ledger (XRPL) and swap traits between NFTs they own, paying with the `LFGO` token via the Xaman (XUMM) app. NFT images are composed dynamically from trait layers with ffmpeg, uploaded to BunnyCDN, and minted on the XRPL.

Two front ends share one pipeline (`lfg_core/`):

- **Discord Activity webapp** (`python -m webapp.server`) — embedded app running inside Discord: wallet registration, LFGO trustline setup, NFT minting, and the Trait Swapper. Setup: [docs/ACTIVITY_SETUP.md](docs/ACTIVITY_SETUP.md).
- **Classic Discord bot** (`python main.py`) — slash-command/button interface for the same mint flow.

Both run side by side and share `lfg_nfts.db` and `lfg_core`.

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

---

## Hackathon Roadmap

The following features are scoped for the hackathon sprint. Each links to its tracking issue with full spec/acceptance criteria.

### NFT Generation & Rules
- [ ] [#40 Trait selection rules engine (declarative `trait_config.yaml`)](../../issues/40)
- [ ] [#28 Port generation rules and exclusions from legacy scripts](../../issues/28)
- [ ] [#38 Bug: ape bodies incorrectly assigned face traits (Eyes/Eyebrows/Mouth)](../../issues/38)
- [ ] [#30 Cross-body-type trait layer swapping rules](../../issues/30)

### UX & Front Ends
- [ ] [#42 Web UI (standalone browser-based mint + collection viewer)](../../issues/42)
- [ ] [#46 Dress-up game (visual trait composer, supersedes Trait Swapper)](../../issues/46)
- [ ] [#43 Telegram integration (commands, notifications, wallet linking)](../../issues/43)
  - Built on the shared-services spine ([#53](../../issues/53)) — one `lfg_service` backend, thin surface clients
  - Spec: [shared-services spine design](docs/superpowers/specs/2026-06-17-shared-services-spine-design.md)
  - Plan: [shared-services spine (Plan 1 of 4)](docs/superpowers/plans/2026-06-17-shared-services-spine.md)
- [ ] [#41 X (Twitter) integration (OAuth2, auto-post on mint)](../../issues/41)

### Trading & Economy
- [ ] [#44 In-app collection Marketplace (list, browse, buy via Xaman)](../../issues/44)
- [ ] [#45 DEX integration — backend (OfferCreate/Cancel, order book)](../../issues/45)
- [ ] [#47 AMM integration — backend (deposit/withdraw/swap, pool stats)](../../issues/47)
- [ ] [#48 BRIX daily distribution (1/day per unlisted NFT, claim flow)](../../issues/48)

### Infrastructure & Tooling
- [ ] [#39 Admin UI for authoring `trait_config.yaml`](../../issues/39)
- [ ] [#29 NFT rarity logic (tiers, weights, metadata scoring)](../../issues/29)
- [ ] [#27 QR callback routing for mobile (UA-aware deep-link)](../../issues/27)
- [ ] [#26 Stand up testnet BRIX/XRP AMM pool](../../issues/26)

### Research
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
├── db_helpers.py            # LFG (mint records) table helpers
├── user_db.py               # Users table (wallet registration)
├── init_db.py               # Database initialization
├── scripts/upload_layers_cdn.py  # One-shot upload of layers/ to BunnyCDN
├── docs/ACTIVITY_SETUP.md   # Discord Activity setup guide
└── legacy/linode/           # Preserved legacy production code
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

Upload layers to BunnyCDN or set `LAYER_SOURCE=local` for development:

```
layers/
├── male/
│   ├── Background/<Value>.png|.gif|.mp4
│   ├── Back/ Body/ Clothing/ Mouth/ Eyebrows/ Eyes/ Head/ Accessory/
├── female/
├── ape/
└── skeleton/
```

Use `scripts/upload_layers_cdn.py` to push a local `layers/` tree.

---

## Running

```bash
# Discord Activity
python -m webapp.server

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
