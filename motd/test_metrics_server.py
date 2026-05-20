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
    SENSORS_JSON = json.dumps({
        "coretemp-isa-0000": {
            "Adapter": "ISA adapter",
            "Package id 0": {
                "temp1_input": 55.0, "temp1_max": 86.0,
                "temp1_crit": 100.0, "temp1_crit_alarm": 0.0,
            },
        }
    })

    @patch("metrics_server.subprocess.check_output")
    def test_returns_expected_fields_and_ranges(self, mock_sub):
        def side(cmd, **kw):
            if cmd[0] == "sensors":
                return self.SENSORS_JSON
            if "rapl-energy-uj" in " ".join(cmd):
                return "100000000\n"
            return "Use%\n45%\n"
        mock_sub.side_effect = side
        # Reads real /proc files — validates structure and value ranges.
        result = metrics_server.get_system_info()
        for key in ("cpu_pct", "ram_pct", "ram_used_gb", "ram_total_gb", "disk_pct", "uptime_s", "cpu_temp_c", "cpu_w"):
            self.assertIn(key, result, f"missing key: {key}")
        self.assertGreaterEqual(result["cpu_pct"], 0)
        self.assertLessEqual(result["cpu_pct"], 100)
        self.assertGreaterEqual(result["ram_pct"], 0)
        self.assertLessEqual(result["ram_pct"], 100)
        self.assertEqual(result["disk_pct"], 45)
        self.assertGreater(result["uptime_s"], 0)
        self.assertEqual(result["cpu_temp_c"], 55)

    @patch("metrics_server.subprocess.check_output")
    def test_cpu_temp_none_when_sensors_unavailable(self, mock_sub):
        def side(cmd, **kw):
            if cmd[0] == "sensors":
                raise Exception("sensors not found")
            if "rapl-energy-uj" in " ".join(cmd):
                return "100000000\n"
            return "Use%\n45%\n"
        mock_sub.side_effect = side
        result = metrics_server.get_system_info()
        self.assertIsNone(result["cpu_temp_c"])


class TestGetCpuPowerW(unittest.TestCase):
    def setUp(self):
        metrics_server._rapl_prev_uj = None
        metrics_server._rapl_prev_ts = None

    @patch("metrics_server.time.monotonic", return_value=1000.0)
    @patch("metrics_server.subprocess.check_output", return_value="500000000\n")
    def test_returns_none_on_first_call(self, _sub, _time):
        self.assertIsNone(metrics_server.get_cpu_power_w())

    @patch("metrics_server.time.monotonic")
    @patch("metrics_server.subprocess.check_output")
    def test_returns_watts_on_second_call(self, mock_sub, mock_time):
        # First call: establish baseline
        mock_time.return_value = 1000.0
        mock_sub.return_value = "0\n"
        metrics_server.get_cpu_power_w()
        # Second call: 5s later, 250 J consumed = 50 W
        mock_time.return_value = 1005.0
        mock_sub.return_value = "250000000\n"
        result = metrics_server.get_cpu_power_w()
        self.assertEqual(result, 50.0)

    @patch("metrics_server.subprocess.check_output", side_effect=Exception("no rapl"))
    def test_returns_none_on_exception(self, _):
        self.assertIsNone(metrics_server.get_cpu_power_w())


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
