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
    # FIX 2: token sent via Authorization header, not ?token= query param (avoids log leaks)
    params: dict[str, str] | None = {"types": ",".join(types)} if types else None
    headers = {"Authorization": f"Bearer {service_token}"}
    backoff = base_delay
    while True:
        # FIX 3: if the client was closed while the generator was live, stop cleanly
        if session.closed:
            return
        try:
            async with session.ws_connect(url, headers=headers, params=params) as ws:
                # FIX 4: only reset backoff after receiving at least one message
                got_message = False
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        d = msg.json()
                        if not got_message:
                            backoff = base_delay  # reset backoff on first real message
                            got_message = True
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
        except RuntimeError:
            # FIX 3: session.closed raises RuntimeError("Session is closed") from
            # ws_connect — treat as a clean stop rather than leaking the exception
            return
        # connection ended or dropped -> reconnect after backoff
        await sleep(backoff)
        backoff = min(backoff * 2, _MAX_BACKOFF) if backoff else base_delay
