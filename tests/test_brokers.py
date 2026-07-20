# tests/test_brokers.py
# #131: known-broker allowlist — built-ins, resolution, JSON overlay, and the
# malformed-file fallback. brokers has no lfg_core.config dependency, so no
# env-guard preamble is needed.
import json
import os

import pytest

from lfg_core import brokers

XRPCAFE = "rpx9JThQ2y37FaGeeJP7PXDUVEXY3PHZSC"


@pytest.fixture(autouse=True)
def _reset_cache(monkeypatch):
    monkeypatch.delenv("BROKER_ALLOWLIST_PATH", raising=False)
    brokers._cache = None
    brokers._cache_key = None
    yield
    brokers._cache = None
    brokers._cache_key = None


class TestBuiltins:
    def test_known_destinations_include_builtins(self):
        assert XRPCAFE in brokers.known_destinations()

    def test_resolve_builds_deep_link(self):
        r = brokers.resolve(XRPCAFE, "00080000ABC")
        assert r == {"name": "xrp.cafe", "url": "https://xrp.cafe/nft/00080000ABC"}

    def test_resolve_no_template_gives_none_url(self):
        r = brokers.resolve("rnPNSonfEN1TWkPH4Kwvkk3693sCT4tsZv", "X")
        assert r is not None
        assert r["name"] == "Art Dept"
        assert r["url"] is None

    def test_resolve_unknown_and_empty(self):
        assert brokers.resolve("rUnknownBroker", "X") is None
        assert brokers.resolve(None, "X") is None
        assert brokers.resolve("", "X") is None


class TestOverlayFile:
    def test_file_adds_and_overrides(self, tmp_path, monkeypatch):
        path = tmp_path / "allow.json"
        path.write_text(
            json.dumps(
                {
                    "rNewBroker111": {
                        "name": "newmkt",
                        "url_template": "https://new.example/n/{nft_id}",
                    },
                    XRPCAFE: {"name": "renamed-cafe"},
                }
            )
        )
        monkeypatch.setenv("BROKER_ALLOWLIST_PATH", str(path))
        assert "rNewBroker111" in brokers.known_destinations()
        assert brokers.resolve("rNewBroker111", "42")["url"] == "https://new.example/n/42"
        # File entry replaces the built-in wholesale (name changed, link gone).
        assert brokers.resolve(XRPCAFE, "42") == {"name": "renamed-cafe", "url": None}

    def test_malformed_file_falls_back_to_builtins(self, tmp_path, monkeypatch):
        path = tmp_path / "bad.json"
        path.write_text("not json{")
        monkeypatch.setenv("BROKER_ALLOWLIST_PATH", str(path))
        assert XRPCAFE in brokers.known_destinations()
        assert "rNewBroker111" not in brokers.known_destinations()

    def test_missing_file_falls_back_to_builtins(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BROKER_ALLOWLIST_PATH", str(tmp_path / "nope.json"))
        assert brokers.known_destinations() == frozenset(brokers._BUILTIN)

    def test_entry_missing_name_rejected_whole_file(self, tmp_path, monkeypatch):
        path = tmp_path / "noname.json"
        path.write_text(json.dumps({"rX": {"url_template": "https://x/{nft_id}"}}))
        monkeypatch.setenv("BROKER_ALLOWLIST_PATH", str(path))
        assert brokers.known_destinations() == frozenset(brokers._BUILTIN)

    def test_non_string_template_rejected_whole_file(self, tmp_path, monkeypatch):
        path = tmp_path / "badtype.json"
        path.write_text(json.dumps({"rX": {"name": "x", "url_template": 7}}))
        monkeypatch.setenv("BROKER_ALLOWLIST_PATH", str(path))
        assert brokers.known_destinations() == frozenset(brokers._BUILTIN)

    def test_bad_placeholder_template_rejected_whole_file(self, tmp_path, monkeypatch):
        # {nftid} (typo), positional {0}, and a stray brace would each raise
        # inside resolve() at serve time — must be rejected at load instead.
        for bad in ("https://x/{nftid}", "https://x/{0}", "https://x/{nft_id"):
            path = tmp_path / "badtpl.json"
            path.write_text(json.dumps({"rX": {"name": "x", "url_template": bad}}))
            monkeypatch.setenv("BROKER_ALLOWLIST_PATH", str(path))
            brokers._cache = None
            brokers._cache_key = None
            assert brokers.known_destinations() == frozenset(brokers._BUILTIN), bad

    def test_file_edit_picked_up_without_restart(self, tmp_path, monkeypatch):
        # Greptile P2 on PR #281: the cache is (path, mtime)-keyed so an
        # operator edit (e.g. pulling a compromised broker) takes effect on
        # the next call, no restart.
        path = tmp_path / "allow.json"
        path.write_text(json.dumps({"rLive111": {"name": "livemkt"}}))
        monkeypatch.setenv("BROKER_ALLOWLIST_PATH", str(path))
        assert "rLive111" in brokers.known_destinations()
        path.write_text(json.dumps({"rOther222": {"name": "othermkt"}}))
        os.utime(path, (1, 2_000_000_000))  # force a distinct mtime
        dests = brokers.known_destinations()
        assert "rLive111" not in dests
        assert "rOther222" in dests
