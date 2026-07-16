# Rollout: staging/prod stack split (#223)

One-time steps, in order, AFTER the code lands on `main`. Steps 1–7 are safe
to run while prod serves traffic; only step 8 restarts prod processes.

## 1. Create the deploy branch (prod pins here)
    git push origin main:deploy

## 2. Pin ~/LFG to deploy
    cd ~/LFG && git fetch origin && git checkout -B deploy origin/deploy
(Identical tree to main at this moment — nothing running changes.)

## 3. Remove the retired post-merge hook from the live checkout
    rm -f ~/LFG/.git/hooks/post-merge

## 4. Build the staging checkout
    git clone git@github.com:Team-Hamsa/LFG.git ~/LFG-staging
    cd ~/LFG-staging && ./setup.sh
    cp docs/ops/env.staging.example ~/LFG-staging/.env
Start from the staging example, NOT `~/LFG/.env` — copying the prod `.env`
wholesale drags every prod secret into a lower-trust checkout, and if any
staging override is missed, staging quietly points at prod (prod DB rows,
prod BunnyCDN folder, prod Discord guild). Instead, copy over **only** the
credentials staging legitimately reuses:
    - XUMM_API_KEY / XUMM_API_SECRET (shared XUMM app)
    - BUNNY_CDN_ACCESS_KEY / BUNNY_CDN_STORAGE_ZONE / BUNNY_CDN_BASE_URL
    - SEED (already the testnet seed — safe to share; not a prod secret)
    - TOKEN_ISSUER_ADDRESS / TOKEN_CURRENCY_HEX
Then set every remaining value from `docs/ops/env.staging.example` (ports,
DB paths, taxons, `BUNNY_CDN_FOLDER`, etc.) — do NOT leave any commented
placeholder from the example unfilled.
**Never copy `DISCORD_BOT_TOKEN`, `TELEGRAM_BOT_TOKEN`, or `SERVICE_TOKEN_*`
from prod** — those identify prod's live bot/service credentials; staging
gets its own once the staging Discord app / BotFather bot exist (see
"Later" below), and until then `stg-bot`/`stg-telegram` stay stopped.
    $EDITOR ~/LFG-staging/.env

## 5. Move the testnet listener + start staging
    pm2 delete lfg-index-testnet
    pm2 start ~/LFG-staging/ecosystem.staging.config.js
    pm2 stop stg-bot stg-telegram        # until staging tokens exist
    pm2 save

## 6. Staging ingress (second Funnel route)
    tailscale serve --bg --set-path /lfg-staging http://127.0.0.1:8177
    tailscale funnel status   # verify /lfg (8176) and /lfg-staging (8177)

## 7. Start the prod deployer (no restarts yet — deploy == main)
    pm2 start ~/LFG/ecosystem.prod.config.js --only lfg-deployer
    pm2 save

## 8. Adopt the prod ecosystem file (hard cutover — prod restarts here)
Existing lfg-* processes keep their old ad-hoc definitions until restarted
via the file. This step actually bounces live prod processes.

A one-time `curl -s localhost:8176/api/health` showing `active_sessions: 0`
is NOT a drain barrier — a new session can start in the gap between that
check and the actual `pm2 delete`/`pm2 start` below. Instead:
    pm2 stop lfg-activity     # stops the webapp from accepting new sessions
    curl -s localhost:8176/api/health   # re-check active_sessions == 0
Repeat the re-check (or just pick an off-hours window) until it's actually
quiet, THEN:
    pm2 delete lfg-bot lfg-activity lfg-telegram lfg-index-mainnet lfg-snapshot
    pm2 start ~/LFG/ecosystem.prod.config.js
    pm2 save

## 9. Verify end-to-end
- Push a trivial commit to main → `pm2 logs stg-deployer` shows the
  fast-forward; a doc-only commit advances without restarts.
- `scripts/promote.sh` → `pm2 logs lfg-deployer` shows drain + restart.
- `curl -s localhost:8177/api/health` and `:8176/api/health` both OK.

## Later (non-blocking ops)
- Create the staging Discord app (install to a test guild) and BotFather
  bot; fill tokens in ~/LFG-staging/.env; `pm2 restart stg-bot stg-telegram`.
