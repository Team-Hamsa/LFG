# tests/test_sdk_account_link.py
# SDK coverage for the #90 account view + cross-surface link flow.
import asyncio

from surfaces._client.client import LFGServiceClient
from tests.mock_service import build_mock_service
from tests.sdk_helpers import make_client, run


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---- Task 4: account() ----


def test_account_calls_endpoint():
    async def _inner():
        app = build_mock_service()
        server, client = await make_client(app)
        async with client:
            body = await client.account("42", username="neo")
            assert body["wallet"] == "rMOCK"
            assert app["state"]["hits"]["/api/account"] == 1
        await server.close()

    run(_inner())


# ---- Task 6: link_start / link_status / wait_for_link ----


def test_link_start_sends_link_flag():
    async def _inner():
        app = build_mock_service()
        server, client = await make_client(app)
        async with client:
            out = await client.link_start("42", username="neo")
            assert out["uuid"] == "sg1"
            assert app["state"]["last_signin_link_flag"] is True
        await server.close()

    run(_inner())


def test_link_status_hits_signin_path():
    async def _inner():
        app = build_mock_service()
        server, client = await make_client(app)
        async with client:
            out = await client.link_status("42", "sg1")
            assert out["signed"] is True
        await server.close()

    run(_inner())


def test_wait_for_link_polls_to_signed(monkeypatch):
    c = LFGServiceClient("http://svc", "tok", "telegram")
    states = [
        {"state": "pending"},
        {"state": "signed", "wallet": "rXRPL", "account": {"wallet": "rXRPL", "identities": []}},
    ]

    async def fake_status(user_id, uuid):
        return states.pop(0)

    async def no_sleep(_):
        return None

    monkeypatch.setattr(c, "link_status", fake_status)
    out = _run(c.wait_for_link("55", "u1", interval=0, sleep=no_sleep))
    assert out["state"] == "signed"
    assert out["account"]["wallet"] == "rXRPL"
