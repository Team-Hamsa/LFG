#!/usr/bin/env bash
# Install the `amend` amendment voting CLI.
# Run as: sudo bash install-amend.sh
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "ERROR: run with sudo: sudo bash $0"
  exit 1
fi

echo "=== Installing Python dependencies into .venv ==="
/home/hamsa/.venv/bin/pip install rich prompt_toolkit

echo "=== Installing amend script ==="
cp "$(dirname "$0")/amend" /usr/local/bin/amend
chmod 755 /usr/local/bin/amend

echo "=== Writing sudoers entry ==="
tee /etc/sudoers.d/amend > /dev/null << 'EOF'
hamsa ALL=(ALL) NOPASSWD: /usr/bin/cat /etc/opt/ripple/rippled.cfg
hamsa ALL=(ALL) NOPASSWD: /usr/bin/cp /etc/opt/ripple/rippled.cfg /etc/opt/ripple/rippled.cfg.bak
hamsa ALL=(ALL) NOPASSWD: /usr/bin/tee /etc/opt/ripple/rippled.cfg
hamsa ALL=(ALL) NOPASSWD: /bin/systemctl restart rippled
hamsa ALL=(ALL) NOPASSWD: /bin/journalctl -u rippled -n 20 --no-pager
EOF
chmod 0440 /etc/sudoers.d/amend
visudo -c
echo "=== Done. Run: amend ==="
