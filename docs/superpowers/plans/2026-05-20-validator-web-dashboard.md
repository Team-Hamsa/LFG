# Validator Web Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose a live read-only JSON metrics endpoint on the validator server via Tailscale Funnel, and render it as a cyberpunk terminal dashboard at `joshuahamsa.github.io/validator.html`.

**Architecture:** A Python HTTP server (`metrics_server.py`) collects validator + system stats on each request and serves them as JSON on `localhost:8080`. Tailscale Funnel exposes that port at a public HTTPS URL. A static GitHub Pages page fetches the endpoint every 5 seconds and renders a box-drawing terminal dashboard in the existing site aesthetic.

**Tech Stack:** Python 3.10, `http.server` (stdlib), `unittest` (stdlib), systemd, Tailscale Funnel, vanilla HTML/JS/CSS (no build step).

---

## File Map

**Server (git-tracked in `/home/hamsa/motd/`):**
- Create: `/home/hamsa/motd/metrics_server.py` — HTTP server + all data collection functions
- Create: `/home/hamsa/motd/test_metrics_server.py` — full test suite
- Create: `/etc/systemd/system/metrics-server.service` — systemd unit (not git-tracked)

**GitHub Pages repo (clone to `~/joshuahamsa.github.io/`):**
- Create: `~/joshuahamsa.github.io/validator.html` — dashboard page (terminal box + JS fetch)
- Modify: `~/joshuahamsa.github.io/styles.css` — add `.val-*` color/layout classes
- Modify: `~/joshuahamsa.github.io/index.html` — add validator link in projects section

---

## Task 1: Write all failing tests

**Files:**
- Create: `/home/hamsa/motd/test_metrics_server.py`

- [ ] **Step 1: Create the test file**

```python
#!/usr/bin/env python3
"""Tests for metrics_server.py"""
import http.server
import json
import os
import sys
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import mock_open, patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import metrics_server


MOCK_METRICS = {
    "timestamp": "2026-05-20T00:00:00Z",
    "validator": {
        "state": "proposing", "ledger_seq": 95821034, "ledger_age_s": 3,
        "load_factor": 1.0, "peers": 42, "peer_disconnects": 7,
        "rippled_uptime_s": 3600, "build_version": "2.3.0",
        "amendment_blocked": False,
    },
    "identity": {
        "public_key": "ED1234567890ABCDEF",
        "public_key_short": "ED1234...CDEF",
        "domain": "joshuahamsa.com",
        "manifest_seq": "3",
        "revoked": False,
    },
    "system": {
        "cpu_pct": 12, "ram_pct": 50, "ram_used_gb": 4.0,
        "ram_total_gb": 8, "disk_pct": 34, "uptime_s": 86400,
    },
    "network": {
        "lan_ip": "192.168.1.100", "tailscale_ip": "100.64.1.2",
        "ssh_sessions": 1, "p2p_open": True,
    },
    "alerts": ["WRN slow_close 4.2s"],
}


class TestHTTPIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.patcher = patch("metrics_server.collect_metrics", return_value=MOCK_METRICS)
        cls.patcher.start()
        cls.server = http.server.HTTPServer(
            ("127.0.0.1", 0), metrics_server.MetricsHandler
        )
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever)
        cls.thread.daemon = True
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.patcher.stop()

    def test_metrics_returns_200(self):
        with urllib.request.urlopen(
            f"http://127.0.0.1:{self.port}/metrics"
        ) as resp:
            self.assertEqual(resp.status, 200)

    def test_metrics_cors_header(self):
        with urllib.request.urlopen(
            f"http://127.0.0.1:{self.port}/metrics"
        ) as resp:
            self.assertEqual(resp.headers["Access-Control-Allow-Origin"], "*")

    def test_metrics_content_type(self):
        with urllib.request.urlopen(
            f"http://127.0.0.1:{self.port}/metrics"
        ) as resp:
            self.assertIn("application/json", resp.headers["Content-Type"])

    def test_metrics_valid_json_top_level_keys(self):
        with urllib.request.urlopen(
            f"http://127.0.0.1:{self.port}/metrics"
        ) as resp:
            data = json.loads(resp.read())
        for key in ("timestamp", "validator", "identity", "system", "network", "alerts"):
            self.assertIn(key, data, f"missing top-level key: {key}")

    def test_unknown_path_returns_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(f"http://127.0.0.1:{self.port}/unknown")
        self.assertEqual(ctx.exception.code, 404)


class TestGetValidatorInfo(unittest.TestCase):
    RIPPLED_JSON = json.dumps({
        "result": {
            "info": {
                "server_state": "proposing",
                "validated_ledger": {"seq": 95821034, "age": 3},
                "load_factor": 1.0,
                "peers": 42,
                "peer_disconnects": 7,
                "uptime": 3600,
                "build_version": "2.3.0",
                "amendment_blocked": False,
            }
        }
    })

    @patch("metrics_server.subprocess.check_output")
    def test_parses_server_info(self, mock_sub):
        mock_sub.return_value = self.RIPPLED_JSON
        result = metrics_server.get_validator_info()
        self.assertEqual(result["state"], "proposing")
        self.assertEqual(result["ledger_seq"], 95821034)
        self.assertEqual(result["ledger_age_s"], 3)
        self.assertEqual(result["load_factor"], 1.0)
        self.assertEqual(result["peers"], 42)
        self.assertEqual(result["peer_disconnects"], 7)
        self.assertEqual(result["rippled_uptime_s"], 3600)
        self.assertEqual(result["build_version"], "2.3.0")
        self.assertFalse(result["amendment_blocked"])

    @patch("metrics_server.subprocess.check_output")
    def test_returns_error_state_on_failure(self, mock_sub):
        mock_sub.side_effect = Exception("rippled unavailable")
        result = metrics_server.get_validator_info()
        self.assertEqual(result["state"], "error")
        self.assertEqual(result["peers"], 0)
        self.assertIn("build_version", result)


class TestGetIdentity(unittest.TestCase):
    VALIDATOR_DATA = json.dumps({
        "public_key": "ED1234567890ABCDEF",
        "domain": "joshuahamsa.com",
        "token_sequence": "3",
        "revoked": False,
    })

    @patch("builtins.open", new_callable=mock_open, read_data=VALIDATOR_DATA)
    def test_parses_validator_json(self, _):
        result = metrics_server.get_identity()
        self.assertEqual(result["domain"], "joshuahamsa.com")
        self.assertEqual(result["manifest_seq"], "3")
        self.assertFalse(result["revoked"])
        self.assertTrue(result["public_key"].startswith("ED"))
        self.assertIn("...", result["public_key_short"])

    @patch("builtins.open", side_effect=FileNotFoundError)
    def test_returns_defaults_on_missing_file(self, _):
        result = metrics_server.get_identity()
        self.assertEqual(result["public_key"], "unknown")
        self.assertEqual(result["domain"], "joshuahamsa.com")
        self.assertFalse(result["revoked"])


class TestGetSystemInfo(unittest.TestCase):
    @patch("metrics_server.subprocess.check_output", return_value="Use%\n45%\n")
    def test_returns_expected_fields_and_ranges(self, _):
        # Reads real /proc files — validates structure and value ranges.
        result = metrics_server.get_system_info()
        for key in ("cpu_pct", "ram_pct", "ram_used_gb", "ram_total_gb", "disk_pct", "uptime_s"):
            self.assertIn(key, result, f"missing key: {key}")
        self.assertGreaterEqual(result["cpu_pct"], 0)
        self.assertLessEqual(result["cpu_pct"], 100)
        self.assertGreaterEqual(result["ram_pct"], 0)
        self.assertLessEqual(result["ram_pct"], 100)
        self.assertEqual(result["disk_pct"], 45)
        self.assertGreater(result["uptime_s"], 0)


class TestGetNetworkInfo(unittest.TestCase):
    @patch("metrics_server.subprocess.check_output")
    def test_parses_tailscale_lan_ssh_p2p(self, mock_sub):
        def side(cmd, **kw):
            if "route" in cmd:
                return "8.8.8.8 via 192.168.1.1 dev eth0 src 192.168.1.100 uid 0\n"
            if cmd == ["ip", "addr", "show"]:
                return "3: tailscale0:\n    inet 100.64.1.2/32 scope global tailscale0\n"
            if cmd[0] == "ss" and "-l" not in cmd:
                return "ESTAB  0  0  192.168.1.100:22  192.168.1.50:54321\n"
            if "-tnlp" in cmd:
                return "LISTEN  0  128  0.0.0.0:51235  0.0.0.0:*\n"
            return ""
        mock_sub.side_effect = side
        result = metrics_server.get_network_info()
        self.assertEqual(result["lan_ip"], "192.168.1.100")
        self.assertEqual(result["tailscale_ip"], "100.64.1.2")
        self.assertEqual(result["ssh_sessions"], 1)
        self.assertTrue(result["p2p_open"])

    @patch("metrics_server.subprocess.check_output", side_effect=Exception("no network"))
    def test_returns_safe_defaults_on_failure(self, _):
        result = metrics_server.get_network_info()
        self.assertEqual(result["tailscale_ip"], "unknown")
        self.assertEqual(result["lan_ip"], "unknown")
        self.assertEqual(result["ssh_sessions"], 0)
        self.assertFalse(result["p2p_open"])


class TestGetAlerts(unittest.TestCase):
    JOURNAL_OUTPUT = "\n".join([
        "2026-05-20T10:00:00+0000 server rippled[123]: normal log line",
        "2026-05-20T10:00:01+0000 server rippled[123]: WRN slow close 4.2s",
        "2026-05-20T10:00:02+0000 server rippled[123]: another normal line",
        "2026-05-20T10:00:03+0000 server rippled[123]: ERR peer disconnect",
        "2026-05-20T10:00:04+0000 server rippled[123]: normal again",
        "2026-05-20T10:00:05+0000 server rippled[123]: WRN timeout 4s",
        "2026-05-20T10:00:06+0000 server rippled[123]: WRN extra alert",
    ])

    @patch("metrics_server.subprocess.check_output")
    def test_returns_last_3_matching_lines(self, mock_sub):
        mock_sub.return_value = self.JOURNAL_OUTPUT
        result = metrics_server.get_alerts()
        self.assertEqual(len(result), 3)
        self.assertTrue(any("ERR peer disconnect" in a for a in result))
        self.assertTrue(any("WRN timeout 4s" in a for a in result))
        self.assertTrue(any("WRN extra alert" in a for a in result))
        # First WRN must be excluded (it's not in the last 3)
        self.assertFalse(any("WRN slow close" in a for a in result))

    @patch("metrics_server.subprocess.check_output")
    def test_returns_empty_when_no_matching_lines(self, mock_sub):
        mock_sub.return_value = "normal line\nanother normal line\n"
        result = metrics_server.get_alerts()
        self.assertEqual(result, [])

    @patch("metrics_server.subprocess.check_output", side_effect=Exception("journalctl error"))
    def test_returns_empty_on_exception(self, _):
        result = metrics_server.get_alerts()
        self.assertEqual(result, [])

    @patch("metrics_server.subprocess.check_output")
    def test_strips_journalctl_prefix(self, mock_sub):
        mock_sub.return_value = (
            "2026-05-20T10:00:00+0000 server rippled[123]: WRN slow close 4.2s\n"
        )
        result = metrics_server.get_alerts()
        self.assertEqual(len(result), 1)
        self.assertFalse(result[0].startswith("2026"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
```

- [ ] **Step 2: Run tests to confirm they fail (metrics_server.py doesn't exist yet)**

```bash
cd /home/hamsa/motd
python3 -m unittest test_metrics_server 2>&1 | head -20
```

Expected: `ModuleNotFoundError: No module named 'metrics_server'`

---

## Task 2: Implement metrics_server.py skeleton — make HTTP tests pass

**Files:**
- Create: `/home/hamsa/motd/metrics_server.py`

- [ ] **Step 1: Create the server with a stub `collect_metrics()`**

```python
#!/usr/bin/env python3
"""Read-only JSON metrics endpoint for XRPL validator dashboard.

GET /metrics  ->  200 application/json + CORS header
All other paths  ->  404
"""
import datetime
import http.server
import json
import os
import re
import subprocess
from pathlib import Path

RIPPLED = "/usr/local/bin/rippled"
_json_files = list(Path("/home/hamsa/.ripple").glob("*.json"))
VALIDATOR_JSON = str(_json_files[0]) if _json_files else ""
PORT = 8080


def get_validator_info():
    return {
        "state": "unknown",
        "ledger_seq": 0,
        "ledger_age_s": 0,
        "load_factor": 0.0,
        "peers": 0,
        "peer_disconnects": 0,
        "rippled_uptime_s": 0,
        "build_version": "unknown",
        "amendment_blocked": False,
    }


def get_identity():
    return {
        "public_key": "unknown",
        "public_key_short": "unkn...own",
        "domain": "joshuahamsa.com",
        "manifest_seq": "?",
        "revoked": False,
    }


def get_system_info():
    return {
        "cpu_pct": 0,
        "ram_pct": 0,
        "ram_used_gb": 0.0,
        "ram_total_gb": 0,
        "disk_pct": 0,
        "uptime_s": 0,
    }


def get_network_info():
    return {
        "lan_ip": "unknown",
        "tailscale_ip": "unknown",
        "ssh_sessions": 0,
        "p2p_open": False,
    }


def get_alerts():
    return []


def collect_metrics():
    return {
        "timestamp": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "validator": get_validator_info(),
        "identity": get_identity(),
        "system": get_system_info(),
        "network": get_network_info(),
        "alerts": get_alerts(),
    }


class MetricsHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/metrics":
            self.send_response(404)
            self.end_headers()
            return
        data = collect_metrics()
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass  # suppress per-request console logging


if __name__ == "__main__":
    server = http.server.HTTPServer(("127.0.0.1", PORT), MetricsHandler)
    print(f"metrics-server listening on 127.0.0.1:{PORT}", flush=True)
    server.serve_forever()
```

- [ ] **Step 2: Run HTTP integration tests — they should now pass**

```bash
cd /home/hamsa/motd
python3 -m unittest test_metrics_server.TestHTTPIntegration -v
```

Expected output:
```
test_metrics_content_type ... ok
test_metrics_cors_header ... ok
test_metrics_returns_200 ... ok
test_metrics_valid_json_top_level_keys ... ok
test_unknown_path_returns_404 ... ok
```

- [ ] **Step 3: Commit the skeleton**

```bash
cd /home/hamsa/motd
git add metrics_server.py test_metrics_server.py
git commit -m "feat: add metrics_server.py skeleton with HTTP interface and tests"
```

---

## Task 3: Implement `get_validator_info()` and `get_identity()`

**Files:**
- Modify: `/home/hamsa/motd/metrics_server.py` — replace stub functions

- [ ] **Step 1: Run the failing validator tests to confirm baseline**

```bash
cd /home/hamsa/motd
python3 -m unittest test_metrics_server.TestGetValidatorInfo test_metrics_server.TestGetIdentity -v
```

Expected: `TestGetValidatorInfo.test_parses_server_info FAIL`, `TestGetIdentity.test_parses_validator_json FAIL`

- [ ] **Step 2: Replace `get_validator_info()` in `metrics_server.py`**

```python
def get_validator_info():
    try:
        raw = subprocess.check_output(
            ["sudo", RIPPLED, "server_info"],
            timeout=5, text=True, stderr=subprocess.DEVNULL
        )
        info = json.loads(raw)["result"]["info"]
        vl = info.get("validated_ledger", {})
        return {
            "state": info.get("server_state", "unknown"),
            "ledger_seq": vl.get("seq", 0),
            "ledger_age_s": vl.get("age", 0),
            "load_factor": info.get("load_factor", 1.0),
            "peers": info.get("peers", 0),
            "peer_disconnects": info.get("peer_disconnects", 0),
            "rippled_uptime_s": info.get("uptime", 0),
            "build_version": info.get("build_version", "unknown"),
            "amendment_blocked": info.get("amendment_blocked", False),
        }
    except Exception:
        return {
            "state": "error",
            "ledger_seq": 0,
            "ledger_age_s": 0,
            "load_factor": 0.0,
            "peers": 0,
            "peer_disconnects": 0,
            "rippled_uptime_s": 0,
            "build_version": "unknown",
            "amendment_blocked": False,
        }
```

- [ ] **Step 3: Replace `get_identity()` in `metrics_server.py`**

```python
def get_identity():
    try:
        with open(VALIDATOR_JSON) as f:
            data = json.load(f)
        pubkey = data.get("public_key", "unknown")
        short = (pubkey[:6] + "..." + pubkey[-4:]) if len(pubkey) > 10 else pubkey
        return {
            "public_key": pubkey,
            "public_key_short": short,
            "domain": data.get("domain", "joshuahamsa.com"),
            "manifest_seq": str(data.get("token_sequence", "?")),
            "revoked": data.get("revoked", False),
        }
    except Exception:
        return {
            "public_key": "unknown",
            "public_key_short": "unkn...own",
            "domain": "joshuahamsa.com",
            "manifest_seq": "?",
            "revoked": False,
        }
```

- [ ] **Step 4: Run validator + identity tests — they should pass**

```bash
cd /home/hamsa/motd
python3 -m unittest test_metrics_server.TestGetValidatorInfo test_metrics_server.TestGetIdentity -v
```

Expected: all 4 tests `ok`

- [ ] **Step 5: Commit**

```bash
cd /home/hamsa/motd
git add metrics_server.py
git commit -m "feat: implement get_validator_info and get_identity"
```

---

## Task 4: Implement `get_system_info()`, `get_network_info()`, `get_alerts()`

**Files:**
- Modify: `/home/hamsa/motd/metrics_server.py` — replace remaining stub functions

- [ ] **Step 1: Replace `get_system_info()` in `metrics_server.py`**

```python
def get_system_info():
    # CPU: 1-min load average / core count (same method as MOTD bash script)
    with open("/proc/loadavg") as f:
        load_avg = float(f.read().split()[0])
    cores = os.cpu_count() or 1
    cpu_pct = min(100, round(load_avg / cores * 100))

    # RAM
    mem = {}
    with open("/proc/meminfo") as f:
        for line in f:
            parts = line.split()
            if len(parts) >= 2:
                mem[parts[0].rstrip(":")] = int(parts[1])
    total_kb = mem.get("MemTotal", 1)
    avail_kb = mem.get("MemAvailable", 0)
    used_kb = total_kb - avail_kb
    ram_pct = round(used_kb * 100 / total_kb)
    ram_used_gb = round(used_kb / 1_048_576, 1)
    ram_total_gb = round(total_kb / 1_048_576)

    # Disk
    out = subprocess.check_output(["df", "/", "--output=pcent"], text=True)
    disk_pct = int(out.strip().split("\n")[1].strip().rstrip("%"))

    # Uptime
    with open("/proc/uptime") as f:
        uptime_s = int(float(f.read().split()[0]))

    return {
        "cpu_pct": cpu_pct,
        "ram_pct": ram_pct,
        "ram_used_gb": ram_used_gb,
        "ram_total_gb": ram_total_gb,
        "disk_pct": disk_pct,
        "uptime_s": uptime_s,
    }
```

- [ ] **Step 2: Replace `get_network_info()` in `metrics_server.py`**

```python
def get_network_info():
    lan_ip = "unknown"
    try:
        out = subprocess.check_output(
            ["ip", "route", "get", "8.8.8.8"],
            text=True, stderr=subprocess.DEVNULL
        )
        m = re.search(r'src (\S+)', out)
        if m:
            lan_ip = m.group(1)
    except Exception:
        pass

    tailscale_ip = "unknown"
    try:
        out = subprocess.check_output(
            ["ip", "addr", "show"], text=True, stderr=subprocess.DEVNULL
        )
        m = re.search(r'inet (100\.\d+\.\d+\.\d+)', out)
        if m:
            tailscale_ip = m.group(1)
    except Exception:
        pass

    ssh_sessions = 0
    try:
        out = subprocess.check_output(
            ["ss", "-tnp"], text=True, stderr=subprocess.DEVNULL
        )
        ssh_sessions = sum(
            1 for line in out.splitlines()
            if line.startswith("ESTAB") and
            re.search(r':22\b', line.split()[3] if len(line.split()) > 3 else "")
        )
    except Exception:
        pass

    p2p_open = False
    try:
        out = subprocess.check_output(
            ["ss", "-tnlp"], text=True, stderr=subprocess.DEVNULL
        )
        p2p_open = ":51235" in out
    except Exception:
        pass

    return {
        "lan_ip": lan_ip,
        "tailscale_ip": tailscale_ip,
        "ssh_sessions": ssh_sessions,
        "p2p_open": p2p_open,
    }
```

- [ ] **Step 3: Replace `get_alerts()` in `metrics_server.py`**

```python
def get_alerts():
    try:
        out = subprocess.check_output(
            ["journalctl", "-u", "rippled", "-n", "60", "--no-pager", "-o", "short-iso"],
            text=True, timeout=5, stderr=subprocess.DEVNULL
        )
        alerts = []
        for line in out.splitlines():
            if re.search(r'(?i)\b(warn|error|wrn|err)\b', line):
                cleaned = re.sub(r'^\S+ \S+ rippled\[\d+\]: ', '', line)
                alerts.append(cleaned[:120])
        return alerts[-3:] if alerts else []
    except Exception:
        return []
```

- [ ] **Step 4: Run the full test suite**

```bash
cd /home/hamsa/motd
python3 -m unittest test_metrics_server -v
```

Expected: all tests `ok`. Final line: `Ran N tests in X.Xs — OK`

- [ ] **Step 5: Smoke test the live server**

```bash
# Terminal 1: start the server
python3 /home/hamsa/motd/metrics_server.py &

# Terminal 2: hit the endpoint
curl -s http://127.0.0.1:8080/metrics | python3 -m json.tool | head -40
curl -sI http://127.0.0.1:8080/metrics | grep -i "access-control"
```

Expected: valid JSON with non-zero `ledger_seq` and `Access-Control-Allow-Origin: *`

```bash
# Kill the test server
kill %1
```

- [ ] **Step 6: Commit**

```bash
cd /home/hamsa/motd
git add metrics_server.py
git commit -m "feat: implement system, network, and alerts data collection"
```

---

## Task 5: Install systemd service + sudoers entry

**Files:**
- Create: `/etc/systemd/system/metrics-server.service`

- [ ] **Step 1: Write the service file**

```bash
sudo tee /etc/systemd/system/metrics-server.service > /dev/null << 'EOF'
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
```

- [ ] **Step 2: Add passwordless sudo entry for rippled server_info**

```bash
sudo tee /etc/sudoers.d/metrics-server > /dev/null << 'EOF'
hamsa ALL=(ALL) NOPASSWD: /usr/local/bin/rippled server_info
EOF
sudo chmod 0440 /etc/sudoers.d/metrics-server
```

Verify the sudoers syntax is valid:

```bash
sudo visudo -c
```

Expected: `parsed OK`

- [ ] **Step 3: Enable and start the service**

```bash
sudo systemctl daemon-reload
sudo systemctl enable metrics-server
sudo systemctl start metrics-server
sudo systemctl status metrics-server
```

Expected: `Active: active (running)`

- [ ] **Step 4: Verify the endpoint responds**

```bash
curl -s http://127.0.0.1:8080/metrics | python3 -m json.tool | grep -E '"state"|"ledger_seq"'
```

Expected: `"state": "proposing"` (or current state), non-zero ledger_seq

---

## Task 6: Configure Tailscale Funnel + verify public endpoint

- [ ] **Step 1: Find your Tailscale machine hostname**

```bash
tailscale status | head -5
```

Note the hostname shown for this machine (e.g. `validator`). The public URL will be `https://<hostname>.ts.net/metrics`.

- [ ] **Step 2: Enable Tailscale Funnel for port 8080**

```bash
sudo tailscale funnel --bg 8080
```

Expected output includes: `Available on the internet: https://<hostname>.ts.net`

If you see `Funnel not available`, Funnel must be enabled at tailscale.com → Settings → Funnel → Enable.

- [ ] **Step 3: Verify the public endpoint**

Wait 10–15 seconds for DNS to propagate, then:

```bash
curl -s https://<YOUR_HOSTNAME>.ts.net/metrics | python3 -m json.tool | grep '"state"'
curl -sI https://<YOUR_HOSTNAME>.ts.net/metrics | grep -i "access-control"
```

Expected: `"state": "proposing"` and `access-control-allow-origin: *`

- [ ] **Step 4: Confirm Funnel persists across reboots**

```bash
sudo tailscale funnel status
```

Expected: shows port 8080 as funneled with background mode enabled.

---

## Task 7: Clone the GitHub Pages repo

- [ ] **Step 1: Clone the repo**

```bash
cd ~
git clone https://github.com/joshuahamsa/joshuahamsa.github.io.git
cd joshuahamsa.github.io
```

- [ ] **Step 2: Confirm the expected files are present**

```bash
ls index.html styles.css script.js
```

Expected: all three files present

---

## Task 8: Write `validator.html`

**Files:**
- Create: `~/joshuahamsa.github.io/validator.html`

Replace `YOUR_TAILSCALE_HOSTNAME` with the hostname found in Task 6 Step 1.

- [ ] **Step 1: Create the file**

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>XRPL Validator — Joshua Hamsa</title>
  <link rel="stylesheet" href="styles.css">
  <link rel="icon" href="favicon.ico" type="image/x-icon">
</head>
<body>
  <div class="container">
    <header>
      <div class="terminal-header">
        <div class="terminal-buttons">
          <span class="terminal-button"></span>
          <span class="terminal-button"></span>
          <span class="terminal-button"></span>
        </div>
        <div class="terminal-title">validator.joshuahamsa.com</div>
      </div>
    </header>

    <main>
      <div id="val-offline" class="val-offline" style="display:none">
        <span class="val-red">[OFFLINE]</span> last seen: <span id="val-last-seen">—</span>
      </div>
      <pre id="val-pre" class="val-pre">connecting...</pre>
      <div id="val-footer" class="val-footer val-dim">updated — · refreshing every 5s</div>
    </main>
  </div>

  <script>
    // Replace YOUR_TAILSCALE_HOSTNAME with the result of `tailscale status` on the server.
    // Example: if your hostname is "validator", the URL is https://validator.ts.net/metrics
    const METRICS_URL = 'https://YOUR_TAILSCALE_HOSTNAME.ts.net/metrics';
    const FETCH_INTERVAL_MS = 5000;
    const FETCH_TIMEOUT_MS = 4000;

    let lastData = null;
    let lastSeenAt = null;

    // Right-pad an HTML string to `width` visible characters.
    // Strips HTML tags before measuring so color spans don't inflate the count.
    function rpad(html, width) {
      const visible = html.replace(/<[^>]*>/g, '');
      return html + ' '.repeat(Math.max(0, width - visible.length));
    }

    function span(cls, text) {
      return `<span class="val-${cls}">${text}</span>`;
    }

    function progressBar(pct) {
      const filled = Math.min(10, Math.floor(pct * 10 / 100));
      const bar = '█'.repeat(filled) + '░'.repeat(10 - filled);
      const cls = pct >= 81 ? 'red' : pct >= 61 ? 'amber' : 'green';
      return span(cls, bar);
    }

    function pctSpan(pct) {
      const cls = pct >= 81 ? 'red' : pct >= 61 ? 'amber' : 'green';
      return span(cls, String(pct).padStart(3) + '%');
    }

    function formatUptime(secs) {
      const d = Math.floor(secs / 86400);
      const h = Math.floor((secs % 86400) / 3600);
      const m = Math.floor((secs % 3600) / 60);
      return `${d}d ${String(h).padStart(2, '0')}h ${String(m).padStart(2, '0')}m`;
    }

    function stateColor(state) {
      if (state === 'proposing' || state === 'full') return 'green';
      if (['connected', 'syncing', 'tracking'].includes(state)) return 'amber';
      return 'red';
    }

    // Build a two-column row: left col = 38 visible chars, right col = 39.
    function twoCol(left, right) {
      return `║${rpad(left, 38)}║${rpad(right, 39)}║`;
    }

    function buildLines(data) {
      const v = data.validator;
      const id = data.identity;
      const s = data.system;
      const n = data.network;
      const a = data.alerts;

      const H78 = '═'.repeat(78);
      const H38 = '═'.repeat(38);
      const H39 = '═'.repeat(39);
      const lines = [];

      // Header
      lines.push(`╔${H78}╗`);
      const title = 'LFG XRPL VALIDATOR';
      const lp = Math.floor((78 - title.length) / 2);
      lines.push(`║${' '.repeat(lp)}${span('magenta', title)}${' '.repeat(78 - title.length - lp)}║`);
      const sub = `▸▸  ${id.domain}  ·  ${id.public_key_short}  ·  Manifest #${id.manifest_seq}  ◂◂`;
      const sp = Math.floor((78 - sub.length) / 2);
      lines.push(`║${' '.repeat(sp)}${span('dim', sub)}${' '.repeat(78 - sub.length - sp)}║`);

      // Validator | System
      lines.push(`╠${H38}╦${H39}╣`);
      lines.push(twoCol(` ${span('cyan', '── VALIDATOR')}${'─'.repeat(25)}`,
                        ` ${span('cyan', '── SYSTEM')}${'─'.repeat(29)}`));

      const sc = stateColor(v.state);
      lines.push(twoCol(`  state    ${span(sc, v.state)}`,
                        `  CPU  ${progressBar(s.cpu_pct)} ${pctSpan(s.cpu_pct)}`));
      lines.push(twoCol(`  ledger   ${span('white', String(v.ledger_seq))}`,
                        `  RAM  ${progressBar(s.ram_pct)} ${span(s.ram_pct >= 81 ? 'red' : s.ram_pct >= 61 ? 'amber' : 'green', s.ram_used_gb + '/' + s.ram_total_gb + 'G')}`));
      const ageCls = v.ledger_age_s > 10 ? 'amber' : 'green';
      lines.push(twoCol(`  age      ${span(ageCls, v.ledger_age_s + 's')}`,
                        `  Disk ${progressBar(s.disk_pct)} ${pctSpan(s.disk_pct)}`));
      const loadCls = v.load_factor > 1.0 ? 'amber' : 'green';
      lines.push(twoCol(`  load     ${span(loadCls, v.load_factor.toFixed(2))}`,
                        `  Up   ${span('green', formatUptime(s.uptime_s))}`));
      const peerCls = v.peers < 4 ? 'red' : v.peers < 10 ? 'amber' : 'green';
      lines.push(twoCol(`  peers    ${span(peerCls, String(v.peers))}`, ``));
      lines.push(twoCol(`  version  ${span('dim', v.build_version)}`, ``));

      // Network | Alerts
      lines.push(`╠${H38}╬${H39}╣`);
      lines.push(twoCol(` ${span('cyan', '── NETWORK')}${'─'.repeat(27)}`,
                        ` ${span('cyan', '── ALERTS')}${'─'.repeat(29)}`));

      const netRows = [
        `  LAN       ${span('white', n.lan_ip)}`,
        `  Tailscale ${span('white', n.tailscale_ip)}`,
        `  SSH       ${span('white', n.ssh_sessions + ' active')}`,
        `  P2P :51235 ${span(n.p2p_open ? 'green' : 'red', n.p2p_open ? 'OPEN' : 'CLOSED')}`,
      ];
      const alertRows = a.length > 0 ? a : ['no recent alerts'];
      const maxRows = Math.max(netRows.length, alertRows.length);
      for (let i = 0; i < maxRows; i++) {
        const left = netRows[i] || '';
        const raw = alertRows[i] || '';
        const aCls = /err|error/i.test(raw) ? 'red' : /warn|wrn/i.test(raw) ? 'amber' : 'green';
        const right = raw ? `  ${span(aCls, raw.slice(0, 37))}` : '';
        lines.push(twoCol(left, right));
      }

      // Identity row
      lines.push(`╠${H78}╣`);
      const revCls = id.revoked ? 'red' : 'green';
      const revLabel = id.revoked ? 'YES ⚠' : 'NO';
      const identVisible = `  ◈ IDENTITY  Key: ${id.public_key_short}  ·  Domain: ${id.domain}  ·  Revoked: ${revLabel}`;
      const identPad = ' '.repeat(Math.max(0, 78 - identVisible.length));
      lines.push(`║  ${span('yellow', '◈ IDENTITY')}  Key: ${span('white', id.public_key_short)}  ·  Domain: ${span('white', id.domain)}  ·  Revoked: ${span(revCls, revLabel)}${identPad}║`);
      lines.push(`╚${H78}╝`);

      return lines;
    }

    function renderData(data) {
      document.getElementById('val-pre').innerHTML = buildLines(data).join('\n');
      document.getElementById('val-footer').textContent =
        `updated ${data.timestamp} · refreshing every 5s`;
    }

    async function fetchMetrics() {
      const controller = new AbortController();
      const tid = setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS);
      try {
        const res = await fetch(METRICS_URL, { signal: controller.signal });
        clearTimeout(tid);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        lastData = data;
        lastSeenAt = new Date();
        document.getElementById('val-offline').style.display = 'none';
        document.getElementById('val-pre').classList.remove('val-dimmed');
        renderData(data);
      } catch (e) {
        clearTimeout(tid);
        if (lastSeenAt) {
          document.getElementById('val-last-seen').textContent = lastSeenAt.toUTCString();
          document.getElementById('val-offline').style.display = 'block';
          document.getElementById('val-pre').classList.add('val-dimmed');
          if (lastData) renderData(lastData);
        }
      }
    }

    fetchMetrics();
    setInterval(fetchMetrics, FETCH_INTERVAL_MS);
  </script>
</body>
</html>
```

- [ ] **Step 2: Replace `YOUR_TAILSCALE_HOSTNAME` with the actual hostname**

```bash
# Use the hostname found in Task 6 Step 1
sed -i "s/YOUR_TAILSCALE_HOSTNAME/<your-actual-hostname>/g" \
  ~/joshuahamsa.github.io/validator.html
```

---

## Task 9: Update `styles.css` with validator classes

**Files:**
- Modify: `~/joshuahamsa.github.io/styles.css`

- [ ] **Step 1: Append validator CSS to the end of `styles.css`**

```css
/* ── Validator dashboard ──────────────────────────────────────────────────── */

.val-pre {
  background-color: var(--bg-color);
  color: var(--text-color);
  font-family: 'Courier New', monospace;
  font-size: 14px;
  line-height: 1.4;
  overflow-x: auto;
  padding: 0;
  margin: 0;
  white-space: pre;
  border: none;
}

.val-offline {
  font-family: 'Courier New', monospace;
  font-size: 14px;
  padding: 4px 0 8px 0;
}

.val-footer {
  font-family: 'Courier New', monospace;
  font-size: 12px;
  padding-top: 6px;
}

.val-dimmed { opacity: 0.45; }

/* MOTD-matched color palette */
.val-magenta { color: #c040c0; }
.val-cyan    { color: #00ffff; }
.val-yellow  { color: #ffff44; }
.val-green   { color: var(--accent-color); }
.val-red     { color: #ff4444; }
.val-amber   { color: #cc8800; }
.val-white   { color: #f0f0f0; }
.val-dim     { opacity: 0.45; }
```

---

## Task 10: Update `index.html` and deploy

**Files:**
- Modify: `~/joshuahamsa.github.io/index.html`

- [ ] **Step 1: Add validator entry in the projects section**

In `index.html`, find `<!-- Add more projects as needed -->` and insert the following block immediately before it:

```html
          <div class="project">
            <h3>XRPL Validator Dashboard</h3>
            <div class="project-meta">
              <span class="project-date">2026-05-20</span>
            </div>
            <p>Live cyberpunk terminal dashboard for my XRPL validator node — real-time ledger, system, and network stats.</p>
            <div class="project-links">
              <a href="/validator.html">cd /validator</a>
            </div>
          </div>
```

- [ ] **Step 2: Commit and push**

```bash
cd ~/joshuahamsa.github.io
git add validator.html styles.css index.html
git commit -m "feat: add live XRPL validator dashboard"
git push origin main
```

- [ ] **Step 3: Smoke test after deploy (wait ~60s for Pages to build)**

Open `https://joshuahamsa.github.io/validator.html` in a browser.

Verify:
- Dashboard box renders with box-drawing borders
- Validator state, ledger seq, peers are populated
- CPU/RAM/Disk progress bars show
- Footer shows a recent timestamp
- After 5 seconds, the timestamp updates
- Disconnect from the network (or stop the metrics service) and confirm the `[OFFLINE]` banner appears with "last seen" timestamp and dimmed data

- [ ] **Step 4: Re-enable metrics service if stopped during testing**

```bash
sudo systemctl status metrics-server
# if stopped:
sudo systemctl start metrics-server
```
