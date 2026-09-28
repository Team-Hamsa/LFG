# Self-hosted node rate-limit exemption. Our own xrpld lists this host under
# `secure_gateway`; a request that carries a non-empty `X-User` header is then
# exempt from resource charging (no admin rights). The header must go ONLY to
# private/loopback endpoints -- never to public fallbacks.

import asyncio
import subprocess
import sys

import httpx
import pytest

from lfg_core import xrpl_rpc

OK_BODY = {"result": {"status": "success", "ledger_index": 7}}


def _capture(monkeypatch):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=OK_BODY)

    monkeypatch.setattr(xrpl_rpc, "_http_transport", httpx.MockTransport(handler))
    return seen


def _post(url: str) -> None:
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(xrpl_rpc._post_json_rpc(url, {"method": "ledger"}, 5.0))
    finally:
        loop.close()


@pytest.mark.parametrize(
    "url",
    ["http://10.30.0.176:5005/", "http://127.0.0.1:5005/", "http://192.168.1.5:51234"],
)
def test_x_user_sent_to_private_node_when_configured(monkeypatch, url):
    monkeypatch.setattr(xrpl_rpc, "GATEWAY_USER", "lfg")
    seen = _capture(monkeypatch)
    _post(url)
    assert seen[0].headers.get("X-User") == "lfg"


@pytest.mark.parametrize(
    "url",
    [
        "https://xrplcluster.com/",
        "https://s1.ripple.com:51234/",
        "http://44.244.177.231:51234/",
        "http://localhost.evil.example/",
        "http://169.254.1.1:5005/",
        "http://0.0.0.0:5005/",
        "http://[fe80::1]:5005/",
    ],
)
def test_x_user_never_sent_to_public_endpoints(monkeypatch, url):
    monkeypatch.setattr(xrpl_rpc, "GATEWAY_USER", "lfg")
    seen = _capture(monkeypatch)
    _post(url)
    assert "X-User" not in seen[0].headers


def test_no_x_user_when_unset(monkeypatch):
    monkeypatch.setattr(xrpl_rpc, "GATEWAY_USER", "")
    seen = _capture(monkeypatch)
    _post("http://10.30.0.176:5005/")
    assert "X-User" not in seen[0].headers


def test_gateway_user_is_read_from_env_at_import():
    # Exercises the real import-time wiring (the tests above patch the constant).
    code = "from lfg_core import xrpl_rpc; print(repr(xrpl_rpc.GATEWAY_USER))"
    for env_value, expected in (("  lfg \n", "'lfg'"), ("", "''")):
        out = subprocess.run(
            [sys.executable, "-c", code],
            env={**_base_env(), "XRPL_RPC_X_USER": env_value},
            capture_output=True,
            text=True,
            check=True,
        )
        assert out.stdout.strip() == expected


def _base_env() -> dict[str, str]:
    import os

    return {k: v for k, v in os.environ.items() if k != "XRPL_RPC_X_USER"}
