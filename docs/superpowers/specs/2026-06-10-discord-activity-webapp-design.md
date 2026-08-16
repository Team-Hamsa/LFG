# Discord Activity Webapp — Design

Date: 2026-06-10 · Branch: `webapp-activity` · Status: approved (autonomous /goal run; defaults chosen per CLAUDE.md)

## Goal

A webapp version of the LFG mint bot that runs as a **Discord Activity** (embedded
iframe app via `@discord/embedded-app-sdk`), letting users register a wallet,
set the LFGO trustline, and mint NFTs from inside Discord — same XRPL/XUMM
pipeline as the bot, web UI instead of slash commands.

## Approaches considered

1. **Python backend + static JS frontend, shared core module (CHOSEN).**
   Extract the proven mint pipeline out of `main.py` into `lfg_core/`, serve a
   small aiohttp API + static frontend. Reuses all working XRPL/XUMM/BunnyCDN
   code; no new runtime; aiohttp already a dependency.
2. Node.js rewrite (Discord's official Activity samples are JS). Rejected:
   discards ~3k lines of working Python XRPL code.
3. Duplicate the flow into the webapp without refactoring. Rejected: two copies
   of the mint pipeline drift apart.

## Architecture

```
webapp-activity branch
├── lfg_core/                  # shared, Discord-free business logic
│   ├── config.py              # env loading (incl. DISCORD_CLIENT_ID/SECRET)
│   ├── traits.py              # layer sorting, random selection, ffmpeg compose
│   ├── xrpl_ops.py            # mint_nft, create_nft_offer (from main.py)
│   ├── xumm_ops.py            # payment link/QR, trustline + accept payloads,
│   │                          #   payment subscription watcher
│   └── mint_flow.py           # MintSession state machine orchestrating a mint
├── webapp/
│   ├── server.py              # aiohttp app: API + static files
│   └── client/                # index.html, app.js, style.css
│       └── (embedded-app-sdk via esm bundle, no Node build step)
└── main.py                    # bot still runs unchanged (imports untouched)
```

### Activity specifics

- Frontend calls `DiscordSDK.ready()` → `authorize()` (scopes `identify`) →
  POST `/api/token` (backend exchanges code with `DISCORD_CLIENT_SECRET`) →
  `authenticate()`. Backend issues a signed session token used as Bearer auth.
- Discord's Activity proxy enforces CSP: all network calls must be same-origin
  (mapped under `/.proxy/`). Therefore the backend serves **everything**: API,
  frontend, and QR codes (`/api/qr.png?d=...` rendered server-side with
  `qrcode`); XUMM deep links open via `sdk.commands.openExternalLink`.
- Dev-portal setup documented in `docs/ACTIVITY_SETUP.md` (URL mapping `/` →
  backend host, enable Activities, redirect URI).

### Mint flow (state machine in `mint_flow.py`)

`POST /api/mint` creates a `MintSession` (in-memory dict keyed by UUID) and a
background task: `awaiting_payment → generating → minting → creating_offer →
offer_ready → done | failed | payment_timeout`. The frontend polls
`GET /api/mint/{id}` every ~3 s and renders the current stage, payment QR,
then the NFTokenAcceptOffer QR/deeplink. Payment verification reuses the
sender-verified XRPL subscription watcher. DB writes reuse `db_helpers` /
`user_db` unchanged.

### API

| Route | Purpose |
|---|---|
| `POST /api/token` | OAuth code → access token + session token |
| `GET /api/me` | Discord identity + registered wallet |
| `POST /api/register` | Save/replace wallet (validated XRPL classic address) |
| `POST /api/trustline` | XUMM TrustSet payload → QR/deeplink |
| `POST /api/mint` | Start mint session |
| `GET /api/mint/{id}` | Poll session state |
| `GET /api/qr.png` | Server-rendered QR for any payload (session-scoped) |

### Error handling

Each background-task stage catches and stores `error` on the session so the
poller surfaces it; payment timeout is its own terminal state. API returns
401 without a valid session token, 400 on invalid wallet, 409 on mint while
another session for the same user is active.

### Testing

`webapp/test_smoke.py`: imports all modules, builds the aiohttp app, asserts
routes exist, exercises wallet validation and the session state machine with
the XRPL/XUMM calls stubbed. Run with `python -m pytest webapp/test_smoke.py`.
Live end-to-end (real XUMM scan) remains manual, per the existing checklist.
