import json
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from xrpl.models import IssuedCurrencyAmount

from lfg_core import atomic_mint, xrpl_actions
from lfg_service import app as server


def _request(method: str, path: str, body: dict | None = None):
    request = make_mocked_request(method, path, app=web.Application())

    async def _json():
        return body or {}

    request.json = _json  # type: ignore[method-assign]
    return request


def _body(response: web.Response) -> dict:
    return json.loads(response.body.decode())


@pytest.fixture(autouse=True)
def _action_state(monkeypatch, tmp_path):
    monkeypatch.setattr(server.config, "WEBAPP_DEV_MODE", True)
    monkeypatch.setattr(server, "atomic_mint_sessions", {})
    monkeypatch.setattr(server, "_action_create_hits", {})
    monkeypatch.setattr(
        server.db_path,
        "app_db_path",
        lambda network=None: str(tmp_path / "actions.db"),
    )
    monkeypatch.setattr(server, "_push_token", AsyncMock(return_value=None))


@pytest.mark.asyncio
async def test_action_routes_resolve_before_dynamic_and_static_routes():
    app = server.create_app()
    active = await app.router.resolve(
        _request("GET", "/api/actions/mint/active")
    )
    status = await app.router.resolve(
        _request("GET", "/api/actions/mint/session-1")
    )
    discovery = await app.router.resolve(
        _request("GET", "/.well-known/xrpl-actions.json")
    )
    assert active.handler is server.handle_action_active
    assert status.handler is server.handle_action_status
    assert discovery.handler is server.handle_actions_discovery


@pytest.mark.asyncio
async def test_action_metadata_reports_ledger_gate(monkeypatch):
    monkeypatch.setattr(
        server,
        "_action_readiness",
        AsyncMock(
            return_value=xrpl_actions.BatchCapability(
                False, "batch_unavailable"
            )
        ),
    )
    response = await server.handle_action_metadata(
        _request("GET", "/api/actions/mint")
    )
    body = _body(response)
    assert response.status == 200
    assert body["enabled"] is False
    assert body["unavailableReason"] == "batch_unavailable"
    assert body["transactionTypes"] == [
        "Payment",
        "NFTokenMint",
        "NFTokenAcceptOffer",
    ]


@pytest.mark.asyncio
async def test_create_rejects_body_account_different_from_session_wallet():
    response = await server.handle_action_create(
        _request("POST", "/api/actions/mint", {"account": "rForeign"})
    )
    assert response.status == 403
    assert _body(response)["code"] == "wallet_mismatch"


@pytest.mark.asyncio
async def test_create_returns_202_and_starts_background_preparation(
    monkeypatch,
):
    started = []
    persisted = []
    monkeypatch.setattr(
        server,
        "_action_readiness",
        AsyncMock(return_value=xrpl_actions.BatchCapability(True, None)),
    )
    monkeypatch.setattr(
        server,
        "_persist_atomic_session",
        AsyncMock(side_effect=lambda session: persisted.append(session.id)),
    )
    monkeypatch.setattr(
        server, "_schedule_atomic_mint", lambda session: started.append(session.id)
    )
    response = await server.handle_action_create(
        _request(
            "POST",
            "/api/actions/mint",
            {
                "account": server.mock_economy.DEV_OWNER,
                "campaign": "x-mint-link",
            },
        )
    )
    body = _body(response)
    assert response.status == 202
    assert body["state"] == atomic_mint.PREPARING
    assert persisted == [body["sessionId"]]
    assert started == [body["sessionId"]]


@pytest.mark.asyncio
async def test_create_enforces_one_active_action(monkeypatch):
    monkeypatch.setattr(
        server,
        "_action_readiness",
        AsyncMock(return_value=xrpl_actions.BatchCapability(True, None)),
    )
    existing = atomic_mint.AtomicMintSession.new(
        user_id="dev",
        wallet=server.mock_economy.DEV_OWNER,
        platform="discord",
        network="testnet",
    )
    server.atomic_mint_sessions[existing.id] = existing
    response = await server.handle_action_create(
        _request(
            "POST",
            "/api/actions/mint",
            {"account": server.mock_economy.DEV_OWNER},
        )
    )
    assert response.status == 409
    assert _body(response)["session"]["sessionId"] == existing.id


@pytest.mark.asyncio
async def test_action_create_rate_limit_is_separate_and_wallet_scoped(
    monkeypatch,
):
    monkeypatch.setattr(server.config, "XRPL_ACTIONS_CREATE_LIMIT", 3)
    monkeypatch.setattr(
        server,
        "_action_readiness",
        AsyncMock(return_value=xrpl_actions.BatchCapability(True, None)),
    )
    monkeypatch.setattr(server, "_persist_atomic_session", AsyncMock())
    monkeypatch.setattr(server, "_schedule_atomic_mint", lambda session: None)
    for _ in range(3):
        response = await server.handle_action_create(
            _request(
                "POST",
                "/api/actions/mint",
                {"account": server.mock_economy.DEV_OWNER},
            )
        )
        assert response.status == 202
        server.atomic_mint_sessions.clear()
    limited = await server.handle_action_create(
        _request(
            "POST",
            "/api/actions/mint",
            {"account": server.mock_economy.DEV_OWNER},
        )
    )
    assert limited.status == 429
    assert _body(limited)["code"] == "rate_limited"


@pytest.mark.asyncio
async def test_awaiting_status_exposes_one_canonical_batch(monkeypatch):
    session = atomic_mint.AtomicMintSession.new(
        user_id="dev",
        wallet=server.mock_economy.DEV_OWNER,
        platform="discord",
        network="testnet",
    )
    session.state = atomic_mint.AWAITING_SIGNATURE
    session.batch_json = {
        "TransactionType": "Batch",
        "Account": session.wallet,
        "RawTransactions": [
            {"RawTransaction": {"TransactionType": "Payment"}},
            {"RawTransaction": {"TransactionType": "NFTokenMint"}},
            {
                "RawTransaction": {
                    "TransactionType": "NFTokenAcceptOffer"
                }
            },
        ],
    }
    session.xumm_uuid = "u1"
    session.xumm_url = "https://xumm.app/sign/u1"
    session.qr_url = "https://qr.example/u1.png"
    server.atomic_mint_sessions[session.id] = session
    monkeypatch.setattr(server, "_refresh_atomic_mint", AsyncMock())
    request = _request(
        "GET", f"/api/actions/mint/{session.id}"
    )
    request._match_info = {"session_id": session.id}
    response = await server.handle_action_status(request)
    body = _body(response)
    assert response.status == 200
    assert body["transaction"]["TransactionType"] == "Batch"
    assert [
        row["RawTransaction"]["TransactionType"]
        for row in body["transaction"]["RawTransactions"]
    ] == ["Payment", "NFTokenMint", "NFTokenAcceptOffer"]
    assert body["wallets"]["xaman"]["deeplink"].endswith("/u1")
    assert "accept" not in body


@pytest.mark.asyncio
async def test_status_hides_foreign_session_as_not_found():
    session = atomic_mint.AtomicMintSession.new(
        user_id="someone-else",
        wallet=server.mock_economy.DEV_OWNER,
        platform="discord",
        network="testnet",
    )
    server.atomic_mint_sessions[session.id] = session
    request = _request("GET", f"/api/actions/mint/{session.id}")
    request._match_info = {"session_id": session.id}
    response = await server.handle_action_status(request)
    assert response.status == 404


def test_persisted_action_round_trip_restores_fixed_transaction():
    session = atomic_mint.AtomicMintSession.new(
        user_id="dev",
        wallet=server.mock_economy.DEV_OWNER,
        platform="discord",
        network="testnet",
        campaign="x-mint-link",
    )
    session.state = atomic_mint.AWAITING_SIGNATURE
    session.payment = xrpl_actions.MintPayment(
        "LFGO",
        "1",
        "rrrrrrrrrrrrrrrrrrrrrhoLvTp",
        IssuedCurrencyAmount(
            currency="4C46474F00000000000000000000000000000000",
            issuer="rrrrrrrrrrrrrrrrrrrrrhoLvTp",
            value="1",
        ),
    )
    session.pay_with = "LFGO"
    session.pay_amount = "1"
    session.ticket_sequence = 77
    session.offer_id = "OFFER"
    session.batch_json = {
        "TransactionType": "Batch",
        "Account": session.wallet,
    }
    session.inner_hashes = ("PAY", "MINT", "ACCEPT")
    session.last_ledger_sequence = 500
    session.xumm_uuid = "u1"
    session.xumm_url = "https://xumm.app/sign/u1"
    session.headroom_reserved = True
    session.assets_prepared = True
    server._write_atomic_session(session)

    (restored,) = server._load_atomic_sessions()
    assert restored.id == session.id
    assert restored.batch_json == session.batch_json
    assert restored.inner_hashes == session.inner_hashes
    assert restored.ticket_sequence == 77
    assert isinstance(restored.payment.amount, IssuedCurrencyAmount)
    assert restored.payment.amount.value == "1"
    assert restored.xumm_url == session.xumm_url
    assert restored.headroom_reserved is True
    assert restored.assets_prepared is True


@pytest.mark.asyncio
async def test_startup_loads_and_schedules_reconciliation(monkeypatch):
    session = atomic_mint.AtomicMintSession.new(
        user_id="dev",
        wallet=server.mock_economy.DEV_OWNER,
        platform="discord",
        network="testnet",
    )
    scheduled = []
    monkeypatch.setattr(server, "_load_atomic_sessions", lambda: [session])
    monkeypatch.setattr(
        server,
        "_schedule_atomic_reconciliation",
        lambda value: scheduled.append(value.id),
    )
    await server._start_atomic_resume(web.Application())
    assert server.atomic_mint_sessions[session.id] is session
    assert scheduled == [session.id]
