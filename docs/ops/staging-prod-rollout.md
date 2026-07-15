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
    cp ~/LFG/.env ~/LFG-staging/.env
    # then apply every override in docs/ops/env.staging.example
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

## 8. Adopt the prod ecosystem file (first drain-restart of prod)
Existing lfg-* processes keep their old ad-hoc definitions until restarted
via the file. At a quiet moment:
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
