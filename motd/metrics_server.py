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
            "peer_disconnects": int(info.get("peer_disconnects", 0) or 0),
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
