# Surface SDK — Design Spec (Spine Plan 2 of 4)

**Date:** 2026-06-25
**Parent spec:** `docs/superpowers/specs/2026-06-17-shared-services-spine-design.md` (§5.1, §7, §8, §9 step 2)
**Issues:** forward-looking for #53 (Discord migration, Plan 3) and #43 (Telegram, Plan 4) — both consume this SDK.
**Status:** Approved design, pre-implementation.

## 1. Problem

Plan 1 shipped the `lfg_service` spine: a REST surface
(`/api/session`, `/api/register`, `/api/me`, `/api/mint*`, `/api/swap*`,
`/api/signin*`, `/api/nfts`, `/api/economy`, `/api/equip*`, `/api/harvest*`,
`/api/assemble*`, `/api/qr.png`, `/api/img`, `/api/config`) plus two WebSocket
channels (`/events` firehose with service-token auth, `/events/me` user-scoped).

Plans 3 (Discord, #53) and 4 (Telegram, #43) each become a **thin surface
process** that drives this service. Without a shared client, every surface would
re-implement the same plumbing: Bearer service-token headers, minting and
caching per-user HMAC session tokens, retry/backoff on transient failures,
`{error, code}` → exception mapping, and a reconnecting WebSocket event loop.
That duplication is exactly the divergence the spine exists to prevent — the
client-side analogue of the server-side `lfg_core` sharing.

This spec defines the **Surface SDK** (`surfaces/_client/`): one async Python
client that wraps the whole service contract so each surface is a thin adapter.

## 2. Goals & Non-Goals

### Goals
- One authoritative async client, `LFGServiceClient`, wrapping **every** service
  endpoint (complete coverage, not just the immediate Discord/Telegram subset).
- Per-identity **user-session token** management inside the SDK: lazily mint via
  `POST /api/session`, cache per `user_id`, refresh on 401 — callers never juggle
  tokens.
- **Retry/backoff** on transient REST failures, reusing the existing
  `RETRY_MAX_ATTEMPTS` / `RETRY_BASE_DELAY` knobs.
- A **reconnecting** `/events` subscription exposed as an async iterator, so a
  dropped WebSocket is invisible to the caller.
- Typed exceptions mapped from `{error, code}` + HTTP status, so surfaces map
  codes to platform-friendly messages.
- Full unit coverage against a **mock service** (no `lfg_core` dependency).

### Non-Goals (this spec)
- The Discord (#53) and Telegram (#43) surface adapters themselves — Plans 3–4.
  This plan delivers only the client they will import.
- A synchronous client. Both `discord.py` and `python-telegram-bot` run asyncio
  event loops and the service is aiohttp; the SDK is async-only.
- `/events/me` (user-scoped, browser-facing) helper methods. Bots use the
  service-token `/events` firehose; the browser talks to the service directly.
  `events()` covers the firehose; a `user_session` filter is a later add if a
  surface ever needs per-user WS.
- Pushing event publication into `lfg_core` flows (a Plan 3 follow-up) — the SDK
  consumes whatever the service already publishes.
- Packaging/publishing the SDK as a standalone distributable — it is an in-repo
  package imported by sibling surface processes.

## 3. Architecture

A single client class owns one `aiohttp.ClientSession` for REST and a
reconnecting WebSocket for events. Used as an async context manager so the
session is opened and closed deterministically.

```
surfaces/                    # surface processes live here (discord_bot/, telegram_bot/ — Plans 3-4)
  __init__.py
  _client/                   # the shared SDK (this spec)
    __init__.py              #   public exports: LFGServiceClient, Event, ServiceError + subclasses
    client.py                #   LFGServiceClient: construction, ClientSession, all REST methods,
                             #     per-identity session-token cache, 401 refresh-and-retry
    events.py                #   events() async-iterator + reconnect-with-backoff
    errors.py                #   exception hierarchy + _raise_for_status(resp)
    _retry.py                #   backoff helper (reuses RETRY_MAX_ATTEMPTS / RETRY_BASE_DELAY)
```

The client-side event type is **re-exported from `lfg_service.events.Event`** —
one dataclass on both sides of the wire, no divergence. Importing `lfg_service`
from `surfaces/_client` is acceptable: they ship in the same repo and the SDK is
intrinsically a client *of* that service.

### Module responsibilities

- **`client.py`** — `LFGServiceClient`. Construction/config, the lazily-opened
  shared `ClientSession`, every REST method, and the per-`user_id` session-token
  cache (mint-once, refresh-on-401). Delegates backoff to `_retry.py`, error
  mapping to `errors.py`, and the WS loop to `events.py`.
- **`events.py`** — `events(types=None)`: an async generator that connects the
  WS, yields `Event`s, and on any drop reconnects with backoff. The `types`
  filter is passed through as the `?types=` query the service already honours.
- **`errors.py`** — the exception hierarchy and `_raise_for_status(resp)`, which
  reads `{error, code}` and raises the right typed error for the status.
- **`_retry.py`** — a small `with_retry(coro_factory, *, max_attempts, base_delay,
  retry_on)` helper implementing exponential backoff. Shared by REST calls and
  the WS reconnect loop.

## 4. Public API

```python
LFGServiceClient(
    base_url: str,
    service_token: str,
    surface: str,                 # 'discord' | 'telegram' | ... — for logging/labels
    *,
    timeout: float = 30.0,
    max_attempts: int = RETRY_MAX_ATTEMPTS,
    base_delay: float = RETRY_BASE_DELAY,
)
# async context manager:
async with LFGServiceClient(base, tok, "discord") as c:
    ...
# or explicit: await c.close()
```

Where a `start_*` method takes `**kwargs`, those forward verbatim to the
corresponding service endpoint's existing JSON body (the SDK invents no new
request fields); the implementation plan pins each signature to the live route.

### 4.1 Identity / session

The SDK holds the **service token** and, on the first user-scoped call for a
`user_id`, mints a user-session token via `POST /api/session`, caches it, and
attaches it to subsequent user-scoped calls. A 401 on a user-scoped call evicts
the cached token, re-mints once, and retries the original call exactly once.

```python
await c.create_session(user_id: str, username: str) -> str   # low-level; rarely called directly
await c.register(user_id: str, username: str, wallet: str) -> dict
await c.me(user_id: str) -> dict
```

### 4.2 Mint

```python
await c.start_mint(user_id: str, **kwargs) -> dict       # -> {session_id, payment/QR data, ...}
await c.mint_status(session_id: str) -> dict
await c.regenerate(session_id: str) -> dict
await c.wait_for_mint(session_id: str, *, interval: float = 2.0, timeout: float = 180.0) -> dict
```

`wait_for_mint` polls `mint_status` until a terminal state — a backstop for
surfaces that do not wire `/events`. It is a convenience wrapper, not the primary
completion path.

### 4.3 Swap, sign-in, NFTs, economy

```python
await c.start_swap(user_id: str, **kwargs) -> dict
await c.swap_status(session_id: str) -> dict
await c.wait_for_swap(session_id: str, **kw) -> dict      # mirrors wait_for_mint

await c.signin_start(user_id: str) -> dict                # XUMM sign-in payload
await c.signin_status(payload_uuid: str) -> dict

await c.nfts(user_id: str) -> dict                        # owned NFTs
await c.economy(user_id: str) -> dict                     # require_wallet endpoint
await c.equip_start(user_id, **kw) -> dict;    await c.equip_status(session_id) -> dict
await c.harvest_start(user_id, **kw) -> dict;  await c.harvest_status(session_id) -> dict
await c.assemble_start(user_id, **kw) -> dict; await c.assemble_status(session_id) -> dict
```

### 4.4 Media / config

```python
await c.qr_png(uuid: str) -> bytes      # raw PNG bytes (binary endpoint)
await c.img(url: str) -> bytes          # CDN image proxy bytes
await c.config() -> dict                # public client config
```

### 4.5 Events

```python
async for ev in c.events(types: list[str] | None = None):
    # ev: lfg_service.events.Event  (type, ts, identity, wallet, data)
    ...
```

`events()` connects the `/events` firehose (service-token auth), passes
`types` through as the server-side `?types=` filter, and **reconnects with
backoff on any drop** — the `async for` loop never terminates on a transient
disconnect. Surfaces run it in a background task and fan out to channels/DMs.

## 5. Error Model

`errors.py` raises typed exceptions from the service's structured errors:

| Exception | Trigger | `.status` |
|---|---|---|
| `ServiceError` (base) | carries `.code`, `.message`, `.status` | — |
| `AuthError` | bad/expired service or session token | 401 |
| `BadRequest` | malformed request | 400 |
| `NotFound` | unknown session id / route | 404 |
| `ServiceUnavailable` | network error or 5xx after retries exhausted | 5xx / none |

`_raise_for_status(resp)` parses `{error, code}` from the body when present and
selects the subclass by HTTP status; an unmapped status falls back to
`ServiceError`. Surfaces catch these and render per-platform messages.

## 6. Retry & Reconnect

Both reuse `_retry.with_retry` (exponential backoff: `base_delay * 2 ** n`,
capped at `max_attempts`).

- **REST** retries on: connection errors, timeouts, HTTP 5xx, and 429.
  It does **not** retry 4xx — those are deterministic client errors — **except**
  that a 401 on a *user-scoped* call is handled one level up by the session
  cache (evict → re-mint → single retry), not by `with_retry`.
- **WS `events()`** retries the connect/stream loop on any disconnect or
  connection error, with the same backoff, indefinitely (a long-lived bot wants
  to stay subscribed). Backoff resets after a successful sustained connection.

This mirrors the spec parent §7 (surface SDK treats the service as fallible;
timeouts, retries with backoff, WS reconnects).

## 7. Testing

`tests/test_surface_sdk.py` runs the SDK against a **small mock aiohttp app**
defined in the test module — canned endpoints that mimic the service contract,
with **no** `lfg_core` import — so SDK tests are fast and isolated. The mock can
be told to fail N times then succeed, return 401 once, advance a mint session to
terminal across polls, and emit a queued event then drop the WS.

Required assertions (spec parent §8 "Surface SDK tests against a mock service"):

- **Auth header** — every REST call carries `Authorization: Bearer <service_token>`;
  user-scoped calls additionally carry the minted session token.
- **Session cache** — a user-session token is minted once for a `user_id` and
  reused across subsequent user-scoped calls (mock counts `/api/session` hits).
- **401 refresh-and-retry** — a user-scoped call that gets one 401 re-mints the
  session and retries once, then succeeds; a *persistent* 401 surfaces `AuthError`.
- **REST retry** — a call that gets N×5xx then 200 succeeds after backoff;
  exhausting `max_attempts` raises `ServiceUnavailable`; a 4xx raises immediately
  (no retry).
- **Typed errors** — 400/404/401 map to `BadRequest`/`NotFound`/`AuthError` with
  the correct `.code` parsed from the body.
- **Event reconnect** — `events()` yields an event, survives a forced WS drop,
  reconnects, and yields a subsequent event; the `types` filter is sent.
- **Binary endpoints** — `qr_png`/`img` return raw `bytes`, not decoded JSON.

Tests follow the repo-native sync style where practical (aiohttp
`TestServer`/`TestClient`), matching Plan 1's `tests/`.

## 8. Decomposition note

This is spine step 2 of 4. It delivers only the importable client. The
implementation plan (`docs/superpowers/plans/2026-06-25-surface-sdk.md`) sequences
it as: package skeleton + errors + retry helper → REST client with session cache
→ reconnecting `events()` → mock-service test suite. Plans 3 (Discord #53) and 4
(Telegram #43) import `surfaces._client.LFGServiceClient` and add no client
plumbing of their own.
