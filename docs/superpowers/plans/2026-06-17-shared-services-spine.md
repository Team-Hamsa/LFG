# Shared-Services Spine — Implementation Plan (Plan 1 of 4)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the `lfg_service` spine — a generalized identity model, service-token auth, an in-memory event bus, an identity-assertion endpoint, and user/firehose WebSocket event channels — on top of the existing `webapp` backend, without breaking the live Discord Activity.

**Architecture:** Add new focused modules (`identity`, `auth`, `events`) under a new `lfg_service/` package, each independently tested. Wire them into the existing `webapp/server.py` in place. Mint/swap flows publish domain events at terminal transitions; trusted surfaces consume them via `/events` (service-token) and the browser via `/events/me` (user-session, self-filtered). Promote `webapp/server.py` to `lfg_service/app.py` as the final, mechanical task.

**Tech Stack:** Python 3.10, aiohttp, sqlite3, xrpl-py, pytest, asyncio.

**Spec:** `docs/superpowers/specs/2026-06-17-shared-services-spine-design.md`

**This plan covers spec §4.1–4.4 (auth, REST `/api/session`, identity, event bus) and the §6 EventBus interface.** Discord migration (#53), the surface SDK, and Telegram (#43) are Plans 2–4, written once this lands.

## Global Constraints

- Python version floor: **3.10** (uses `X | None` unions, `dict[...]` generics).
- Database file: **`lfg_nfts.db`** (constant `DATABASE` in `user_db.py`); all new DB code uses the same path constant.
- XRPL address validation: use `xrpl.core.addresscodec.is_valid_classic_address` (already a dependency).
- No new always-on infra: event bus is **in-memory only** this plan; Redis is a future drop-in behind the `EventBus` protocol — do not add a Redis dependency.
- All blocking sqlite calls inside aiohttp handlers run via `await asyncio.to_thread(...)` (matches existing `webapp/server.py` pattern).
- Existing `webapp/test_smoke.py` must continue to pass at every task boundary.
- Pre-commit gate is real and blocking (ruff, ruff-format, mypy, gitleaks, pytest). Run `pre-commit run --files <changed>` before each commit; types must satisfy mypy.

---

### Task 1: Identity module + `identities` table + migration

**Files:**
- Create: `lfg_service/__init__.py` (empty package marker)
- Create: `lfg_service/identity.py`
- Test: `tests/test_identity.py`

**Interfaces:**
- Consumes: `user_db.DATABASE` (the sqlite path constant `"lfg_nfts.db"`).
- Produces:
  - `ensure_identities_table() -> None`
  - `link(platform: str, platform_user_id: str, platform_username: str, wallet: str) -> bool` — upsert on `(platform, platform_user_id)`.
  - `resolve(platform: str, platform_user_id: str) -> str | None` — returns wallet or None.
  - `migrate_users_to_identities() -> int` — copies `Users` rows to `identities` as `platform='discord'`; returns count migrated; idempotent.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_identity.py
import sqlite3
import lfg_service.identity as identity


def _fresh_db(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    monkeypatch.setattr(identity, "DATABASE", str(db))
    return str(db)


def test_link_and_resolve(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    identity.ensure_identities_table()
    assert identity.resolve("telegram", "999") is None
    assert identity.link("telegram", "999", "alice", "rWALLET1") is True
    assert identity.resolve("telegram", "999") == "rWALLET1"


def test_link_upserts_wallet(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    identity.ensure_identities_table()
    identity.link("discord", "1", "bob", "rOLD")
    identity.link("discord", "1", "bob", "rNEW")
    assert identity.resolve("discord", "1") == "rNEW"


def test_same_user_id_different_platforms_are_distinct(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    identity.ensure_identities_table()
    identity.link("discord", "1", "bob", "rDISCORD")
    identity.link("telegram", "1", "bob", "rTELEGRAM")
    assert identity.resolve("discord", "1") == "rDISCORD"
    assert identity.resolve("telegram", "1") == "rTELEGRAM"


def test_migrate_users_is_idempotent(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path, monkeypatch)
    # seed a legacy Users table
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE Users (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "discord_id TEXT NOT NULL UNIQUE, discord_name TEXT NOT NULL, wallet TEXT NOT NULL)"
    )
    conn.execute("INSERT INTO Users (discord_id, discord_name, wallet) VALUES ('7','carol','rC')")
    conn.commit()
    conn.close()
    identity.ensure_identities_table()
    assert identity.migrate_users_to_identities() == 1
    assert identity.resolve("discord", "7") == "rC"
    # second run migrates nothing new and does not error
    assert identity.migrate_users_to_identities() == 0
    assert identity.resolve("discord", "7") == "rC"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_identity.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'lfg_service.identity'` (or AttributeError).

- [ ] **Step 3: Write minimal implementation**

```python
# lfg_service/__init__.py
# lfg_service: the shared backend service spine consumed by every surface.
```

```python
# lfg_service/identity.py
# Generalized identity: maps (platform, platform_user_id) -> XRPL wallet.
# The wallet is the canonical account; account_id is a reserved hook for
# future linked multi-surface profiles (nullable, unused now).

import logging
import sqlite3

from user_db import DATABASE  # single source of truth for the db path


def ensure_identities_table() -> None:
    conn = sqlite3.connect(DATABASE)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS identities (
                platform          TEXT NOT NULL,
                platform_user_id  TEXT NOT NULL,
                platform_username TEXT,
                wallet            TEXT NOT NULL,
                account_id        INTEGER,
                created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (platform, platform_user_id)
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def link(platform: str, platform_user_id: str, platform_username: str, wallet: str) -> bool:
    try:
        conn = sqlite3.connect(DATABASE)
        conn.execute(
            """
            INSERT INTO identities (platform, platform_user_id, platform_username, wallet)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(platform, platform_user_id) DO UPDATE SET
                platform_username = excluded.platform_username,
                wallet = excluded.wallet
            """,
            (platform, platform_user_id, platform_username, wallet),
        )
        conn.commit()
        return True
    except Exception as e:
        logging.error(f"identity.link failed: {e}")
        return False
    finally:
        conn.close()


def resolve(platform: str, platform_user_id: str) -> str | None:
    try:
        conn = sqlite3.connect(DATABASE)
        cur = conn.execute(
            "SELECT wallet FROM identities WHERE platform = ? AND platform_user_id = ?",
            (platform, platform_user_id),
        )
        row = cur.fetchone()
        return row[0] if row else None
    except Exception as e:
        logging.error(f"identity.resolve failed: {e}")
        return None
    finally:
        conn.close()


def migrate_users_to_identities() -> int:
    """Copy legacy Users rows into identities as platform='discord'. Idempotent."""
    conn = sqlite3.connect(DATABASE)
    try:
        names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        if "Users" not in names:
            return 0
        rows = conn.execute("SELECT discord_id, discord_name, wallet FROM Users").fetchall()
        migrated = 0
        for discord_id, discord_name, wallet in rows:
            exists = conn.execute(
                "SELECT 1 FROM identities WHERE platform='discord' AND platform_user_id=?",
                (discord_id,),
            ).fetchone()
            if exists:
                continue
            conn.execute(
                "INSERT INTO identities (platform, platform_user_id, platform_username, wallet) "
                "VALUES ('discord', ?, ?, ?)",
                (discord_id, discord_name, wallet),
            )
            migrated += 1
        conn.commit()
        return migrated
    finally:
        conn.close()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_identity.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add lfg_service/__init__.py lfg_service/identity.py tests/test_identity.py
git commit -m "feat(service): generalized identities table with resolve/link/migrate"
```

---

### Task 2: Service-token auth

**Files:**
- Create: `lfg_service/auth.py`
- Test: `tests/test_service_auth.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `service_tokens() -> dict[str, str]` — maps token → surface name, read from env `SERVICE_TOKEN_<SURFACE>` vars.
  - `surface_for_token(token: str | None) -> str | None` — returns surface name for a valid token, else None.
  - `require_service_token(handler)` — aiohttp decorator; rejects with 401 unless a valid `Authorization: Bearer <token>` is present; on success sets `request["surface"]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_service_auth.py
import asyncio
import types

import lfg_service.auth as auth


def test_surface_for_token(monkeypatch):
    monkeypatch.setenv("SERVICE_TOKEN_DISCORD", "tok-d")
    monkeypatch.setenv("SERVICE_TOKEN_TELEGRAM", "tok-t")
    assert auth.surface_for_token("tok-d") == "discord"
    assert auth.surface_for_token("tok-t") == "telegram"
    assert auth.surface_for_token("nope") is None
    assert auth.surface_for_token(None) is None


def _fake_request(headers):
    req = types.SimpleNamespace()
    req.headers = headers
    req.store = {}
    req.__setitem__ = req.store.__setitem__
    req.__getitem__ = req.store.__getitem__
    return req


def test_require_service_token_rejects_missing(monkeypatch):
    monkeypatch.setenv("SERVICE_TOKEN_DISCORD", "tok-d")

    @auth.require_service_token
    async def handler(request):
        return "ok"

    resp = asyncio.run(handler(_fake_request({})))
    assert resp.status == 401


def test_require_service_token_accepts_valid_and_tags_surface(monkeypatch):
    monkeypatch.setenv("SERVICE_TOKEN_DISCORD", "tok-d")
    seen = {}

    @auth.require_service_token
    async def handler(request):
        seen["surface"] = request["surface"]
        return "ok"

    result = asyncio.run(handler(_fake_request({"Authorization": "Bearer tok-d"})))
    assert result == "ok"
    assert seen["surface"] == "discord"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_service_auth.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'lfg_service.auth'`.

- [ ] **Step 3: Write minimal implementation**

```python
# lfg_service/auth.py
# Service-token auth: gates which trusted surface PROCESS may call the API.
# Distinct from end-user (HMAC session) auth — see webapp session tokens.

import os

from aiohttp import web


def service_tokens() -> dict[str, str]:
    """token -> surface name, from SERVICE_TOKEN_<SURFACE> env vars."""
    out: dict[str, str] = {}
    prefix = "SERVICE_TOKEN_"
    for key, value in os.environ.items():
        if key.startswith(prefix) and value:
            out[value] = key[len(prefix):].lower()
    return out


def surface_for_token(token: str | None) -> str | None:
    if not token:
        return None
    return service_tokens().get(token)


def _bearer(request) -> str | None:
    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer "):
        return header[len("Bearer "):]
    return None


def require_service_token(handler):
    async def wrapper(request):
        surface = surface_for_token(_bearer(request))
        if not surface:
            return web.json_response({"error": "unauthorized", "code": "bad_service_token"}, status=401)
        request["surface"] = surface
        return await handler(request)

    return wrapper
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_service_auth.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add lfg_service/auth.py tests/test_service_auth.py
git commit -m "feat(service): per-surface service-token auth"
```

---

### Task 3: EventBus protocol + InMemoryEventBus

**Files:**
- Create: `lfg_service/events.py`
- Test: `tests/test_events.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `Event` dataclass: fields `type: str`, `ts: int`, `identity: dict | None`, `wallet: str | None`, `data: dict`. Method `to_dict() -> dict`.
  - `EventBus` Protocol: `async publish(event: Event) -> None`; `subscribe(predicate: Callable[[Event], bool]) -> AbstractAsyncContextManager[AsyncIterator[Event]]`.
  - `InMemoryEventBus` implementing the protocol (each subscriber gets its own `asyncio.Queue`; publish fans out to all matching subscribers; non-matching events are skipped per subscriber).

**Notes:** `predicate` lets `/events` pass `lambda e: True` (firehose, optionally type-filtered) and `/events/me` pass `lambda e: e.wallet == my_wallet`. The contract tests here are what a future Redis implementation must also pass (spec §6).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_events.py
import asyncio

import pytest

from lfg_service.events import Event, InMemoryEventBus


def _evt(type_, wallet):
    return Event(type=type_, ts=1, identity=None, wallet=wallet, data={"n": 1})


@pytest.mark.asyncio
async def test_subscriber_receives_matching_event():
    bus = InMemoryEventBus()
    async with bus.subscribe(lambda e: True) as stream:
        await bus.publish(_evt("mint.completed", "rA"))
        evt = await asyncio.wait_for(stream.__anext__(), timeout=1)
    assert evt.type == "mint.completed"
    assert evt.wallet == "rA"


@pytest.mark.asyncio
async def test_predicate_filters_out_other_users():
    bus = InMemoryEventBus()
    async with bus.subscribe(lambda e: e.wallet == "rME") as stream:
        await bus.publish(_evt("mint.completed", "rOTHER"))  # filtered out
        await bus.publish(_evt("mint.completed", "rME"))     # delivered
        evt = await asyncio.wait_for(stream.__anext__(), timeout=1)
    assert evt.wallet == "rME"


@pytest.mark.asyncio
async def test_two_subscribers_both_receive():
    bus = InMemoryEventBus()
    async with bus.subscribe(lambda e: True) as s1, bus.subscribe(lambda e: True) as s2:
        await bus.publish(_evt("swap.completed", "rA"))
        e1 = await asyncio.wait_for(s1.__anext__(), timeout=1)
        e2 = await asyncio.wait_for(s2.__anext__(), timeout=1)
    assert e1.type == e2.type == "swap.completed"


def test_event_to_dict():
    d = _evt("mint.failed", "rA").to_dict()
    assert d == {"type": "mint.failed", "ts": 1, "identity": None, "wallet": "rA", "data": {"n": 1}}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_events.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'lfg_service.events'`.

(Note: `pytest-asyncio` is required; if `@pytest.mark.asyncio` is unrecognized, add `asyncio_mode = auto` under `[tool.pytest.ini_options]` in `pyproject.toml` and add `pytest-asyncio` to `requirements.txt` as part of this task's commit.)

- [ ] **Step 3: Write minimal implementation**

```python
# lfg_service/events.py
# In-process pub/sub event bus. The EventBus protocol is the seam a future
# Redis Streams implementation drops into (spec §6); these semantics are the
# contract that implementation must also satisfy.

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class Event:
    type: str
    ts: int
    identity: dict | None
    wallet: str | None
    data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "type": self.type,
            "ts": self.ts,
            "identity": self.identity,
            "wallet": self.wallet,
            "data": self.data,
        }


class EventBus(Protocol):
    async def publish(self, event: Event) -> None: ...

    def subscribe(self, predicate: Callable[[Event], bool]): ...


class InMemoryEventBus:
    def __init__(self) -> None:
        self._subscribers: set[tuple[Callable[[Event], bool], asyncio.Queue]] = set()

    async def publish(self, event: Event) -> None:
        for predicate, queue in list(self._subscribers):
            try:
                if predicate(event):
                    queue.put_nowait(event)
            except Exception:
                # a misbehaving predicate or full queue must not break fan-out
                pass

    @asynccontextmanager
    async def subscribe(self, predicate: Callable[[Event], bool]) -> AsyncIterator[AsyncIterator[Event]]:
        queue: asyncio.Queue = asyncio.Queue()
        entry = (predicate, queue)
        self._subscribers.add(entry)

        async def _stream() -> AsyncIterator[Event]:
            while True:
                yield await queue.get()

        try:
            yield _stream()
        finally:
            self._subscribers.discard(entry)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_events.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add lfg_service/events.py tests/test_events.py requirements.txt pyproject.toml
git commit -m "feat(service): in-memory EventBus with subscribe-predicate contract"
```

---

### Task 4: Wire identity into the webapp + ensure tables on startup

**Files:**
- Modify: `webapp/server.py` (imports near line 24; `create_app` near line 448; `handle_me` ~168; `handle_register` ~181)
- Test: `tests/test_server_identity_wiring.py`

**Interfaces:**
- Consumes: `lfg_service.identity.{ensure_identities_table, migrate_users_to_identities, resolve, link}`.
- Produces: server reads/writes identity through `lfg_service.identity` (keyed `platform='discord'` for the Activity), and ensures+migrates tables at app creation.

**Why:** the Activity is a `discord` surface; routing it through `identity` proves the generalized model end-to-end before other surfaces exist, and keeps `Users`/`identities` in sync during transition.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_server_identity_wiring.py
import lfg_service.identity as identity
from webapp import server


def test_create_app_ensures_and_migrates(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    monkeypatch.setattr(identity, "DATABASE", str(db))
    called = {}
    monkeypatch.setattr(identity, "ensure_identities_table", lambda: called.setdefault("ensure", True))
    monkeypatch.setattr(identity, "migrate_users_to_identities", lambda: called.setdefault("migrate", 0))
    server.create_app()
    assert called.get("ensure") is True
    assert "migrate" in called
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_server_identity_wiring.py -v`
Expected: FAIL (assertion error — `create_app` does not yet call identity setup).

- [ ] **Step 3: Write minimal implementation**

Add to imports (after line 25, `from user_db import ...`):

```python
from lfg_service import identity as identity_store
```

In `handle_register`, replace the `register_user` call with a dual write (keep `Users` in sync during transition):

```python
    if not await asyncio.to_thread(register_user, user["id"], user["name"], wallet):
        return web.json_response({"error": "registration failed", "code": "register_failed"}, status=500)
    await asyncio.to_thread(identity_store.link, "discord", user["id"], user["name"], wallet)
    return web.json_response({"ok": True, "wallet": wallet})
```

In `handle_me`, resolve the wallet via identity (fall back to legacy `get_user`):

```python
    wallet = await asyncio.to_thread(identity_store.resolve, "discord", user["id"])
    if wallet is None:
        record = await asyncio.to_thread(get_user, user["id"])
        wallet = record["address"] if record else None
    return web.json_response({"id": user["id"], "username": user["name"], "wallet": wallet})
```

In `create_app`, after `app = web.Application()` and before route registration, ensure tables:

```python
    identity_store.ensure_identities_table()
    identity_store.migrate_users_to_identities()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_server_identity_wiring.py tests/test_identity.py webapp/test_smoke.py -v`
Expected: PASS (smoke test still green).

- [ ] **Step 5: Commit**

```bash
git add webapp/server.py tests/test_server_identity_wiring.py
git commit -m "feat(service): route Activity register/me through generalized identity"
```

---

### Task 5: `/api/session` — surface-asserted user sessions

**Files:**
- Modify: `webapp/server.py` (add handler; register route in `create_app`)
- Test: `tests/test_session_endpoint.py`

**Interfaces:**
- Consumes: `lfg_service.auth.require_service_token`; existing `make_session_token(user: dict) -> str`.
- Produces: `POST /api/session` — body `{platform_user_id, platform_username}`; requires a valid service token; returns `{session_token, user: {id, username}}`. The asserted `id` is the platform user-id; the surface name comes from the validated service token (`request["surface"]`). This is how bots (Discord/Telegram) obtain a user session without OAuth.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_session_endpoint.py
import pytest
from aiohttp.test_utils import TestClient, TestServer

from webapp import server


@pytest.fixture
async def client(monkeypatch, tmp_path):
    import lfg_service.identity as identity
    monkeypatch.setattr(identity, "DATABASE", str(tmp_path / "t.db"))
    monkeypatch.setenv("SERVICE_TOKEN_TELEGRAM", "tok-t")
    app = server.create_app()
    async with TestClient(TestServer(app)) as c:
        yield c


async def test_session_requires_service_token(client):
    resp = await client.post("/api/session", json={"platform_user_id": "5", "platform_username": "x"})
    assert resp.status == 401


async def test_session_issues_token(client):
    resp = await client.post(
        "/api/session",
        json={"platform_user_id": "5", "platform_username": "neo"},
        headers={"Authorization": "Bearer tok-t"},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["user"] == {"id": "5", "username": "neo"}
    assert body["session_token"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_session_endpoint.py -v`
Expected: FAIL with 404 (route not registered).

- [ ] **Step 3: Write minimal implementation**

Add handler (near the other handlers, e.g. after `handle_token`):

```python
from lfg_service.auth import require_service_token


@require_service_token
async def handle_session(request):
    body = await request.json()
    pid = (body.get("platform_user_id") or "").strip()
    pname = (body.get("platform_username") or "").strip()
    if not pid:
        return web.json_response({"error": "missing platform_user_id", "code": "bad_request"}, status=400)
    token = make_session_token({"id": pid, "name": pname})
    return web.json_response({"session_token": token, "user": {"id": pid, "username": pname}})
```

Register in `create_app` (with the other `add_post` lines):

```python
    app.router.add_post("/api/session", handle_session)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_session_endpoint.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add webapp/server.py tests/test_session_endpoint.py
git commit -m "feat(service): POST /api/session for surface-asserted user sessions"
```

---

### Task 6: Event publishing from flows + `/events` and `/events/me` WebSockets

**Files:**
- Modify: `webapp/server.py` (instantiate a module-level `InMemoryEventBus`; add two WS handlers; register routes; publish on mint/swap terminal transitions)
- Test: `tests/test_event_endpoints.py`

**Interfaces:**
- Consumes: `lfg_service.events.{Event, InMemoryEventBus}`; `lfg_service.auth.surface_for_token`; existing `verify_session_token(token) -> dict | None`; `lfg_service.identity.resolve`.
- Produces:
  - module-level `BUS = InMemoryEventBus()` in `webapp/server.py`.
  - `async def publish_event(type_, identity, wallet, data) -> None` helper.
  - `GET /events` — service-token auth (query `?token=` or `Authorization`), optional `?types=mint.completed,swap.completed` filter, streams matching events as JSON text frames (firehose).
  - `GET /events/me` — user-session-token auth (`?token=<session_token>`), resolves caller → wallet, streams **only** events whose `wallet` equals the caller's wallet.

**Note on publishing:** mint/swap sessions run as background tasks in `mint_flow`/`swap_flow`. To avoid editing `lfg_core` in this plan, publish from the **server** side: when `handle_mint_status` (or a small watcher) observes a session entering a terminal success/fail state for the first time, call `publish_event`. Implement via a per-session `published` flag checked in the status handler. (Pushing publication into `lfg_core` flows directly is a documented follow-up in Plan 3 when the bot needs it without polling.)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_event_endpoints.py
import asyncio
import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

import lfg_service.identity as identity
from webapp import server


@pytest.fixture
async def client(monkeypatch, tmp_path):
    monkeypatch.setattr(identity, "DATABASE", str(tmp_path / "t.db"))
    monkeypatch.setenv("SERVICE_TOKEN_DISCORD", "tok-d")
    app = server.create_app()
    identity.ensure_identities_table()
    identity.link("discord", "42", "me", "rME")
    async with TestClient(TestServer(app)) as c:
        yield c


async def test_events_requires_service_token(client):
    resp = await client.get("/events")
    assert resp.status == 401


async def test_events_me_streams_only_own_wallet(client):
    token = server.make_session_token({"id": "42", "name": "me"})
    ws = await client.ws_connect(f"/events/me?token={token}")
    # publish one event for this user and one for another
    await server.publish_event("mint.completed", {"platform": "discord", "platform_user_id": "99"}, "rOTHER", {"n": 0})
    await server.publish_event("mint.completed", {"platform": "discord", "platform_user_id": "42"}, "rME", {"n": 1})
    msg = await asyncio.wait_for(ws.receive(), timeout=2)
    payload = json.loads(msg.data)
    assert payload["wallet"] == "rME"
    assert payload["data"]["n"] == 1
    await ws.close()


async def test_events_firehose_receives_all(client):
    ws = await client.ws_connect("/events?token=tok-d")
    await server.publish_event("swap.completed", {"platform": "discord", "platform_user_id": "1"}, "rANY", {})
    msg = await asyncio.wait_for(ws.receive(), timeout=2)
    assert json.loads(msg.data)["type"] == "swap.completed"
    await ws.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_event_endpoints.py -v`
Expected: FAIL (404 / `publish_event` undefined).

- [ ] **Step 3: Write minimal implementation**

Add near the top of `webapp/server.py` (after imports):

```python
from lfg_service.auth import surface_for_token
from lfg_service.events import Event, InMemoryEventBus

BUS = InMemoryEventBus()


async def publish_event(type_: str, identity_obj, wallet, data) -> None:
    await BUS.publish(Event(type=type_, ts=int(time.time()), identity=identity_obj, wallet=wallet, data=data or {}))
```

Add the two WS handlers:

```python
async def _ws_stream(request, predicate):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    async with BUS.subscribe(predicate) as stream:
        async for event in stream:
            if ws.closed:
                break
            await ws.send_str(json.dumps(event.to_dict()))
    return ws


async def handle_events(request):
    token = request.query.get("token") or (
        request.headers.get("Authorization", "").removeprefix("Bearer ")
    )
    if not surface_for_token(token):
        return web.json_response({"error": "unauthorized", "code": "bad_service_token"}, status=401)
    types_param = request.query.get("types")
    allowed = set(types_param.split(",")) if types_param else None
    return await _ws_stream(request, lambda e: allowed is None or e.type in allowed)


async def handle_events_me(request):
    payload = verify_session_token(request.query.get("token", ""))
    if not payload:
        return web.json_response({"error": "unauthorized", "code": "bad_session"}, status=401)
    wallet = await asyncio.to_thread(identity_store.resolve, "discord", payload["id"])
    if wallet is None:
        return web.json_response({"error": "no wallet", "code": "no_wallet"}, status=403)
    return await _ws_stream(request, lambda e: e.wallet == wallet)
```

Register routes in `create_app`:

```python
    app.router.add_get("/events", handle_events)
    app.router.add_get("/events/me", handle_events_me)
```

Publish on terminal transition: in `handle_mint_status` (after fetching the session, before returning), add a one-shot publish:

```python
    if session.state in mint_flow.TERMINAL_STATES and not getattr(session, "_published", False):
        session._published = True
        ok = session.state == mint_flow.OFFER_READY
        await publish_event(
            "mint.completed" if ok else "mint.failed",
            {"platform": "discord", "platform_user_id": session.discord_id},
            session.wallet_address,
            session.to_dict(),
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_event_endpoints.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add webapp/server.py tests/test_event_endpoints.py
git commit -m "feat(service): /events firehose + self-filtered /events/me + mint event publish"
```

---

### Task 7: Promote `webapp/server.py` → `lfg_service/app.py`

**Files:**
- Create: `lfg_service/app.py` (moved content)
- Modify: `webapp/__init__.py` (re-export shim), `webapp/test_smoke.py` (import path), `README`/run docs if present
- Test: existing `tests/` + `webapp/test_smoke.py` (now importing from `lfg_service`)

**Interfaces:**
- Consumes: everything built in Tasks 1–6.
- Produces: canonical entrypoint `python -m lfg_service.app`; `lfg_service.app.create_app()`. `webapp` keeps a thin re-export so nothing in-flight breaks.

**Note:** mechanical move. The browser client dir (`webapp/client`) can stay put for this plan; `CLIENT_DIR` in the moved file is updated to point at it absolutely. Physically relocating `client/` into `lfg_service/` is deferred to the Web UI plan (#42) to keep this task low-risk.

- [ ] **Step 1: Move the file and add a shim**

```bash
git mv webapp/server.py lfg_service/app.py
```

In `lfg_service/app.py`, update `CLIENT_DIR` to resolve the existing client dir absolutely:

```python
CLIENT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "webapp", "client"
)
```

Add `webapp/__init__.py` re-export so old imports keep working:

```python
# webapp/__init__.py — compatibility shim; the service now lives in lfg_service.app
from lfg_service import app as server  # noqa: F401
```

- [ ] **Step 2: Update test + smoke imports**

In `webapp/test_smoke.py` and every `from webapp import server` in `tests/`, change to:

```python
from lfg_service import app as server
```

- [ ] **Step 3: Run the full suite to verify nothing broke**

Run: `pytest tests/ webapp/test_smoke.py -v`
Expected: PASS (all tasks' tests green under the new path).

- [ ] **Step 4: Update run docs**

Replace any `python -m webapp.server` invocation in `README`/docs with `python -m lfg_service.app`. (Search: `grep -rn "webapp.server" --include=*.md .`)

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "refactor(service): promote webapp/server.py to lfg_service/app.py"
```

---

## Self-Review

**Spec coverage:**
- §4.1 service auth → Task 2; two-layer model preserved (user-session HMAC untouched, service-token added).
- §4.2 REST `/api/session` → Task 5; other routes pre-exist and are promoted in Task 7.
- §4.3 identity model + `identities` table + migration → Task 1; wired into the live surface in Task 4.
- §4.4 `/events` (service-token) + `/events/me` (user-scoped, self-filtered) → Task 6; security-critical scoping test present (`test_events_me_streams_only_own_wallet`).
- §6 EventBus interface + in-memory impl + contract tests → Task 3 (no Redis dependency added).
- §8 tests: identity migration idempotence (Task 1), EventBus contract (Task 3), `/events/me` scoping (Task 6), smoke migrated (Task 7).

**Deferred to later plans (correctly out of scope):** surface SDK, Discord `main.py` inversion, Telegram, pushing event publication into `lfg_core` flows (Task 6 note), relocating `client/`.

**Placeholder scan:** none — every code/test step contains concrete code and exact commands.

**Type consistency:** `resolve(platform, platform_user_id)`, `link(platform, platform_user_id, platform_username, wallet)`, `Event(type, ts, identity, wallet, data)`, `surface_for_token(token)`, `publish_event(type_, identity_obj, wallet, data)` are used consistently across Tasks 1, 4, 5, 6.

**Note on swap events:** Task 6 wires mint publication via the status handler; swap publication mirrors it exactly in the swap status handler when the Telegram plan needs it (the `/events` firehose and bus are swap-ready today). Mint is sufficient to prove the bus this plan.
