# LFG — XRPL NFT Minting Bot & Discord Activity

![Discord](https://img.shields.io/badge/Discord-Bot%20%2B%20Activity-blue) ![XRPL](https://img.shields.io/badge/XRPL-NFT-green) ![Xaman](https://img.shields.io/badge/Xaman%20(XUMM)-Integration-orange)

**LFG** lets users mint NFTs on the XRP Ledger (XRPL) and swap traits between
NFTs they own, paying with the `LFGO` token via the Xaman (XUMM) app. NFT
images are composed dynamically from trait layers with ffmpeg, uploaded to
BunnyCDN, and minted on the XRPL.

This branch (`webapp-activity`) ships **two front ends over one shared
pipeline** (`lfg_core/`):

- **Discord Activity webapp** (`python -m webapp.server`) — an embedded app
  that runs inside Discord: wallet registration, LFGO trustline setup, NFT
  minting, and the **Trait Swapper** (ported from
  [Trait-Swapper](https://github.com/joshuahamsa/Trait-Swapper)). Setup guide:
  [docs/ACTIVITY_SETUP.md](docs/ACTIVITY_SETUP.md).
- **Classic Discord bot** (`python main.py`) — slash-command/button interface
  for the same mint flow.

Both can run side by side; they share `lfg_nfts.db` and the `lfg_core`
modules.

---

## Features

- **Dynamic NFT generation** — random trait selection per body type
  (male/female/ape/skeleton), composited with ffmpeg; `.gif`/`.mp4` layers
  produce animated (video) NFTs with a PNG thumbnail.
- **Unified trait layer store** — mint and swap pull layers from a single
  CDN tree (`layers/<gender>/<TraitType>/<Value>.ext`), downloaded on demand
  and cached locally; `LAYER_SOURCE=local` for development.
- **Trait Swapper** — pick two of your collection NFTs (same body type) and
  choose traits to exchange. Since the XRPL **Dynamic NFTs** amendment, new
  mints are mutable (not burnable, `NFT_FLAGS=24`) and are swapped **in
  place** via `NFTokenModify` (fee collected upfront via a XUMM BRIX
  payment); legacy burnable NFTs are still burned and reminted — as mutable —
  with replacements minted **before** the originals are burned. Every
  on-chain step is journaled to `swap_records/`, and failures roll back
  (replacements burned, modifies reverted) so no NFT is lost.
- **Xaman (XUMM) signing** — all user transactions (payment, trustline, NFT
  offer acceptance) are signed in the user's own wallet via QR code or deep
  link; the server never holds user keys.
- **Replay-safe payment watching** — payments are verified against
  `meta.delivered_amount` over the XRPL websocket (rippled API v2 shapes),
  with a time-bounded backfill so a payment that lands during a reconnect is
  still caught but old payments can't be replayed.
- **BunnyCDN hosting** — images and metadata JSON uploaded to Bunny storage,
  served from the public CDN.

---

## Repository layout

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
└── legacy/linode/           # Preserved legacy production code (see its README)
```

---

## Prerequisites

- Python 3.10+
- `ffmpeg` on the system path
- A Discord application — bot token plus Client ID/Secret for the Activity.
  The portal also needs privileged gateway intents (classic bot) and the app
  installed + Activities enabled (webapp); full step-by-step in
  [docs/ACTIVITY_SETUP.md](docs/ACTIVITY_SETUP.md#1-discord-developer-portal).
- Xaman (XUMM) API credentials — [Xaman Developer Console](https://apps.xumm.dev/)
- BunnyCDN storage zone credentials
- A funded XRPL account ([testnet faucet](https://xrpl.org/xrp-testnet-faucet.html) for testing)

---

## Installation

```bash
git clone https://github.com/joshuahamsa/Mint-Bot.git
cd Mint-Bot
sudo apt-get update && sudo apt-get install -y ffmpeg
pip install -r requirements.txt
```

### Environment variables

Create a `.env` in the repo root. Required:

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

Discord Activity (webapp) additionally needs:

```plaintext
DISCORD_CLIENT_ID=...
DISCORD_CLIENT_SECRET=...
WEBAPP_SESSION_SECRET=...    # long random string
WEBAPP_PORT=8080
```

Everything else has sensible defaults — see `lfg_core/config.py` for the full
list (XRPL endpoints, NFT taxon/fees, layer store, Trait Swapper settings).
Defaults point at **testnet**; set `XRPL_JSON_RPC_URL` / `XRPL_WS_URL` for
mainnet.

### Trait layers

Upload the canonical layer tree to BunnyCDN (default folder `layers/`):

```
layers/
├── male/
│   ├── Background/<Value>.png|.gif|.mp4
│   ├── Back/ Body/ Clothing/ Mouth/ Eyebrows/ Eyes/ Head/ Accessory/
├── female/
├── ape/
└── skeleton/
```

File stems are the metadata trait values, verbatim. Use
`scripts/upload_layers_cdn.py` to push a local `layers/` tree to the CDN, or
set `LAYER_SOURCE=local` to develop without a CDN.

---

## Running

**Discord Activity webapp** (see [docs/ACTIVITY_SETUP.md](docs/ACTIVITY_SETUP.md)
for portal configuration and HTTPS tunneling):

```bash
python -m webapp.server
```

**Classic bot:**

```bash
python main.py
```

**Tests:**

```bash
python3 -m pytest webapp/test_smoke.py
```

---

## Usage (inside the Activity)

1. Launch the Activity from a voice channel or the App Launcher.
2. Register your XRPL wallet (first time only).
3. Optionally set the LFGO trustline (QR / Xaman deep link).
4. **Mint** — pay 1 LFGO, watch progress, accept the NFT offer in Xaman.
5. **Trait Swapper** — pick two of your NFTs, choose traits to exchange,
   confirm; accept both re-minted NFTs via QR (priced in BRIX).

The classic bot exposes the same mint flow via `/letsgo`, wallet registration
via `/register <wallet>`, and an admin panel via `/admin` (stats, NFT lookup,
burns with audit logging).

---

## Contributing

1. Fork the repository.
2. Create a branch (`git checkout -b feature/YourFeatureName`).
3. Commit and push your changes.
4. Open a pull request.

---

## License

MIT — see [LICENSE](LICENSE).

---

## Acknowledgments

- [xrpl-py](https://github.com/XRPLF/xrpl-py) — XRPL client library
- [Xaman (XUMM) SDK](https://github.com/XRPL-Labs/XUMM-SDK) — transaction signing
- [Discord Embedded App SDK](https://github.com/discord/embedded-app-sdk) — Activity runtime
- [BunnyCDN](https://bunny.net/) — image/metadata hosting
- [FFmpeg](https://ffmpeg.org/) — trait layer compositing
