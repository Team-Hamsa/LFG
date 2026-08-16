# tests/test_sdk_x_admin.py
# LFGServiceClient.x_status/x_pause/x_resume (Task 7, #41) — the three thin
# admin methods that mirror create_session's request shape: a direct
# _request(token=self._service_token, ...) call, bypassing the per-user
# _user_request/session-token cache entirely (no user_id in play). Mirrors
# tests/test_sdk_sessions.py's mock_service + sdk_helpers pattern.
import pytest
from aiohttp.test_utils import TestServer

from surfaces._client.client import LFGServiceClient
from surfaces._client.errors import AuthError
from tests.mock_service import SERVICE_TOKEN, build_mock_service
from tests.sdk_helpers import make_client, run


def test_x_status_uses_service_token_no_user_session():
    async def _inner():
        app = build_mock_service()
        server, client = await make_client(app)
        async with client:
            status = await client.x_status()
            assert status == {
                "paused": False,
                "month_posts": 3,
                "budget": 100,
                "enabled": True,
            }
            # No /api/session mint — this is a process-level call, not a
            # per-user one (the create_session-style direct _request shape).
            assert app["state"]["session_hits"] == 0
        await server.close()

    run(_inner())


def test_x_pause_then_status_then_resume_round_trip():
    async def _inner():
        app = build_mock_service()
        server, client = await make_client(app)
        async with client:
            paused = await client.x_pause()
            assert paused == {"paused": True}

            status = await client.x_status()
            assert status["paused"] is True

            resumed = await client.x_resume()
            assert resumed == {"paused": False}

            status2 = await client.x_status()
            assert status2["paused"] is False
        await server.close()

    run(_inner())


def test_x_admin_methods_hit_expected_paths():
    async def _inner():
        app = build_mock_service()
        server, client = await make_client(app)
        async with client:
            await client.x_status()
            await client.x_pause()
            await client.x_resume()
        await server.close()
        return app

    app = run(_inner())
    assert app["state"]["hits"]["/api/admin/x/status"] == 1
    assert app["state"]["hits"]["/api/admin/x/pause"] == 1
    assert app["state"]["hits"]["/api/admin/x/resume"] == 1


def test_x_status_rejects_bad_service_token():
    async def _inner():
        app = build_mock_service()
        server = TestServer(app)
        await server.start_server()
        base = str(server.make_url("")).rstrip("/")
        client = LFGServiceClient(base, "WRONG", "test", base_delay=0.0)
        async with client:
            with pytest.raises(AuthError):
                await client.x_status()
        await server.close()

    run(_inner())


def test_service_token_constant_is_accepted():
    # Sanity: SERVICE_TOKEN (used implicitly by make_client) is what the mock
    # checks — guards against the fixture and the mock silently drifting apart.
    async def _inner():
        app = build_mock_service()
        server, client = await make_client(app)
        assert client._service_token == SERVICE_TOKEN  # noqa: SLF001
        async with client:
            await client.x_status()
        await server.close()

    run(_inner())
