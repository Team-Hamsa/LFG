# Discord Activity Setup

How to run the LFG mint webapp as a Discord Activity (embedded app).

## 1. Discord Developer Portal

In your application at https://discord.com/developers/applications:

1. **OAuth2 → General**: note the **Client ID** and **Client Secret**.
2. **Activities → Settings** (or "Embedded App SDK"): enable Activities for
   the app.
3. **Activities → URL Mappings**: add
   | Prefix | Target |
   |---|---|
   | `/` | `your-backend-host.example.com` (the host running `webapp/server.py`, HTTPS) |
   | `/esm` | `esm.sh` |

   The `/esm` mapping lets the frontend import `@discord/embedded-app-sdk`
   through the Activity proxy (`/.proxy/esm/...`) without a Node build step.
4. **OAuth2 → Redirects**: add your mapped URL (any valid URL works; the
   Embedded App SDK uses `response_type: code` with an in-app flow).

## 2. Environment variables

Add to `.env` (in addition to the existing bot variables):

```
DISCORD_CLIENT_ID=<application client id>
DISCORD_CLIENT_SECRET=<application client secret>
WEBAPP_SESSION_SECRET=<long random string>
WEBAPP_PORT=8080
```

## 3. Run the backend

```bash
python -m webapp.server
```

Expose it over HTTPS (Discord requires it). For development, a tunnel works:

```bash
cloudflared tunnel --url http://localhost:8080
# or: ngrok http 8080
```

Put the tunnel hostname in the `/` URL mapping.

## 4. Launch in Discord

Join a voice channel (or use the App Launcher in chat) → Activities → pick
your app. The flow inside the Activity:

1. SDK handshake + OAuth (`identify` scope) — automatic.
2. Register your XRPL wallet (first time only).
3. Optionally set the LFGO trustline (QR / Xaman deep link).
4. Mint: pay 1 LFGO (QR), watch progress, then accept the NFT offer (QR).
5. Trait Swapper: pick two of your collection NFTs (same body type), choose
   traits to exchange, confirm — the originals are burned, re-crafted NFTs
   are reminted and offered back (priced in BRIX); accept both via QR.

## Unified trait layer store (mint + swap)

Both the mint flow and the Trait Swapper pull trait layers from a **single
tree** hosted on BunnyCDN storage (no local copies, no double upload):

```
<storage zone>/layers/          # LAYERS_CDN_FOLDER (default "layers")
├── male/
│   ├── Background/<Value>.png|.gif|.mp4
│   ├── Back/ Body/ Clothing/ Mouth/ Eyebrows/ Eyes/ Head/ Accessory/
├── female/
├── ape/
└── skeleton/
```

Rules:
- The **file stem is the metadata trait value, verbatim** (`Rainbow Puke.png`
  → trait value "Rainbow Puke") — exact case and spaces; no normalization.
- Trait-type folder names are exact-case: `Background, Back, Body, Clothing,
  Mouth, Eyebrows, Eyes, Head, Accessory` (compositing order; a value of
  `None` means "no file").
- `.png`, `.gif`, `.mp4` are allowed; any non-PNG layer makes the composed
  NFT a video (audio carried over, PNG thumbnail generated for metadata).
- Mint picks a random gender directory, then one random value per trait
  type. Swap resolves exact values from the NFT metadata.

Layer files are downloaded on demand and cached in `LAYER_CACHE_DIR`
(default `.layer_cache/`); directory listings use the Bunny storage API.
For development without a CDN, set `LAYER_SOURCE=local` and put the same
tree in `LAYERS_DIR` (default `layers/`).

## Trait Swapper configuration

Optional env overrides (defaults match the original Trait-Swapper bot):
`SWAP_ISSUER_ADDRESS`, `SWAP_TAXON` (1760), `SWAP_CDN_FOLDER` (LFGO, output
uploads), `SWAP_OFFER_CURRENCY_HEX` / `SWAP_OFFER_ISSUER` /
`SWAP_OFFER_AMOUNT` (10 BRIX), `SWAP_MAX_NFT_NUMBER` (3535).

Safety: nothing is burned until both replacement images and metadata are
uploaded to the CDN; missing layer files fail the swap before any burn.

## Notes

- The Activity proxy's CSP blocks cross-origin requests, so the backend
  serves everything same-origin: the frontend, the API, and QR codes
  (`/api/qr.png?d=...` rendered server-side). External links (Xaman) are
  opened with `sdk.commands.openExternalLink`.
- Mint sessions are in-memory; restarting the server drops in-flight mints
  (terminal records are still in SQLite once minted).
- The bot (`python main.py`) and the webapp can run side by side; both share
  `lfg_nfts.db` and the `lfg_core` pipeline.
