# Discord Activity Setup

How to run the LFG mint webapp as a Discord Activity (embedded app).

## 1. Discord Developer Portal

Everything in this section is one-time portal configuration at
https://discord.com/developers/applications → your application. Do the steps
**in order** — each tab depends on the one before it. Tab names are in the
left sidebar.

### 1a. Bot — credentials and privileged intents

1. **Bot → Token**: copy the bot token → `DISCORD_BOT_TOKEN` in `.env`
   (classic bot only; the Activity does not use it).
2. **Bot → Privileged Gateway Intents**: turn **all three ON**:
   - ☑ **Presence Intent**
   - ☑ **Server Members Intent**
   - ☑ **Message Content Intent**

   > ⚠️ Required by `main.py` (it sets `intents.presences`,
   > `intents.members`, `intents.message_content`). If any is off, the
   > classic bot **crashes on startup** with `PrivilegedIntentsRequired`.
   > The Activity webapp alone does not need these, but enable them anyway
   > if you'll run both.

### 1b. OAuth2 — credentials and redirect

1. **OAuth2 → General**: copy the **Client ID** → `DISCORD_CLIENT_ID` and the
   **Client Secret** → `DISCORD_CLIENT_SECRET` in `.env`.
2. **OAuth2 → Redirects**: add your HTTPS backend URL (your tunnel hostname,
   see §3). Any valid mapped URL works — the Embedded App SDK uses an in-app
   `response_type: code` flow and the server exchanges the code for an
   `identify`-scoped token. Without a registered redirect, the in-app
   `authorize()` call fails.

### 1c. Installation — how the app gets added to a server

The Activity can only be launched in a server where the app is installed.

1. **Installation → Installation Contexts**: enable **Guild Install** (and
   **User Install** if you want it launchable from DMs).
2. **Installation → Default Install Settings → Guild Install**:
   - **Scopes:** `bot`, `applications.commands`
   - **Permissions:** `Send Messages`, `Embed Links`, `Attach Files`,
     `Read Message History`, `Use Application Commands`
3. **Installation → Install Link**: copy it, open it, and add the app to your
   server. (This is also what registers the `/letsgo`, `/register`, `/admin`
   slash commands for the classic bot.)

### 1d. Activities — enable the embedded app

1. **Activities → Settings**: toggle **Enable Activities** ON.
2. **Activities → URL Mappings**: add one row:

   | Prefix | Target |
   |---|---|
   | `/` | `your-backend-host.example.com` (the host running `webapp/server.py`, HTTPS, no scheme) |

   > No `/esm` mapping is needed: the Embedded App SDK is **vendored
   > same-origin** at `webapp/client/vendor/embedded-app-sdk.js`, and
   > `app.js` loads it with
   > `const { DiscordSDK } = await import('./vendor/embedded-app-sdk.js');`.
   > (esm.sh bundles re-export root-absolute paths that resolve outside the
   > Activity's `/.proxy` sub-path and are blocked by the CSP, so the old
   > `/esm` → `esm.sh` mapping never worked reliably — **remove it** if you
   > still have it configured.)

   > ⚠️ With an ngrok-free / cloudflared quick tunnel, the hostname **changes
   > every time the tunnel restarts**. When that happens you must re-paste the
   > new hostname into both the `/` URL Mapping (here) **and** the OAuth2
   > Redirect (§1b). This is the single most common "it stopped working"
   > cause — see §3 for a stable-URL option.

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
python -m webapp.server          # listens on WEBAPP_PORT (default 8080)
```

Verify it's up locally before tunnelling:

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:$WEBAPP_PORT/   # expect 200
```

Discord requires the Activity to be served over **HTTPS**. For development, a
tunnel works (point it at your `WEBAPP_PORT`):

```bash
cloudflared tunnel --url http://localhost:8080
# or: ngrok http 8080
```

Copy the tunnel's HTTPS hostname into the `/` URL Mapping (§1d) **and** the
OAuth2 Redirect (§1b).

**Stable URL (recommended).** Quick tunnels hand you a new random hostname on
every restart, and each change means re-editing two portal fields. To stop
the churn, use a hostname that doesn't move:

- **ngrok reserved domain** (paid): `ngrok http --domain=your-name.ngrok.app 8080`
- **cloudflared named tunnel** (free, needs a domain on Cloudflare):
  `cloudflared tunnel route dns <tunnel> activity.yourdomain.com`
- Or run the tunnel under pm2 so it survives reboots, and only re-paste the
  portal fields when the hostname actually changes.

With a fixed hostname you configure the portal once and never touch it again.

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
`SWAP_OFFER_AMOUNT` (10 BRIX), `SWAP_MAX_NFT_NUMBER` (3535),
`SWAP_RECORDS_DIR` (swap_records, on-chain journal for recovery).

Safety: nothing is burned until both replacement images and metadata are on
the CDN *and* both replacement NFTs are already minted; missing layer files
fail the swap before any burn, a mint failure burns the orphaned replacements
back (originals untouched), and every on-chain step is journaled to
`SWAP_RECORDS_DIR` so an administrator can recover a partial swap.

## Notes

- The Activity proxy's CSP blocks cross-origin requests, so the backend
  serves everything same-origin: the frontend, the API, and QR codes
  (`/api/qr.png?d=...` rendered server-side). External links (Xaman) are
  opened with `sdk.commands.openExternalLink`.
- Mint sessions are in-memory; restarting the server drops in-flight mints
  (terminal records are still in SQLite once minted).
- The bot (`python main.py`) and the webapp can run side by side; both share
  `lfg_nfts.db` and the `lfg_core` pipeline.
