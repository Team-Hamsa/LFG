#!/usr/bin/env bash
# Install metrics-server systemd service + sudoers entry for XRPL validator dashboard.
# Run as: sudo bash install-service.sh
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "ERROR: run this script with sudo: sudo bash $0"
  exit 1
fi

echo "=== Writing /etc/systemd/system/metrics-server.service ==="
tee /etc/systemd/system/metrics-server.service > /dev/null << 'EOF'
[Unit]
Description=XRPL Validator Metrics HTTP Server
After=network.target rippled.service

[Service]
Type=simple
User=hamsa
ExecStart=/usr/bin/python3 /home/hamsa/motd/metrics_server.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
echo "  done."

echo "=== Writing /etc/sudoers.d/metrics-server ==="
tee /etc/sudoers.d/metrics-server > /dev/null << 'EOF'
hamsa ALL=(ALL) NOPASSWD: /usr/local/bin/rippled server_info
EOF
chmod 0440 /etc/sudoers.d/metrics-server
visudo -c
echo "  done."

echo "=== Enabling and starting metrics-server ==="
systemctl daemon-reload
systemctl enable metrics-server
systemctl start metrics-server
sleep 2
systemctl status metrics-server

echo ""
echo "=== Verifying endpoint ==="
curl -s http://127.0.0.1:8080/metrics | python3 -m json.tool | grep -E '"state"|"ledger_seq"'
echo ""
echo "=== Done. metrics-server is live at http://127.0.0.1:8080/metrics ==="
