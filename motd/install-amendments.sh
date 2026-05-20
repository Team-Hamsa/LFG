#!/usr/bin/env bash
# Add sudoers entry for rippled feature command and restart metrics-server.
# Run as: sudo bash install-amendments.sh
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "ERROR: run this script with sudo: sudo bash $0"
  exit 1
fi

echo "=== Updating /etc/sudoers.d/metrics-server ==="
tee /etc/sudoers.d/metrics-server > /dev/null << 'EOF'
hamsa ALL=(ALL) NOPASSWD: /usr/local/bin/rippled server_info
hamsa ALL=(ALL) NOPASSWD: /usr/local/bin/rippled feature
hamsa ALL=(ALL) NOPASSWD: /usr/local/bin/rapl-energy-uj
hamsa ALL=(ALL) NOPASSWD: /usr/local/bin/motd-validator-render
EOF
chmod 0440 /etc/sudoers.d/metrics-server
visudo -c
echo "  done."

echo "=== Restarting metrics-server ==="
systemctl restart metrics-server
sleep 2
systemctl status metrics-server --no-pager

echo ""
echo "=== Verifying amendments in endpoint ==="
curl -s http://127.0.0.1:8080/metrics | python3 -m json.tool | grep -A 5 '"amendments"'
echo ""
echo "=== Done ==="
