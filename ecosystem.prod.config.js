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
    { name: "lfg-deployer", cwd: CWD, script: "scripts/deployer.py", interpreter: PY, args: ["prod"] },
  ],
};
