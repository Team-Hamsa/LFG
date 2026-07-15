# tests/test_bulk_mint_service.py
# Task 10 (#215): service-layer wiring for bulk mint — start/status endpoints,
# per-user active-job lock, and a startup resume sweep.
#
# Env-guard preamble: importing lfg_service.app freezes lfg_core.config
# constants at import time; set the same defaults test_bulk_mint_flow.py /
# test_smoke.py use so collection order can't strand them.
import os
import sys

os.environ.setdefault("XUMM_API_KEY", "test")
os.environ.setdefault("XUMM_API_SECRET", "test")
os.environ.setdefault("SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2")  # throwaway test seed
os.environ.setdefault("TOKEN_ISSUER_ADDRESS", "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
os.environ.setdefault("TOKEN_CURRENCY_HEX", "4C46474F00000000000000000000000000000000")
os.environ.setdefault("BUNNY_CDN_ACCESS_KEY", "test")
os.environ.setdefault("BUNNY_CDN_STORAGE_ZONE", "test")
os.environ.setdefault("LAYER_SOURCE", "local")
os.environ.setdefault("BUNNY_PULL_ZONE", "nft.pullzone.example")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio  # noqa: E402

import pytest  # noqa: E402
from aiohttp.test_utils import make_mocked_request  # noqa: E402

from lfg_core import bulk_mint_flow  # noqa: E402
from lfg_service import app as server  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _post_request(path, body):
    """Mirrors tests/test_market_api.py's _post_request: stub request.json()
    since there's no full aiohttp TestClient fixture wired for these routes."""
    req = make_mocked_request("POST", path)

    async def _json():
        return body

    req.json = _json  # type: ignore[method-assign]
    return req


class _StatusReq:
    """Minimal GET-status request stand-in (match_info + a settable per-request
    store), mirroring tests/test_market_api.py's _StatusReq."""

    headers: dict = {}

    def __init__(self, session_id):
        self.match_info = {"session_id": session_id}
        self._store = {}

    def __getitem__(self, k):
        return self._store[k]

    def __setitem__(self, k, v):
        self._store[k] = v


@pytest.fixture
def dev_auth(monkeypatch):
    """require_auth/require_wallet in dev mode inject user {'id': 'dev'} and a
    fixed dev wallet address; isolate the module-level bulk_sessions dict."""
    monkeypatch.setattr(server.config, "WEBAPP_DEV_MODE", True)
    monkeypatch.setattr(server, "bulk_sessions", {})
    return server.bulk_sessions


def test_bulk_routes_registered():
    app = server.create_app()
    paths = {getattr(r.resource, "canonical", "") for r in app.router.routes()}
    assert "/api/mint/bulk" in paths
    assert "/api/mint/bulk/active" in paths
    assert "/api/mint/bulk/{session_id}" in paths


def test_bulk_route_registered_before_mint_session_wildcard():
    app = server.create_app()
    ordered = [
        getattr(r.resource, "canonical", "")
        for r in app.router.routes()
        if getattr(r.resource, "canonical", "").startswith("/api/mint/")
    ]
    # /api/mint/bulk(/active) must precede /api/mint/{session_id} or the
    # wildcard swallows "bulk" as a session id (aiohttp dispatches in
    # registration order).
    assert ordered.index("/api/mint/bulk") < ordered.index("/api/mint/{session_id}")
    assert ordered.index("/api/mint/bulk/active") < ordered.index("/api/mint/{session_id}")


def test_bulk_start_rejects_invalid_quantity(dev_auth):
    req = _post_request("/api/mint/bulk", {"quantity": 0})
    resp = _run(server.handle_bulk_mint_start(req))
    assert resp.status == 400
    import json as _json

    assert _json.loads(resp.body.decode())["error"] == "invalid_quantity"


def test_bulk_start_rejects_when_collection_full(dev_auth, monkeypatch):
    monkeypatch.setattr(bulk_mint_flow.supply, "remaining_headroom", lambda net: 0)
    req = _post_request("/api/mint/bulk", {"quantity": 5})
    resp = _run(server.handle_bulk_mint_start(req))
    assert resp.status == 409
    import json as _json

    payload = _json.loads(resp.body.decode())
    assert payload["error"] == "collection_full"


def test_bulk_status_not_found_for_unknown_session(dev_auth):
    resp = _run(server.handle_bulk_mint_status(_StatusReq("nope")))
    assert resp.status == 404


def test_bulk_active_returns_null_when_none(dev_auth):
    resp = _run(server.handle_bulk_mint_active(_StatusReq(None)))
    import json as _json

    assert _json.loads(resp.body.decode()) == {"session": None}


def test_bulk_active_returns_live_job(dev_auth):
    job = bulk_mint_flow.BulkMintJob(
        discord_id="dev", wallet_address="rTest", requested_qty=2, platform="discord"
    )
    dev_auth[job.id] = job
    resp = _run(server.handle_bulk_mint_active(_StatusReq(None)))
    import json as _json

    body = _json.loads(resp.body.decode())
    assert body["session"]["id"] == job.id
