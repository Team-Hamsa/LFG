# XRPL Validator Login Dashboard — Design Spec
**Date:** 2026-05-19  
**Status:** Approved

---

## Overview

A cyberpunk-styled, ANSI-colored ASCII dashboard displayed at SSH login on an Ubuntu Server running an XRPL rippled validator node. Replaces all default Ubuntu MOTD output.

### Goals
- Show all critical validator and system health data at a glance on login
- Look visually striking in a terminal (cyberpunk aesthetic, ANSI colors)
- Be instant to display (cached render, ≤60s stale)
- Work at 80 columns minimum (phone-friendly)

---

## Architecture: Cached Render (Option A+C)

```
/etc/cron.d/motd-validator
  └── runs every 60s
  └── executes /usr/local/bin/motd-validator-render
        ├── calls: rippled server_info / peers (runs as root, no sudo needed)
        ├── reads: /proc/loadavg, /proc/meminfo, /proc/net/if_inet6, df, ss, journalctl
        ├── reads: /home/hamsa/.ripple/*.json (validator identity)
        └── writes: /var/cache/motd-validator (the pre-rendered ANSI output)

/etc/update-motd.d/  (all existing scripts disabled)
/etc/update-motd.d/10-validator-dashboard
  └── cat /var/cache/motd-validator
```

Login is instant — the MOTD script just cats the cache file. The cron job handles all live data fetching and rendering in the background every 60 seconds.

---

## Layout (80 columns, 6 data sections)

```
╔══════════════════════════════════════════════════════════════════════════════╗
║   [LFG XRPL in figlet block letters, centered, ~59 chars wide, 6 rows]     ║
║         ▸▸  joshuahamsa.com  ·  nHUdwC···2y9F  ·  Manifest #2  ◂◂         ║
╠════════════════════════════════════╦═══════════════════════════════════════╣
║  ◈ VALIDATOR          (left col)   ║  ◈ SYSTEM             (right col)      ║
║    State   ▓▓▓▓▓▓▓▓▓▓  FULL       ║    CPU   [████████░░]  78%             ║
║    Ledger  104,324,243             ║    RAM   [██████░░░░]  6.2 / 16 GB     ║
║    Age     2s                      ║    Disk  [███░░░░░░░]  142 / 500 GB    ║
║    Load    1.0×                    ║    Up    42d 13h 07m                   ║
╠════════════════════════════════════╬═══════════════════════════════════════╣
║  ◈ PEERS  (8 connected)            ║  ◈ NETWORK                             ║
║    12ms  45.77.90.12   full        ║    WAN   172.233.154.249               ║
║    18ms  51.222.10.4   full        ║    SSH   3 active sessions              ║
║    24ms  104.244.73.9  partial     ║    P2P   ✓ port 51235                  ║
╠════════════════════════════════════╩═══════════════════════════════════════╣
║  ◈ ALERTS                                                                    ║
║    [14:23] WARN  Slow ledger close: 4200ms                                  ║
║    [14:15] WARN  Peer disconnect: 104.244.73.9                              ║
║    [14:10] INFO  Ledger 104,324,200 validated in 3.1s                       ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  ◈ IDENTITY  Key: nHUdwC···2y9F  ·  Domain: joshuahamsa.com  ·  Active ✓   ║
╚══════════════════════════════════════════════════════════════════════════════╝
            ⟨ cached 2026-05-19 14:24:01 UTC  ·  23s ago ⟩
```

Column split: left=38 inner chars, right=39 inner chars, divider=╬ (1), two outer ║ (2) = 80 total.

---

## ANSI Color Scheme (Cyberpunk)

| Element | Color |
|---|---|
| Box borders (╔═╗╠╬╣╚╝║) | Bright cyan (`\e[96m`) |
| Header block letters | Magenta (`\e[35m`) |
| Section labels (◈) | Bright yellow (`\e[93m`) |
| Keys/labels | Dim white (`\e[37m`) |
| Good values (FULL, low age, low load) | Bright green (`\e[92m`) |
| Warning values (high load, stale ledger) | Bright yellow (`\e[93m`) |
| Alert WARN lines | Bright yellow (`\e[93m`) |
| Alert ERR/CRIT lines | Bright red (`\e[91m`) |
| Alert INFO lines | Cyan (`\e[36m`) |
| Progress bar fill (▓/█) | Cyan or green depending on level |
| Identity row | Dim white / green for "Active ✓" |
| Cache timestamp | Dim (`\e[2m`) |

---

## Data Sources

| Section | Source |
|---|---|
| Server state, ledger seq/age, load, peers | `rippled server_info` (JSON via `jq`) — cron runs as root |
| Peer list with latency | `rippled peers` (JSON via `jq`) — cron runs as root |
| CPU usage | `/proc/loadavg` + `/proc/cpuinfo` |
| RAM | `/proc/meminfo` |
| Disk | `df -h /` |
| Uptime | `/proc/uptime` |
| WAN IP | `hostname -I` or `/proc/net/if_inet6` |
| Active SSH sessions | `ss -tnp` filtered for port 22 |
| P2P port open | `ss -tlnp` check for port 51235 |
| Recent alerts | `journalctl -u rippled -n 20 --no-pager -o cat` filtered for WARN\|ERROR |
| Validator identity | `/home/hamsa/.ripple/*.json` (public_key, domain, token_sequence, revoked) |

---

## Files Created / Modified

| Path | Action | Purpose |
|---|---|---|
| `/usr/local/bin/motd-validator-render` | Create | Main render script (bash) |
| `/var/cache/motd-validator` | Create (by cron) | Pre-rendered ANSI dashboard |
| `/etc/cron.d/motd-validator` | Create | Runs render every 60s as root |
| `/etc/update-motd.d/10-validator-dashboard` | Create | MOTD script: cats the cache |
| `/etc/update-motd.d/00-header` | Disable (chmod -x) | Remove default Ubuntu header |
| `/etc/update-motd.d/10-help-text` | Disable | Remove help text |
| `/etc/update-motd.d/50-landscape-sysinfo` | Disable | Remove landscape sysinfo |
| `/etc/update-motd.d/50-motd-news` | Disable | Remove news |
| `/etc/update-motd.d/85-fwupd` | Disable | Remove firmware update notice |
| `/etc/update-motd.d/90-updates-available` | Disable | Remove update count |
| `/etc/update-motd.d/91-contract-ua-esm-status` | Disable | Remove ESM notice |
| `/etc/update-motd.d/91-release-upgrade` | Disable | Remove release upgrade prompt |
| `/etc/update-motd.d/92-unattended-upgrades` | Disable | Remove unattended upgrades |
| `/etc/update-motd.d/95-hwe-eol` | Disable | Remove HWE notice |
| `/etc/update-motd.d/97-overlayroot` | Disable | Remove overlayroot notice |
| `/etc/update-motd.d/98-fsck-at-reboot` | Disable | Remove fsck notice |
| `/etc/update-motd.d/98-reboot-required` | Disable | Remove reboot required |

---

## Render Script Logic

```
motd-validator-render (bash):
  1. Load validator identity from ~/.ripple/*.json
  2. Call rippled server_info → parse state, ledger_seq, ledger_age, load_factor, peers count
  3. Call rippled peers → parse top 3 peers by latency (address, latency, complete_ledgers)
  4. Read /proc/meminfo → MemTotal, MemAvailable → used, percent
  5. Read /proc/loadavg → 1-min load average
  6. Read /proc/cpuinfo → core count → compute load%
  7. Run df -h / → disk used/total/percent
  8. Read /proc/uptime → format as Xd Xh Xm
  9. Run ss → SSH session count, P2P port check, WAN IP
  10. Run journalctl → last 3 alert lines (WARN/ERROR/INFO)
  11. Render full dashboard with ANSI codes into a temp file
  12. Atomically move temp file to /var/cache/motd-validator
```

Atomic write (write to `.tmp`, then `mv`) prevents a login from reading a half-rendered file.

---

## Progress Bar Helper

10-character bar using `█` (filled) and `░` (empty). Thresholds:
- 0–60%: green fill
- 61–80%: yellow fill  
- 81–100%: red fill

---

## Constraints

- **80-column safe:** All lines padded/truncated to exactly 80 chars.
- **No external deps beyond `jq`** (already installed with rippled).
- **Runs as root** (cron) to call `rippled` CLI without sudo prompts.
- **Graceful degradation:** If `rippled` is unreachable, show "OFFLINE" in red rather than crashing.
- **Atomic cache write:** `mv` from `.tmp` so logins never see partial renders.
