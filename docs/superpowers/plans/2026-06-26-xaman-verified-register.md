# Xaman-Verified `/register` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the unverified manual `/register <wallet>` on both the Discord and Telegram bots with a Xaman sign-in flow that records the **verified** wallet address.

**Architecture:** The service already verifies ownership in `/api/signin` (on a signed Xaman approval it stores the proven address itself). This makes that flow platform-aware (replacing a discord-only gate with `(platform, id)`-keyed sign-in payloads), adds a `wait_for_signin` SDK poller mirroring `wait_for_mint`, and rebuilds each bot's `/register` to drive sign-in: start → show QR → poll → report. The bots stop calling `client.register()`; the service does the storage.

**Tech Stack:** Python 3, aiohttp (service), `python-telegram-bot` v22 + `discord.py` (adapters), the existing `surfaces._client.LFGServiceClient`, repo-native sync tests (`asyncio.new_event_loop()` + direct call — NOT pytest-asyncio).

## Global Constraints

- **Backward compatibility:** every platform default is `"discord"`; the **webapp's** sign-in (tokens default `platform="discord"`) must behave byte-for-byte as before. A regression test pins it.
- **Legacy `Users` write stays discord-only:** on a signed sign-in, `register_user(...)` is called only when the sign-in payload's `platform == "discord"` (preserves the Greptile P1 fix — non-discord platforms live in `identities` only). `identity_store.link(...)` is called for every platform.
- **Cross-surface isolation:** sign-in payloads are keyed by `(platform, user_id)`; a token of one platform must get `404` on another platform's payload.
- **Bots drop the manual path:** `/register` takes **no** wallet argument on either bot; the bots no longer call `client.register()`. The `POST /api/register` endpoint and `client.register()` SDK method are **kept** (webapp/admin), just bot-unused.
- **Telegram package never imports `discord`.** Reuse `surfaces/_shared/*` for cross-surface helpers.
- **Test style:** repo-native sync (`def test_...` + a `_run(coro)` loop helper); seeds, when needed, use the throwaway `sEdSKaCy2JT7JaM7v95H9SxkhP9wS2r`.
- **mypy:** `lfg_service.app` + `surfaces.*` are in the relaxed override; run the FULL `.venv/bin/mypy .` before claiming clean.
- **SourceTag unaffected:** sign-in builds a XUMM SignIn payload service-side (`xumm_ops.create_signin_payload`); no new inline XRPL tx in any surface.

---

### Task 1: Service — platform-aware sign-in

**Files:**
- Modify: `lfg_service/app.py` — `handle_signin_start` (~502-522), `handle_signin_status` (~525-566)
- Test: `tests/test_service_signin_platform.py` (create)
- Modify: `tests/test_service_platform_register.py` — delete the two PR-B discord-gate sign-in tests (`test_signin_status_rejects_non_discord`, `test_signin_start_rejects_non_discord`); they assumed sign-in is discord-only, which this task reverses.

**Interfaces:**
- Produces: `signin_payloads[uuid]` record shape `{platform, user_id, name, created_at}` (was `{discord_id, name, created_at}`). `POST /api/signin` stores `platform=_platform(user)`, `user_id=user["id"]` and returns `{uuid, signin_link}`. `GET /api/signin/{uuid}` ownership is `(platform, user_id)`; on signed it returns `{"state":"signed","wallet":<addr>}`, links under `rec["platform"]`, and writes the legacy `Users` table only when `rec["platform"] == "discord"`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_service_signin_platform.py
import asyncio
import time

import lfg_service.app as app
from lfg_service.app import make_session_token


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _Req:
    def __init__(self, token, match_info=None):
        self.headers = {"Authorization": f"Bearer {token}"}
        self.match_info = match_info or {}
        self._store: dict = {}

    def __getitem__(self, k):
        return self._store[k]

    def __setitem__(self, k, v):
        self._store[k] = v


def test_signin_start_tags_platform(monkeypatch):
    monkeypatch.setattr(app.config, "WEBAPP_DEV_MODE", False)

    async def fake_create(return_url=None):
        return {"uuid": "u1", "xumm_url": "https://xumm.app/sign/abc"}

    monkeypatch.setattr(app.xumm_ops, "create_signin_payload", fake_create)
    app.signin_payloads.pop("u1", None)
    token = make_session_token({"id": "55", "name": "tg", "platform": "telegram"})
    resp = _run(app.handle_signin_start(_Req(token)))
    assert resp.status == 200
    rec = app.signin_payloads["u1"]
    assert rec["platform"] == "telegram" and rec["user_id"] == "55"
    app.signin_payloads.pop("u1", None)


def test_signin_status_cross_platform_404(monkeypatch):
    monkeypatch.setattr(app.config, "WEBAPP_DEV_MODE", False)
    app.signin_payloads["u2"] = {
        "platform": "telegram", "user_id": "55", "name": "tg", "created_at": time.time()
    }
    # a discord:55 token must NOT be able to read the telegram:55 payload
    token = make_session_token({"id": "55", "name": "d", "platform": "discord"})
    resp = _run(app.handle_signin_status(_Req(token, {"payload_uuid": "u2"})))
    assert resp.status == 404
    app.signin_payloads.pop("u2", None)


def test_signin_signed_links_under_platform_no_legacy(monkeypatch):
    monkeypatch.setattr(app.config, "WEBAPP_DEV_MODE", False)
    legacy = {"called": False}
    linked = {}
    app.signin_payloads["u3"] = {
        "platform": "telegram", "user_id": "55", "name": "tg", "created_at": time.time()
    }

    async def fake_status(uuid):
        return {"signed": True, "account": "rXRPL", "opened": True, "expired": False}

    monkeypatch.setattr(app.xumm_ops, "get_payload_status", fake_status)
    monkeypatch.setattr(app, "is_valid_classic_address", lambda w: True)

    def fake_reg(uid, name, w):
        legacy["called"] = True
        return True

    monkeypatch.setattr(app, "register_user", fake_reg)

    def fake_link(platform, uid, name, wallet):
        linked["args"] = (platform, uid, wallet)
        return True

    monkeypatch.setattr(app.identity_store, "link", fake_link)
    token = make_session_token({"id": "55", "name": "tg", "platform": "telegram"})
    resp = _run(app.handle_signin_status(_Req(token, {"payload_uuid": "u3"})))
    assert resp.status == 200
    assert linked["args"] == ("telegram", "55", "rXRPL")
    assert legacy["called"] is False  # non-discord: identities only


def test_signin_signed_discord_writes_legacy(monkeypatch):
    monkeypatch.setattr(app.config, "WEBAPP_DEV_MODE", False)
    legacy = {}
    app.signin_payloads["u4"] = {
        "platform": "discord", "user_id": "9", "name": "d", "created_at": time.time()
    }

    async def fake_status(uuid):
        return {"signed": True, "account": "rDISCORD", "opened": True, "expired": False}

    monkeypatch.setattr(app.xumm_ops, "get_payload_status", fake_status)
    monkeypatch.setattr(app, "is_valid_classic_address", lambda w: True)
    monkeypatch.setattr(app, "register_user", lambda uid, name, w: legacy.update(args=(uid, name, w)) or True)
    monkeypatch.setattr(app.identity_store, "link", lambda *a: True)
    token = make_session_token({"id": "9", "name": "d", "platform": "discord"})
    resp = _run(app.handle_signin_status(_Req(token, {"payload_uuid": "u4"})))
    assert resp.status == 200
    assert legacy["args"] == ("9", "d", "rDISCORD")  # discord still writes legacy Users
```

- [ ] **Step 2: Run them — RED**

Run: `.venv/bin/pytest tests/test_service_signin_platform.py -v`
Expected: FAIL — `handle_signin_start` still gates non-discord to 404 / stores `discord_id`; `handle_signin_status` still discord-gated.

- [ ] **Step 3: Implement — `handle_signin_start`**

Replace the body (drop the discord gate; tag the record with platform + user_id):

```python
@require_auth
async def handle_signin_start(request):
    """Create a XUMM SignIn payload; the user scans it in Xaman and their
    wallet address is captured on approval — no manual address entry."""
    user = request["user"]
    _prune_signin_payloads()
    payload = await xumm_ops.create_signin_payload(return_url=await _request_return_url(request))
    if not payload:
        return web.json_response({"error": "could not reach Xaman"}, status=502)
    signin_payloads[payload["uuid"]] = {
        "platform": _platform(user),
        "user_id": user["id"],
        "name": user["name"],
        "created_at": time.time(),
    }
    return web.json_response({"uuid": payload["uuid"], "signin_link": payload["xumm_url"]})
```

- [ ] **Step 4: Implement — `handle_signin_status`**

```python
@require_auth
async def handle_signin_status(request):
    uuid = request.match_info["payload_uuid"]
    rec = signin_payloads.get(uuid)
    # Ownership keyed by (platform, user_id) — cross-surface isolation: a
    # colliding numeric id on another platform cannot read/complete this payload.
    if (
        not rec
        or rec["user_id"] != request["user"]["id"]
        or rec["platform"] != _platform(request["user"])
    ):
        return web.json_response({"error": "not found"}, status=404)
    s = await xumm_ops.get_payload_status(uuid)
    if not s:
        return web.json_response({"error": "could not reach Xaman"}, status=502)
    if s["signed"] and s["account"] and is_valid_classic_address(s["account"]):
        platform = rec["platform"]
        # Legacy Users table is keyed by discord_id with no platform column —
        # only discord writes it; other platforms live in identities only.
        if platform == "discord":
            if not await asyncio.to_thread(
                register_user, rec["user_id"], rec["name"], s["account"]
            ):
                return web.json_response({"error": "registration failed"}, status=500)
        linked = await asyncio.to_thread(
            identity_store.link, platform, rec["user_id"], rec["name"], s["account"]
        )
        if not linked:
            logging.error(
                "identity.link failed for %s:%s — /events/me may 403 until restart-migrate",
                platform,
                rec["user_id"],
            )
        del signin_payloads[uuid]
        return web.json_response({"state": "signed", "wallet": s["account"]})
    if s["expired"]:
        del signin_payloads[uuid]
        return web.json_response({"state": "expired"})
    return web.json_response({"state": "opened" if s["opened"] else "pending"})
```

- [ ] **Step 5: Delete the now-obsolete PR-B gate tests**

In `tests/test_service_platform_register.py`, delete `test_signin_status_rejects_non_discord` and `test_signin_start_rejects_non_discord` (their premise — sign-in is discord-only — is reversed here; the new isolation behavior is covered by `test_signin_status_cross_platform_404`). Leave the `handle_register` tests intact.

- [ ] **Step 6: GREEN + regression**

Run: `.venv/bin/pytest tests/test_service_signin_platform.py tests/ -k "signin or register" -q`
Expected: PASS — new tests green; the webapp/discord sign-in paths (default `discord`) unchanged; no leftover references to the deleted tests.

- [ ] **Step 7: Commit**

```bash
git add lfg_service/app.py tests/test_service_signin_platform.py tests/test_service_platform_register.py
git commit -m "feat(service): platform-aware Xaman sign-in (replaces discord-only gate)"
```

---

### Task 2: SDK — `wait_for_signin`

**Files:**
- Modify: `surfaces/_client/client.py` (add `SIGNIN_TERMINAL` near `MINT_TERMINAL` ~24; add `wait_for_signin` near `signin_status` ~290)
- Test: `tests/test_sdk_signin_poll.py` (create)

**Interfaces:**
- Consumes: the existing `signin_status(user_id, uuid)` and `_poll(fetch, terminal, interval, timeout, sleep)`.
- Produces: `SIGNIN_TERMINAL: frozenset[str] = frozenset({"signed", "expired"})`; `async wait_for_signin(self, user_id, uuid, *, interval=2.0, timeout=180.0, sleep=asyncio.sleep) -> dict[str, Any]` — polls `signin_status` until a terminal state, returning the last status (caller treats a non-terminal timeout like "expired").

- [ ] **Step 1: Write the failing test**

```python
# tests/test_sdk_signin_poll.py
import asyncio

from surfaces._client.client import SIGNIN_TERMINAL, LFGServiceClient


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_signin_terminal_set():
    assert SIGNIN_TERMINAL == frozenset({"signed", "expired"})


def test_wait_for_signin_returns_on_signed(monkeypatch):
    c = LFGServiceClient("http://svc", "tok", "telegram")
    states = [
        {"state": "pending"},
        {"state": "opened"},
        {"state": "signed", "wallet": "rXRPL"},
    ]

    async def fake_status(user_id, uuid):
        return states.pop(0)

    async def no_sleep(_):
        return None

    monkeypatch.setattr(c, "signin_status", fake_status)
    out = _run(c.wait_for_signin("55", "u1", interval=0, sleep=no_sleep))
    assert out["state"] == "signed" and out["wallet"] == "rXRPL"


def test_wait_for_signin_returns_last_on_timeout(monkeypatch):
    c = LFGServiceClient("http://svc", "tok", "telegram")

    async def fake_status(user_id, uuid):
        return {"state": "pending"}  # never terminal

    async def no_sleep(_):
        return None

    monkeypatch.setattr(c, "signin_status", fake_status)
    out = _run(c.wait_for_signin("55", "u1", interval=0, timeout=0, sleep=no_sleep))
    assert out["state"] == "pending"  # last non-terminal status on timeout
```

- [ ] **Step 2: Run it — RED**

Run: `.venv/bin/pytest tests/test_sdk_signin_poll.py -v`
Expected: FAIL — `ImportError: cannot import name 'SIGNIN_TERMINAL'` / `wait_for_signin` missing.

- [ ] **Step 3: Implement**

Add the terminal set near the others (~line 25):

```python
SIGNIN_TERMINAL: frozenset[str] = frozenset({"signed", "expired"})
```

Add the method right after `signin_status` (~line 290):

```python
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
```

- [ ] **Step 4: GREEN**

Run: `.venv/bin/pytest tests/test_sdk_signin_poll.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add surfaces/_client/client.py tests/test_sdk_signin_poll.py
git commit -m "feat(sdk): wait_for_signin poller (mirrors wait_for_mint)"
```

---

### Task 3: Shared — `signin_result` outcome messages

**Files:**
- Create: `surfaces/_shared/signin_result.py`
- Test: `tests/test_shared_signin_result.py` (create)

**Interfaces:**
- Produces: `signin_outcome(state: str) -> str` — maps `"signed"`/`"expired"` (and any other/timeout state) to a user-facing sentence both bots render. Pure; no surface imports.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_shared_signin_result.py
from surfaces._shared.signin_result import signin_outcome


def test_expired_message():
    assert "expired" in signin_outcome("expired").lower()
    assert "/register" in signin_outcome("expired")


def test_non_signed_fallback_is_expired_style():
    # any non-signed terminal/timeout state reads as "didn't complete, try again"
    msg = signin_outcome("pending")
    assert "/register" in msg


def test_signed_has_no_retry_prompt():
    # "signed" is success — outcome() is only used for the NON-signed branches,
    # but it must still return a benign string if ever called with "signed".
    assert isinstance(signin_outcome("signed"), str)
```

- [ ] **Step 2: Run it — RED**

Run: `.venv/bin/pytest tests/test_shared_signin_result.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement**

```python
# surfaces/_shared/signin_result.py
# Surface-agnostic outcome message for the NON-signed branches of the Xaman
# sign-in /register flow (signed is handled inline with the verified wallet).
# Shared by the Discord + Telegram adapters so wording stays identical; each
# surface wraps the returned string in its own embed/caption.

_EXPIRED = "Sign-in expired before you approved it — run /register again to try once more."


def signin_outcome(state: str) -> str:
    # Only "signed" is success (handled by the caller with the wallet address);
    # every other terminal/timeout state is reported as a retry prompt.
    if state == "signed":
        return "Signed in."
    return _EXPIRED
```

- [ ] **Step 4: GREEN + add to mypy override note**

Run: `.venv/bin/pytest tests/test_shared_signin_result.py -v`
Expected: PASS. (`surfaces._shared.*` is already in the relaxed mypy override — no pyproject change needed.)

- [ ] **Step 5: Commit**

```bash
git add surfaces/_shared/signin_result.py tests/test_shared_signin_result.py
git commit -m "feat(surfaces): shared signin_outcome message helper"
```

---

### Task 4: Telegram — Xaman `/register`

**Files:**
- Modify: `surfaces/telegram_bot/render.py` (add `signin_caption`)
- Create: `surfaces/telegram_bot/register_view.py`
- Modify: `surfaces/telegram_bot/commands.py` (`register` → `handle_register`; drop `_register_impl` manual path + the `wallet` arg)
- Test: `tests/test_telegram_register.py` (create)
- Modify: `tests/test_telegram_commands.py` — delete the three manual-register tests (`test_register_happy_path`, `test_register_missing_arg_shows_usage`, `test_register_service_error_surfaced`); the flow is now covered by `tests/test_telegram_register.py`.

**Interfaces:**
- Consumes: `svc.signin_start(user_id)`→`{uuid, signin_link}`; `svc.qr_png(link)`; `svc.wait_for_signin(user_id, uuid)`→`{state, wallet?}`; `render.{signin_caption, photo_input, error_caption}`; `surfaces._shared.mint_result.friendly_error`; `surfaces._shared.signin_result.signin_outcome`.
- Produces: `async handle_register(svc, update, context) -> None`; `surfaces/telegram_bot/commands.register(update, context)` delegates to it.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_telegram_register.py
import asyncio
from types import SimpleNamespace

from surfaces._client.errors import ServiceError
from surfaces.telegram_bot import register_view


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _Bot:
    def __init__(self):
        self.photos = []
        self.messages = []

    async def send_photo(self, chat_id, photo, caption=None):
        self.photos.append((chat_id, photo, caption))

    async def send_message(self, chat_id, text):
        self.messages.append((chat_id, text))


def _update_ctx(bot, uid="55"):
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=int(uid), username="tg", full_name="TG"),
        effective_chat=SimpleNamespace(id=999),
    )
    return update, SimpleNamespace(bot=bot)


class _Svc:
    def __init__(self, start, final, qr=b"PNG"):
        self._start = start
        self._final = final
        self._qr = qr

    async def signin_start(self, user_id):
        if isinstance(self._start, Exception):
            raise self._start
        return self._start

    async def qr_png(self, data):
        return self._qr

    async def wait_for_signin(self, user_id, uuid):
        return self._final


def test_signed_registers_and_reports_wallet():
    bot = _Bot()
    update, ctx = _update_ctx(bot)
    svc = _Svc(
        start={"uuid": "u1", "signin_link": "https://xumm.app/sign/abc"},
        final={"state": "signed", "wallet": "rXRPL"},
    )
    _run(register_view.handle_register(svc, update, ctx))
    assert bot.photos and bot.photos[0][0] == 999  # QR sent
    assert any("rXRPL" in m[1] for m in bot.messages)  # verified wallet reported


def test_expired_reports_retry():
    bot = _Bot()
    update, ctx = _update_ctx(bot)
    svc = _Svc(
        start={"uuid": "u1", "signin_link": "https://xumm.app/sign/abc"},
        final={"state": "expired"},
    )
    _run(register_view.handle_register(svc, update, ctx))
    assert any("/register" in m[1] for m in bot.messages)


def test_service_error_at_start_reports_friendly():
    bot = _Bot()
    update, ctx = _update_ctx(bot)
    svc = _Svc(start=ServiceError("down", status=503), final={})
    _run(register_view.handle_register(svc, update, ctx))
    assert bot.messages and not bot.photos  # no QR; an error message instead
```

> Confirm `ServiceError("down", status=503)` matches `surfaces/_client/errors.py` (the mint tests show the canonical construction).

- [ ] **Step 2: Run it — RED**

Run: `.venv/bin/pytest tests/test_telegram_register.py -v`
Expected: FAIL — module/handler missing.

- [ ] **Step 3: Add the render caption**

In `surfaces/telegram_bot/render.py` add:

```python
def signin_caption(signin_link: str) -> str:
    return (
        "🔐 Verify your wallet with Xaman\n\n"
        "Scan the QR with Xaman (or open the link) and approve the sign-in.\n"
        "Your wallet address is captured on approval — nothing to type.\n\n"
        f"Open in Xaman: {signin_link}\n"
        "(the request expires after a few minutes)"
    )
```

- [ ] **Step 4: Implement `register_view.py`**

```python
# surfaces/telegram_bot/register_view.py
# Xaman-verified /register for Telegram: signin_start -> QR photo ->
# wait_for_signin -> report the verified wallet (the service stores it on
# 'signed'). The bot never sends an address itself — ownership is proven in
# Xaman. Standalone coroutine so tests drive it with fakes.
import logging
from typing import Any

from surfaces._client import LFGServiceClient
from surfaces._client.errors import ServiceError
from surfaces._shared.mint_result import friendly_error
from surfaces._shared.signin_result import signin_outcome
from surfaces.telegram_bot import render


async def handle_register(svc: LFGServiceClient, update: Any, context: Any) -> None:
    bot = context.bot
    chat_id = update.effective_chat.id
    user_id = str(update.effective_user.id)

    try:
        session = await svc.signin_start(user_id)
    except ServiceError as e:
        await bot.send_message(chat_id, render.error_caption(friendly_error(e)))
        return

    uuid = session["uuid"]
    signin_link = session.get("signin_link", "")

    try:
        qr_png = await svc.qr_png(signin_link)
    except ServiceError as e:
        logging.error(f"signin QR render failed: {e}")
        await bot.send_message(chat_id, render.error_caption(friendly_error(e)))
        return
    await bot.send_photo(
        chat_id,
        photo=render.photo_input(qr_png, "signin_qr.png"),
        caption=render.signin_caption(signin_link),
    )

    try:
        final = await svc.wait_for_signin(user_id, uuid)
    except ServiceError as e:
        await bot.send_message(chat_id, render.error_caption(friendly_error(e)))
        return

    if final.get("state") == "signed":
        wallet = final.get("wallet", "")
        await bot.send_message(chat_id, f"✅ Wallet verified and registered: {wallet}")
        return
    await bot.send_message(chat_id, signin_outcome(str(final.get("state") or "")))
```

- [ ] **Step 5: Rewire `commands.py` (drop the manual path)**

Replace `register`/`_register_impl` in `surfaces/telegram_bot/commands.py` with:

```python
async def register(update: Any, context: Any) -> None:
    from surfaces.telegram_bot.bot import svc  # noqa: PLC0415  # lazy — bot.py
    from surfaces.telegram_bot.register_view import handle_register  # noqa: PLC0415

    await handle_register(svc, update, context)
```

Delete `_register_impl` (and the now-unused `ServiceError` import if nothing else in the file uses it — `mint` doesn't; check and drop it if dead).

- [ ] **Step 6: Delete the obsolete manual-register tests**

In `tests/test_telegram_commands.py` delete `test_register_happy_path`, `test_register_missing_arg_shows_usage`, `test_register_service_error_surfaced` (manual path removed; covered by `tests/test_telegram_register.py`). Keep any `mint`/`start` tests.

- [ ] **Step 7: GREEN + checks**

Run: `.venv/bin/pytest tests/test_telegram_register.py tests/test_telegram_commands.py -q && .venv/bin/ruff check . && .venv/bin/mypy .`
Expected: PASS + clean. Confirm `surfaces/telegram_bot/` still imports no `discord`.

- [ ] **Step 8: Commit**

```bash
git add surfaces/telegram_bot/render.py surfaces/telegram_bot/register_view.py surfaces/telegram_bot/commands.py tests/test_telegram_register.py tests/test_telegram_commands.py
git commit -m "feat(telegram): Xaman-verified /register (replaces manual path)"
```

---

### Task 5: Discord — Xaman `/register`

**Files:**
- Modify: `surfaces/discord_bot/render.py` (add `signin_embed`)
- Create: `surfaces/discord_bot/register_view.py`
- Modify: `surfaces/discord_bot/commands.py` (`register` command drops the `wallet` param + calls `handle_register`; drop `_register_impl`)
- Test: `tests/test_discord_register.py` — **replace** its contents (the two manual-register tests) with sign-in-flow tests.

**Interfaces:**
- Consumes: `svc.signin_start/qr_png/wait_for_signin`; `render.{signin_embed, error_embed, file_from_png}`; `friendly_error`; `signin_outcome`.
- Produces: `async handle_register(svc, interaction) -> None`; `surfaces/discord_bot/commands.register(interaction)` (no `wallet` parameter) delegates to it.

- [ ] **Step 1: Write the failing test** (rewrite `tests/test_discord_register.py`)

```python
# tests/test_discord_register.py
import asyncio
from types import SimpleNamespace

from surfaces._client.errors import ServiceError
from surfaces.discord_bot import register_view


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _interaction():
    sent = []

    async def defer(ephemeral=True):
        return None

    async def followup_send(embed=None, file=None, ephemeral=True):
        sent.append(embed)

    inter = SimpleNamespace(
        user=SimpleNamespace(id=9, __str__=lambda self: "d#1"),
        response=SimpleNamespace(defer=defer),
        followup=SimpleNamespace(send=followup_send),
    )
    return inter, sent


class _Svc:
    def __init__(self, start, final, qr=b"PNG"):
        self._start, self._final, self._qr = start, final, qr

    async def signin_start(self, user_id):
        if isinstance(self._start, Exception):
            raise self._start
        return self._start

    async def qr_png(self, data):
        return self._qr

    async def wait_for_signin(self, user_id, uuid):
        return self._final


def test_signed_reports_wallet():
    inter, sent = _interaction()
    svc = _Svc(
        start={"uuid": "u1", "signin_link": "https://xumm.app/sign/abc"},
        final={"state": "signed", "wallet": "rXRPL"},
    )
    _run(register_view.handle_register(svc, inter))
    # at least the QR embed + a success embed mentioning the wallet
    assert any("rXRPL" in (e.description or "") for e in sent if e is not None)


def test_service_error_reports_friendly():
    inter, sent = _interaction()
    svc = _Svc(start=ServiceError("down", status=503), final={})
    _run(register_view.handle_register(svc, inter))
    assert sent  # an error embed was sent
```

> Note: `discord.Embed.description` is read in the assertion; `signin_embed`/the success embed must put the wallet in the description. Confirm `discord.File`/`Embed` usage against `surfaces/discord_bot/render.py` (mirrors `payment_embed`/`offer_embed`).

- [ ] **Step 2: Run it — RED**

Run: `.venv/bin/pytest tests/test_discord_register.py -v`
Expected: FAIL — `register_view` missing.

- [ ] **Step 3: Add `signin_embed` to `surfaces/discord_bot/render.py`**

```python
def signin_embed(signin_link: str) -> Embed:
    embed = Embed(
        title="🔐 Verify your wallet with Xaman",
        description=(
            "Scan the QR with Xaman and approve the sign-in — your wallet "
            "address is captured on approval, nothing to type.\n\n"
            f"[Open in Xaman]({signin_link})"
        ),
        color=0x00FF00,
    )
    embed.set_image(url="attachment://signin_qr.png")
    embed.set_footer(text="The sign-in request expires after a few minutes")
    return embed
```

- [ ] **Step 4: Implement `surfaces/discord_bot/register_view.py`**

```python
# surfaces/discord_bot/register_view.py
# Xaman-verified /register for Discord: signin_start -> QR embed ->
# wait_for_signin -> report the verified wallet (the service stores it on
# 'signed'). Standalone coroutine so tests drive it with a fake interaction.
import logging

import discord

from surfaces._client import LFGServiceClient
from surfaces._client.errors import ServiceError
from surfaces._shared.mint_result import friendly_error
from surfaces._shared.signin_result import signin_outcome
from surfaces.discord_bot import render


async def handle_register(svc: LFGServiceClient, interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    user_id = str(interaction.user.id)

    try:
        session = await svc.signin_start(user_id)
    except ServiceError as e:
        await interaction.followup.send(embed=render.error_embed(friendly_error(e)), ephemeral=True)
        return

    uuid = session["uuid"]
    signin_link = session.get("signin_link", "")

    try:
        qr_png = await svc.qr_png(signin_link)
    except ServiceError as e:
        logging.error(f"signin QR render failed: {e}")
        await interaction.followup.send(embed=render.error_embed(friendly_error(e)), ephemeral=True)
        return
    await interaction.followup.send(
        embed=render.signin_embed(signin_link),
        file=render.file_from_png(qr_png, "signin_qr.png"),
        ephemeral=True,
    )

    try:
        final = await svc.wait_for_signin(user_id, uuid)
    except ServiceError as e:
        await interaction.followup.send(embed=render.error_embed(friendly_error(e)), ephemeral=True)
        return

    if final.get("state") == "signed":
        wallet = final.get("wallet", "")
        done = discord.Embed(
            title="✅ Wallet verified and registered",
            description=f"Your registered wallet: **{wallet}**",
            color=0x00FF00,
        )
        await interaction.followup.send(embed=done, ephemeral=True)
        return
    await interaction.followup.send(
        embed=render.error_embed(signin_outcome(str(final.get("state") or ""))), ephemeral=True
    )
```

- [ ] **Step 5: Rewire `surfaces/discord_bot/commands.py`**

Replace the `register` command + `_register_impl` with (drop the `wallet` parameter):

```python
@tree.command(name="register", description="Verify and register your wallet with Xaman")
async def register(interaction: discord.Interaction) -> None:
    from surfaces.discord_bot.register_view import handle_register

    await handle_register(svc, interaction)
```

Delete `_register_impl`. Drop the now-unused `ServiceError` import from `commands.py` if nothing else there uses it (verify — `letsgo`/`MintView` don't).

- [ ] **Step 6: GREEN + checks**

Run: `.venv/bin/pytest tests/test_discord_register.py -q && .venv/bin/ruff check . && .venv/bin/mypy .`
Expected: PASS + clean.

- [ ] **Step 7: Commit**

```bash
git add surfaces/discord_bot/render.py surfaces/discord_bot/register_view.py surfaces/discord_bot/commands.py tests/test_discord_register.py
git commit -m "feat(discord): Xaman-verified /register (replaces manual path)"
```

---

### Task 6: Full gate + finish

**Files:** none (verification + branch finish)

- [ ] **Step 1: Full suite**

Run: `.venv/bin/pytest tests/ -q && .venv/bin/pytest webapp/ -q`
Expected: all PASS. (The webapp suite confirms the discord-default sign-in path is unchanged.)

- [ ] **Step 2: Lint + type-check**

Run: `.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy .`
Expected: clean. Fix any `warn_return_any`/format issues inline and re-run.

- [ ] **Step 3: Grep for leftovers**

Run: `grep -rn "client.register(" surfaces/ ; grep -rn "import discord" surfaces/telegram_bot/`
Expected: **no** `client.register(` call in either bot adapter; **no** `discord` import in `surfaces/telegram_bot/`.

- [ ] **Step 4: Push + draft PR**

```bash
git push -u origin feat/xaman-verified-register
gh pr create --draft --repo Team-Hamsa/LFG \
  --title "feat: Xaman-verified /register for Discord + Telegram bots" \
  --body "Replaces the unverified manual /register on both bots with a Xaman sign-in flow (scan QR -> sign -> verified wallet stored). Makes service sign-in (platform, id)-keyed (replacing the discord-only gate, isolation preserved), adds wait_for_signin to the SDK. /api/register + client.register() kept (webapp/admin), bot-unused. Backward-compatible (webapp byte-identical). Spec: docs/superpowers/specs/2026-06-26-xaman-verified-register-design.md"
```

- [ ] **Step 5: Flip ready when settled**

```bash
gh pr ready <number> --repo Team-Hamsa/LFG
```
Route through CodeRabbit/Greptile; resolve findings; merge only after review is handled.

---

## Self-Review

**Spec coverage:**
- §3 service platform-aware sign-in → Task 1. ✓
- §4 SDK `wait_for_signin` → Task 2. ✓
- §5 bot flow (both surfaces) + shared `signin_outcome` → Tasks 3 (shared), 4 (telegram), 5 (discord). ✓
- §6 removed (bot manual path / `wallet` arg) + kept (`/api/register`, `client.register()`) → Tasks 4 + 5 drop the bot path; Global Constraints + Task 6 Step 3 assert the endpoint/method are untouched. ✓
- §1 legacy-write discord-only → Task 1 Step 4 + test. ✓
- §8 testing (isolation, legacy-discord-only, webapp regression, SDK poll, per-surface bot tests) → Tasks 1, 2, 4, 5, 6. ✓
- §7 error handling (start failure, expired, timeout) → Tasks 4/5 handlers + tests. ✓

**Placeholder scan:** No TBD/TODO. Two "confirm against errors.py / render.py" notes (Tasks 4, 5) are verification cues with complete runnable code beside them, not placeholders.

**Type consistency:** `signin_start`→`{uuid, signin_link}`, `signin_status`/`wait_for_signin`→`{state, wallet?}`, `signin_outcome(state)->str`, `handle_register(svc, update, context)` (telegram) / `handle_register(svc, interaction)` (discord), `signin_payloads` rec `{platform, user_id, name, created_at}` — consistent across Tasks 1–5 and against the existing SDK/service/render code read to write this plan.
