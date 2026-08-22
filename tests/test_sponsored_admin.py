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
    # A per-claim offer-recovery failure no longer takes admission down
    # campaign-wide (prod 2026-08-17 unfunded / 2026-08-20 offer-blocked
    # wallets each wedged EVERY restart). The claim stays 'minted' with its
    # last_error persisted and is retried next boot; readiness stays True.
    assert body["recovery_ready"] is True
    assert fields["Recovery"] != "❌ Sponsored disabled"
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


@pytest.fixture
def _clean_reverify_state():
    """Snapshot + restore module-level reverify state so ordering can't couple tests."""
    saved_state = dict(server._reverify_state)
    saved_tasks = dict(server._reverify_tasks)
    server._reverify_state.clear()
    server._reverify_tasks.clear()
    try:
        yield
    finally:
        for task in server._reverify_tasks.values():
            if not task.done():
                task.cancel()
        server._reverify_state.clear()
        server._reverify_state.update(saved_state)
        server._reverify_tasks.clear()
        server._reverify_tasks.update(saved_tasks)


def test_start_kicks_reverify(monkeypatch, _clean_reverify_state):
    kicked: list[tuple[str, str]] = []
    monkeypatch.setattr(
        server, "kick_archive_reverify", lambda net, actor: kicked.append((net, actor))
    )
    headers = {"Authorization": "Bearer tok-d"}
    _run(server.handle_sponsored_mint_start(_Request(headers, {"actor": "admin:42"})))
    assert kicked == [(server.config.XRPL_NETWORK, "admin:42")]


def test_kick_archive_reverify_is_single_flight(monkeypatch, _clean_reverify_state):
    started = {"n": 0}

    async def fake_job(network, actor):
        started["n"] += 1
        await asyncio.sleep(3600)

    monkeypatch.setattr(server, "run_archive_reverify", fake_job)

    async def scenario():
        server.kick_archive_reverify("testnet", "admin:42")
        server.kick_archive_reverify("testnet", "admin:42")  # joins, doesn't double-run
        await asyncio.sleep(0)
        assert started["n"] == 1
        server._reverify_tasks["testnet"].cancel()
        try:
            await server._reverify_tasks["testnet"]
        except asyncio.CancelledError:
            pass

    _run(scenario())


def test_status_exposes_reverify_block(_clean_reverify_state):
    network = server.config.XRPL_NETWORK
    server._reverify_state[network] = {
        "state": "failed",
        "error": "genesis_mismatch",
        "finished_at": 1_800_000_000,
    }
    headers = {"Authorization": "Bearer tok-d"}
    resp = _run(server.handle_sponsored_mint_status(_Request(headers, {})))
    body = json.loads(resp.body)
    assert body["reverify"] == {
        "state": "failed",
        "error": "genesis_mismatch",
        "finished_at": 1_800_000_000,
    }


def test_run_archive_reverify_cancellation_sets_terminal_state_and_reraises(
    monkeypatch, _clean_reverify_state
):
    """asyncio.CancelledError is a BaseException (3.8+), not Exception — a bare
    `except Exception` around the reverify job would swallow a task cancel and
    leave _reverify_state stuck 'running' forever with no audit row. The fix
    must catch CancelledError explicitly, record a terminal 'failed: cancelled'
    state + audit row, and re-raise so the cancellation still propagates."""

    async def hanging_reverify(conn, request_fn, *, network, now=None):
        await asyncio.Event().wait()

    audits: list[str] = []
    monkeypatch.setattr(server.archive_reverify, "reverify_archive", hanging_reverify)
    monkeypatch.setattr(server, "_reverify_client", _fake_ws_client_factory())
    monkeypatch.setattr(
        server.sponsored_mint,
        "audit_archive_reverify",
        lambda db, *, network, actor, result, now=None: audits.append(result),
    )

    async def scenario():
        task = asyncio.create_task(server.run_archive_reverify("testnet", "admin:42"))
        # Let the job start and reach the hang point before cancelling.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert task.cancelled()

    _run(scenario())
    assert server._reverify_state["testnet"] == {
        "state": "failed",
        "error": "cancelled",
        "finished_at": server._reverify_state["testnet"]["finished_at"],
    }
    assert server._reverify_state["testnet"]["finished_at"] is not None
    assert audits == ["failed: cancelled"]


def _fake_ws_client_factory():
    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    return lambda: _FakeClient()


def test_run_archive_reverify_success_path(monkeypatch, tmp_path):
    class _Result:
        ok = True
        reason = None
        ledger_max = 600_000
        provenance = "auto-reverify …"

    async def fake_reverify(conn, request_fn, *, network, now=None):
        return _Result()

    async def fake_wait(path, *, network, **kw):
        return True

    audits: list[str] = []
    monkeypatch.setattr(server.archive_reverify, "reverify_archive", fake_reverify)
    monkeypatch.setattr(server.archive_reverify, "wait_for_archive_usable", fake_wait)
    monkeypatch.setattr(
        server.sponsored_mint,
        "audit_archive_reverify",
        lambda db, *, network, actor, result, now=None: audits.append(result),
    )
    monkeypatch.setattr(server, "_reverify_client", _fake_ws_client_factory())
    _run(server.run_archive_reverify("testnet", "admin:42"))
    assert server._reverify_state["testnet"]["state"] == "ok"
    assert audits == ["ok"]


def test_run_archive_reverify_heartbeat_timeout(monkeypatch, tmp_path):
    class _Result:
        ok = True
        reason = None
        ledger_max = 600_000
        provenance = "auto-reverify …"

    async def fake_reverify(conn, request_fn, *, network, now=None):
        return _Result()

    async def fake_wait(path, *, network, **kw):
        return False

    audits: list[str] = []
    monkeypatch.setattr(server.archive_reverify, "reverify_archive", fake_reverify)
    monkeypatch.setattr(server.archive_reverify, "wait_for_archive_usable", fake_wait)
    monkeypatch.setattr(
        server.sponsored_mint,
        "audit_archive_reverify",
        lambda db, *, network, actor, result, now=None: audits.append(result),
    )
    monkeypatch.setattr(server, "_reverify_client", _fake_ws_client_factory())
    _run(server.run_archive_reverify("testnet", "admin:42"))
    assert server._reverify_state["testnet"]["state"] == "failed"
    assert server._reverify_state["testnet"]["error"].startswith("heartbeat_timeout")
    assert audits and audits[0].startswith("failed: ")


def test_run_archive_reverify_failure_path(monkeypatch, tmp_path):
    class _Result:
        ok = False
        reason = "genesis_mismatch"
        ledger_max = None
        provenance = None

    async def fake_reverify(conn, request_fn, *, network, now=None):
        return _Result()

    async def fake_wait(path, *, network, **kw):
        return True

    audits: list[str] = []
    monkeypatch.setattr(server.archive_reverify, "reverify_archive", fake_reverify)
    monkeypatch.setattr(server.archive_reverify, "wait_for_archive_usable", fake_wait)
    monkeypatch.setattr(
        server.sponsored_mint,
        "audit_archive_reverify",
        lambda db, *, network, actor, result, now=None: audits.append(result),
    )
    monkeypatch.setattr(server, "_reverify_client", _fake_ws_client_factory())
    _run(server.run_archive_reverify("testnet", "admin:42"))
    assert server._reverify_state["testnet"]["state"] == "failed"
    assert server._reverify_state["testnet"]["error"] == "genesis_mismatch"
    assert audits and audits[0].startswith("failed: ")


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
