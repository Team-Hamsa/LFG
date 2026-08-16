# Service-token auth: gates which trusted surface PROCESS may call the API.
# Distinct from end-user (HMAC session) auth — see webapp session tokens.

import os
from collections.abc import Callable, Coroutine
from typing import Any

from aiohttp import web


def service_tokens() -> dict[str, str]:
    """token -> surface name, from SERVICE_TOKEN_<SURFACE> env vars."""
    out: dict[str, str] = {}
    prefix = "SERVICE_TOKEN_"
    for key, value in os.environ.items():
        if key.startswith(prefix) and value:
            out[value] = key[len(prefix) :].lower()
    return out


def surface_for_token(token: str | None) -> str | None:
    if not token:
        return None
    return service_tokens().get(token)


def _bearer(request: Any) -> str | None:
    header: str = request.headers.get("Authorization", "")
    if header.startswith("Bearer "):
        return header[len("Bearer ") :]
    return None


def require_service_token(
    handler: Callable[..., Coroutine[Any, Any, Any]],
) -> Callable[..., Coroutine[Any, Any, Any]]:
    async def wrapper(request: Any) -> Any:
        surface = surface_for_token(_bearer(request))
        if not surface:
            return web.json_response(
                {"error": "unauthorized", "code": "bad_service_token"}, status=401
            )
        request["surface"] = surface
        return await handler(request)

    return wrapper
