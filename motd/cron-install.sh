#!/bin/bash
# Run with: sudo bash /home/hamsa/motd/cron-install.sh

set -euo pipefail

# Create cron job
cat > /etc/cron.d/motd-validator <<'CRONEOF'
# Refresh the MOTD validator dashboard cache every ~30 seconds
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin
* * * * * root /usr/local/bin/motd-validator-render
* * * * * root sleep 30 && /usr/local/bin/motd-validator-render
CRONEOF
chmod 644 /etc/cron.d/motd-validator
echo "Created /etc/cron.d/motd-validator"

# Create MOTD entry script
cat > /etc/update-motd.d/10-validator-dashboard <<'MOTDEOF'
#!/bin/sh
if [ -f /var/cache/motd-validator ]; then
    cat /var/cache/motd-validator
else
    /usr/local/bin/motd-validator-render
    cat /var/cache/motd-validator
fi
MOTDEOF
chmod +x /etc/update-motd.d/10-validator-dashboard
echo "Created /etc/update-motd.d/10-validator-dashboard"

# Run initial render
/usr/local/bin/motd-validator-render
echo "Initial render done:"
ls -lh /var/cache/motd-validator

echo ""
echo "Cron status:"
systemctl is-active cron || true
