# Standalone Web Surface — build.letseffinggo.com (design)

**Date:** 2026-07-16
**Status:** Approved for implementation
**Goal:** Navigating to `https://build.letseffinggo.com` in any browser loads the
same Activity interface that runs inside Discord and Telegram, fully functional
(mint, swap, dress-up, marketplace, leaderboards), with the user's XRPL wallet
as their identity.

## Context (verified on the box, 2026-07-16)

- The Activity client (`webapp/client/`, vanilla JS, no build step) is served by
  `lfg_service` on :8176 (prod) and already runs three auth branches: Discord
  (embedded-app SDK → `POST /api/token`), Telegram (`initData` →
  `POST /api/telegram/auth`), and a degraded unauthenticated dev mode.
- The prod API is already publicly reachable over HTTPS via Tailscale Funnel:
  `https://letseffinggo.tail82fcc6.ts.net/lfg` → `localhost:8176`.
- `letseffinggo.com` DNS is hosted on Google Cloud DNS;
  `www.letseffinggo.com` is a CNAME to GitHub Pages (`team-hamsa.github.io`,
  repo `Team-Hamsa/pages-site`). No record exists for `build.` yet.
- The box's port **80 is publicly reachable** (verified end-to-end with an
  external fetch of a marker file served by local nginx); port **443 is not
  forwarded** (verified with a live listener + external probe). No gcloud CLI or
  DNS API credentials exist on the box.
- Session auth: HMAC session tokens (`make_session_token`, 6 h TTL) keyed on
  `(platform, platform_user_id)`; `identities` table maps that pair → wallet.
  A XUMM SignIn flow (`/api/signin*`) already exists for wallet linking, but it
  requires an existing session — it cannot bootstrap one.
- The client draws cross-origin images into canvas but never reads back
  (`toDataURL`/`getImageData` absent), so a cross-origin API/image host cannot
  taint anything user-visible.

## Approaches considered

1. **GitHub Pages front-end + CORS'd API over the existing funnel (CHOSEN).**
   Publish `webapp/client/` to GitHub Pages from the LFG repo with the custom
   domain `build.letseffinggo.com`; the client calls the prod API cross-origin
   at the funnel URL. TLS is GitHub's problem; ingress is the funnel that
   already serves production. Only user action required: **one DNS CNAME**.
2. **Same-box nginx + Let's Encrypt.** Architecturally cleaner (same-origin,
   zero client/CORS changes), but blocked on TWO user-owned resources: a DNS
   A record AND a router port-forward for 443 (confirmed absent), plus
   residential-IP/DDNS fragility. Prepared as a possible future upgrade; not
   viable autonomously today.
3. **Cloudflare Tunnel.** Requires migrating the zone's nameservers to
   Cloudflare — the largest user action, no autonomous path. Rejected.

## Design

### 1. Service: web auth arm (4th surface, platform `"web"`)

The wallet **is** the identity: `platform="web"`, `platform_user_id=<classic
address>`. `identity.resolve("web", wallet)` then returns the wallet itself, so
every existing `@require_wallet` flow (mint/swap/economy/market) works
unchanged. `memos._SURFACE_TO_PLATFORM` gains `"web" → PLATFORM_WEBAPP` so
on-chain provenance attributes the surface correctly (the enum value already
exists; unknown surfaces currently fall back to `backend`).

Two new **client-callable** endpoints (same trust posture as
`/api/telegram/auth`: unauthenticated but abuse-limited), mirroring the
existing signin pair:

- `POST /api/web/signin` — creates a XUMM SignIn payload
  (`xumm_ops.create_signin_payload`; SignIn is SourceTag/memo-exempt by
  existing convention). Returns `{uuid, signin_link}` — byte-compatible with
  `/api/signin` so the client QR rendering is reused. Per-IP in-memory rate
  limit (sliding window, default 5/min) since payload creation hits the XUMM
  API; 429 over the limit. Payload records live in a separate
  `web_signin_payloads` dict with the same TTL pruning as `signin_payloads`.
  The XUMM `return_url` is the request `Origin` when (and only when) that
  origin is in the CORS allowlist, so Xaman's post-sign button bounces back to
  the site.
- `GET /api/web/signin/{payload_uuid}` — polls XUMM. On
  `signed && is_valid_classic_address(account)`:
  `identity.link("web", account, <existing handle_for_wallet(account) or
  shortened address>, account)`, capture the issued push `user_token`
  (`identity.set_user_token`, best-effort), delete the record, and return
  `{state: "signed", wallet, session_token, user}` where `session_token` is
  `make_session_token({"id": account, "name": …, "platform": "web"})`.
  `pending` / `opened` / `expired` states mirror `handle_signin_status`.
  Unknown uuid → 404. **No ownership pre-check is possible pre-auth** — the
  uuid (128-bit, unguessable, single-use, short-TTL) is the bearer secret,
  exactly like the XUMM deep-link itself.

### 2. Service: CORS middleware

aiohttp middleware, dark by default. `WEB_ALLOWED_ORIGINS` env var
(comma-separated exact origins, e.g.
`https://build.letseffinggo.com,https://team-hamsa.github.io`); empty/unset =
middleware passes everything through untouched (current behavior, all
surfaces unaffected). For requests whose `Origin` header exactly matches an
allowlisted origin:

- add `Access-Control-Allow-Origin: <origin>` + `Vary: Origin` to the response;
- answer `OPTIONS` preflights with 204 + `Access-Control-Allow-Methods:
  GET, POST, DELETE, OPTIONS`, `Access-Control-Allow-Headers: Authorization,
  Content-Type`, `Access-Control-Max-Age: 3600`.

No `Access-Control-Allow-Credentials` (auth rides the `Authorization` header,
not cookies). Non-matching origins get no CORS headers → browser blocks, same
as today.

### 3. Client: web surface branch + configurable API base

- New `webapp/client/config.js`, loaded by `index.html` before `app.js`.
  Repo default: `window.LFG_WEB = null;` (nothing changes for Discord/
  Telegram/dev). The Pages deploy overwrites it with
  `window.LFG_WEB = { apiBase: "https://letseffinggo.tail82fcc6.ts.net/lfg" };`.
- `app.js`: `const API_BASE = (window.LFG_WEB && window.LFG_WEB.apiBase) || ''`
  prefixes the fetch in `api()`, plus `qrUrl()` and `imgUrl()` (QR and image
  proxy URLs are used in `<img src>`, fine cross-origin; canvas draw without
  readback is safe). The dev live-reload `EventSource` stays same-origin-only
  (skipped in web mode).
- Boot: `insideWeb = !!window.LFG_WEB && !insideDiscord && !insideTelegram`.
  `setupWeb()`: restore `localStorage["lfg_web_session"]` and validate with
  `/api/me`; on miss/401/expiry run the web signin flow — the existing
  `register-panel` QR UI (`renderSignin`) pointed at `/api/web/signin*`; on
  `signed`, store the returned session token in localStorage, set
  `sessionToken`, and enter the identical UI (`showMintHome` path).
  `externalOpener` default (`window.open`) already fits a plain browser.
  The degraded dev-mode message is preserved when `LFG_WEB` is null.
- "Change wallet" in web mode re-runs the web signin (new wallet = new
  identity row + fresh token) and replaces the stored token.

### 4. Deployment: GitHub Pages from the LFG repo

`.github/workflows/pages.yml` — on push to `deploy` (prod parity: the client
published to Pages always matches the API the funnel serves) and
`workflow_dispatch`: assemble `_site/` = `webapp/client/**` with `config.js`
rewritten to the funnel API base, upload with `actions/upload-pages-artifact`,
publish with `actions/deploy-pages`. Repo one-time config (gh api): enable
Pages with `build_type=workflow`, set custom domain `build.letseffinggo.com`
(+ enforce HTTPS once the cert exists). Until DNS lands the site serves at
`https://team-hamsa.github.io/LFG/` — relative asset paths work under both.

### 5. Ops

- Prod `.env` gains `WEB_ALLOWED_ORIGINS=https://build.letseffinggo.com,https://team-hamsa.github.io`
  (staging analog optional, pointing at :8177's funnel path).
- **Single user action:** add a DNS record at Google Cloud DNS:
  `build.letseffinggo.com. CNAME team-hamsa.github.io.` GitHub then issues the
  cert automatically and the goal URL goes live.
- Future upgrade path (optional): forward router port 443 → box, add an nginx
  vhost + certbot, flip DNS to an A record — same-origin serving with zero
  client change (config.js falls back to same-origin).

## Error handling

- Rate-limited signin start → 429 `{code: "rate_limited"}`; client shows retry.
- XUMM unreachable → 502 (mirrors existing signin handlers).
- Expired/foreign uuid → `expired`/404; client re-offers the QR.
- Session expiry mid-use → existing 401 handling; web mode clears the stored
  token and returns to the signin panel instead of dying silently.
- CORS misconfig fails closed (no header → browser block), never opens other
  surfaces to new origins.

## Testing

- pytest (service): CORS middleware matrix (no env → no headers; allowlisted
  origin → headers + preflight 204; foreign origin → nothing), web signin start
  (rate limit, payload shape), web signin status (pending → signed issues a
  valid token whose `verify_session_token` payload is platform="web"
  id=wallet; identity row written; push token captured; expired; 404),
  `/api/me` end-to-end with a web token. XUMM mocked (existing pattern).
- Existing suites must stay green (no behavior change with
  `WEB_ALLOWED_ORIGINS` unset and `LFG_WEB` null).
- Manual/e2e: interface loads at the Pages URL, cross-origin preflight + signin
  round-trip against prod funnel.

## Non-goals

- No cookie/refresh-token session persistence beyond the existing 6 h HMAC TTL.
- No same-box TLS ingress (blocked on router 443; documented as upgrade path).
- No Discord/Telegram behavior change of any kind.
