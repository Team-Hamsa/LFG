"""Discord-only admin endpoints for the fee-cover campaign + SDK contract."""

from __future__ import annotations

import asyncio
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from lfg_service import app as server
from surfaces._client.client import LFGServiceClient

DISCORD = {"Authorization": "Bearer tok-d"}
TELEGRAM = {"Authorization": "Bearer tok-t"}
KNOBS = {"coverage_pct": "100", "budget_xrp": "50", "wallet_cap_xrp": "5", "min_bid_xrp": "1"}


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _Request:
    def __init__(self, headers, body=None):
        self.headers = headers
        self._body = body or {}
        self._store = {}

    def __getitem__(self, key):
        return self._store[key]

    def __setitem__(self, key, value):
        self._store[key] = value

    async def json(self):
        return self._body


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("SERVICE_TOKEN_DISCORD", "tok-d")
    monkeypatch.setenv("SERVICE_TOKEN_TELEGRAM", "tok-t")
    app_db = str(tmp_path / "app.db")
    monkeypatch.setattr(server, "_fee_cover_db", lambda: app_db)
    monkeypatch.setattr(server.config, "XRPL_NETWORK", "testnet")

    async def balance(address):
        return 123_000_000

    monkeypatch.setattr(server.xrpl_ops, "get_xrp_balance_drops", balance)


def _call(name, headers=DISCORD, body=None):
    resp = _run(getattr(server, name)(_Request(headers, body)))
    return resp.status, json.loads(resp.body)


_HANDLERS = (
    ("handle_fee_cover_status", None),
    ("handle_fee_cover_start", {"actor": "discord:1", **KNOBS}),
    ("handle_fee_cover_update", {"actor": "discord:1", **KNOBS}),
    ("handle_fee_cover_stop", {"actor": "discord:1"}),
)


@pytest.mark.parametrize(("name", "body"), _HANDLERS)
def test_requires_a_service_token(name, body):
    assert _call(name, headers={}, body=body)[0] == 401


@pytest.mark.parametrize(("name", "body"), _HANDLERS)
def test_rejects_non_discord_surfaces(name, body):
    status, payload = _call(name, headers=TELEGRAM, body=body)
    assert status == 403 and payload["code"] == "wrong_surface"


def test_status_before_any_campaign():
    status, payload = _call("handle_fee_cover_status")
    assert status == 200
    assert payload["state"] == "never_started" and payload["campaign"] is None
    assert payload["issuer_balance_drops"] == 123_000_000


@pytest.mark.parametrize(
    "body",
    [
        {**KNOBS},  # no actor
        {"actor": " ", **KNOBS},
        {"actor": "discord:1", **KNOBS, "coverage_pct": "100.5"},
        {"actor": "discord:1", **KNOBS, "coverage_pct": "12.345"},
        {"actor": "discord:1", **KNOBS, "budget_xrp": "0"},
        {"actor": "discord:1", **KNOBS, "budget_xrp": "abc"},
        {"actor": "discord:1", **KNOBS, "wallet_cap_xrp": "NaN"},
        {"actor": "discord:1", **KNOBS, "min_bid_xrp": "0.0000001"},
        {"actor": "discord:1", **KNOBS, "duration_hours": "-1"},
    ],
)
def test_start_rejects_bad_input(body):
    status, payload = _call("handle_fee_cover_start", body=body)
    assert status == 400 and payload["code"] == "bad_request"


def test_start_update_stop_lifecycle():
    status, payload = _call(
        "handle_fee_cover_start", body={"actor": "discord:1", **KNOBS, "duration_hours": "2"}
    )
    assert status == 200 and payload["result"] == "started" and payload["state"] == "active"
    campaign = payload["campaign"]
    assert (
        campaign["coverage_bps"],
        campaign["budget_drops"],
        campaign["wallet_cap_drops"],
        campaign["min_bid_drops"],
    ) == (
        10_000,
        50_000_000,
        5_000_000,
        1_000_000,
    )
    assert campaign["ends_at"] == campaign["started_at"] + 7_200
    assert (
        _call("handle_fee_cover_start", body={"actor": "discord:2", **KNOBS})[1]["result"]
        == "already_active"
    )

    _, payload = _call(
        "handle_fee_cover_update",
        body={"actor": "discord:1", **KNOBS, "coverage_pct": "50", "min_bid_xrp": "0"},
    )
    assert payload["result"] == "updated"
    assert (
        payload["campaign"]["coverage_bps"] == 5_000 and payload["campaign"]["min_bid_drops"] == 0
    )

    _, payload = _call("handle_fee_cover_stop", body={"actor": "discord:1"})
    assert payload["result"] == "stopped" and payload["state"] == "stopped"
    assert (
        _call("handle_fee_cover_stop", body={"actor": "discord:1"})[1]["result"]
        == "already_inactive"
    )
    assert (
        _call("handle_fee_cover_update", body={"actor": "discord:1", **KNOBS})[1]["result"]
        == "no_live_campaign"
    )


def test_routes_are_registered():
    routes = {
        (r.method, r.resource.canonical) for r in server.create_app().router.routes() if r.resource
    }
    assert ("GET", "/api/admin/fee-cover/status") in routes
    for action in ("start", "update", "stop"):
        assert ("POST", f"/api/admin/fee-cover/{action}") in routes


def test_sdk_fee_cover_methods_send_the_service_token_and_payload():
    async def inner():
        app = web.Application()
        calls = []

        async def handler(request):
            body = await request.json() if request.method == "POST" else None
            calls.append((request.method, request.path, request.headers.get("Authorization"), body))
            return web.json_response({"state": "active"})

        app.router.add_get("/api/admin/fee-cover/status", handler)
        for action in ("start", "update", "stop"):
            app.router.add_post(f"/api/admin/fee-cover/{action}", handler)
        test_server = TestServer(app)
        await test_server.start_server()
        base = str(test_server.make_url("")).rstrip("/")
        client = LFGServiceClient(base, "svc-test", "discord", base_delay=0.0)
        async with client:
            assert (await client.fee_cover_status())["state"] == "active"
            await client.fee_cover_start("discord:42", coverage_pct="100", budget_xrp="50")
            await client.fee_cover_update("discord:42", coverage_pct="50")
            await client.fee_cover_stop("discord:42")
        await test_server.close()
        assert calls == [
            ("GET", "/api/admin/fee-cover/status", "Bearer svc-test", None),
            (
                "POST",
                "/api/admin/fee-cover/start",
                "Bearer svc-test",
                {"actor": "discord:42", "coverage_pct": "100", "budget_xrp": "50"},
            ),
            (
                "POST",
                "/api/admin/fee-cover/update",
                "Bearer svc-test",
                {"actor": "discord:42", "coverage_pct": "50"},
            ),
            ("POST", "/api/admin/fee-cover/stop", "Bearer svc-test", {"actor": "discord:42"}),
        ]

    _run(inner())
