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

## Lifecycle — events() teardown

`events()` is an **infinite async generator** that reconnects automatically.
A consumer that `break`s out of `async for ev in svc.events()` without closing
the generator leaks the open WebSocket until the client itself is closed.

Always run it as a background task you cancel on shutdown:

```python
async def _listen(svc):
    async for ev in svc.events():
        await handle(ev)

task = asyncio.create_task(_listen(svc))
# ... on shutdown:
task.cancel()
await asyncio.gather(task, return_exceptions=True)
```

Or, if consuming inline, call `aclose()` when done:

```python
agen = svc.events()
try:
    async for ev in agen:
        ...
        break  # early exit
finally:
    await agen.aclose()
```

## Not wrapped (by design)

The following `lfg_service` endpoints are intentionally absent from this SDK:

- **`/events/me`** — user-scoped browser firehose (spec Non-Goal; bots use the
  `/events` service-token firehose wrapped above).
- **`/api/layer`** — browser image-proxy detail endpoint (browser-only concern).

Plan 3/4 authors: these omissions are deliberate, not gaps.
