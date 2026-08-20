// Staging stack (~/LFG-staging, branch: main, testnet, economy enabled).
// pm2 start ecosystem.staging.config.js
// stg-bot / stg-telegram need staging tokens in ~/LFG-staging/.env first —
// until then start the file and pm2 stop stg-bot stg-telegram. (#223)
const CWD = "/home/hamsa/LFG-staging";
const PY = `${CWD}/.venv/bin/python`;

module.exports = {
  apps: [
    { name: "stg-bot", cwd: CWD, script: "main.py", interpreter: PY },
    { name: "stg-activity", cwd: CWD, script: `${PY}`, args: ["-m", "webapp.server"], interpreter: "none" },
    { name: "stg-telegram", cwd: CWD, script: "run_telegram.py", interpreter: PY },
    { name: "stg-index-testnet", cwd: CWD, script: "scripts/onchain_listener.py", interpreter: PY, args: ["--network", "testnet", "listen"] },
    { name: "stg-snapshot", cwd: CWD, script: "scripts/snapshot_balances.py", interpreter: PY, args: ["--network", "testnet"], cron_restart: "10 0 * * *", autorestart: false },
    { name: "stg-economy-reconcile", cwd: CWD, script: "scripts/economy_nightly_reconcile.py", interpreter: PY, args: ["--network", "testnet"], cron_restart: "20 0 * * *", autorestart: false },
    { name: "stg-economy-audit", cwd: CWD, script: "scripts/audit_trait_economy.py", interpreter: PY, args: ["--network", "testnet"], cron_restart: "25 0 * * *", autorestart: false },
    // BRIX daily drip accrual (#48), testnet twin of lfg-brix-accrue — same 00:40 slot.
    // No stg-sourcetag twin: the metrics badge is a mainnet-only, main-committing job.
    { name: "stg-brix-accrue", cwd: CWD, script: "scripts/accrue_brix.py", interpreter: PY, args: ["--network", "testnet"], cron_restart: "40 0 * * *", autorestart: false },
    // Nightly market_listings/buy_offers self-heal sweep (#288) — 03:30 UTC, offset from the 00:10-00:25 crons.
    // pm2 cron_restart fires in HOST-LOCAL time (no timezone option); this box runs Etc/UTC
    // (timedatectl, verified 2026-08-16) — keep the host on UTC or adjust these schedules.
    { name: "stg-market-sweep", cwd: CWD, script: "scripts/backfill_market.py", interpreter: PY, args: ["--network", "testnet", "--report"], cron_restart: "30 3 * * *", autorestart: false },
    { name: "stg-deployer", cwd: CWD, script: "scripts/deployer.py", interpreter: PY, args: ["staging"] },
  ],
};
