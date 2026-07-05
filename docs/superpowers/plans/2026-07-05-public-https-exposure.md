# Public HTTPS Exposure — Ops Runbook (Plan)

**Spec:** docs/superpowers/specs/2026-07-05-public-https-exposure-design.md
**Issue:** #89 (Part B). Also unblocks x-integration §6.2, web-ui-rescope, qr-deeplink return_urls.
**Nature:** ops checklist with verification commands, not TDD tasks. Any step
marked **[FOLLOW-UP]** requires a code change and is out of scope for this runbook.

## Phase 0 — Inputs from the user (blockers, all currently ASSUMED not verified)

- [ ] **Discover the current Discord Activity serving path (do this FIRST).**
      The live Activity is reached via `*.discordsays.com` → a URL-mapping
      target configured in the Discord dev portal, so some public path to
      :8176 already exists. Discover and reconcile:
      1. Discord dev portal → the Activity's app → URL Mappings: record the
         root mapping target (domain/IP:port).
      2. On the host: `sudo ss -tlnp | grep -E ':80|:443|:8176'` and
         `pm2 status` / `systemctl list-units | grep -E 'caddy|nginx|cloudflared'`
         — is there an existing proxy/tunnel?
      3. Reconcile → record the outcome in this file:
         - **Same path** (mapping already points at a host we'll serve from):
           reuse it; skip conflicting Phase 1 steps.
         - **Coexist** (e.g. mapping hits raw :8176 or a tunnel): leave it
           untouched; Caddy adds a NEW vhost on 80/443 only. Verify no port
           conflict before Phase 1.
         - **Replace**: only if the user explicitly opts in; requires updating
           the dev portal mapping and a live-Activity re-test.
      The default for this runbook is **coexist / don't touch** — the rollout
      must not change the Activity's serving path; Phase 2 re-verifies the
      Activity still loads after Caddy is live.
- [ ] DNS provider access for `letseffinggo.com`; create `A app → <linode-ip>`.
      Verify: `dig +short app.letseffinggo.com` returns the Linode IP.
- [ ] Confirm ports 80/443 open inbound (Linode Cloud Firewall / `sudo ufw status`)
      and nothing already listening: `sudo ss -ltnp | grep -E ':80|:443'` → empty
      (if NOT empty, resolve via the Phase 0 discovery reconciliation above
      before proceeding — do not evict whatever serves the Activity today).
- [ ] Confirm `:8176` is NOT internet-reachable (app binds 0.0.0.0, app.py:1301):
      from an outside host `curl -m5 http://<linode-ip>:8176/api/config` → should fail.
      If it succeeds, add a firewall deny for 8176 before go-live.
- [ ] Confirm prod env: `grep -E 'WEBAPP_DEV_MODE|TELEGRAM_BOT_TOKEN' .env` —
      DEV_MODE unset/0; bot token present.

## Phase 1 — Install Caddy + site config

- [ ] `sudo apt install caddy` (Debian/Ubuntu repo per caddyserver.com/docs/install).
- [ ] `/etc/caddy/Caddyfile`:

```caddyfile
app.letseffinggo.com {
    encode gzip
    header {
        # HSTS ramp: 1h now, 1y in Phase 5 after soak. No includeSubDomains
        # (apex/marketing site isn't ours to commit).
        Strict-Transport-Security "max-age=3600"
        X-Content-Type-Options "nosniff"
        Referrer-Policy "strict-origin-when-cross-origin"
    }
    @blocked path /events /events/me /__dev/reload /api/session
    respond @blocked 404
    reverse_proxy localhost:8176
}
```
  Note: no X-Frame-Options / frame-ancestors — Telegram/Discord iframe the app.
- [ ] `sudo systemctl reload caddy` → `journalctl -u caddy | tail` shows cert obtained.
- [ ] Verify TLS: `curl -sI https://app.letseffinggo.com/api/config` → 200, valid cert.
- [ ] Verify redirect: `curl -sI http://app.letseffinggo.com/` → 308 to https.

## Phase 2 — Route exposure verification (from an external host or `--resolve`)

- [ ] Public OK: `curl -s https://app.letseffinggo.com/api/config` → JSON with `"dev_mode": false`. **If true, STOP** — auth is bypassed (app.py:293).
- [ ] Public OK: `/` returns index.html; `/api/leaderboard?board=users_nfts` → 200.
- [ ] Auth gate holds: `curl -s https://app.../api/me` → 401.
- [ ] Blocked at proxy (all → 404 from Caddy, not the app):
      `/events`, `/events/me`, `/__dev/reload`, `/api/session` (POST).
      Safety citations: no client consumer of `/events/me` or `/events` exists —
      the only events reference in `webapp/client/app.js` is the dev-mode-gated
      `/__dev/reload` EventSource (app.js:1410-1411); bots reach the service via
      `LFG_SERVICE_URL` (loopback), constructed at surfaces/discord_bot/bot.py:46
      and surfaces/telegram_bot/bot.py:16 (required env: discord_bot/config.py:33,
      telegram_bot/config.py:21). Verify live: `grep LFG_SERVICE_URL .env` →
      localhost URL.
- [ ] Local consumers unaffected: bots still reach `LFG_SERVICE_URL` directly
      (they bypass Caddy) — check pm2 logs for `lfg-bot` / `lfg-telegram`
      reconnect health after any restart.
- [ ] **Activity unaffected:** launch the Discord Activity and confirm it loads
      and mints/swaps poll normally — its serving path (Phase 0 discovery) must
      be unchanged by the new vhost.
- [ ] Rate limiting **[FOLLOW-UP if needed day 1]**: stock Caddy has no rate-limit
      module; either xcaddy-build with `mholt/caddy-ratelimit` for
      `/api/qr.png`, `/api/telegram/auth`, and (future) `/api/web/signin`, or
      accept in-app caps until the web-signin feature lands. Decide before
      flipping web-ui-rescope live.

## Phase 3 — Env cutover + services

- [ ] `.env`: set `TELEGRAM_MINI_APP_URL=https://app.letseffinggo.com`
      (must be https — config validation rejects otherwise, per #89 Part A).
- [ ] Reserve (no-ops until their features merge):
      `PUBLIC_SHARE_BASE_URL=https://app.letseffinggo.com`,
      `WEBAPP_TRUST_PROXY=1` **[FOLLOW-UP: flag doesn't exist in config yet —
      ships with web-ui-rescope; setting it early is harmless]**.
- [ ] `pm2 restart lfg-activity lfg-telegram && pm2 status`.

## Phase 4 — Per-consumer verification

**Telegram (#89 — closes the issue):**
- [ ] BotFather → Bot Settings → Configure Menu Button / Mini App URL → set URL; BotFather accepts it.
- [ ] `/start` in a private chat shows the "🎮 Open App" button (gated on TELEGRAM_MINI_APP_URL).
- [ ] Launch Mini App on a phone: app loads, `POST /api/telegram/auth` returns 200
      (check `pm2 logs lfg-activity`); a forged/empty initData → 401.
- [ ] Comment results on #89 and close.

**X cards (when x-integration lands):**
- [ ] `curl -s https://app.letseffinggo.com/nft/1 | grep twitter:card` (route
      doesn't exist yet — expect 404 today; re-run post-merge).
- [ ] X Card Validator (cards-dev.twitter.com) fetches the URL successfully.

**Web sign-in (when web-ui-rescope lands):**
- [ ] `POST /api/web/signin` from an external IP; confirm the per-IP pending cap
      triggers on the real client IP, not the proxy's (requires WEBAPP_TRUST_PROXY
      code + Caddy's X-Forwarded-For — Caddy sets it by default).

**QR/deeplink return_urls (when that work lands):**
- [ ] A return_url pointing at `https://app.letseffinggo.com/...` opens post-sign.

## Phase 5 — Monitoring

- [ ] `pm2 status` includes caddy? No — caddy is systemd; check
      `systemctl is-enabled caddy` → enabled (survives reboot).
- [ ] Simple uptime check: pm2 cron (pattern of lfg-snapshot) or external ping
      hitting `https://app.letseffinggo.com/api/config` and alerting on non-200.
      Minimal local version:
      `pm2 start scripts/... --cron "*/10 * * * *"` **[FOLLOW-UP: tiny check
      script if no external monitor is chosen]** — or just use a free external
      monitor (UptimeRobot) pointed at `/api/config`.
- [ ] **HSTS raise (after ≥1 week soak with no rollback):** bump
      `Strict-Transport-Security` to `max-age=31536000` in the Caddyfile,
      `systemctl reload caddy`. Still no `includeSubDomains`.
- [ ] Cert renewal: automatic in Caddy; spot-check monthly with
      `echo | openssl s_client -connect app.letseffinggo.com:443 2>/dev/null | openssl x509 -noout -enddate`.

## Rollback

1. Mini App off: unset `TELEGRAM_MINI_APP_URL`, `pm2 restart lfg-activity lfg-telegram`
   (feature-flag off by design; button disappears, /api/telegram/auth stays but is harmless).
2. Full un-expose: `sudo systemctl stop caddy` (or delete the site block + reload).
3. DNS record removal — last resort, slow to propagate.
No database or app state changes anywhere in this runbook.

## Explicit follow-up items (code changes, NOT part of this runbook)

- F1: `WEBAPP_TRUST_PROXY` config flag + XFF parsing — ships with web-ui-rescope.
- F2: `PUBLIC_SHARE_BASE_URL` consumption — ships with x-integration.
- F3: `/events/me` moves its token out of the query string if it ever needs public exposure.
- F4 (optional hardening): pass `host="127.0.0.1"` to `web.run_app` (lfg_service/app.py:1301)
  so raw :8176 is loopback-only regardless of firewall.
- F5 (optional): xcaddy build with caddy-ratelimit for proxy-level per-IP limits.
