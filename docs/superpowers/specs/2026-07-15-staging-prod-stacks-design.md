# Staging/Prod Stack Split with Branch-Driven Deploys (#223)

**Date:** 2026-07-15
**Issue:** [#223](https://github.com/Team-Hamsa/LFG/issues/223)
**Status:** Approved design

## Problem

Everything runs as one pm2 stack on one box with a single `.env`, straddling
networks (`XRPL_NETWORK=mainnet` while the trait economy is testnet-gated).
Testing anything means flipping env vars on the production stack and
restarting live services. There is also no formal deploy trigger: "deploy" is
a manual pull on the box plus a post-merge hook that restarts one process.

## Goal

Replicate a work-style branch-driven flow on this single box:

- **`main` = staging.** Merging a dev branch to `main` auto-deploys the
  staging stack (testnet, economy enabled).
- **`deploy` = prod.** Promoting — fast-forwarding `deploy` to `main` —
  auto-deploys the prod stack (mainnet).

## Branch model

- New long-lived branch **`deploy`**, always an ancestor of `main`.
- **Promotion** is a fast-forward: `git push origin main:deploy`. A small
  wrapper, `scripts/promote.sh`, shows the commit range being promoted
  (`deploy..main` log) and asks for confirmation before pushing.
- The deployer enforces **fast-forward-only** on both branches. A force-push
  or diverged history halts that stack's deploys with a loud log line instead
  of clobbering the checkout. There is no automated rollback: rolling prod
  back is a documented manual path (`git push origin <sha>:deploy
  --force-with-lease`, then `deployer --force-reset` on the box).

## Checkouts

| | prod | staging |
|---|---|---|
| Path | `~/LFG` (existing, unchanged) | `~/LFG-staging` (new clone) |
| Branch | `deploy` | `main` |
| XRPL network | mainnet | testnet, `ECONOMY_ENABLED=1` |
| Activity port | 8176 | 8177 |
| Env file | `~/LFG/.env` (existing) | `~/LFG-staging/.env` |
| venv | `~/LFG/.venv` | `~/LFG-staging/.venv` |

`~/LFG` stays prod so the existing pm2 cwds, mainnet DBs, `.env`, and the
2 GB image archive never move. Consequence (accepted): the `~/LFG` working
copy sits on `deploy`, not `main` — day-to-day dev happens on feature
branches/worktrees as it already does.

## Env split — no code change

`lfg_core/config.py` already calls `load_dotenv()` (checkout-local `.env`)
and all DB paths are network-suffixed (`lfg_nfts_testnet.db`,
`onchain_testnet.db`, `history_testnet.db`). So the "env selector" is simply
that each checkout has its own `.env`. Both stay gitignored. A committed
`docs/ops/env.staging.example` documents the staging deltas:

```
XRPL_NETWORK=testnet
ECONOMY_ENABLED=1
WEBAPP_PORT=8177
DISCORD_BOT_TOKEN=<staging bot app token>
TELEGRAM_BOT_TOKEN=<staging BotFather token>
SERVICE_TOKEN_DISCORD/<TELEGRAM>=<distinct staging values>
DISCORD_GUILD_ID=<test guild>
```

Verification task during implementation: audit for any absolute-path or
port assumptions that would collide between stacks (e.g. `LFG_SERVICE_URL`,
`ECONOMY_RECORDS_DIR`, report dirs, XUMM app keys — staging reuses the same
XUMM app; SourceTag is network-agnostic and unchanged).

## pm2 ecosystem files (committed)

Two files formalize what is currently ad-hoc `pm2 start` state:

- **`ecosystem.prod.config.js`** — the existing `lfg-*` set minus
  `lfg-index-testnet`: `lfg-bot`, `lfg-activity` (:8176), `lfg-telegram`,
  `lfg-index-mainnet`, `lfg-snapshot` (cron, `--no-autorestart`), plus the
  new `lfg-deployer`.
- **`ecosystem.staging.config.js`** — `stg-bot`, `stg-activity` (:8177),
  `stg-telegram`, `stg-index-testnet`, `stg-snapshot`, `stg-deployer`, all
  with cwd `~/LFG-staging`.

`lfg-index-testnet` moves out of prod into the staging stack. `stg-bot` and
`stg-telegram` are defined but left stopped until staging Discord/Telegram
tokens exist (ops step, non-blocking).

## Deployer

**`scripts/deployer.py`** (Python, so it rides the existing pytest/mypy/ruff
gate), run per stack as a pm2 process (`stg-deployer` / `lfg-deployer`) on a
~60 s loop:

1. `git fetch origin`.
2. If the tracked branch (`main` for staging, `deploy` for prod) moved and
   the update is a fast-forward of the local checkout → `git merge
   --ff-only`; otherwise log loudly and skip (halt until a human intervenes;
   `--force-reset` flag for the documented rollback path).
3. If `requirements*.txt` changed in the update, `pip install -r` into the
   stack's venv before restarting.
4. **Drain-aware restart** of the stack's processes, generalizing the
   existing post-merge hook's logic: poll the stack's
   `/api/health` `active_sessions`; restart when it reaches 0.
   - Staging: drain timeout **2 min** (killing a testnet session is cheap),
     then restart anyway.
   - Prod: full **15 min** drain; on timeout or unreachable health endpoint,
     **refuse** the auto-restart and log the manual command — exactly the
     current hook's fail-safe posture.
   - Restart scope mirrors the current hook's path filter: only restart when
     the update touches `webapp/ | lfg_service/ | lfg_core/ | surfaces/ |
     requirements*.txt | *.py` — doc-only deploys advance the checkout
     without a restart.
5. The deployer never restarts itself mid-run; pm2 picks up a changed
   `deployer.py` on its next natural restart (documented: `pm2 restart
   *-deployer` after changing the deployer itself).

The **post-merge hook retires in the prod checkout** (deploys no longer
arrive via manual pulls). `scripts/hooks/post-merge` is removed with it.

## Ingress

Second Tailscale Funnel route: `/lfg-staging` → `127.0.0.1:8177`, alongside
the existing `/lfg` → :8176. The staging Discord Activity / Telegram Mini
App URL points at the staging path.

## Docs

- CLAUDE.md "Running (pm2-managed)" section rewritten around the two stacks:
  process tables for both, the promote flow, deployer behavior, and the
  branch model (`main` auto-deploys staging only; prod requires
  `promote.sh`).
- `docs/ops/env.staging.example` as above.

## Testing

- **Unit (pytest):** deployer decision logic — ff-only detection, path
  filter, requirements-change detection, drain-poll state machine (health
  endpoint mocked), staging-vs-prod timeout/refusal posture. Git interactions
  behind a thin subprocess seam so tests run against throwaway temp repos.
- **Manual E2E after rollout:** push a trivial commit to `main`, watch
  staging self-deploy from the `stg-deployer` log; run `promote.sh`, watch
  prod drain-and-deploy; verify a doc-only commit advances checkouts without
  restarts.

## Rollout order

1. Land the code (deployer, ecosystem files, promote.sh, docs) on `main`.
2. Create the `deploy` branch at current `main`; pin `~/LFG` to it.
3. Clone `~/LFG-staging`, build its venv, write its `.env`.
4. `pm2 start ecosystem.staging.config.js`; delete old `lfg-index-testnet`
   from prod; start `lfg-deployer` in prod via the prod ecosystem file.
5. Add the Funnel staging route.
6. Later (ops, non-blocking): create staging Discord app + Telegram bot,
   fill tokens, start `stg-bot`/`stg-telegram`.

## Out of scope

- Automated rollback tooling (manual documented path only).
- Staging Discord/Telegram app creation (user ops step).
- Any change to the per-kind network seam / `ECONOMY_ENABLED` gate in
  `lfg_service` — the staging stack makes it *testable*; removing it is the
  economy-mainnet go-live work (#185 lineage), not this issue.
