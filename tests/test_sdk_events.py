# tests/test_sdk_events.py
import asyncio

from tests.mock_service import build_mock_service
from tests.sdk_helpers import make_client, run


def test_events_yields_across_a_reconnect():
    # connection 1 emits evt #1 then the mock closes the WS (forcing reconnect);
    # connection 2 emits evt #2.
    script = {
        1: [{"type": "mint.completed", "ts": 1, "identity": None, "wallet": "rA", "data": {"n": 1}}],
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
