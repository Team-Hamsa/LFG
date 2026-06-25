# surfaces/_client/client.py
# LFGServiceClient: the async client every surface shares. Owns one aiohttp
# ClientSession; wraps the lfg_service REST + WS contract with retry, typed
# errors, and a per-user session-token cache (added in Task 4).

from __future__ import annotations

import asyncio
from types import TracebackType
from typing import Any, Literal, overload

import aiohttp

from ._retry import RETRY_BASE_DELAY, RETRY_MAX_ATTEMPTS, with_retry
from .errors import ServiceError, ServiceUnavailable, error_for


class LFGServiceClient:
    def __init__(
        self,
        base_url: str,
        service_token: str,
        surface: str,
        *,
        timeout: float = 30.0,
        max_attempts: int = RETRY_MAX_ATTEMPTS,
        base_delay: float = RETRY_BASE_DELAY,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._service_token = service_token
        self._surface = surface
        self._timeout = timeout
        self._max_attempts = max_attempts
        self._base_delay = base_delay
        self._session: aiohttp.ClientSession | None = None
        self._user_sessions: dict[str, str] = {}  # user_id -> session token (Task 4)

    async def __aenter__(self) -> LFGServiceClient:
        self._session = aiohttp.ClientSession()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    def _require_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            raise RuntimeError("LFGServiceClient must be used as an async context manager")
        return self._session

    @staticmethod
    def _retryable(exc: Exception) -> bool:
        if isinstance(exc, ServiceError):
            return exc.status is not None and (exc.status >= 500 or exc.status == 429)
        return isinstance(exc, (aiohttp.ClientError, asyncio.TimeoutError))

    @overload
    async def _request(
        self,
        method: str,
        path: str,
        *,
        token: str | None = ...,
        json: dict[str, Any] | None = ...,
        params: dict[str, str] | None = ...,
        expect: Literal["json"] = ...,
    ) -> dict[str, Any]: ...

    @overload
    async def _request(
        self,
        method: str,
        path: str,
        *,
        token: str | None = ...,
        json: dict[str, Any] | None = ...,
        params: dict[str, str] | None = ...,
        expect: Literal["bytes"],
    ) -> bytes: ...

    async def _request(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        json: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
        expect: str = "json",
    ) -> Any:
        session = self._require_session()
        url = self._base + path
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        timeout = aiohttp.ClientTimeout(total=self._timeout)

        async def attempt() -> Any:
            async with session.request(
                method, url, json=json, params=params, headers=headers, timeout=timeout
            ) as resp:
                if resp.status >= 400:
                    try:
                        body = await resp.json()
                    except Exception:
                        body = None
                    raise error_for(resp.status, body) or ServiceError(f"HTTP {resp.status}")
                if expect == "bytes":
                    return await resp.read()
                return await resp.json()

        try:
            return await with_retry(
                attempt,
                max_attempts=self._max_attempts,
                base_delay=self._base_delay,
                retryable=self._retryable,
            )
        except ServiceError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise ServiceUnavailable(str(exc), code="network_error", status=None) from exc

    # ---- public (no-auth) endpoints ----

    async def config(self) -> dict[str, Any]:
        return await self._request("GET", "/api/config")

    async def qr_png(self, data: str) -> bytes:
        return await self._request("GET", "/api/qr.png", params={"d": data}, expect="bytes")

    async def img(self, url: str) -> bytes:
        return await self._request("GET", "/api/img", params={"u": url}, expect="bytes")
