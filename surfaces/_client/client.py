# surfaces/_client/client.py
# LFGServiceClient: the async client every surface shares. Owns one aiohttp
# ClientSession; wraps the lfg_service REST + WS contract with retry, typed
# errors, and a per-user session-token cache (added in Task 4).

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from types import TracebackType
from typing import Any, Literal, overload

import aiohttp

from lfg_service.events import Event

from ._retry import RETRY_BASE_DELAY, RETRY_MAX_ATTEMPTS, with_retry
from .errors import AuthError, ServiceError, ServiceUnavailable, error_for
from .events import stream_events

# Terminal states copied from lfg_core.mint_flow / swap_flow TERMINAL_STATES.
# Duplicated (not imported) so the SDK never depends on lfg_core.
MINT_TERMINAL: frozenset[str] = frozenset(
    {"offer_ready", "done", "failed", "payment_timeout", "cancelled"}
)
SWAP_TERMINAL: frozenset[str] = frozenset({"done", "failed", "offers_ready", "payment_timeout"})
SIGNIN_TERMINAL: frozenset[str] = frozenset({"signed", "expired"})


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
        self._session_locks: dict[str, asyncio.Lock] = {}  # per-user lock for double-checked mint

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
        # Rate limiting (HTTP 429, or any response coded "rate_limited" — the
        # service says that with 503 + Retry-After when XUMM itself is limiting
        # us) is deliberate back-pressure: retrying multiplies the very
        # overload it signals. The 2026-07-17 XUMM 429 incident was a handful
        # of sign-ins amplified 5x by this retry loop. Plain 5xx stays
        # retryable — those are genuinely transient.
        if isinstance(exc, ServiceError):
            if exc.code == "rate_limited":
                return False
            return exc.status is not None and exc.status >= 500
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
        session_token: str = body["session_token"]
        return session_token

    async def _session_token(self, user_id: str, username: str = "") -> str:
        # Fast path: already cached (no lock needed for read).
        token = self._user_sessions.get(user_id)
        if token is not None:
            return token
        # Slow path: lazily create a per-user lock and double-check inside it so
        # only the first concurrent caller mints a session; the rest reuse it.
        if user_id not in self._session_locks:
            self._session_locks[user_id] = asyncio.Lock()
        async with self._session_locks[user_id]:
            token = self._user_sessions.get(user_id)  # re-check inside lock
            if token is None:
                token = await self.create_session(user_id, username)
                self._user_sessions[user_id] = token
        return token

    async def _user_request(
        self, method: str, path: str, user_id: str, *, username: str = "", **kw: Any
    ) -> dict[str, Any]:
        token = await self._session_token(user_id, username)
        try:
            result: dict[str, Any] = await self._request(method, path, token=token, **kw)
            return result
        except AuthError:
            # cached session rejected: evict only if we still hold the stale token
            # (another concurrent 401-refresh may have already replaced it).
            if user_id not in self._session_locks:
                self._session_locks[user_id] = asyncio.Lock()
            stale_token = token
            async with self._session_locks[user_id]:
                if self._user_sessions.get(user_id) == stale_token:
                    # We still own the stale entry — re-mint.
                    self._user_sessions.pop(user_id, None)
                    new_token = await self.create_session(user_id, username)
                    self._user_sessions[user_id] = new_token
            # Always retry with whatever is now cached (freshly minted or
            # already refreshed by a concurrent caller).
            token = await self._session_token(user_id, username)
            result = await self._request(method, path, token=token, **kw)
            return result

    async def register(self, user_id: str, username: str, wallet: str) -> dict[str, Any]:
        return await self._user_request(
            "POST", "/api/register", user_id, username=username, json={"wallet": wallet}
        )

    async def me(self, user_id: str, *, username: str = "") -> dict[str, Any]:
        return await self._user_request("GET", "/api/me", user_id, username=username)

    async def account(self, user_id: str, *, username: str = "") -> dict[str, Any]:
        """The caller's account view: {wallet, identities} (#90)."""
        return await self._user_request("GET", "/api/account", user_id, username=username)

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

    async def wait_for_signin(
        self,
        user_id: str,
        uuid: str,
        *,
        interval: float = 2.0,
        timeout: float = 180.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> dict[str, Any]:
        return await self._poll(
            lambda: self.signin_status(user_id, uuid), SIGNIN_TERMINAL, interval, timeout, sleep
        )

    # ---- cross-surface link (#90) ----
    # Proving the SAME wallet on a 2nd surface IS the link. These reuse the
    # sign-in machinery with a link=true flag; the service adds an "account"
    # view to the signed response so the surface can confirm the linked handles.

    async def link_start(self, user_id: str, *, username: str = "") -> dict[str, Any]:
        return await self._user_request(
            "POST", "/api/signin", user_id, username=username, json={"link": True}
        )

    async def link_status(self, user_id: str, uuid: str) -> dict[str, Any]:
        return await self._user_request("GET", f"/api/signin/{uuid}", user_id)

    async def wait_for_link(
        self,
        user_id: str,
        uuid: str,
        *,
        interval: float = 2.0,
        timeout: float = 180.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> dict[str, Any]:
        return await self._poll(
            lambda: self.link_status(user_id, uuid), SIGNIN_TERMINAL, interval, timeout, sleep
        )

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

    # ---- events ----

    async def events(self, types: list[str] | None = None) -> AsyncGenerator[Event, None]:
        """Subscribe to the /events service-token firehose.

        The iterator is **infinite** and reconnects transparently on dropped
        connections.  The consumer MUST either run this in a cancellable task
        or call ``aclose()`` on the generator to release the open WebSocket;
        otherwise the connection leaks until the client is closed.

        Args:
            types: optional list of event type names to filter (e.g.
                ``["mint.completed", "mint.failed"]``).  ``None`` receives all
                event types.

        Raises:
            AuthError: immediately (no retry) when the /events handshake is
                rejected with HTTP 401 or 403.
        """
        # FIX 3: if the client was closed before the generator is first iterated,
        # stop cleanly rather than raising RuntimeError from _require_session.
        if self._session is None or self._session.closed:
            return
        session = self._require_session()
        async for event in stream_events(
            session, self._base, self._service_token, types, base_delay=self._base_delay
        ):
            yield event

    # ---- admin: X (Twitter) posting pause/resume (Task 7, #41) ----
    # Process-level admin actions, no end-user identity involved — same
    # direct _request(token=self._service_token, ...) shape as create_session,
    # not the per-user _user_request path.

    async def x_status(self) -> dict[str, Any]:
        return await self._request("GET", "/api/admin/x/status", token=self._service_token)

    async def x_pause(self) -> dict[str, Any]:
        return await self._request("POST", "/api/admin/x/pause", token=self._service_token)

    async def x_resume(self) -> dict[str, Any]:
        return await self._request("POST", "/api/admin/x/resume", token=self._service_token)
