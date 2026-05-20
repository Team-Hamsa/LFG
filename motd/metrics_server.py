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
import time
from pathlib import Path

RIPPLED = "/usr/local/bin/rippled"
RAPL_READER = "/usr/local/bin/rapl-energy-uj"
_json_files = list(Path("/home/hamsa/.ripple").glob("*.json"))
VALIDATOR_JSON = str(_json_files[0]) if _json_files else ""
PORT = 8080

FEATURES_MACRO = "/home/hamsa/rippled/include/xrpl/protocol/detail/features.macro"
RIPPLED_CFG = "/etc/opt/ripple/rippled.cfg"


def _parse_vote_defaults() -> dict:
    try:
        source = open(FEATURES_MACRO).read()
        matches = re.findall(
            r'XRPL_(?:FEATURE|FIX)\s*\(\s*(\w+)\s*,\s*Supported::\w+\s*,'
            r'\s*VoteBehavior::(\w+)\s*\)',
            source,
        )
        return {name: ("yes" if vote == "DefaultYes" else "no") for name, vote in matches}
    except Exception:
        return {}


_VOTE_DEFAULTS: dict = _parse_vote_defaults()


def _parse_cfg_overrides() -> dict:
    try:
        text = open(RIPPLED_CFG).read()
    except Exception:
        return {}
    overrides: dict = {}
    section = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("["):
            section = line.strip("[]")
        elif section == "veto_amendments" and line and not line.startswith("#"):
            overrides[line] = "no"
        elif section == "amendments" and line and not line.startswith("#"):
            overrides[line] = "yes"
    return overrides


# RAPL inter-sample state — power is Δenergy / Δtime across fetch interval
_rapl_prev_uj: "int | None" = None
_rapl_prev_ts: "float | None" = None


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
            "ledger_seq": int(vl.get("seq", 0) or 0),
            "ledger_age_s": vl.get("age", 0),
            "load_factor": info.get("load_factor", 1.0),
            "peers": int(info.get("peers", 0) or 0),
            "peer_disconnects": int(info.get("peer_disconnects", 0) or 0),
            "rippled_uptime_s": int(info.get("uptime", 0) or 0),
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

    # CPU temperature (lm-sensors coretemp — Package id 0 is the die temp)
    cpu_temp_c = None
    try:
        sens = json.loads(subprocess.check_output(
            ["sensors", "-j"], text=True, stderr=subprocess.DEVNULL
        ))
        for chip, chip_data in sens.items():
            if chip.startswith("coretemp") and isinstance(chip_data, dict):
                pkg = chip_data.get("Package id 0", {})
                for k, v in pkg.items():
                    if k.endswith("_input"):
                        cpu_temp_c = round(float(v))
                        break
            if cpu_temp_c is not None:
                break
    except Exception:
        pass

    return {
        "cpu_pct": cpu_pct,
        "ram_pct": ram_pct,
        "ram_used_gb": ram_used_gb,
        "ram_total_gb": ram_total_gb,
        "disk_pct": disk_pct,
        "uptime_s": uptime_s,
        "cpu_temp_c": cpu_temp_c,
        "cpu_w": get_cpu_power_w(),
    }


def get_cpu_power_w():
    """Return CPU package power in watts (RAPL), or None on first call/error."""
    global _rapl_prev_uj, _rapl_prev_ts
    try:
        now = time.monotonic()
        cur_uj = int(subprocess.check_output(
            ["sudo", RAPL_READER], text=True, stderr=subprocess.DEVNULL
        ).strip())
        power_w = None
        if _rapl_prev_uj is not None and _rapl_prev_ts is not None:
            dt_s = now - _rapl_prev_ts
            delta_uj = cur_uj - _rapl_prev_uj
            if dt_s > 0 and delta_uj >= 0:
                power_w = round(delta_uj / (dt_s * 1_000_000), 1)
        _rapl_prev_uj = cur_uj
        _rapl_prev_ts = now
        return power_w
    except Exception:
        return None


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
            ["ss", "-l", "-tnlp"], text=True, stderr=subprocess.DEVNULL
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


def get_amendments() -> list:
    try:
        raw = subprocess.check_output(
            ["sudo", RIPPLED, "feature"],
            timeout=10, text=True, stderr=subprocess.DEVNULL,
        )
        features = json.loads(raw)["result"]["features"]
        overrides = _parse_cfg_overrides()
        result = []
        for hash_, data in features.items():
            if data.get("enabled"):
                continue
            name = data.get("name", "")
            vote = overrides.get(hash_) or _VOTE_DEFAULTS.get(name, "no")
            result.append({
                "name": name,
                "vote": vote,
                "supported": data.get("supported", False),
                "majority": "majority" in data,
            })
        result.sort(key=lambda x: (not x["majority"], x["name"]))
        return result
    except Exception:
        return []


def collect_metrics():
    return {
        "timestamp": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "validator": get_validator_info(),
        "identity": get_identity(),
        "system": get_system_info(),
        "network": get_network_info(),
        "alerts": get_alerts(),
        "amendments": get_amendments(),
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
