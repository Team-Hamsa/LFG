# surfaces/_client/events.py
# Reconnecting /events subscription. Exposed as an async iterator so a dropped
# WebSocket is invisible to the caller — the loop reconnects with backoff and
# resumes yielding.

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable

import aiohttp

from lfg_service.events import Event  # shared event dataclass (allowed cross-import)

_MAX_BACKOFF = 30.0


async def stream_events(
    session: aiohttp.ClientSession,
    base_url: str,
    service_token: str,
    types: list[str] | None,
    *,
    base_delay: float,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> AsyncIterator[Event]:
    url = base_url + "/events"
    params = {"token": service_token}
    if types:
        params["types"] = ",".join(types)
    backoff = base_delay
    while True:
        try:
            async with session.ws_connect(url, params=params) as ws:
                backoff = base_delay  # reset after a successful connect
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        d = msg.json()
                        yield Event(
                            type=d["type"],
                            ts=d["ts"],
                            identity=d.get("identity"),
                            wallet=d.get("wallet"),
                            data=d.get("data", {}),
                        )
                    elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                        break
        except (aiohttp.ClientError, asyncio.TimeoutError):
            pass
        # connection ended or dropped -> reconnect after backoff
        await sleep(backoff)
        backoff = min(backoff * 2, _MAX_BACKOFF) if backoff else base_delay
