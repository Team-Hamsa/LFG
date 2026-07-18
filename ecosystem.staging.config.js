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
    { name: "stg-deployer", cwd: CWD, script: "scripts/deployer.py", interpreter: PY, args: ["staging"] },
  ],
};
