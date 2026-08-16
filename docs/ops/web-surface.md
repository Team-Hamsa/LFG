# Web surface (build.letseffinggo.com) — ops runbook

The Activity as a plain website (#240). Front-end on GitHub Pages, API on the
prod funnel. Spec: `docs/superpowers/specs/2026-07-16-web-surface-design.md`.

## Moving parts

| Piece | Where | Trigger |
|---|---|---|
| Front-end | GitHub Pages (this repo, workflow build) | `.github/workflows/pages.yml` on push to `deploy` |
| API | prod `lfg-activity` :8176 via funnel `https://letseffinggo.tail82fcc6.ts.net/lfg` | `scripts/promote.sh` |
| CORS | `WEB_ALLOWED_ORIGINS` in prod `.env` | service restart |
| Auth | `POST /api/web/signin` → XUMM SignIn → `platform="web"` session | — |

## Go-live checklist

1. **Merge + promote** — the Pages workflow fires on the `deploy` push and
   publishes `webapp/client/` with `config.js` pointing at the funnel.
2. **Prod env** — `WEB_ALLOWED_ORIGINS=https://build.letseffinggo.com,https://team-hamsa.github.io`
   in `~/LFG/.env` (already staged 2026-07-16), picked up on the deployer's
   drain-restart.
3. **DNS (user-owned, Google Cloud DNS zone `letseffinggo.com`)** — add:
   ```
   build.letseffinggo.com.  CNAME  team-hamsa.github.io.
   ```
   GitHub then auto-provisions the Let's Encrypt cert (minutes to ~1 h).
4. **Enforce HTTPS once the cert exists:**
   ```bash
   gh api -X PUT repos/Team-Hamsa/LFG/pages -f https_enforced=true
   ```

## Verifying

```bash
# Pages serving (post-DNS; pre-DNS use the edge directly):
curl -s https://build.letseffinggo.com/ | grep -o 'app.js?v=[0-9]*'
curl -s --resolve build.letseffinggo.com:80:185.199.108.153 \
  http://build.letseffinggo.com/config.js        # pre-DNS spot-check

# CORS preflight against prod:
curl -si -X OPTIONS -H 'Origin: https://build.letseffinggo.com' \
  -H 'Access-Control-Request-Method: POST' \
  https://letseffinggo.tail82fcc6.ts.net/lfg/api/web/signin | head -8
# expect: 204 + Access-Control-Allow-Origin echo

# Signin bootstrap (creates a real XUMM payload — rate-limited 5/min/IP):
curl -s -X POST https://letseffinggo.tail82fcc6.ts.net/lfg/api/web/signin
```

## Failure modes

- **Site up, API calls fail with CORS errors** → `WEB_ALLOWED_ORIGINS` missing
  on prod or service not restarted since; check
  `curl -si -X OPTIONS …` above. Origins are exact-match (scheme included).
- **White screen + zero requests in the webapp log** → funnel down; restart
  `tailscaled` (known failure mode, see `lfg-activity-funnel-ingress` memory).
- **`build.letseffinggo.com` 404s from GitHub** → Pages custom domain unset or
  another repo claimed it; `gh api repos/Team-Hamsa/LFG/pages` should show
  `"cname": "build.letseffinggo.com"`.
- **Stale client after a promote** → Pages workflow failed; re-run with
  `gh workflow run pages.yml --ref deploy`.
