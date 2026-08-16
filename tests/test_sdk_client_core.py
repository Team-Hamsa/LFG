# tests/test_sdk_client_core.py
import pytest

from surfaces._client.errors import NotFound, ServiceUnavailable
from tests.mock_service import build_mock_service
from tests.sdk_helpers import make_client, run


def test_config_roundtrip():
    async def _inner():
        server, client = await make_client(build_mock_service())
        async with client:
            assert await client.config() == {"ok": True, "network": "testnet"}
        await server.close()

    run(_inner())


def test_qr_and_img_return_bytes():
    async def _inner():
        server, client = await make_client(build_mock_service())
        async with client:
            assert await client.qr_png("HELLO") == b"\x89PNG\r\n"
            assert await client.img("https://cdn/x.png") == b"IMGDATA"
        await server.close()

    run(_inner())


def test_retries_5xx_then_succeeds():
    async def _inner():
        app = build_mock_service(flaky={"/api/config": 2})
        server, client = await make_client(app)
        async with client:
            body = await client.config()  # 503, 503, then 200
            assert body["ok"] is True
            assert app["state"]["hits"]["/api/config"] == 3
        await server.close()

    run(_inner())


def test_exhausted_retries_raise_service_unavailable():
    async def _inner():
        app = build_mock_service(flaky={"/api/config": 9})
        server, client = await make_client(app, max_attempts=2)
        async with client:
            with pytest.raises(ServiceUnavailable):
                await client.config()
        await server.close()

    run(_inner())


def test_4xx_raises_immediately_without_retry():
    async def _inner():
        server, client = await make_client(build_mock_service())
        async with client:
            with pytest.raises(NotFound):
                # a definitely-missing route returns 404 -> NotFound, no retry
                await client._request("GET", "/api/nope")
        await server.close()

    run(_inner())
