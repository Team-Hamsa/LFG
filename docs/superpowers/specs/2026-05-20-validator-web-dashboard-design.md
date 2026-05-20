# XRPL Validator Web Dashboard — Design Spec
**Date:** 2026-05-20
**Status:** Approved

## Overview

A live, public-facing web dashboard at `joshuahamsa.github.io/validator.html` showing real-time XRPL validator and server health metrics. Data is served by a read-only JSON endpoint on the home server, exposed publicly via Tailscale Funnel. The frontend polls the endpoint every 5 seconds and renders in the existing terminal aesthetic of the site.

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│  Home Server (VLAN)                                          │
│                                                              │
│  metrics_server.py  ──►  :8080/metrics  (JSON)              │
│         │                                                    │
│  (reads rippled, /proc, sensors, etc.)                       │
│         │                                                    │
│  tailscale funnel --bg 8080                                  │
│         │                                                    │
└─────────┼────────────────────────────────────────────────────┘
          │  HTTPS (Tailscale Funnel)
          ▼
  https://<hostname>.ts.net/metrics
          │
          │  fetch() every 5s
          ▼
  joshuahamsa.github.io/validator.html
  (static HTML/JS — no build step)
```

## Server-Side Components

### `metrics_server.py`
- Location: `/home/hamsa/motd/metrics_server.py` (git-tracked alongside existing MOTD scripts)
- Listens on `localhost:8080`
- Serves one route: `GET /metrics` → JSON response
- Includes `Access-Control-Allow-Origin: *` CORS header on every response
- Data sources:
  - `rippled server_info` — validator state, ledger seq, ledger age, load factor, peer count, build version
  - `/proc/cpuinfo`, `/proc/stat` — CPU usage percentage
  - `/proc/meminfo` — RAM usage percentage
  - `/proc/uptime` — system uptime in seconds
  - `df /` — disk usage percentage
  - `ss -H -t state established '( dport = :22 )'` — active SSH connections
  - Tailscale IP from `/proc/net/if_inet6` or `ip addr` output
  - Last 3 WARN/ERR lines from journalctl (same filter as MOTD)
- New metric categories (power consumption, temperature, etc.) are added as new top-level JSON keys — no breaking changes

### JSON Schema
```json
{
  "timestamp": "2026-05-20T14:32:00Z",
  "validator": {
    "state": "proposing",
    "ledger_seq": 95821034,
    "ledger_age_s": 3,
    "load_factor": 1.0,
    "peers": 42,
    "build_version": "2.3.0"
  },
  "system": {
    "cpu_pct": 12,
    "ram_pct": 58,
    "disk_pct": 34,
    "uptime_s": 1209600
  },
  "network": {
    "tailscale_ip": "100.x.x.x",
    "ssh_connections": 1
  },
  "alerts": [
    "WRN slow_close 4.2s",
    "ERR peer_disconnect 100.x.x.x"
  ]
}
```

### `metrics-server.service`
- Systemd unit file installed to `/etc/systemd/system/metrics-server.service`
- Runs `metrics_server.py` as user `hamsa`
- `Restart=always`, `RestartSec=5`
- `WantedBy=multi-user.target` — starts on boot
- `hamsa` needs passwordless sudo for specific rippled commands: add a sudoers entry for `hamsa ALL=(ALL) NOPASSWD: /usr/local/bin/rippled server_info` (and `rippled peers` if added later). This avoids running the network service as root.

### Tailscale Funnel
- `tailscale funnel --bg 8080` persists the config across reboots
- Exposes `https://<hostname>.ts.net/metrics` publicly
- Requires Tailscale Funnel enabled on the account (available on Personal free plan)
- Only `/metrics` endpoint is served — no other surface area exposed

## Frontend — `validator.html`

### Placement
- New page: `joshuahamsa.github.io/validator.html`
- Linked from `index.html` as a project entry (same style as existing project links)
- No build step — plain HTML + vanilla JS

### Visual Design
- Same terminal chrome as the rest of the site: dark `#0f0f0f` background, `Courier New` monospace, macOS-style traffic light buttons in the terminal header
- MOTD color palette for content: magenta (`#ff00ff` or close) for the page header, cyan for section titles, amber for warnings, red for errors/offline, green for healthy values
- 80-character-wide terminal box, centered, with box-drawing borders (`╔═╗`, `║`, `╚═╝`)
- Two-column layout for Validator/System and Peers/Network rows (mirrors MOTD structure)
- Alerts row below the two-column section
- Footer: last-fetched UTC timestamp, fetch interval indicator

### Fetch Cycle
- `setInterval` fires every 5000ms
- On success: update DOM in place, no page reload, clear any offline banner
- On failure (network error, non-200 response, timeout): show `[OFFLINE]` banner in red, dim last known data, display "last seen: <timestamp>"
- First fetch fires immediately on page load (no 5s wait on initial view)
- Timeout: 4 seconds per fetch (avoids stale in-flight requests piling up)

### Styles
- Additions to existing `styles.css` — new classes prefixed `.validator-` to avoid conflicts
- Progress bars: CSS `::before` width driven by inline `style` set from JS (same visual concept as MOTD bash bars)

## Deployment Steps

### Server
1. Write `metrics_server.py` to `/home/hamsa/motd/`
2. Test locally: `python3 metrics_server.py` → `curl localhost:8080/metrics`
3. Install and start systemd service
4. Run `tailscale funnel --bg 8080`
5. Verify public endpoint: `curl https://<hostname>.ts.net/metrics`
6. Confirm CORS header present: `curl -I https://<hostname>.ts.net/metrics`

### Frontend
1. Add `validator.html` to `joshuahamsa.github.io` repo
2. Update `styles.css` with validator-specific classes
3. Add link in `index.html`
4. Push to `main` — GitHub Pages deploys automatically
5. Manual smoke test: open `validator.html`, confirm data populates, confirm offline banner appears when server is unreachable

## Extensibility

New metrics (power consumption, temperature, etc.) are added by:
1. Adding a new top-level key to the JSON in `metrics_server.py`
2. Adding a corresponding display block in `validator.html`

No schema versioning needed — the frontend reads only the keys it knows about and ignores unknown ones.

## Out of Scope
- Authentication / access control (data is intentionally public and read-only)
- Historical charting / time-series storage
- Alerting / notifications
- Mobile-optimized layout (terminal aesthetic targets desktop)
