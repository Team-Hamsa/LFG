# tests/test_sdk_events.py
import asyncio

import pytest
from aiohttp.test_utils import TestServer

from surfaces._client.client import LFGServiceClient
from surfaces._client.errors import AuthError
from surfaces._client.events import stream_events
from tests.mock_service import SERVICE_TOKEN, build_mock_service
from tests.sdk_helpers import make_client, run


def test_events_raises_auth_error_on_bad_service_token():
    async def _inner():
        app = build_mock_service()  # /events requires the real SERVICE_TOKEN
        server = TestServer(app)
        await server.start_server()
        base = str(server.make_url("")).rstrip("/")
        client = LFGServiceClient(base, "WRONG-TOKEN", "test", base_delay=0.0)
        async with client:
            agen = client.events()
            with pytest.raises(AuthError):
                await agen.__anext__()
            await agen.aclose()
        await server.close()

    run(_inner())


def test_events_yields_across_a_reconnect():
    # connection 1 emits evt #1 then the mock closes the WS (forcing reconnect);
    # connection 2 emits evt #2.
    script = {
        1: [
            {"type": "mint.completed", "ts": 1, "identity": None, "wallet": "rA", "data": {"n": 1}}
        ],
        2: [{"type": "mint.failed", "ts": 2, "identity": None, "wallet": "rB", "data": {"n": 2}}],
    }

    async def _inner():
        app = build_mock_service(events_script=script)
        server, client = await make_client(app)
        received = []
        async with client:
            agen = client.events(types=["mint.completed", "mint.failed"])
            for _ in range(2):
                received.append(await asyncio.wait_for(agen.__anext__(), timeout=2))
            await agen.aclose()
        await server.close()
        return app, received

    app, received = run(_inner())
    assert [e.data["n"] for e in received] == [1, 2]
    assert received[0].type == "mint.completed"
    assert app["state"]["last_event_types"] == "mint.completed,mint.failed"


def test_events_stops_cleanly_when_client_closed():
    # FIX 3: closing the client while a generator is live should end the generator
    # cleanly (StopAsyncIteration), not raise RuntimeError("Session is closed").
    async def _inner():
        app = build_mock_service(events_script={})  # server always closes immediately
        server, client = await make_client(app)
        async with client:
            agen = client.events()
            # Close the underlying aiohttp session while the generator is live
            await client.close()
            try:
                await asyncio.wait_for(agen.__anext__(), timeout=1.0)
                result = "yielded"
            except StopAsyncIteration:
                result = "stop"
            except RuntimeError:
                result = "runtime_error"
            finally:
                await agen.aclose()
        await server.close()
        return result

    result = run(_inner())
    assert result == "stop", f"Expected StopAsyncIteration (clean stop), got: {result}"


class _StopBackoffTest(Exception):
    """Sentinel exception to break out of the reconnect loop in the backoff test."""


def test_backoff_grows_when_server_immediately_closes():
    # FIX 4: when the server accepts but immediately closes without sending any
    # messages, backoff must grow exponentially (not reset on each empty connect).
    async def _inner():
        app = build_mock_service(events_script={})  # every conn: accept then close immediately
        server, client = await make_client(app)

        recorded_delays: list[float] = []
        call_count = 0

        async def recording_sleep(delay: float) -> None:
            nonlocal call_count
            call_count += 1
            recorded_delays.append(delay)
            if call_count >= 3:
                raise _StopBackoffTest  # break out after 3 reconnect cycles

        base = str(server.make_url("")).rstrip("/")
        async with client:
            session = client._require_session()
            try:
                async for _ in stream_events(
                    session,
                    base,
                    SERVICE_TOKEN,
                    None,
                    base_delay=0.1,
                    sleep=recording_sleep,
                ):
                    pass  # no messages expected
            except _StopBackoffTest:
                pass  # raised by recording_sleep to stop the loop

        await server.close()
        return recorded_delays

    delays = run(_inner())
    assert len(delays) >= 3, f"Expected at least 3 recorded delays, got {delays}"
    # Each delay must be strictly greater than the previous (exponential growth)
    for i in range(1, len(delays)):
        assert delays[i] > delays[i - 1], (
            f"Backoff not growing: delays[{i - 1}]={delays[i - 1]}, delays[{i}]={delays[i]}"
        )
