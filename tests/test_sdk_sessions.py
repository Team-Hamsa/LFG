import pytest
from aiohttp.test_utils import TestServer

from surfaces._client.client import LFGServiceClient
from surfaces._client.errors import AuthError
from tests.mock_service import build_mock_service
from tests.sdk_helpers import make_client, run


def test_session_minted_once_and_reused():
    async def _inner():
        app = build_mock_service()
        server, client = await make_client(app)
        async with client:
            await client.me("42", username="neo")
            await client.me("42")
            await client.register("42", "neo", "rWALLET")
            assert app["state"]["session_hits"] == 1  # one mint for user 42, reused
        await server.close()

    run(_inner())


def test_distinct_users_get_distinct_sessions():
    async def _inner():
        app = build_mock_service()
        server, client = await make_client(app)
        async with client:
            await client.me("1")
            await client.me("2")
            assert app["state"]["session_hits"] == 2
        await server.close()

    run(_inner())


def test_401_triggers_refresh_and_retry():
    async def _inner():
        app = build_mock_service(expire_session_once=True)
        server, client = await make_client(app)
        async with client:
            body = await client.me("42", username="neo")  # first call 401s, then refresh succeeds
            assert body["wallet"] == "rMOCK"
            assert app["state"]["session_hits"] == 2  # initial mint + one refresh
        await server.close()

    run(_inner())


def test_bad_service_token_raises_auth_error():
    async def _inner():
        app = build_mock_service()
        server = TestServer(app)
        await server.start_server()
        base = str(server.make_url("")).rstrip("/")
        client = LFGServiceClient(base, "WRONG", "test", base_delay=0.0)
        async with client:
            with pytest.raises(AuthError):
                await client.create_session("42", "neo")
        await server.close()

    run(_inner())
