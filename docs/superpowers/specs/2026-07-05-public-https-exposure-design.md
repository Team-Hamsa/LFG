# Public HTTPS Exposure of the Webapp (:8176) — Design

**Issue:** [#89 — Telegram Mini App, Part B (ops)](https://github.com/Team-Hamsa/LFG/issues/89)
**Date:** 2026-07-05
**Type:** Ops runbook design (no app code changes except env; follow-ups flagged explicitly)

## 1. Why now — one blocker, four consumers

`lfg_service` (the Activity backend + vanilla-JS webapp) listens on `:8176`
(`config.WEBAPP_PORT`, lfg_core/config.py:129; `web.run_app(create_app(), port=config.WEBAPP_PORT)`,
lfg_service/app.py:1301 — **no `host=` argument, so aiohttp binds 0.0.0.0**, i.e. the
raw port is already reachable on every interface unless the Linode firewall blocks it).
Today the only public consumers reach it through Discord's `*.discordsays.com` proxy.

> **Open discovery item — how does discordsays reach us TODAY?** Discord
> Activity URL mappings proxy client traffic from `*.discordsays.com` to a
> target the developer configured in the dev portal — meaning **some public
> path to :8176 already exists** (an open :8176 given the 0.0.0.0 bind, a
> tunnel/cloudflared, or an existing proxy on the box — unknown to this spec,
> not discoverable from the repo). Phase 0 of the plan discovers that mapping
> and reconciles: the new Caddy vhost may **replace** it, **coexist** with it,
> or **be the same path**. Until discovered, the assumptions in §4 (":80/:443
> free", "no existing web server", greenfield DNS) are provisional, and the
> rollout MUST NOT change the Activity's current serving path without an
> explicit verification step (plan Phase 2, "Activity unaffected").

Four spec'd features now need a **public HTTPS origin** we control:

| Consumer | Needs | Spec |
|---|---|---|
| Telegram Mini App (#89 Part B) | BotFather requires an `https://` URL; `TELEGRAM_MINI_APP_URL` validation rejects non-https (per #89 Part A) | 2026-06-26-telegram-mini-app-design.md |
| X integration §6.2 | X's crawler must fetch `GET /nft/<number>` OG cards; `PUBLIC_SHARE_BASE_URL` config, URLs built from config never Host header | 2026-07-05-x-integration-design.md |
| Web UI rescope | Standalone browser wallet sign-in (`POST /api/web/signin`) + public profiles; per-IP abuse controls need real client IPs → `WEBAPP_TRUST_PROXY` + proxy-set `X-Forwarded-For` | 2026-07-05-web-ui-rescope-design.md |
| QR deep-link routing | `return_url` targets on our own host | 2026-07-05-qr-deeplink-routing-design.md |

One exposure job unblocks all four. This spec designs that exposure; the plan
file is the executable runbook.

## 2. Verified serving reality (code-grounded)

- **Entry point:** `webapp/server.py` is a 7-line shim re-exporting
  `lfg_service.app.main` (webapp/server.py:1-7); pm2 process `lfg-activity`.
- **Middleware:** single `no_cache_mw` (app.py:1290-1298) sets
  `Cache-Control: no-store` unless the handler opts out. **No CORS middleware,
  no rate limiting, no security headers** exist in-app — the proxy must supply
  headers; same-origin design means no CORS is needed (do NOT add permissive CORS).
- **Auth model:** bearer session tokens via `require_auth` (app.py:291-306) /
  `require_wallet` (app.py:309-323). `WEBAPP_DEV_MODE=1` **bypasses auth entirely**
  (app.py:293-295, 313-315) — hard prerequisite: OFF in prod
  (default off, lfg_core/config.py:136).
- **Static:** `add_static("/", CLIENT_DIR)` (app.py:1289) + `/` index (app.py:1288).

### 2.1 Route exposure audit (every route, app.py:1254-1289)

**Public-safe, unauthenticated by design:**

| Route | Line | Notes |
|---|---|---|
| `GET /api/config` | 1254 | client_id + flags only (app.py:1015-1023) |
| `POST /api/token` | 1255 | Discord OAuth code exchange — code is the secret |
| `POST /api/telegram/auth` | 1257 | HMAC-validates Telegram initData; 503 when TELEGRAM_BOT_TOKEN unset (app.py:399-434) |
| `GET /api/leaderboard` | 1267 | public by design, 60s cache (app.py:504+) |
| `GET /api/qr.png` | 1270 | bounded input ≤2048 (app.py:1026-1032); CPU-costly → proxy rate limit |
| `GET /api/img` | 1271 | CDN proxy, allow-listed bases only, no redirects (app.py:1045-1061) — not an open SSRF |
| `GET /api/layer` | 1272 | path-traversal guarded (`/`, `..`, len checks, app.py:1064-1086) |
| `GET /`, static files | 1288-1289 | the webapp itself |

**Auth-gated in-app (session token; safe to expose, nothing extra needed):**
`/api/me`, `/api/account`, `/api/register`, `/api/mint*`, `/api/signin*`,
`/api/nfts`, `/api/swap*`, `/api/closet`, `/api/economy`, `/api/equip*`,
`/api/harvest*`, `/api/assemble*`, `/api/extract*`, `/api/deposit*` (1258-1284).
Status handlers additionally check session ownership (app.py:326-340).

**Service-token / internal — recommend blocking at the proxy:**

| Route | Line | Why |
|---|---|---|
| `POST /api/session` | 1256 | `@require_service_token` (app.py:387) — only the Discord/Telegram bots call it, and they run on the same host → keep it loopback-only. Auth-gated, so exposure isn't a hole, but blocking removes the token-guessing surface. |
| `GET /events` (WS firehose) | 1285 | service-token gated (app.py:204-210, token also accepted **in the query string** — would land in proxy access logs if exposed). Consumers are local bots → block. |
| `GET /events/me` | 1286 | session-token gated (app.py:215-222) but token passed as `?token=`. **Verified NOT consumed by the Activity client:** the only `events`/`EventSource` reference in `webapp/client/app.js` is the dev-reload stream (`app.js:1410-1411`, gated on `cfg.dev_mode`); no WebSocket or `/events/me` subscription exists in the client, so blocking cannot break the live Activity even once discordsays traffic flows through Caddy. Leave blocked until a consumer needs it publicly; unblocking requires F3 (move token out of query string) first. |
| `GET /__dev/reload` | 1287 | returns 404 unless WEBAPP_DEV_MODE (app.py:1215-1216); only client consumer is dev-mode-gated (app.js:1410-1411) — block, belt-and-braces. |

Blocking `/api/session` and `/events` is provably safe for the bots: both
surfaces build their `LFGServiceClient` from `LFG_SERVICE_URL`
(surfaces/discord_bot/bot.py:46, surfaces/telegram_bot/bot.py:16; required env
per surfaces/discord_bot/config.py:33 and surfaces/telegram_bot/config.py:21),
which is loopback (`http://localhost:8000` per the CLAUDE.md `.env` convention)
— bot traffic never traverses the public host. Plan Phase 0 re-verifies the
live `.env` value before go-live.

**No admin HTTP routes exist** — admin ops are Discord `/admin` + CLI scripts;
nothing to hide there.

### 2.2 Follow-up items (app-side, explicitly NOT done by this runbook)

- **F1 — `WEBAPP_TRUST_PROXY`:** does not exist in config today (grep of
  lfg_core/config.py: absent). It ships with the web-ui-rescope work. This
  runbook makes the proxy send `X-Forwarded-For` now so the flag is truthful
  the day that code lands.
- **F2 — `PUBLIC_SHARE_BASE_URL`:** ships with x-integration; runbook just
  reserves the value.
- **F3 — `/events/me` query-string token** if it ever needs public exposure.
- **F4 (optional):** bind aiohttp to `127.0.0.1` (`web.run_app(..., host=...)`,
  app.py:1301) so the raw :8176 is unreachable even without a firewall — one-line
  change but it IS a code change; until then the Linode Cloud Firewall / ufw does the job.

## 3. Reverse-proxy choice

**Recommendation: Caddy.** Honest framing: this is a solo-operator-maintenance
preference, not benchmarking — all three options serve this traffic fine.

| Option | Certs | Ongoing maintenance | Notes |
|---|---|---|---|
| **Caddy** ✅ | automatic ACME issue + renew, zero cron | apt package, config is ~10 lines, renewal failures self-retry | single static binary, sane TLS defaults |
| nginx + certbot | certbot timer, occasional renewal breakage to notice | two moving parts, more config | most familiar/most documentation |
| Cloudflare Tunnel | CF-terminated | adds a third party + `cloudflared` daemon; hides origin IP (nice) but client IPs arrive in `CF-Connecting-IP`, more XFF nuance for the signin abuse controls | good fallback if the Linode can't open 80/443 |

Caddy wins on "least things to remember": auto-TLS, auto-renew, auto 80→443
redirect, HTTP/2 by default. Config sketch (final version in the plan):

```caddyfile
app.letseffinggo.com {
    encode gzip
    header {
        # HSTS ramp: start low so a rollback-to-HTTP stays cheap for returning
        # browsers; raise to 31536000 only after the cutover has soaked (plan
        # Phase 5). No includeSubDomains — the apex/marketing site isn't ours
        # to commit.
        Strict-Transport-Security "max-age=3600"
        X-Content-Type-Options "nosniff"
        Referrer-Policy "strict-origin-when-cross-origin"
        # NO X-Frame-Options / frame-ancestors CSP here: Telegram + Discord
        # must be able to iframe the app. Revisit per-route if needed.
    }
    @blocked path /events /events/me /__dev/reload /api/session
    respond @blocked 404
    reverse_proxy localhost:8176   # sets X-Forwarded-For/-Proto automatically
}
```

## 4. Subdomain + env plan

- **`app.letseffinggo.com` → Linode public IP** (A record). The apex stays
  whatever it is today (marketing site / EXTERNAL_WEBSITE_URL).
- Env after cutover (service + bot `.env`):
  - `TELEGRAM_MINI_APP_URL=https://app.letseffinggo.com`
  - `PUBLIC_SHARE_BASE_URL=https://app.letseffinggo.com` (takes effect when x-integration lands)
  - `WEBAPP_TRUST_PROXY=1` (takes effect when web-ui-rescope lands)
  - `WEBAPP_DEV_MODE` unset/0 (verify, don't assume)
- BotFather: `/mybots → <bot> → Bot Settings → Menu Button / Configure Mini App`
  → set the HTTPS URL (port 443 implied).

### Assumptions / inputs needed from the user (NOT verified)

1. **DNS control** for letseffinggo.com (registrar/provider unknown) — user must
   create the A record.
2. **Linode firewall state** — whether 80/443 are open and whether :8176 is
   currently exposed to the internet (it binds 0.0.0.0, see §2) is unverified.
3. **No existing web server** already holding :80/:443 on the box (assumed free).
4. **TELEGRAM_BOT_TOKEN** already set service-side (memory says the Telegram
   surface runs, but confirm in `.env`).

## 5. Hardening checklist (design level)

1. `WEBAPP_DEV_MODE` off — else every route is unauthenticated (app.py:293).
2. Proxy blocks `/events`, `/events/me`, `/__dev/reload`, `/api/session` (§2.1).
3. Proxy sets `X-Forwarded-For` + `X-Forwarded-Proto` (Caddy default); app only
   trusts it once `WEBAPP_TRUST_PROXY` exists (F1) — never trust XFF without the proxy.
4. Proxy-level rate limiting on the abuse-sensitive endpoints (`/api/web/signin`
   when it lands, `/api/qr.png`, `/api/telegram/auth`). Caddy needs the
   `caddy-ratelimit` plugin (xcaddy build) — acceptable fallback for day 1:
   rely on in-app payload caps (web-ui spec's 5-pending-per-IP) and add the
   plugin as a fast follow; documented in the plan.
5. TLS: Caddy defaults (TLS 1.2+, modern ciphers) — do not hand-tune.
6. Only 80 (redirect) + 443 public; close/deny :8176 externally at the firewall
   (F4 makes this belt-and-braces later).
7. No CORS headers added — same-origin by design.

## 6. Rollout order & rollback

Order: firewall/DNS → Caddy serving a placeholder → point at :8176 → verify
routes/blocks → set env + `pm2 restart lfg-activity lfg-telegram` → BotFather →
per-consumer verification (plan §4). Telegram goes live only at the env step —
everything before it is invisible to users.

Rollback: `TELEGRAM_MINI_APP_URL` unset + restart = Mini App feature-off by
design (Part A gating); `systemctl stop caddy` (or remove the site block)
= full un-exposure; DNS record removal = last resort. No app state to unwind.

## 7. Non-goals

No app code changes (env only; F1-F4 are separate follow-ups), no CDN, no WAF,
no multi-host/HA, no apex-domain migration, no change to Discord Activity
serving (still via discordsays proxy), no `/nft/<number>` implementation
(that's x-integration's).
