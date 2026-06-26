# Telegram Mini App: parity with the Discord Activity — Design

**Issue:** [Team-Hamsa/LFG #89](https://github.com/Team-Hamsa/LFG/issues/89)
**Status:** Design / planning only — no application code in this doc.
**Depends on:** #87 (Telegram inline-keyboard launch buttons), #90 (account / `identities` auth mapping). Both merged.

## 1. Goal

Serve the **exact same** vanilla-JS app that powers the Discord **Activity**
(`webapp/client/`, served by `lfg_service` on `:8176`, including mint, trait
swapper, and the Dressing Room economy) inside Telegram as a **Mini App /
WebApp**.

The backend (`lfg_service.app`) is already platform-aware: session tokens carry
a `platform` field, `identities` maps `(platform, platform_user_id) → wallet`,
and the Telegram surface already authenticates as `platform="telegram"` for the
bot SDK. The remaining work is small and surgical:

1. A **Telegram WebApp launch button** that opens the Mini App URL.
2. An **auth-mapping endpoint** that validates Telegram's signed `initData` and
   mints a `platform="telegram"` session token.
3. **Client dual-mode boot** — `app.js` detects Telegram vs Discord and runs the
   right handshake, then runs the identical UI.
4. **HTTPS exposure** of `:8176` to the public internet (the ops decision —
   §7).

## 2. Current Discord Activity auth flow (the pattern Telegram mirrors)

Inside Discord the page is served through Discord's Activity proxy, with the
Embedded App SDK vendored same-origin. The flow, end-to-end:

**Client (`webapp/client/app.js`):**
```js
const params = new URLSearchParams(window.location.search);
const insideDiscord = params.has('frame_id');   // boot discriminator
...
async function setupDiscord() {
  const { DiscordSDK, Common } = await import('./vendor/embedded-app-sdk.js');
  const { client_id: clientId } = await api('/api/config');   // public config
  const sdk = new DiscordSDK(clientId);
  await sdk.ready();
  const { code } = await sdk.commands.authorize({ client_id: clientId,
    response_type: 'code', state: '', prompt: 'none', scope: ['identify'] });
  const tokenData = await api('/api/token', {                 // ← token exchange
    method: 'POST', body: JSON.stringify({ code }),
  });
  sessionToken = tokenData.session_token;                     // ← stored
  await sdk.commands.authenticate({ access_token: tokenData.access_token });
  ...
}
```
Then `main()`:
```js
await setupDiscord();
me = await api('/api/me');               // Authorization: Bearer <sessionToken>
if (me.wallet) showMintHome();
else { status(...); await startSignin(); }  // Xaman sign-in → registers wallet
```

Every subsequent API call carries the session token: `api()` sets
`headers['Authorization'] = 'Bearer ' + sessionToken`.

**Server (`lfg_service/app.py`):** the token-exchange endpoint is
**`POST /api/token`** (`handle_token`). It:
1. exchanges the OAuth `code` at `https://discord.com/api/oauth2/token`,
2. fetches `/users/@me` to get the Discord user id + username,
3. mints a session token via `make_session_token({"id": user["id"], "name":
   username})` — note **no `platform` key → defaults to `"discord"`** (see
   `make_session_token` / `_platform`).

**Session token shape** (`make_session_token` / `verify_session_token`): a
base64url JSON body `{"id","name","platform","exp"}` plus an HMAC-SHA256 sig
over the body, keyed by `_session_secret()` (`WEBAPP_SESSION_SECRET`, or a
SHA-256 derivation of `XUMM_API_SECRET` as fallback). `require_auth` verifies
the sig + expiry and puts the payload in `request["user"]`; `_platform(user)`
reads `user.get("platform", "discord")`; `_resolve_wallet(platform, uid)` maps
it to a wallet via `identity_store.resolve` (with a Discord-only legacy `Users`
fallback).

**Critical observation:** there are already *two* session-token issuers:
- `POST /api/token` — Discord OAuth → `platform="discord"`.
- `POST /api/session` (`handle_session`, **service-token gated**) — used by the
  bot surfaces' `LFGServiceClient` to mint a token for an arbitrary
  `platform_user_id` under `request["surface"]` (e.g. `"telegram"`).

`/api/session` is *not* usable by the Mini App's browser JS: it requires a
`SERVICE_TOKEN_*` secret that must never ship to a client. The Mini App needs a
**new, client-callable** endpoint that proves the Telegram user's identity from
`initData` instead of a shared secret. That is the heart of #89.

## 3. Telegram `initData` validation

When Telegram launches a Mini App it injects a signed launch payload as
`window.Telegram.WebApp.initData` (a URL-encoded query string) and a parsed
mirror `window.Telegram.WebApp.initDataUnsafe`. The raw `initData` string is
HMAC-signed by Telegram using the bot token; the server validates it to trust
the embedded `user.id` **without** any OAuth round-trip.

### 3.1 Validation algorithm (canonical, per Telegram docs)

Given the raw `initData` query string and the bot token:

1. Parse `initData` into key/value pairs (URL-decoded values).
2. Remove the `hash` field; keep its value aside as `received_hash`.
   (Also remove `signature` if present — it belongs to the newer Ed25519 scheme
   and is not part of the HMAC data-check-string.)
3. Build the **data-check-string**: sort the remaining fields by key
   (lexicographically), and join as `"key=value"` lines separated by `\n`
   (newline). Example:
   `auth_date=<...>\nchat_instance=<...>\nquery_id=<...>\nuser=<json>`.
4. Derive the secret key:
   `secret_key = HMAC_SHA256(key="WebAppData", message=bot_token)`
   — note the inversion: the **literal string `"WebAppData"` is the HMAC key**
   and the **bot token is the message** (this is the #1 gotcha; swapping them
   silently fails every check).
5. Compute `calc_hash = hex( HMAC_SHA256(key=secret_key, message=data_check_string) )`.
6. Accept iff `hmac.compare_digest(calc_hash, received_hash)` (constant-time).
7. **Staleness:** reject if `now - int(auth_date) > MAX_AGE` (recommend
   `MAX_AGE = 3600` s, configurable via `TELEGRAM_INITDATA_MAX_AGE`). This is
   the replay defense — `initData` carries no nonce, so freshness is the only
   guard.

On success, parse the `user` field (a JSON object) → `user.id` (int),
`user.username` / `first_name` for the display handle.

### 3.2 Where it lives

A new **client-callable** endpoint on `lfg_service.app`:

```
POST /api/telegram/auth
  body: { "init_data": "<raw window.Telegram.WebApp.initData string>" }
  → 200 { "session_token": "...", "user": { "id": "<tg_id>", "username": "<handle>" } }
  → 401 { "error": "invalid initData", "code": "bad_initdata" }   (bad hash / stale)
  → 503 { "error": "telegram mini app not configured" }           (no bot token)
```

A small pure module `lfg_service/telegram_auth.py` holds the validator
(`validate_init_data(init_data: str, bot_token: str, max_age: int) -> dict |
None`) so it is unit-testable in isolation (see plan §A1). The handler:

1. reads `init_data` from the JSON body,
2. calls `validate_init_data(...)` with `config.TELEGRAM_BOT_TOKEN`,
3. on success mints `make_session_token({"id": str(tg_id), "name": handle,
   "platform": "telegram"})` and returns it.

**No auto-registration.** The handler does **not** create or look up a wallet —
it only proves identity and issues a token. Wallet resolution stays exactly as
today: `/api/me` returns `wallet: null` for an unregistered Telegram user, and
the client falls into the existing Xaman sign-in flow (`startSignin()`), which
links `(telegram, tg_id) → wallet` via the already-platform-aware
`handle_signin_status`. Do **not** mint a wallet on the user's behalf.

### 3.3 Config

The validator needs the bot token. The service runs in a different process from
the Telegram bot, but both read the same `.env`, so `TELEGRAM_BOT_TOKEN` is
already available to import in `lfg_core/config.py` (add it there, optional —
empty string disables `/api/telegram/auth` with a 503). Also add
`TELEGRAM_MINI_APP_URL` (the public HTTPS URL the launch button points at) and
`TELEGRAM_INITDATA_MAX_AGE` (default 3600).

## 4. Client detection & dual-mode boot

`app.js` currently hardcodes Discord. The change is minimal: branch in `main()`
on the runtime, factor the Discord-specific setup behind a feature check, and
add a parallel `setupTelegram()`.

### 4.1 Detection

```js
const tg = window.Telegram?.WebApp;
const insideTelegram = !!(tg && tg.initData);     // Telegram injected a signed payload
const insideDiscord = params.has('frame_id');      // existing discriminator
```

The Telegram WebApp JS bridge (`telegram-web-app.js`) must be loaded in
`index.html` (`<script src="https://telegram.org/js/telegram-web-app.js">`)
**before** `app.js`. It is a no-op outside Telegram (`window.Telegram` simply
stays undefined), so it is safe to always include and does not affect the
Discord path. Discord's CSP currently forbids cross-origin scripts; the vendored
SDK exists for that reason. **Decision needed:** either (a) load
`telegram-web-app.js` only when a `?tgWebApp=1` (or similar) query hint is
present so it never trips Discord's CSP, or (b) vendor it same-origin like the
Discord SDK. Recommend **(b) vendor it** — same pattern, no CSP surprises,
offline-safe (see §6, Parity gaps).

### 4.2 Telegram setup

```js
async function setupTelegram() {
  const tg = window.Telegram.WebApp;
  tg.ready();
  tg.expand();                                  // use full height
  const data = await api('/api/telegram/auth', {
    method: 'POST',
    body: JSON.stringify({ init_data: tg.initData }),
  });
  sessionToken = data.session_token;            // same storage as Discord path
  externalOpener = (url) => tg.openLink(url);   // Telegram's external-link API
  return data.user;
}
```

### 4.3 Boot branch in `main()`

```js
if (insideTelegram) {
  await setupTelegram();
} else if (insideDiscord) {
  await setupDiscord();
} else {
  status('Open this inside Telegram or Discord. (Dev mode: API calls unauthorized.)');
  return;
}
me = await api('/api/me');
if (me.wallet) showMintHome();
else { status(`Hey ${me.username} — sign in with Xaman to start building.`); await startSignin(); }
```

Everything after `me = await api('/api/me')` is **unchanged**. The session token
is platform-stamped, so all of mint / swap / dressup / signin already route to
the correct `identities` rows with no further changes.

### 4.4 Platform-specific shims to generalize

- **`externalOpener`** — Discord uses `sdk.commands.openExternalLink({url})`;
  Telegram uses `tg.openLink(url)` (or `tg.openTelegramLink` for `t.me` URLs).
  Already abstracted behind `openExternal()` / `externalOpener`; just set it in
  `setupTelegram()`.
- **`discordCtx()`** — returns `{guild_id, channel_id}` from query params for the
  XUMM `return_url`. Telegram has no such context; it returns `{}` (already
  null-safe server-side — `xumm_ops.discord_return_url(None, None)` yields no
  return button). Rename is optional; behavior is correct as-is. Mini Apps can
  also pass a Telegram deep-link return URL (`tg://...`) — out of scope for MVP.
- **Orientation lock** (`sdk.commands.setOrientationLockState`) — Discord-only,
  already in `setupDiscord()`; no Telegram equivalent needed.
- **`confirmDialog`** in-app overlay — built because Discord's sandboxed iframe
  no-ops `window.confirm`. Telegram offers `tg.showConfirm()`, but the existing
  overlay works in both; keep it (no change).

## 5. Launch surface

Two non-exclusive options to open the Mini App from Telegram:

1. **Inline `WebAppInfo` button** (builds directly on #87's inline keyboards).
   Add a button to the `/start` menu and/or a dedicated `/app` command:
   ```python
   from telegram import InlineKeyboardButton, WebAppInfo
   InlineKeyboardButton("🎮 Open LFG App", web_app=WebAppInfo(url=config.TELEGRAM_MINI_APP_URL))
   ```
   `TELEGRAM_MINI_APP_URL` comes from `surfaces/telegram_bot/config.py`
   (new optional env var; the button is omitted when unset). **Constraint:**
   `web_app` buttons only work in **private chats** (DMs with the bot), not in
   group inline keyboards — fine for `/start` and `/app` in DM.
2. **BotFather menu button** — a one-time `@BotFather → /setmenubutton` (or
   `setChatMenuButton` API call) that pins a persistent "Open App" button in the
   chat input area pointing at the same URL. No code beyond setting it once;
   good for discoverability.

**Recommendation:** ship **both** — the inline button (code, tested) for the
in-conversation entry, and the BotFather menu button (ops, one-time) for the
persistent entry. The menu button can also be set programmatically in
`_post_init` via `application.bot.set_chat_menu_button(...)` so it survives
redeploys without manual BotFather steps — preferred.

## 6. Parity gaps (Activity assumes Discord)

| Area | Discord assumption | Telegram generalization |
|------|--------------------|-------------------------|
| Boot discriminator | `params.has('frame_id')` | add `window.Telegram?.WebApp` branch |
| Auth handshake | SDK `authorize()` → `/api/token` | `tg.initData` → `/api/telegram/auth` |
| External links | `sdk.commands.openExternalLink` | `tg.openLink` |
| SDK loading | vendored `embedded-app-sdk.js` | vendor `telegram-web-app.js` (recommended) |
| Status copy | "Connecting to Discord…" (`index.html`) | neutral "Connecting…" (cosmetic) |
| `return_url` ctx | guild/channel query params | none (`{}`, already null-safe) |
| Session `platform` default | `"discord"` when key absent | `make_session_token` must be called **with** `platform:"telegram"` — verified by the `/api/telegram/auth` handler |
| CSP | Discord injects a strict CSP; `/api/img`, `/api/qr.png`, `/api/layer` exist to keep everything same-origin | Telegram WebApps have no equivalent forced CSP, but keeping same-origin proxies is harmless and preserves one code path — **no change** |

The same-origin proxy endpoints (`/api/img`, `/api/qr.png`, `/api/layer`) and
the in-app `confirmDialog` overlay all work unchanged in Telegram — they were
defensive and happen to be portable. **No UI logic forks** beyond boot.

## 7. HTTPS / hosting — THE OPS DECISION ⚠️

**This is the one blocker that needs a human decision and is not codeable.**

The Discord Activity is served to clients **through Discord's own proxy** —
Discord terminates TLS and forwards to `:8176` over its tunnel, so `lfg_service`
itself never needs a public cert. **Telegram has no such proxy.** A Telegram
Mini App URL must be a **public HTTPS URL with a valid (non-self-signed) TLS
certificate**, reachable by Telegram's servers and the user's device, on a
standard port (443). BotFather rejects `http://`, IP literals, and ports other
than 443/88/80→443.

Options to expose `:8176` publicly over HTTPS:

| Option | How | Pros | Cons |
|--------|-----|------|------|
| **A. Cloudflare Tunnel** (`cloudflared`) | Run a tunnel daemon on the host; map `lfg.example.com → localhost:8176`. Cloudflare issues/terminates TLS. | No inbound ports opened; free; valid cert automatic; survives dynamic IP; trivial to add/remove. | Adds Cloudflare as a dependency in the request path; needs a domain on Cloudflare DNS. |
| **B. Reverse proxy + Let's Encrypt** (nginx/Caddy on the host) | Caddy auto-provisions a cert for `lfg.example.com` and proxies `:443 → :8176`. | Self-hosted, no third party in path; Caddy makes certs one-line. | Requires a public domain + open inbound :443 + the host being internet-reachable (NAT/firewall). |
| **C. Reuse existing fronting** (Tailscale Funnel / existing OAuth2 proxy) | If the host already has Tailscale Funnel or an OAuth2 proxy in front, point a hostname at `:8176`. | Reuses infra already trusted by the team. | Tailscale Funnel cert/hostname is `*.ts.net` (works, but ties the Mini App URL to it); an OAuth2 proxy in front would block Telegram's unauthenticated fetch — must bypass auth for the Mini App path. |

**Recommendation: Option A (Cloudflare Tunnel).** It is the lowest-risk,
lowest-ops path: no inbound firewall changes, automatic valid TLS, a stable
hostname for `TELEGRAM_MINI_APP_URL` and BotFather, and it is independent of the
host's public reachability. The webapp already sets `Cache-Control: no-store`
and same-origin proxies, so it sits cleanly behind a tunnel. **The user must
choose and provision this before the Mini App can go live** — flagged as the
top open question.

Whichever option is chosen, also confirm **CORS**: today the Activity is served
same-origin (HTML + API from `:8176`), so there is no CORS at all. The Mini App
should likewise be served **same-origin** from the same public hostname (the
tunnel/proxy fronts the whole `lfg_service` app, not just the static files), so
no CORS headers are needed. **Do not** split static hosting from the API onto
different origins — that would force CORS and complicate the session-token
header. Keep it one origin.

## 8. Security

- **initData replay / staleness:** `initData` has no nonce, so reject stale
  `auth_date` (`MAX_AGE = 3600`s). The session token it mints is short-lived
  (`SESSION_TTL = 6h`) and re-derivable, so a replayed-within-window `initData`
  only re-proves the same identity — acceptable. Document that lowering
  `MAX_AGE` tightens the replay window at the cost of re-auth friction.
- **Bot token as HMAC secret:** the bot token is the validation secret — treat
  it as a credential. It is already in `.env`; ensure `/api/telegram/auth`
  never echoes it, never logs `init_data` at INFO, and that a missing token
  yields a clean 503 (feature-off) rather than validating everything as
  invalid. The token lives only server-side; it is **never** sent to the client
  (unlike the Discord `client_id`, which is public).
- **CORS / origin:** serve same-origin (§7) → no CORS surface. If a future
  split forces CORS, allowlist exactly the Mini App origin, `POST` +
  `Authorization`, no wildcard.
- **Cross-surface identity isolation (#90 — must be preserved):** the minted
  token carries `platform="telegram"`. Every ownership check in `app.py`
  (`session.discord_id == user["id"] AND session.platform == _platform(user)`,
  the signin payload ownership check, `_resolve_wallet`) already keys on
  `(platform, id)`. A Telegram Mini App session therefore **can never** read or
  act as a Discord identity, even if the numeric ids collide — this is exactly
  the property the existing `test_service_signin_platform.py` tests assert.
  The new endpoint must reuse `make_session_token` with the explicit
  `platform:"telegram"` and must **never** fall back to `"discord"`. Add a test
  mirroring `test_signin_status_cross_platform_404` for the
  `/api/telegram/auth`-minted token.
- **Don't trust `initDataUnsafe`:** the client-readable
  `Telegram.WebApp.initDataUnsafe` is *unsigned* — only the raw `initData`
  string passed to the server and HMAC-validated there may be trusted. The
  server extracts `user.id` from the *validated* payload, never from anything
  the client asserts separately.

## 9. Open questions / decisions for the user

1. **HTTPS / hosting (BLOCKER):** Cloudflare Tunnel (recommended) vs reverse
   proxy + Let's Encrypt vs reuse existing fronting? Need a stable public HTTPS
   hostname for `TELEGRAM_MINI_APP_URL` + BotFather. **Nothing ships without
   this.**
2. **Launch surface:** inline `WebAppInfo` button only, BotFather menu button
   only, or both (recommended)? Set the menu button programmatically in
   `_post_init` or manually via BotFather?
3. **Registration gate:** load the app for unregistered Telegram users and
   prompt Xaman sign-in inline (recommended — mirrors Discord), or require
   `/register` first and refuse to open the Mini App until registered? Design
   assumes the former.
4. **Vendor `telegram-web-app.js` vs CDN load:** vendor same-origin
   (recommended, CSP/offline-safe) or load from `telegram.org`?
5. **`initData` max age:** confirm `3600`s default acceptable for the replay
   window.
