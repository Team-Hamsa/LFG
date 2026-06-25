# surfaces/_client/events.py
# Reconnecting /events subscription. Exposed as an async iterator so a dropped
# WebSocket is invisible to the caller — the loop reconnects with backoff and
# resumes yielding.

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable

import aiohttp

from lfg_service.events import Event  # shared event dataclass (allowed cross-import)

from .errors import AuthError

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
    """Reconnecting /events WebSocket subscription.

    The iterator is **infinite** and reconnects transparently on dropped
    connections or transient server errors.  The consumer MUST either run this
    in a cancellable task or call ``aclose()`` on the generator to release the
    open WebSocket; otherwise the connection leaks until the client is closed.

    Raises:
        AuthError: immediately (no retry) when the /events handshake is
            rejected with HTTP 401 or 403, indicating a bad or expired
            ``service_token``.
    """
    url = base_url + "/events"
    params: dict[str, str] = {"token": service_token}
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
        except aiohttp.WSServerHandshakeError as exc:
            if exc.status in (401, 403):
                raise AuthError(
                    "events subscription rejected",
                    code="bad_service_token",
                    status=exc.status,
                ) from exc
            # transient handshake failure (e.g. 5xx) -> fall through to reconnect
        except (aiohttp.ClientError, asyncio.TimeoutError):
            pass
        # connection ended or dropped -> reconnect after backoff
        await sleep(backoff)
        backoff = min(backoff * 2, _MAX_BACKOFF) if backoff else base_delay
