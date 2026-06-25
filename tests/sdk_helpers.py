# tests/sdk_helpers.py
# Shared event-loop driver + client/server builder for the SDK test suite.
# The repo has no pytest-asyncio; tests stay sync and drive coroutines here.

import asyncio
from collections.abc import Coroutine
from typing import Any, TypeVar

from aiohttp import web
from aiohttp.test_utils import TestServer

from surfaces._client.client import LFGServiceClient
from tests.mock_service import SERVICE_TOKEN

T = TypeVar("T")


def run(coro: Coroutine[Any, Any, T]) -> T:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def make_client(app: web.Application, **kw: Any) -> tuple[TestServer, LFGServiceClient]:
    server = TestServer(app)
    await server.start_server()
    base = str(server.make_url("")).rstrip("/")
    return server, LFGServiceClient(base, SERVICE_TOKEN, "test", base_delay=0.0, **kw)


async def noop_sleep(_delay: float) -> None:
    return None
