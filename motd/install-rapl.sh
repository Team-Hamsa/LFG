#!/usr/bin/env bash
# Install RAPL reader wrapper + update sudoers for CPU power metrics.
# Run as: sudo bash install-rapl.sh
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "ERROR: run this script with sudo: sudo bash $0"
  exit 1
fi

echo "=== Writing /usr/local/bin/rapl-energy-uj ==="
tee /usr/local/bin/rapl-energy-uj > /dev/null << 'EOF'
#!/bin/sh
cat /sys/class/powercap/intel-rapl:0/energy_uj
EOF
chmod 0755 /usr/local/bin/rapl-energy-uj
echo "  done."

echo "=== Updating /etc/sudoers.d/metrics-server ==="
tee /etc/sudoers.d/metrics-server > /dev/null << 'EOF'
hamsa ALL=(ALL) NOPASSWD: /usr/local/bin/rippled server_info
hamsa ALL=(ALL) NOPASSWD: /usr/local/bin/rapl-energy-uj
EOF
chmod 0440 /etc/sudoers.d/metrics-server
visudo -c
echo "  done."

echo "=== Restarting metrics-server ==="
systemctl restart metrics-server
sleep 2
systemctl status metrics-server --no-pager

echo ""
echo "=== Verifying cpu_w in endpoint ==="
echo "(first request sets baseline — cpu_w will be null; wait 5s and try again)"
curl -s http://127.0.0.1:8080/metrics | python3 -m json.tool | grep '"cpu_w"'
sleep 6
curl -s http://127.0.0.1:8080/metrics | python3 -m json.tool | grep '"cpu_w"'
echo ""
echo "=== Done ==="
