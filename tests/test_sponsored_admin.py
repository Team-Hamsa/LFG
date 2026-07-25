# Service and SDK contract for SourceTag-sponsored mint campaign controls.
import os

os.environ.setdefault("DISCORD_BOT_TOKEN", "test")
os.environ.setdefault("ADMIN_LOG_CHANNEL_ID", "1")
os.environ.setdefault("LFG_SERVICE_URL", "http://svc")
os.environ.setdefault("XUMM_API_KEY", "test")
os.environ.setdefault("XUMM_API_SECRET", "test")
os.environ.setdefault("SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2")
os.environ.setdefault("TOKEN_ISSUER_ADDRESS", "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
os.environ.setdefault("TOKEN_CURRENCY_HEX", "4C46474F00000000000000000000000000000000")
os.environ.setdefault("BUNNY_CDN_ACCESS_KEY", "test")
os.environ.setdefault("BUNNY_CDN_STORAGE_ZONE", "test")
os.environ.setdefault("LAYER_SOURCE", "local")
os.environ.setdefault("BUNNY_PULL_ZONE", "nft.pullzone.example")

import asyncio
import json
import sqlite3
from decimal import Decimal

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from lfg_core import sponsored_mint
from lfg_service import app as server
from surfaces._client.client import LFGServiceClient
from tests.sdk_helpers import run
from tests.sponsored_helpers import prepare_and_forward, ready_history


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _Request:
    def __init__(self, headers: dict[str, str], body: dict | None = None):
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
def _campaign_paths(tmp_path, monkeypatch):
    campaign_db = tmp_path / "campaign.db"
    monkeypatch.setattr(server.config, "XRPL_NETWORK", "mainnet")
    history_db = tmp_path / "history.db"
    ready_history(str(history_db), network="mainnet", now=int(server.time.time()))
    monkeypatch.setattr(server.db_path, "app_db_path", lambda network: str(campaign_db))
    monkeypatch.setattr(server.history_store, "history_db_path", lambda network: str(history_db))
    monkeypatch.setattr(server.xrpl_ops, "get_trustline_balance", _balance("17.5"))
    monkeypatch.setattr(server, "_sponsored_recovery_ready", True, raising=False)


@pytest.fixture(autouse=True)
def _service_tokens(monkeypatch):
    monkeypatch.setenv("SERVICE_TOKEN_DISCORD", "tok-d")
    monkeypatch.setenv("SERVICE_TOKEN_TELEGRAM", "tok-t")


def _balance(value):
    async def result(*args):
        return Decimal(value)

    return result


_HANDLERS = (
    ("handle_sponsored_mint_start", {"actor": "admin:42"}),
    ("handle_sponsored_mint_stop", {"actor": "admin:42"}),
    ("handle_sponsored_mint_status", None),
)


@pytest.mark.parametrize("handler_name,body", _HANDLERS)
def test_sponsored_admin_handlers_require_service_token(handler_name, body):
    handler = getattr(server, handler_name)
    assert _run(handler(_Request({}, body))).status == 401


@pytest.mark.parametrize("handler_name,body", _HANDLERS)
def test_sponsored_admin_handlers_reject_other_surfaces(handler_name, body):
    handler = getattr(server, handler_name)
    response = _run(handler(_Request({"Authorization": "Bearer tok-t"}, body)))
    assert response.status == 403
    assert json.loads(response.body)["code"] == "wrong_surface"


def test_start_requires_non_empty_actor():
    for body in ({}, {"actor": ""}, {"actor": "  "}):
        response = _run(
            server.handle_sponsored_mint_start(_Request({"Authorization": "Bearer tok-d"}, body))
        )
        assert response.status == 400
        assert json.loads(response.body)["code"] == "bad_request"


def test_start_and_stop_are_idempotent_for_discord():
    headers = {"Authorization": "Bearer tok-d"}
    started = _run(server.handle_sponsored_mint_start(_Request(headers, {"actor": "admin:42"})))
    duplicate = _run(server.handle_sponsored_mint_start(_Request(headers, {"actor": "admin:42"})))
    assert started.status == duplicate.status == 200
    assert json.loads(started.body)["state"] == "active"
    assert json.loads(duplicate.body)["campaign_id"] == json.loads(started.body)["campaign_id"]

    stopped = _run(server.handle_sponsored_mint_stop(_Request(headers, {"actor": "admin:42"})))
    repeated = _run(server.handle_sponsored_mint_stop(_Request(headers, {"actor": "admin:42"})))
    assert stopped.status == repeated.status == 200
    assert json.loads(stopped.body)["state"] == json.loads(repeated.body)["state"] == "stopped"


def test_status_exposes_campaign_metrics_and_project_lfgo_balance():
    headers = {"Authorization": "Bearer tok-d"}
    _run(server.handle_sponsored_mint_start(_Request(headers, {"actor": "admin:42"})))

    response = _run(server.handle_sponsored_mint_status(_Request(headers)))
    body = json.loads(response.body)
    assert response.status == 200
    assert body["state"] == "active"
    assert body["countdown_seconds"] > 0
    assert body["cap"] == 100
    assert body["reserved"] == body["minted"] == body["accepted"] == 0
    assert body["burn_pending"] == body["burn_burned"] == 0
    assert body["tagged_sponsored_wallets"] == 0
    assert body["unique_tagged_wallets"] == 0
    assert body["unique_target"] == 300
    assert body["last_operator"] == "admin:42"
    assert isinstance(body["changed_at"], int)
    assert body["lfgo_balance"] == "17.5"
    assert body["recovery_ready"] is True


def test_malformed_offer_startup_keeps_admin_not_ready_and_never_creates_offer(monkeypatch):
    campaign_db = server.db_path.app_db_path(server.config.XRPL_NETWORK)
    history_db = server.history_store.history_db_path(server.config.XRPL_NETWORK)
    started_at = int(server.time.time())
    sponsored_mint.start_campaign(campaign_db, network="mainnet", actor="test", now=started_at)
    reservation = sponsored_mint.reserve_if_eligible(
        campaign_db,
        history_db,
        network="mainnet",
        wallet="rRECIPIENT",
        session_id="malformed-startup",
        now=started_at + 1,
    )
    assert reservation.sponsored
    prepare_and_forward(
        sponsored_mint,
        campaign_db,
        network="mainnet",
        wallet="rRECIPIENT",
        session_id="malformed-startup",
        tx_hash="TX-MALFORMED-STARTUP",
        now=started_at + 2,
    )
    sponsored_mint.record_minted_and_enqueue_burn(
        campaign_db,
        network="mainnet",
        wallet="rRECIPIENT",
        session_id="malformed-startup",
        mint_tx_hash="TX-MALFORMED-STARTUP",
        nft_id="NFT-MALFORMED-STARTUP",
        now=started_at + 3,
    )

    class MalformedResponse:
        result = {
            "offers": [
                {
                    "nft_offer_index": "OFFER-MALFORMED-STARTUP",
                    "amount": 0,
                    "destination": "rRECIPIENT",
                    "flags": server.xrpl_ops.LSF_SELL_NFTOKEN,
                    "owner": "rBOT",
                }
            ]
        }

    class MalformedClient:
        def __init__(self, *args, **kwargs):
            pass

        def request(self, request):
            return MalformedResponse()

    create_calls = []

    async def create_offer(*args, **kwargs):
        create_calls.append((args, kwargs))
        return "OFFER-MUST-NOT-BE-CREATED"

    async def recover_offers_only():
        await server._recover_sponsored_offers(campaign_db, network="mainnet")

    monkeypatch.setattr(server.xrpl_ops, "JsonRpcClient", MalformedClient)
    monkeypatch.setattr(server.xrpl_ops, "create_nft_offer", create_offer)
    monkeypatch.setattr(server, "resume_bulk_jobs", recover_offers_only)

    _run(server._start_bulk_resume({}))

    response = _run(
        server.handle_sponsored_mint_status(_Request({"Authorization": "Bearer tok-d"}))
    )
    body = json.loads(response.body)
    from surfaces.discord_bot.admin import _sponsored_status_embed

    fields = {field.name: field.value for field in _sponsored_status_embed(body).fields}
    with sqlite3.connect(campaign_db) as conn:
        row = conn.execute(
            "SELECT status, offer_id, last_error FROM free_mint_claims "
            "WHERE session_id = 'malformed-startup'"
        ).fetchone()

    assert create_calls == []
    assert body["recovery_ready"] is False
    assert fields["Recovery"] == "❌ Sponsored disabled"
    assert row[0:2] == ("minted", None)
    assert "malformed nft_sell_offers response" in row[2]


def test_status_survives_project_balance_rpc_failure(monkeypatch):
    async def _unavailable(*args):
        raise RuntimeError("RPC unavailable")

    monkeypatch.setattr(server.xrpl_ops, "get_trustline_balance", _unavailable)
    response = _run(
        server.handle_sponsored_mint_status(_Request({"Authorization": "Bearer tok-d"}))
    )
    assert response.status == 200
    assert json.loads(response.body)["lfgo_balance"] is None


def test_off_status_retains_campaign_cap_for_admin_display():
    response = _run(
        server.handle_sponsored_mint_status(_Request({"Authorization": "Bearer tok-d"}))
    )
    body = json.loads(response.body)

    assert body["state"] == "off"
    assert body["cap"] == 100

    from surfaces.discord_bot.admin import _sponsored_status_embed

    fields = {field.name: field.value for field in _sponsored_status_embed(body).fields}
    assert fields["Admitted"] == "0 / 100"
    assert fields["Confirmed"] == "0 / 100"
    assert fields["Recovery"] == "✅ Ready"


def test_status_counts_tagged_sponsored_wallets_from_accepted_claims():
    headers = {"Authorization": "Bearer tok-d"}
    started = _run(server.handle_sponsored_mint_start(_Request(headers, {"actor": "admin:42"})))
    campaign_id = json.loads(started.body)["campaign_id"]
    campaign_db = server.db_path.app_db_path(server.config.XRPL_NETWORK)
    with sqlite3.connect(campaign_db) as conn:
        conn.execute(
            """
            INSERT INTO free_mint_claims (
                id, network, wallet, campaign_id, session_id, status,
                reserved_at, reservation_expires_at, released_at,
                mint_tx_hash, nft_id, offer_id, accept_tx_hash, tagged_at,
                last_error, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'accepted', ?, NULL, NULL,
                      NULL, NULL, NULL, 'accepted-tx', ?, NULL, ?, ?)
            """,
            (
                "tagged-claim",
                server.config.XRPL_NETWORK,
                "rTagged",
                campaign_id,
                "session-tagged",
                1,
                2,
                1,
                2,
            ),
        )

    response = _run(server.handle_sponsored_mint_status(_Request(headers)))
    body = json.loads(response.body)
    assert body["accepted"] == 1
    assert body["tagged_sponsored_wallets"] == 1


def test_status_last_operator_ignores_later_claim_audits():
    headers = {"Authorization": "Bearer tok-d"}
    started = _run(server.handle_sponsored_mint_start(_Request(headers, {"actor": "admin:42"})))
    campaign_id = json.loads(started.body)["campaign_id"]
    campaign_db = server.db_path.app_db_path(server.config.XRPL_NETWORK)
    with sqlite3.connect(campaign_db) as conn:
        conn.execute(
            """
            INSERT INTO free_mint_audit (
                network, actor, action, at, campaign_id, result, details
            ) VALUES (?, ?, 'claim_accepted', ?, ?, 'accepted', NULL)
            """,
            (
                server.config.XRPL_NETWORK,
                "rLaterClaimActor",
                9_999_999_999,
                campaign_id,
            ),
        )

    response = _run(server.handle_sponsored_mint_status(_Request(headers)))
    body = json.loads(response.body)
    assert body["last_operator"] == "admin:42"
    assert body["changed_at"] != 9_999_999_999


def test_sponsored_admin_routes_are_registered_before_static_mount():
    app = server.create_app()
    paths = [getattr(route.resource, "canonical", "") for route in app.router.routes()]
    static_index = paths.index("/")
    for path in (
        "/api/admin/sponsored-mint/start",
        "/api/admin/sponsored-mint/stop",
        "/api/admin/sponsored-mint/status",
    ):
        assert path in paths
        assert paths.index(path) < static_index


def test_sdk_sponsored_mint_methods_use_service_token_and_actor_payload():
    async def _inner():
        app = web.Application()
        calls = []

        async def handler(request):
            body = await request.json() if request.method == "POST" else None
            calls.append((request.method, request.path, request.headers.get("Authorization"), body))
            if request.headers.get("Authorization") != "Bearer svc-test":
                return web.json_response({"error": "unauthorized"}, status=401)
            return web.json_response({"state": "active"})

        app.router.add_get("/api/admin/sponsored-mint/status", handler)
        app.router.add_post("/api/admin/sponsored-mint/start", handler)
        app.router.add_post("/api/admin/sponsored-mint/stop", handler)
        test_server = TestServer(app)
        await test_server.start_server()
        base = str(test_server.make_url("")).rstrip("/")
        client = LFGServiceClient(base, "svc-test", "discord", base_delay=0.0)
        async with client:
            assert (await client.sponsored_mint_status())["state"] == "active"
            await client.sponsored_mint_start("discord:42")
            await client.sponsored_mint_stop("discord:42")
        await test_server.close()
        assert calls == [
            ("GET", "/api/admin/sponsored-mint/status", "Bearer svc-test", None),
            ("POST", "/api/admin/sponsored-mint/start", "Bearer svc-test", {"actor": "discord:42"}),
            ("POST", "/api/admin/sponsored-mint/stop", "Bearer svc-test", {"actor": "discord:42"}),
        ]

    run(_inner())
