# surfaces/_client/client.py
# LFGServiceClient: the async client every surface shares. Owns one aiohttp
# ClientSession; wraps the lfg_service REST + WS contract with retry, typed
# errors, and a per-user session-token cache (added in Task 4).

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from types import TracebackType
from typing import Any, Literal, overload

import aiohttp

from ._retry import RETRY_BASE_DELAY, RETRY_MAX_ATTEMPTS, with_retry
from .errors import AuthError, ServiceError, ServiceUnavailable, error_for

# Terminal states copied from lfg_core.mint_flow / swap_flow TERMINAL_STATES.
# Duplicated (not imported) so the SDK never depends on lfg_core.
MINT_TERMINAL: frozenset[str] = frozenset({"offer_ready", "done", "failed", "payment_timeout"})
SWAP_TERMINAL: frozenset[str] = frozenset({"done", "failed", "offers_ready", "payment_timeout"})


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

    # ---- identity / session ----

    async def create_session(self, user_id: str, username: str = "") -> str:
        body = await self._request(
            "POST",
            "/api/session",
            token=self._service_token,
            json={"platform_user_id": user_id, "platform_username": username},
        )
        return body["session_token"]

    async def _session_token(self, user_id: str, username: str = "") -> str:
        token = self._user_sessions.get(user_id)
        if token is None:
            token = await self.create_session(user_id, username)
            self._user_sessions[user_id] = token
        return token

    async def _user_request(
        self, method: str, path: str, user_id: str, *, username: str = "", **kw: Any
    ) -> Any:
        token = await self._session_token(user_id, username)
        try:
            return await self._request(method, path, token=token, **kw)
        except AuthError:
            # cached session rejected: evict, re-mint once, retry exactly once
            self._user_sessions.pop(user_id, None)
            token = await self._session_token(user_id, username)
            return await self._request(method, path, token=token, **kw)

    async def register(self, user_id: str, username: str, wallet: str) -> dict[str, Any]:
        return await self._user_request(
            "POST", "/api/register", user_id, username=username, json={"wallet": wallet}
        )

    async def me(self, user_id: str, *, username: str = "") -> dict[str, Any]:
        return await self._user_request("GET", "/api/me", user_id, username=username)

    # ---- mint ----

    async def start_mint(self, user_id: str, *, username: str = "") -> dict[str, Any]:
        return await self._user_request("POST", "/api/mint", user_id, username=username)

    async def mint_status(self, user_id: str, session_id: str) -> dict[str, Any]:
        return await self._user_request("GET", f"/api/mint/{session_id}", user_id)

    async def regenerate(self, user_id: str, session_id: str) -> dict[str, Any]:
        return await self._user_request("POST", f"/api/mint/{session_id}/regenerate", user_id)

    async def wait_for_mint(
        self,
        user_id: str,
        session_id: str,
        *,
        interval: float = 2.0,
        timeout: float = 180.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> dict[str, Any]:
        return await self._poll(
            lambda: self.mint_status(user_id, session_id), MINT_TERMINAL, interval, timeout, sleep
        )

    # ---- swap ----

    async def start_swap(
        self, user_id: str, nft1_id: str, nft2_id: str, traits: list[str], *, username: str = ""
    ) -> dict[str, Any]:
        return await self._user_request(
            "POST",
            "/api/swap",
            user_id,
            username=username,
            json={"nft1_id": nft1_id, "nft2_id": nft2_id, "traits": traits},
        )

    async def swap_status(self, user_id: str, session_id: str) -> dict[str, Any]:
        return await self._user_request("GET", f"/api/swap/{session_id}", user_id)

    async def wait_for_swap(
        self,
        user_id: str,
        session_id: str,
        *,
        interval: float = 2.0,
        timeout: float = 180.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> dict[str, Any]:
        return await self._poll(
            lambda: self.swap_status(user_id, session_id), SWAP_TERMINAL, interval, timeout, sleep
        )

    async def _poll(
        self,
        fetch: Callable[[], Awaitable[dict[str, Any]]],
        terminal: frozenset[str],
        interval: float,
        timeout: float,
        sleep: Callable[[float], Awaitable[None]],
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            status = await fetch()
            if status.get("state") in terminal:
                return status
            if time.monotonic() >= deadline:
                return status  # caller inspects non-terminal state on timeout
            await sleep(interval)

    # ---- sign-in / nfts / economy ----

    async def signin_start(self, user_id: str, *, username: str = "") -> dict[str, Any]:
        return await self._user_request("POST", "/api/signin", user_id, username=username)

    async def signin_status(self, user_id: str, payload_uuid: str) -> dict[str, Any]:
        return await self._user_request("GET", f"/api/signin/{payload_uuid}", user_id)

    async def nfts(self, user_id: str) -> dict[str, Any]:
        return await self._user_request("GET", "/api/nfts", user_id)

    async def economy(self, user_id: str) -> dict[str, Any]:
        return await self._user_request("GET", "/api/economy", user_id)

    # ---- trait-economy ops ----

    async def equip_start(self, user_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return await self._user_request("POST", "/api/equip", user_id, json=body)

    async def equip_status(self, user_id: str, session_id: str) -> dict[str, Any]:
        return await self._user_request("GET", f"/api/equip/{session_id}", user_id)

    async def harvest_start(self, user_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return await self._user_request("POST", "/api/harvest", user_id, json=body)

    async def harvest_status(self, user_id: str, session_id: str) -> dict[str, Any]:
        return await self._user_request("GET", f"/api/harvest/{session_id}", user_id)

    async def assemble_start(self, user_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return await self._user_request("POST", "/api/assemble", user_id, json=body)

    async def assemble_status(self, user_id: str, session_id: str) -> dict[str, Any]:
        return await self._user_request("GET", f"/api/assemble/{session_id}", user_id)
