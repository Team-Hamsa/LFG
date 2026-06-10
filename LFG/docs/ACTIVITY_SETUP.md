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

## Notes

- The Activity proxy's CSP blocks cross-origin requests, so the backend
  serves everything same-origin: the frontend, the API, and QR codes
  (`/api/qr.png?d=...` rendered server-side). External links (Xaman) are
  opened with `sdk.commands.openExternalLink`.
- Mint sessions are in-memory; restarting the server drops in-flight mints
  (terminal records are still in SQLite once minted).
- The bot (`python main.py`) and the webapp can run side by side; both share
  `lfg_nfts.db` and the `lfg_core` pipeline.
