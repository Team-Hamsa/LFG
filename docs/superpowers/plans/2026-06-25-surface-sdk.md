# Surface SDK Implementation Plan (Spine Plan 2 of 4)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `surfaces/_client/` — one async Python client (`LFGServiceClient`) that wraps the entire `lfg_service` REST + WebSocket contract so Discord (Plan 3) and Telegram (Plan 4) become thin adapters with no client plumbing of their own.

**Architecture:** A single client class owns one `aiohttp.ClientSession` for REST and a reconnecting WebSocket for events. It holds a per-surface service token, lazily mints + caches a per-user HMAC session token (refreshing on 401), retries transient REST failures with backoff, maps `{error, code}` responses to typed exceptions, and exposes `/events` as an auto-reconnecting async iterator. Built bottom-up: pure primitives (errors, retry) → client core + a mock service → auth/session cache → endpoint method groups → events iterator → public exports.

**Tech Stack:** Python 3.10, aiohttp (client + test server), pytest + pytest-asyncio (`asyncio_mode=auto`, already configured in Plan 1).

**Spec:** `docs/superpowers/specs/2026-06-25-surface-sdk-design.md`

## Global Constraints

- Python version floor: **3.10** (uses `X | None` unions, `dict[...]` / `list[...]` generics).
- **Async-only.** No synchronous client. Use `aiohttp` (already a dependency) for both REST and WS — **no new runtime dependency**.
- **The SDK must NOT import `lfg_core`** (keeps SDK tests fast and isolated). It **MAY** import `lfg_service.events.Event` — the one event dataclass shared across the wire.
- **Do not import retry constants from `main.py`** (legacy bot). The SDK reads the same env knobs itself: `RETRY_MAX_ATTEMPTS` (default `5`) and `RETRY_BASE_DELAY` (default `1.0`) via `os.getenv`.
- **Auth model (from the live service):** user-scoped endpoints use `require_auth`/`require_wallet` and expect `Authorization: Bearer <user-session-token>`. `POST /api/session` and `GET /events` expect `Authorization: Bearer <service-token>` (or `?token=` for the WS). `GET /api/config`, `/api/qr.png`, `/api/img` are public (no auth).
- All blocking sleeps in retry/reconnect use an **injectable `sleep`** parameter (default `asyncio.sleep`) so tests run without real delays.
- Tests follow the repo-native async style already in `tests/` (`async def test_*` + aiohttp `TestServer`/`TestClient`), mirroring `tests/test_event_endpoints.py`.
- Pre-commit gate is real and blocking (ruff, ruff-format, mypy strict, gitleaks, pytest). Run `pre-commit run --files <changed>` before each commit; types must satisfy mypy strict (annotate everything).

---

### Task 1: Error hierarchy + status→exception mapping

**Files:**
- Create: `surfaces/__init__.py` (empty package marker)
- Create: `surfaces/_client/__init__.py` (empty for now; exports added in Task 8)
- Create: `surfaces/_client/errors.py`
- Test: `tests/test_sdk_errors.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `class ServiceError(Exception)` with attributes `message: str`, `code: str | None`, `status: int | None`.
  - Subclasses `BadRequest`, `AuthError`, `NotFound`, `ServiceUnavailable` (all extend `ServiceError`).
  - `error_for(status: int, body: dict | None) -> ServiceError | None` — returns the right exception for a `>= 400` status (parsing `error`/`code` from `body`), or `None` for `< 400`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_sdk_errors.py
from surfaces._client.errors import (
    AuthError,
    BadRequest,
    NotFound,
    ServiceError,
    ServiceUnavailable,
    error_for,
)


def test_error_for_returns_none_for_success():
    assert error_for(200, {"ok": True}) is None
    assert error_for(204, None) is None


def test_error_for_maps_status_and_parses_body():
    err = error_for(400, {"error": "bad input", "code": "bad_request"})
    assert isinstance(err, BadRequest)
    assert err.message == "bad input"
    assert err.code == "bad_request"
    assert err.status == 400


def test_error_for_maps_401_and_404():
    assert isinstance(error_for(401, {"error": "nope", "code": "bad_session"}), AuthError)
    assert isinstance(error_for(404, {"error": "gone", "code": "not_found"}), NotFound)


def test_error_for_5xx_is_service_unavailable():
    err = error_for(503, None)
    assert isinstance(err, ServiceUnavailable)
    assert err.status == 503


def test_error_for_unmapped_4xx_is_base_service_error():
    err = error_for(418, {"error": "teapot"})
    assert type(err) is ServiceError
    assert err.status == 418


def test_subclasses_are_service_errors():
    assert issubclass(AuthError, ServiceError)
    assert issubclass(ServiceUnavailable, ServiceError)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_sdk_errors.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'surfaces'`.

- [ ] **Step 3: Write minimal implementation**

```python
# surfaces/__init__.py
# surfaces: thin per-platform processes (discord_bot/, telegram_bot/, ...) that
# drive the lfg_service spine through the shared _client SDK.
```

```python
# surfaces/_client/__init__.py
# Surface SDK: one async client wrapping the lfg_service REST + WS contract.
# Public exports are populated in the final task.
```

```python
# surfaces/_client/errors.py
# Typed exceptions mapped from the service's {error, code} responses + HTTP status.


class ServiceError(Exception):
    """Base for all SDK errors. Carries the service's structured error fields."""

    def __init__(self, message: str, *, code: str | None = None, status: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status


class BadRequest(ServiceError):
    """400 — malformed request."""


class AuthError(ServiceError):
    """401 — bad/expired service or session token."""


class NotFound(ServiceError):
    """404 — unknown session id / route."""


class ServiceUnavailable(ServiceError):
    """5xx response, or a network error after retries were exhausted."""


_STATUS_MAP: dict[int, type[ServiceError]] = {
    400: BadRequest,
    401: AuthError,
    404: NotFound,
}


def error_for(status: int, body: dict | None) -> ServiceError | None:
    """Return the typed exception for a >= 400 status, or None for success."""
    if status < 400:
        return None
    code = body.get("code") if isinstance(body, dict) else None
    message = body.get("error") if isinstance(body, dict) else None
    cls = _STATUS_MAP.get(status)
    if cls is None:
        cls = ServiceUnavailable if status >= 500 else ServiceError
    return cls(message or f"HTTP {status}", code=code, status=status)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_sdk_errors.py -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add surfaces/__init__.py surfaces/_client/__init__.py surfaces/_client/errors.py tests/test_sdk_errors.py
git commit -m "feat(sdk): error hierarchy + status->exception mapping"
```

---

### Task 2: Retry helper with exponential backoff

**Files:**
- Create: `surfaces/_client/_retry.py`
- Test: `tests/test_sdk_retry.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - Module constants `RETRY_MAX_ATTEMPTS: int` and `RETRY_BASE_DELAY: float` read from env.
  - `async def with_retry(factory: Callable[[], Awaitable[T]], *, max_attempts: int, base_delay: float, retryable: Callable[[Exception], bool], sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> T` — calls `factory()`, retrying (exponential backoff `base_delay * 2 ** (attempt-1)`) while `retryable(exc)` and attempts remain; re-raises the last exception otherwise.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_sdk_retry.py
import pytest

from surfaces._client._retry import with_retry


async def _noop_sleep(_delay: float) -> None:
    return None


async def test_returns_on_first_success():
    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        return "ok"

    result = await with_retry(
        factory, max_attempts=5, base_delay=1.0, retryable=lambda e: True, sleep=_noop_sleep
    )
    assert result == "ok"
    assert calls["n"] == 1


async def test_retries_then_succeeds():
    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("transient")
        return "ok"

    result = await with_retry(
        factory, max_attempts=5, base_delay=1.0, retryable=lambda e: True, sleep=_noop_sleep
    )
    assert result == "ok"
    assert calls["n"] == 3


async def test_does_not_retry_when_not_retryable():
    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        raise ValueError("deterministic")

    with pytest.raises(ValueError):
        await with_retry(
            factory, max_attempts=5, base_delay=1.0, retryable=lambda e: False, sleep=_noop_sleep
        )
    assert calls["n"] == 1


async def test_raises_last_error_after_exhausting_attempts():
    calls = {"n": 0}
    delays: list[float] = []

    async def factory():
        calls["n"] += 1
        raise RuntimeError(f"fail-{calls['n']}")

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    with pytest.raises(RuntimeError, match="fail-3"):
        await with_retry(
            factory, max_attempts=3, base_delay=1.0, retryable=lambda e: True, sleep=record_sleep
        )
    assert calls["n"] == 3
    assert delays == [1.0, 2.0]  # backoff between the 3 attempts: 1*2^0, 1*2^1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_sdk_retry.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'surfaces._client._retry'`.

- [ ] **Step 3: Write minimal implementation**

```python
# surfaces/_client/_retry.py
# Exponential-backoff retry shared by REST calls and the WS reconnect loop.
# Reuses the same env knobs as the rest of the app without importing main.py.

import asyncio
import os
from collections.abc import Awaitable, Callable
from typing import TypeVar

RETRY_MAX_ATTEMPTS: int = int(os.getenv("RETRY_MAX_ATTEMPTS", "5"))
RETRY_BASE_DELAY: float = float(os.getenv("RETRY_BASE_DELAY", "1.0"))

T = TypeVar("T")


async def with_retry(
    factory: Callable[[], Awaitable[T]],
    *,
    max_attempts: int,
    base_delay: float,
    retryable: Callable[[Exception], bool],
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> T:
    """Call factory(), retrying transient failures with exponential backoff.

    Retries while retryable(exc) is True and attempts remain; otherwise the
    last exception propagates. Backoff before retry k is base_delay * 2 ** (k-1).
    """
    attempt = 0
    while True:
        try:
            return await factory()
        except Exception as exc:
            attempt += 1
            if attempt >= max_attempts or not retryable(exc):
                raise
            await sleep(base_delay * (2 ** (attempt - 1)))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_sdk_retry.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add surfaces/_client/_retry.py tests/test_sdk_retry.py
git commit -m "feat(sdk): exponential-backoff retry helper"
```

---

### Task 3: Client core (transport + public endpoints) + mock service fixture

**Files:**
- Create: `surfaces/_client/client.py`
- Create: `tests/mock_service.py` (shared aiohttp mock used by Tasks 3–8)
- Test: `tests/test_sdk_client_core.py`

**Interfaces:**
- Consumes: `surfaces._client.errors.{ServiceError, ServiceUnavailable, error_for}`; `surfaces._client._retry.{with_retry, RETRY_MAX_ATTEMPTS, RETRY_BASE_DELAY}`.
- Produces:
  - `class LFGServiceClient` with:
    - `__init__(self, base_url: str, service_token: str, surface: str, *, timeout: float = 30.0, max_attempts: int = RETRY_MAX_ATTEMPTS, base_delay: float = RETRY_BASE_DELAY)`
    - async context manager (`__aenter__`/`__aexit__`) + `async def close(self) -> None`
    - `async def _request(self, method: str, path: str, *, token: str | None = None, json: dict | None = None, params: dict | None = None, expect: str = "json") -> Any` — retry + error-mapping transport. `expect="bytes"` returns raw bytes.
    - `async def config(self) -> dict`
    - `async def qr_png(self, data: str) -> bytes` — `GET /api/qr.png?d=<data>`
    - `async def img(self, url: str) -> bytes` — `GET /api/img?u=<url>`
  - `tests/mock_service.py`: `SERVICE_TOKEN: str`, `build_mock_service(**opts) -> aiohttp.web.Application` and a `MockState` recording call counts / behavior knobs.

- [ ] **Step 1: Write the shared mock service**

```python
# tests/mock_service.py
# A minimal in-process aiohttp app mimicking the lfg_service contract, with
# knobs for flaky responses, one-shot 401s, mint poll progression, and a
# scripted /events stream. No lfg_core import — keeps SDK tests fast/isolated.

import json
from typing import Any

from aiohttp import WSMsgType, web

SERVICE_TOKEN = "svc-test"


def _bearer(request: web.Request) -> str | None:
    header = request.headers.get("Authorization", "")
    return header[7:] if header.startswith("Bearer ") else None


def build_mock_service(
    *,
    flaky: dict[str, int] | None = None,  # path -> number of leading 503s to emit
    expire_session_once: bool = False,  # first user-scoped call 401s, forcing a refresh
    events_script: dict[int, list[dict]] | None = None,  # connection# -> events to emit then close
) -> web.Application:
    app = web.Application()
    state: dict[str, Any] = {
        "hits": {},  # path -> count
        "session_hits": 0,  # number of /api/session mints
        "fail_left": dict(flaky or {}),
        "live_sessions": set(),  # minted session tokens currently valid
        "expired_once": expire_session_once,
        "events_script": events_script or {},
        "events_conns": 0,
        "last_event_types": None,  # ?types= seen on the last /events connect
        "mint_polls": {},  # session_id -> times polled
    }
    app["state"] = state

    def _count(path: str) -> None:
        state["hits"][path] = state["hits"].get(path, 0) + 1

    def _maybe_flaky(path: str) -> web.Response | None:
        left = state["fail_left"].get(path, 0)
        if left > 0:
            state["fail_left"][path] = left - 1
            return web.json_response({"error": "overloaded", "code": "busy"}, status=503)
        return None

    def _require_session(request: web.Request) -> web.Response | None:
        tok = _bearer(request)
        if state["expired_once"]:
            state["expired_once"] = False
            return web.json_response({"error": "expired", "code": "bad_session"}, status=401)
        if not tok or tok not in state["live_sessions"]:
            return web.json_response({"error": "unauthorized", "code": "bad_session"}, status=401)
        return None

    async def handle_config(request: web.Request) -> web.StreamResponse:
        _count("/api/config")
        flak = _maybe_flaky("/api/config")
        if flak is not None:
            return flak
        return web.json_response({"ok": True, "network": "testnet"})

    async def handle_qr(request: web.Request) -> web.StreamResponse:
        _count("/api/qr.png")
        if not request.query.get("d"):
            return web.json_response({"error": "bad data", "code": "bad_request"}, status=400)
        return web.Response(body=b"\x89PNG\r\n", content_type="image/png")

    async def handle_img(request: web.Request) -> web.StreamResponse:
        _count("/api/img")
        return web.Response(body=b"IMGDATA", content_type="image/jpeg")

    async def handle_session(request: web.Request) -> web.StreamResponse:
        if _bearer(request) != SERVICE_TOKEN:
            return web.json_response({"error": "unauthorized", "code": "bad_service_token"}, status=401)
        body = await request.json()
        state["session_hits"] += 1
        pid = body.get("platform_user_id", "")
        tok = f"sess-{pid}-{state['session_hits']}"
        state["live_sessions"].add(tok)
        return web.json_response(
            {"session_token": tok, "user": {"id": pid, "username": body.get("platform_username", "")}}
        )

    async def handle_me(request: web.Request) -> web.StreamResponse:
        _count("/api/me")
        bad = _require_session(request)
        if bad is not None:
            return bad
        return web.json_response({"id": "u", "username": "u", "wallet": "rMOCK"})

    async def handle_register(request: web.Request) -> web.StreamResponse:
        _count("/api/register")
        bad = _require_session(request)
        if bad is not None:
            return bad
        body = await request.json()
        return web.json_response({"ok": True, "wallet": body.get("wallet")})

    async def handle_mint_start(request: web.Request) -> web.StreamResponse:
        _count("/api/mint")
        bad = _require_session(request)
        if bad is not None:
            return bad
        return web.json_response({"session_id": "m1", "state": "awaiting_payment"})

    async def handle_mint_status(request: web.Request) -> web.StreamResponse:
        bad = _require_session(request)
        if bad is not None:
            return bad
        sid = request.match_info["session_id"]
        state["mint_polls"][sid] = state["mint_polls"].get(sid, 0) + 1
        ready = state["mint_polls"][sid] >= 2
        return web.json_response({"session_id": sid, "state": "offer_ready" if ready else "minting"})

    async def handle_swap_start(request: web.Request) -> web.StreamResponse:
        _count("/api/swap")
        bad = _require_session(request)
        if bad is not None:
            return bad
        return web.json_response({"session_id": "s1", "state": "awaiting_payment"})

    async def handle_swap_status(request: web.Request) -> web.StreamResponse:
        bad = _require_session(request)
        if bad is not None:
            return bad
        return web.json_response({"session_id": request.match_info["session_id"], "state": "done"})

    async def handle_nfts(request: web.Request) -> web.StreamResponse:
        _count("/api/nfts")
        bad = _require_session(request)
        if bad is not None:
            return bad
        return web.json_response({"nfts": []})

    async def handle_signin_start(request: web.Request) -> web.StreamResponse:
        _count("/api/signin")
        bad = _require_session(request)
        if bad is not None:
            return bad
        return web.json_response({"uuid": "sg1", "qr": "data:..."})

    async def handle_signin_status(request: web.Request) -> web.StreamResponse:
        bad = _require_session(request)
        if bad is not None:
            return bad
        return web.json_response({"uuid": request.match_info["payload_uuid"], "signed": True})

    async def handle_generic_session_get(request: web.Request) -> web.StreamResponse:
        # economy / equip-status / harvest-status / assemble-status
        bad = _require_session(request)
        if bad is not None:
            return bad
        return web.json_response({"ok": True, "path": request.path})

    async def handle_generic_session_post(request: web.Request) -> web.StreamResponse:
        # equip / harvest / assemble start
        bad = _require_session(request)
        if bad is not None:
            return bad
        return web.json_response({"session_id": "x1", "state": "started"})

    async def handle_events(request: web.Request) -> web.WebSocketResponse:
        if request.query.get("token") != SERVICE_TOKEN:
            # aiohttp WS handshake can't carry a JSON 401 cleanly; reject pre-upgrade
            raise web.HTTPUnauthorized()
        state["last_event_types"] = request.query.get("types")
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        state["events_conns"] += 1
        for evt in state["events_script"].get(state["events_conns"], []):
            await ws.send_str(json.dumps(evt))
        await ws.close()  # ending the connection forces the client to reconnect
        return ws

    app.router.add_get("/api/config", handle_config)
    app.router.add_get("/api/qr.png", handle_qr)
    app.router.add_get("/api/img", handle_img)
    app.router.add_post("/api/session", handle_session)
    app.router.add_get("/api/me", handle_me)
    app.router.add_post("/api/register", handle_register)
    app.router.add_post("/api/mint", handle_mint_start)
    app.router.add_get("/api/mint/{session_id}", handle_mint_status)
    app.router.add_post("/api/swap", handle_swap_start)
    app.router.add_get("/api/swap/{session_id}", handle_swap_status)
    app.router.add_get("/api/nfts", handle_nfts)
    app.router.add_post("/api/signin", handle_signin_start)
    app.router.add_get("/api/signin/{payload_uuid}", handle_signin_status)
    app.router.add_get("/api/economy", handle_generic_session_get)
    app.router.add_post("/api/equip", handle_generic_session_post)
    app.router.add_get("/api/equip/{session_id}", handle_generic_session_get)
    app.router.add_post("/api/harvest", handle_generic_session_post)
    app.router.add_get("/api/harvest/{session_id}", handle_generic_session_get)
    app.router.add_post("/api/assemble", handle_generic_session_post)
    app.router.add_get("/api/assemble/{session_id}", handle_generic_session_get)
    app.router.add_get("/events", handle_events)
    return app
```

- [ ] **Step 2: Write the failing test**

```python
# tests/test_sdk_client_core.py
import pytest
from aiohttp.test_utils import TestServer

from surfaces._client.client import LFGServiceClient
from surfaces._client.errors import NotFound, ServiceUnavailable
from tests.mock_service import SERVICE_TOKEN, build_mock_service


async def _client(app, **kw):
    server = TestServer(app)
    await server.start_server()
    base = str(server.make_url("")).rstrip("/")
    client = LFGServiceClient(base, SERVICE_TOKEN, "test", base_delay=0.0, **kw)
    return server, client


async def test_config_roundtrip():
    server, client = await _client(build_mock_service())
    async with client:
        body = await client.config()
        assert body == {"ok": True, "network": "testnet"}
    await server.close()


async def test_qr_and_img_return_bytes():
    server, client = await _client(build_mock_service())
    async with client:
        assert await client.qr_png("HELLO") == b"\x89PNG\r\n"
        assert await client.img("https://cdn/x.png") == b"IMGDATA"
    await server.close()


async def test_retries_5xx_then_succeeds():
    app = build_mock_service(flaky={"/api/config": 2})
    server, client = await _client(app)
    async with client:
        body = await client.config()  # 503, 503, then 200
        assert body["ok"] is True
        assert app["state"]["hits"]["/api/config"] == 3
    await server.close()


async def test_exhausted_retries_raise_service_unavailable():
    app = build_mock_service(flaky={"/api/config": 9})
    server, client = await _client(app, max_attempts=2)
    async with client:
        with pytest.raises(ServiceUnavailable):
            await client.config()
    await server.close()


async def test_4xx_raises_immediately_without_retry():
    app = build_mock_service()
    server, client = await _client(app)
    async with client:
        with pytest.raises(NotFound):
            # /api/qr.png with no ?d= returns 400; use a definitely-missing route for 404
            await client._request("GET", "/api/nope")
    await server.close()
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/test_sdk_client_core.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'surfaces._client.client'`.

- [ ] **Step 4: Write minimal implementation**

```python
# surfaces/_client/client.py
# LFGServiceClient: the async client every surface shares. Owns one aiohttp
# ClientSession; wraps the lfg_service REST + WS contract with retry, typed
# errors, and a per-user session-token cache (added in Task 4).

from __future__ import annotations

import asyncio
from types import TracebackType
from typing import Any

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

    async def _request(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        json: dict | None = None,
        params: dict | None = None,
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

    async def config(self) -> dict:
        return await self._request("GET", "/api/config")

    async def qr_png(self, data: str) -> bytes:
        return await self._request("GET", "/api/qr.png", params={"d": data}, expect="bytes")

    async def img(self, url: str) -> bytes:
        return await self._request("GET", "/api/img", params={"u": url}, expect="bytes")
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_sdk_client_core.py -v`
Expected: PASS (5 tests).

- [ ] **Step 6: Commit**

```bash
git add surfaces/_client/client.py tests/mock_service.py tests/test_sdk_client_core.py
git commit -m "feat(sdk): client core (retry transport + public endpoints) + mock service"
```

---

### Task 4: Service-token auth + per-user session cache + 401 refresh

**Files:**
- Modify: `surfaces/_client/client.py`
- Test: `tests/test_sdk_sessions.py`

**Interfaces:**
- Consumes: `LFGServiceClient._request`; the mock's `/api/session`, `/api/me`, `/api/register`.
- Produces (methods on `LFGServiceClient`):
  - `async def create_session(self, user_id: str, username: str = "") -> str` — `POST /api/session` with the **service token**; returns `session_token`.
  - `async def _session_token(self, user_id: str, username: str = "") -> str` — returns a cached token, minting one on first use.
  - `async def _user_request(self, method: str, path: str, user_id: str, *, username: str = "", **kw: Any) -> Any` — attaches the user's session token; on `AuthError`, evicts the cached token, re-mints once, and retries the call exactly once.
  - `async def register(self, user_id: str, username: str, wallet: str) -> dict`
  - `async def me(self, user_id: str, *, username: str = "") -> dict`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_sdk_sessions.py
import pytest
from aiohttp.test_utils import TestServer

from surfaces._client.client import LFGServiceClient
from surfaces._client.errors import AuthError
from tests.mock_service import SERVICE_TOKEN, build_mock_service


async def _client(app, **kw):
    server = TestServer(app)
    await server.start_server()
    base = str(server.make_url("")).rstrip("/")
    return server, LFGServiceClient(base, SERVICE_TOKEN, "test", base_delay=0.0, **kw)


async def test_session_minted_once_and_reused():
    app = build_mock_service()
    server, client = await _client(app)
    async with client:
        await client.me("42", username="neo")
        await client.me("42")
        await client.register("42", "neo", "rWALLET")
        assert app["state"]["session_hits"] == 1  # one mint for user 42, reused
    await server.close()


async def test_distinct_users_get_distinct_sessions():
    app = build_mock_service()
    server, client = await _client(app)
    async with client:
        await client.me("1")
        await client.me("2")
        assert app["state"]["session_hits"] == 2
    await server.close()


async def test_401_triggers_refresh_and_retry():
    app = build_mock_service(expire_session_once=True)
    server, client = await _client(app)
    async with client:
        body = await client.me("42", username="neo")  # first call 401s, then refresh succeeds
        assert body["wallet"] == "rMOCK"
        assert app["state"]["session_hits"] == 2  # initial mint + one refresh
    await server.close()


async def test_bad_service_token_raises_auth_error():
    app = build_mock_service()
    server = TestServer(app)
    await server.start_server()
    base = str(server.make_url("")).rstrip("/")
    client = LFGServiceClient(base, "WRONG", "test", base_delay=0.0)
    async with client:
        with pytest.raises(AuthError):
            await client.create_session("42", "neo")
    await server.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_sdk_sessions.py -v`
Expected: FAIL with `AttributeError: 'LFGServiceClient' object has no attribute 'me'`.

- [ ] **Step 3: Write minimal implementation**

Add to `LFGServiceClient` in `surfaces/_client/client.py` (after `img`):

```python
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

    async def register(self, user_id: str, username: str, wallet: str) -> dict:
        return await self._user_request(
            "POST", "/api/register", user_id, username=username, json={"wallet": wallet}
        )

    async def me(self, user_id: str, *, username: str = "") -> dict:
        return await self._user_request("GET", "/api/me", user_id, username=username)
```

Add `AuthError` to the imports at the top of `client.py`:

```python
from .errors import AuthError, ServiceError, ServiceUnavailable, error_for
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_sdk_sessions.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add surfaces/_client/client.py tests/test_sdk_sessions.py
git commit -m "feat(sdk): service-token auth + per-user session cache with 401 refresh"
```

---

### Task 5: Mint + swap methods, with poll-to-terminal helpers

**Files:**
- Modify: `surfaces/_client/client.py`
- Test: `tests/test_sdk_mint_swap.py`

**Interfaces:**
- Consumes: `LFGServiceClient._user_request`; an injectable `sleep` for the wait helpers.
- Produces (methods on `LFGServiceClient`):
  - `async def start_mint(self, user_id: str, *, username: str = "") -> dict` — `POST /api/mint` (server derives all mint params from the user/wallet; no body fields).
  - `async def mint_status(self, user_id: str, session_id: str) -> dict` — `GET /api/mint/{session_id}`.
  - `async def regenerate(self, user_id: str, session_id: str) -> dict` — `POST /api/mint/{session_id}/regenerate`.
  - `async def wait_for_mint(self, user_id: str, session_id: str, *, interval: float = 2.0, timeout: float = 180.0, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> dict` — polls `mint_status` until `state` is terminal (`MINT_TERMINAL`) or `timeout`.
  - `async def start_swap(self, user_id: str, nft1_id: str, nft2_id: str, traits: list[str], *, username: str = "") -> dict` — `POST /api/swap` body `{nft1_id, nft2_id, traits}`.
  - `async def swap_status(self, user_id: str, session_id: str) -> dict`.
  - `async def wait_for_swap(self, user_id: str, session_id: str, *, interval: float = 2.0, timeout: float = 180.0, sleep: ... = asyncio.sleep) -> dict` — polls until `SWAP_TERMINAL`.
  - Module constants `MINT_TERMINAL: frozenset[str]` and `SWAP_TERMINAL: frozenset[str]` (string literals mirroring `lfg_core.mint_flow`/`swap_flow` `TERMINAL_STATES`, copied to keep the SDK decoupled).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_sdk_mint_swap.py
from aiohttp.test_utils import TestServer

from surfaces._client.client import LFGServiceClient
from tests.mock_service import SERVICE_TOKEN, build_mock_service


async def _noop_sleep(_delay: float) -> None:
    return None


async def _client(app):
    server = TestServer(app)
    await server.start_server()
    base = str(server.make_url("")).rstrip("/")
    return server, LFGServiceClient(base, SERVICE_TOKEN, "test", base_delay=0.0)


async def test_start_mint_returns_session_id():
    server, client = await _client(build_mock_service())
    async with client:
        body = await client.start_mint("42")
        assert body["session_id"] == "m1"
    await server.close()


async def test_wait_for_mint_polls_until_terminal():
    server, client = await _client(build_mock_service())
    async with client:
        await client.start_mint("42")
        final = await client.wait_for_mint("42", "m1", interval=0.0, sleep=_noop_sleep)
        assert final["state"] == "offer_ready"  # mock flips to terminal on the 2nd poll
    await server.close()


async def test_start_swap_sends_trait_body():
    server, client = await _client(build_mock_service())
    async with client:
        body = await client.start_swap("42", "nftA", "nftB", ["Hat"])
        assert body["session_id"] == "s1"
    await server.close()


async def test_swap_status():
    server, client = await _client(build_mock_service())
    async with client:
        body = await client.swap_status("42", "s1")
        assert body["state"] == "done"
    await server.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_sdk_mint_swap.py -v`
Expected: FAIL with `AttributeError: ... has no attribute 'start_mint'`.

- [ ] **Step 3: Write minimal implementation**

Add near the top of `client.py` (after the imports), the terminal-state constants and the needed typing import:

```python
from collections.abc import Awaitable, Callable
import time
```

```python
# Terminal states copied from lfg_core.mint_flow / swap_flow TERMINAL_STATES.
# Duplicated (not imported) so the SDK never depends on lfg_core.
MINT_TERMINAL: frozenset[str] = frozenset({"offer_ready", "done", "failed", "payment_timeout"})
SWAP_TERMINAL: frozenset[str] = frozenset({"done", "failed", "offers_ready", "payment_timeout"})
```

Add these methods to `LFGServiceClient` (after `me`):

```python
    # ---- mint ----

    async def start_mint(self, user_id: str, *, username: str = "") -> dict:
        return await self._user_request("POST", "/api/mint", user_id, username=username)

    async def mint_status(self, user_id: str, session_id: str) -> dict:
        return await self._user_request("GET", f"/api/mint/{session_id}", user_id)

    async def regenerate(self, user_id: str, session_id: str) -> dict:
        return await self._user_request("POST", f"/api/mint/{session_id}/regenerate", user_id)

    async def wait_for_mint(
        self,
        user_id: str,
        session_id: str,
        *,
        interval: float = 2.0,
        timeout: float = 180.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> dict:
        return await self._poll(
            lambda: self.mint_status(user_id, session_id), MINT_TERMINAL, interval, timeout, sleep
        )

    # ---- swap ----

    async def start_swap(
        self, user_id: str, nft1_id: str, nft2_id: str, traits: list[str], *, username: str = ""
    ) -> dict:
        return await self._user_request(
            "POST",
            "/api/swap",
            user_id,
            username=username,
            json={"nft1_id": nft1_id, "nft2_id": nft2_id, "traits": traits},
        )

    async def swap_status(self, user_id: str, session_id: str) -> dict:
        return await self._user_request("GET", f"/api/swap/{session_id}", user_id)

    async def wait_for_swap(
        self,
        user_id: str,
        session_id: str,
        *,
        interval: float = 2.0,
        timeout: float = 180.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> dict:
        return await self._poll(
            lambda: self.swap_status(user_id, session_id), SWAP_TERMINAL, interval, timeout, sleep
        )

    async def _poll(
        self,
        fetch: Callable[[], Awaitable[dict]],
        terminal: frozenset[str],
        interval: float,
        timeout: float,
        sleep: Callable[[float], Awaitable[None]],
    ) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            status = await fetch()
            if status.get("state") in terminal:
                return status
            if time.monotonic() >= deadline:
                return status  # caller inspects non-terminal state on timeout
            await sleep(interval)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_sdk_mint_swap.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add surfaces/_client/client.py tests/test_sdk_mint_swap.py
git commit -m "feat(sdk): mint/swap methods + poll-to-terminal helpers"
```

---

### Task 6: Remaining user-scoped endpoints (sign-in, nfts, economy, equip/harvest/assemble)

**Files:**
- Modify: `surfaces/_client/client.py`
- Test: `tests/test_sdk_remaining.py`

**Interfaces:**
- Consumes: `LFGServiceClient._user_request`.
- Produces (methods on `LFGServiceClient`):
  - `async def signin_start(self, user_id: str, *, username: str = "") -> dict` — `POST /api/signin`.
  - `async def signin_status(self, user_id: str, payload_uuid: str) -> dict` — `GET /api/signin/{payload_uuid}`.
  - `async def nfts(self, user_id: str) -> dict` — `GET /api/nfts`.
  - `async def economy(self, user_id: str) -> dict` — `GET /api/economy`.
  - `async def equip_start(self, user_id: str, body: dict) -> dict` / `async def equip_status(self, user_id: str, session_id: str) -> dict`.
  - `async def harvest_start(self, user_id: str, body: dict) -> dict` / `async def harvest_status(self, user_id: str, session_id: str) -> dict`.
  - `async def assemble_start(self, user_id: str, body: dict) -> dict` / `async def assemble_status(self, user_id: str, session_id: str) -> dict`.

  (`*_start` take an explicit `body: dict` forwarded verbatim — the field names are owned by the live `equip`/`harvest`/`assemble` routes, not the SDK.)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_sdk_remaining.py
from aiohttp.test_utils import TestServer

from surfaces._client.client import LFGServiceClient
from tests.mock_service import SERVICE_TOKEN, build_mock_service


async def _client(app):
    server = TestServer(app)
    await server.start_server()
    base = str(server.make_url("")).rstrip("/")
    return server, LFGServiceClient(base, SERVICE_TOKEN, "test", base_delay=0.0)


async def test_signin_and_nfts_and_economy():
    server, client = await _client(build_mock_service())
    async with client:
        assert (await client.signin_start("42"))["uuid"] == "sg1"
        assert (await client.signin_status("42", "sg1"))["signed"] is True
        assert "nfts" in await client.nfts("42")
        assert (await client.economy("42"))["ok"] is True
    await server.close()


async def test_equip_harvest_assemble_start_and_status():
    server, client = await _client(build_mock_service())
    async with client:
        assert (await client.equip_start("42", {"asset": "x"}))["session_id"] == "x1"
        assert (await client.equip_status("42", "x1"))["ok"] is True
        assert (await client.harvest_start("42", {"nft": "y"}))["session_id"] == "x1"
        assert (await client.harvest_status("42", "x1"))["ok"] is True
        assert (await client.assemble_start("42", {"body": "z"}))["session_id"] == "x1"
        assert (await client.assemble_status("42", "x1"))["ok"] is True
    await server.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_sdk_remaining.py -v`
Expected: FAIL with `AttributeError: ... has no attribute 'signin_start'`.

- [ ] **Step 3: Write minimal implementation**

Add to `LFGServiceClient` (after the swap methods):

```python
    # ---- sign-in / nfts / economy ----

    async def signin_start(self, user_id: str, *, username: str = "") -> dict:
        return await self._user_request("POST", "/api/signin", user_id, username=username)

    async def signin_status(self, user_id: str, payload_uuid: str) -> dict:
        return await self._user_request("GET", f"/api/signin/{payload_uuid}", user_id)

    async def nfts(self, user_id: str) -> dict:
        return await self._user_request("GET", "/api/nfts", user_id)

    async def economy(self, user_id: str) -> dict:
        return await self._user_request("GET", "/api/economy", user_id)

    # ---- trait-economy ops ----

    async def equip_start(self, user_id: str, body: dict) -> dict:
        return await self._user_request("POST", "/api/equip", user_id, json=body)

    async def equip_status(self, user_id: str, session_id: str) -> dict:
        return await self._user_request("GET", f"/api/equip/{session_id}", user_id)

    async def harvest_start(self, user_id: str, body: dict) -> dict:
        return await self._user_request("POST", "/api/harvest", user_id, json=body)

    async def harvest_status(self, user_id: str, session_id: str) -> dict:
        return await self._user_request("GET", f"/api/harvest/{session_id}", user_id)

    async def assemble_start(self, user_id: str, body: dict) -> dict:
        return await self._user_request("POST", "/api/assemble", user_id, json=body)

    async def assemble_status(self, user_id: str, session_id: str) -> dict:
        return await self._user_request("GET", f"/api/assemble/{session_id}", user_id)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_sdk_remaining.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add surfaces/_client/client.py tests/test_sdk_remaining.py
git commit -m "feat(sdk): signin/nfts/economy + equip/harvest/assemble methods"
```

---

### Task 7: Reconnecting `/events` async iterator

**Files:**
- Create: `surfaces/_client/events.py`
- Modify: `surfaces/_client/client.py` (add `events()` delegating to the iterator)
- Test: `tests/test_sdk_events.py`

**Interfaces:**
- Consumes: `LFGServiceClient`'s `aiohttp.ClientSession`, `_base`, `_service_token`, `_base_delay`; `lfg_service.events.Event`.
- Produces:
  - `async def stream_events(session, base_url, service_token, types, *, base_delay, sleep=asyncio.sleep) -> AsyncIterator[Event]` in `events.py` — connects `GET /events?token=...&types=...`, yields `Event`s, and on any disconnect/connection error reconnects with exponential backoff (capped at 30s, reset after a successful connect). Loops indefinitely.
  - `def events(self, types: list[str] | None = None) -> AsyncIterator[Event]` on `LFGServiceClient` (an async generator delegating to `stream_events`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_sdk_events.py
import asyncio

from aiohttp.test_utils import TestServer

from surfaces._client.client import LFGServiceClient
from tests.mock_service import SERVICE_TOKEN, build_mock_service


async def _noop_sleep(_delay: float) -> None:
    return None


async def test_events_yields_across_a_reconnect():
    # connection 1 emits evt #1 then the mock closes the WS (forcing reconnect);
    # connection 2 emits evt #2.
    script = {
        1: [{"type": "mint.completed", "ts": 1, "identity": None, "wallet": "rA", "data": {"n": 1}}],
        2: [{"type": "mint.failed", "ts": 2, "identity": None, "wallet": "rB", "data": {"n": 2}}],
    }
    app = build_mock_service(events_script=script)
    server = TestServer(app)
    await server.start_server()
    base = str(server.make_url("")).rstrip("/")
    client = LFGServiceClient(base, SERVICE_TOKEN, "test", base_delay=0.0)

    received = []
    async with client:
        agen = client.events(types=["mint.completed", "mint.failed"])
        for _ in range(2):
            received.append(await asyncio.wait_for(agen.__anext__(), timeout=2))
        await agen.aclose()

    assert [e.data["n"] for e in received] == [1, 2]
    assert received[0].type == "mint.completed"
    assert app["state"]["last_event_types"] == "mint.completed,mint.failed"
    await server.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_sdk_events.py -v`
Expected: FAIL with `AttributeError: ... has no attribute 'events'` (or ModuleNotFound for events.py).

- [ ] **Step 3: Write minimal implementation**

```python
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
```

Add `events()` to `LFGServiceClient` in `client.py` (after the trait-economy methods), plus the `Event`/`stream_events` imports near the top:

```python
from collections.abc import AsyncIterator
from .events import stream_events
from lfg_service.events import Event
```

```python
    # ---- events ----

    async def events(self, types: list[str] | None = None) -> AsyncIterator[Event]:
        session = self._require_session()
        async for event in stream_events(
            session, self._base, self._service_token, types, base_delay=self._base_delay
        ):
            yield event
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_sdk_events.py -v`
Expected: PASS (1 test).

- [ ] **Step 5: Commit**

```bash
git add surfaces/_client/events.py surfaces/_client/client.py tests/test_sdk_events.py
git commit -m "feat(sdk): reconnecting /events async iterator"
```

---

### Task 8: Public package exports + usage docs + full-suite verification

**Files:**
- Modify: `surfaces/_client/__init__.py`
- Create: `surfaces/_client/README.md`
- Test: `tests/test_sdk_exports.py`

**Interfaces:**
- Consumes: everything from Tasks 1–7.
- Produces: top-level package exports so surfaces import `from surfaces._client import LFGServiceClient, Event, ServiceError, AuthError, BadRequest, NotFound, ServiceUnavailable`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_sdk_exports.py
def test_public_exports_are_importable():
    from surfaces._client import (
        AuthError,
        BadRequest,
        Event,
        LFGServiceClient,
        NotFound,
        ServiceError,
        ServiceUnavailable,
    )

    assert LFGServiceClient.__name__ == "LFGServiceClient"
    assert issubclass(AuthError, ServiceError)
    assert {BadRequest, NotFound, ServiceUnavailable}  # referenced
    assert Event.__name__ == "Event"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_sdk_exports.py -v`
Expected: FAIL with `ImportError: cannot import name 'LFGServiceClient' from 'surfaces._client'`.

- [ ] **Step 3: Write minimal implementation**

```python
# surfaces/_client/__init__.py
# Surface SDK: one async client wrapping the lfg_service REST + WS contract.

from lfg_service.events import Event

from .client import LFGServiceClient
from .errors import (
    AuthError,
    BadRequest,
    NotFound,
    ServiceError,
    ServiceUnavailable,
)

__all__ = [
    "LFGServiceClient",
    "Event",
    "ServiceError",
    "AuthError",
    "BadRequest",
    "NotFound",
    "ServiceUnavailable",
]
```

```markdown
<!-- surfaces/_client/README.md -->
# Surface SDK (`surfaces._client`)

One async client wrapping the `lfg_service` REST + WebSocket contract. Every
surface process (Discord, Telegram, X) constructs one `LFGServiceClient` and
shares it.

```python
from surfaces._client import LFGServiceClient

async with LFGServiceClient(BASE_URL, SERVICE_TOKEN, "discord") as svc:
    await svc.register(user_id, username, wallet)
    mint = await svc.start_mint(user_id)
    final = await svc.wait_for_mint(user_id, mint["session_id"])

    async for ev in svc.events(types=["mint.completed", "mint.failed"]):
        await announce(ev)   # reconnects internally; loop never exits on a drop
```

- **Auth:** the client holds the per-surface **service token**; it mints and
  caches a per-user **session token** automatically (refreshing on 401).
- **Resilience:** REST calls retry transient failures (5xx/429/network) with
  backoff; `events()` reconnects transparently.
- **Errors:** failures raise `ServiceError` subclasses (`AuthError`,
  `BadRequest`, `NotFound`, `ServiceUnavailable`) carrying `.code`/`.status`.

Configuration knobs: `RETRY_MAX_ATTEMPTS` (default 5), `RETRY_BASE_DELAY`
(default 1.0) via environment.
```

- [ ] **Step 4: Run the full SDK suite + the existing service suite**

Run: `pytest tests/test_sdk_errors.py tests/test_sdk_retry.py tests/test_sdk_client_core.py tests/test_sdk_sessions.py tests/test_sdk_mint_swap.py tests/test_sdk_remaining.py tests/test_sdk_events.py tests/test_sdk_exports.py -v`
Expected: PASS (all SDK tests).

Run: `pytest tests/ -q`
Expected: PASS (the SDK adds no regressions to the Plan 1 service suite).

- [ ] **Step 5: Run the pre-commit gate, then commit**

```bash
pre-commit run --files surfaces/_client/__init__.py surfaces/_client/README.md tests/test_sdk_exports.py
git add surfaces/_client/__init__.py surfaces/_client/README.md tests/test_sdk_exports.py
git commit -m "feat(sdk): public package exports + usage README"
```

---

## Self-Review

**Spec coverage:**
- §2 complete-coverage goal → mint (Task 5), swap (Task 5), signin/nfts/economy/equip/harvest/assemble (Task 6), media/config (Task 3), events (Task 7).
- §2 async-only, no new deps → all tasks use aiohttp; constraint stated in Global Constraints.
- §3 module layout (`errors`/`_retry`/`client`/`events`, `Event` re-export) → Tasks 1, 2, 3, 7, 8.
- §4.1 per-user session cache + 401 refresh → Task 4.
- §4.2 mint methods incl. `wait_for_mint` → Task 5.
- §4.3 swap/signin/nfts/economy/equip/harvest/assemble → Tasks 5, 6.
- §4.4 media/config → Task 3.
- §4.5 reconnecting `events()` async iterator + `types` filter → Task 7.
- §5 typed error hierarchy + `error_for` mapping → Task 1.
- §6 REST retry (5xx/429/network, no 4xx) + WS reconnect, reusing `RETRY_*` knobs → Tasks 2, 3, 7.
- §7 mock-service test matrix (auth header, session-cache, 401-refresh, retry, typed errors, event reconnect, binary bytes) → Tasks 3, 4, 5, 7.

**Placeholder scan:** none — every code/test step contains concrete code and exact commands. `equip/harvest/assemble` `body: dict` pass-through is explicit by design (field names owned by the live route), not a placeholder.

**Type consistency:** `_user_request(method, path, user_id, *, username="", **kw)`, `_session_token(user_id, username="")`, `create_session(user_id, username="")`, `_request(method, path, *, token, json, params, expect)`, `stream_events(session, base_url, service_token, types, *, base_delay, sleep)`, `MINT_TERMINAL`/`SWAP_TERMINAL`, and `error_for(status, body)` are used consistently across Tasks 1–8. `Event` is always `lfg_service.events.Event`.

**Decoupling check:** the SDK imports only `lfg_service.events.Event` from the service package and never `lfg_core`; terminal-state strings are copied (not imported). The mock service imports no `lfg_core` either, so SDK tests stay fast and isolated.
```
