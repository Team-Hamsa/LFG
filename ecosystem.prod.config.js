// Prod stack (~/LFG, branch: deploy, mainnet). pm2 start ecosystem.prod.config.js
// NOTE: lfg-index-testnet moved to the staging stack (stg-index-testnet). (#223)
const CWD = "/home/hamsa/LFG";
const PY = `${CWD}/.venv/bin/python`;

module.exports = {
  apps: [
    { name: "lfg-bot", cwd: CWD, script: "main.py", interpreter: PY },
    { name: "lfg-activity", cwd: CWD, script: `${PY}`, args: ["-m", "webapp.server"], interpreter: "none" },
    { name: "lfg-telegram", cwd: CWD, script: "run_telegram.py", interpreter: PY },
    { name: "lfg-index-mainnet", cwd: CWD, script: "scripts/onchain_listener.py", interpreter: PY, args: ["--network", "mainnet", "listen"] },
    { name: "lfg-snapshot", cwd: CWD, script: "scripts/snapshot_balances.py", interpreter: PY, args: ["--network", "mainnet"], cron_restart: "10 0 * * *", autorestart: false },
    { name: "lfg-economy-reconcile", cwd: CWD, script: "scripts/economy_nightly_reconcile.py", interpreter: PY, args: ["--network", "mainnet"], cron_restart: "20 0 * * *", autorestart: false },
    { name: "lfg-economy-audit", cwd: CWD, script: "scripts/audit_trait_economy.py", interpreter: PY, args: ["--network", "mainnet"], cron_restart: "25 0 * * *", autorestart: false },
    // Nightly market_listings/buy_offers self-heal sweep (#288) — 03:30 UTC, offset from the 00:10-00:25 crons.
    { name: "lfg-market-sweep", cwd: CWD, script: "scripts/backfill_market.py", interpreter: PY, args: ["--network", "mainnet", "--report"], cron_restart: "30 3 * * *", autorestart: false },
    { name: "lfg-deployer", cwd: CWD, script: "scripts/deployer.py", interpreter: PY, args: ["prod"] },
    // Public-edge funnel health probe (full TLS handshake through the ts.net funnel).
    // Log-only → reports/funnel_healthcheck.log; catches SSL/blank-site outages the :8176 probe can't see.
    { name: "lfg-funnel-health", cwd: CWD, script: "scripts/funnel_healthcheck.py", interpreter: PY },
  ],
};
