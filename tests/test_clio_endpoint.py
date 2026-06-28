# nft_info / nft_exists are clio-only methods (the plain rippled WS answers
# `unknownCmd` -> None). They must default to a clio endpoint, NOT WS_URL, so a
# fresh/mainnet deploy doesn't silently fail the Closet on-ledger verify gate.

import asyncio
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from lfg_core import config, xrpl_ops  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_clio_endpoint_prefers_explicit_arg():
    assert xrpl_ops._clio_endpoint("wss://explicit") == "wss://explicit"


def test_clio_endpoint_defaults_to_clio_ws_url():
    assert xrpl_ops._clio_endpoint(None) == config.CLIO_WS_URL


def test_clio_ws_url_is_a_clio_host():
    # The default must be a clio host (the plain rippled WS cannot answer
    # nft_info). Both networks' clio hosts contain "clio".
    assert "clio" in config.CLIO_WS_URL


class _FakeWS:
    """Captures the endpoint it was constructed with."""

    last_endpoint: str | None = None

    def __init__(self, endpoint):
        type(self).last_endpoint = endpoint

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def request(self, _req):
        class _Resp:
            result = {"nft_id": "N", "owner": "rA"}

        return _Resp()


def test_nft_info_connects_to_clio_ws_url_by_default(monkeypatch):
    _FakeWS.last_endpoint = None
    monkeypatch.setattr(xrpl_ops, "AsyncWebsocketClient", _FakeWS)
    _run(xrpl_ops.nft_info("N"))
    assert _FakeWS.last_endpoint == config.CLIO_WS_URL


def test_nft_exists_connects_to_clio_ws_url_by_default(monkeypatch):
    _FakeWS.last_endpoint = None
    monkeypatch.setattr(xrpl_ops, "AsyncWebsocketClient", _FakeWS)
    _run(xrpl_ops.nft_exists("N", attempts=1))
    assert _FakeWS.last_endpoint == config.CLIO_WS_URL


def test_explicit_clio_arg_still_wins(monkeypatch):
    _FakeWS.last_endpoint = None
    monkeypatch.setattr(xrpl_ops, "AsyncWebsocketClient", _FakeWS)
    _run(xrpl_ops.nft_info("N", clio="wss://override"))
    assert _FakeWS.last_endpoint == "wss://override"
