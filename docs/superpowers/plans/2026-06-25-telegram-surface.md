# Telegram Surface Implementation Plan (Spine Plan 4 of 4)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Telegram as a first-class user-facing LFG surface (interactive mint, wallet registration, mint announcements + minter DM) on the shared `lfg_service` backend via the Surface SDK.

**Architecture:** Two PRs. **PR A** makes the service *platform-aware* — the HMAC session token carries the originating `platform`, and identity resolution / event publishing stop hardcoding `"discord"`. **PR B** adds `surfaces/telegram_bot/`, a python-telegram-bot v21 adapter that mirrors `surfaces/discord_bot/` and drives `LFGServiceClient`. Telegram builds zero inline XRPL transactions; all minting flows through `lfg_service`.

**Tech Stack:** Python 3, aiohttp (service), `python-telegram-bot>=21` (adapter), the existing `surfaces._client.LFGServiceClient`, repo-native sync test style (`asyncio.new_event_loop()` + direct call — NOT pytest-asyncio).

## Global Constraints

- **Make Waves SourceTag `2606160021`** must be on every XRPL/XUMM transaction. Telegram builds **no** inline tx — all minting goes through `lfg_service`, already stamped via `lfg_core.xrpl_ops`/`xumm_ops`. A parity test asserts the package contains no un-stamped inline tx.
- **Backward compatibility (PR A):** every new platform default is `"discord"`. Discord and the Discord-OAuth webapp must behave byte-for-byte as before; a regression test pins this.
- **Scope:** user-facing only. **No** admin (burn/stats/lookup), **no** inline trustline on Telegram. Admin stays Discord-only.
- **`MintSession.discord_id` field name is kept** (it is "the platform user-id"). Do not rename it; add a separate `platform` field instead.
- **Test style:** sync `_run(coro)` helper with `asyncio.new_event_loop()`; tests use the throwaway valid seed `sEdSKaCy2JT7JaM7v95H9SxkhP9wS2r` where a seed is required. `tests/*` is `ignore_errors=true` (no annotations needed).
- **mypy:** run `.venv/bin/mypy .` (full, not per-file) before claiming clean — per-file misses cross-method `warn_return_any`.
- **Commits:** frequent, one per task minimum. PRs open as `--draft`; flip ready (`gh pr ready`) to trigger CodeRabbit/Greptile; route through review before merge.

---

# PR A — Service platform-awareness (precursor)

Branch: `feat/spine-plan4a-platform-aware`. All edits in `lfg_service/app.py`, `lfg_core/mint_flow.py`, and new tests. Ships and merges before PR B.

### Task A1: Session token carries `platform`

**Files:**
- Modify: `lfg_service/app.py` (`make_session_token` ~135, `handle_session` ~247-257)
- Test: `tests/test_service_platform_token.py` (create)

**Interfaces:**
- Produces: `make_session_token(user)` reads optional `user["platform"]`, stores it in the token payload (default `"discord"`); `verify_session_token` returns it under key `"platform"`. `handle_session` stamps `platform=request["surface"]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_service_platform_token.py
import asyncio

from lfg_service.app import make_session_token, verify_session_token


def test_token_roundtrips_explicit_platform():
    tok = make_session_token({"id": "55", "name": "tg", "platform": "telegram"})
    payload = verify_session_token(tok)
    assert payload["id"] == "55"
    assert payload["platform"] == "telegram"


def test_token_defaults_platform_to_discord():
    tok = make_session_token({"id": "9", "name": "d"})
    payload = verify_session_token(tok)
    assert payload["platform"] == "discord"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_service_platform_token.py -v`
Expected: FAIL — `KeyError: 'platform'` (token payload has no platform yet).

- [ ] **Step 3: Implement — stamp platform into the token**

In `lfg_service/app.py`, change `make_session_token`:

```python
def make_session_token(user: dict[str, Any]) -> str:
    payload = {
        "id": user["id"],
        "name": user["name"],
        "platform": user.get("platform", "discord"),
        "exp": int(time.time()) + SESSION_TTL,
    }
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    sig = hmac.new(_session_secret(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"
```

And in `handle_session`, pass the surface (already set by `require_service_token`) as the platform:

```python
    token = make_session_token({"id": pid, "name": pname, "platform": request["surface"]})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_service_platform_token.py -v`
Expected: PASS (both tests).

- [ ] **Step 5: Commit**

```bash
git add lfg_service/app.py tests/test_service_platform_token.py
git commit -m "feat(service): session token carries originating platform"
```

---

### Task A2: Platform-aware wallet resolution (`handle_me`, `handle_events_me`, `require_wallet`)

**Files:**
- Modify: `lfg_service/app.py` (`handle_events_me` ~100-110, `require_wallet` ~173-187, `handle_me` ~260-267)
- Test: `tests/test_service_platform_resolve.py` (create)

**Interfaces:**
- Produces: `_platform(user) -> str` (`user.get("platform", "discord")`) and `_resolve_wallet(platform, uid) -> str | None` — identity-first via `identity_store.resolve`, with the legacy `get_user` fallback **only when `platform == "discord"`**. `handle_me`, `handle_events_me`, and `require_wallet` all route through these.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_service_platform_resolve.py
import asyncio

import lfg_service.app as app


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_resolve_uses_identity_platform(monkeypatch):
    calls = {}

    def fake_resolve(platform, uid):
        calls["args"] = (platform, uid)
        return "rWalletTELEGRAM" if platform == "telegram" else None

    monkeypatch.setattr(app.identity_store, "resolve", fake_resolve)
    # get_user must NOT be consulted for a non-discord platform
    monkeypatch.setattr(app, "get_user", lambda uid: {"address": "rLEGACY"})
    wallet = _run(app._resolve_wallet("telegram", "55"))
    assert wallet == "rWalletTELEGRAM"
    assert calls["args"] == ("telegram", "55")


def test_resolve_falls_back_to_legacy_for_discord(monkeypatch):
    monkeypatch.setattr(app.identity_store, "resolve", lambda platform, uid: None)
    monkeypatch.setattr(app, "get_user", lambda uid: {"address": "rLEGACY"})
    assert _run(app._resolve_wallet("discord", "9")) == "rLEGACY"


def test_resolve_no_legacy_fallback_for_non_discord(monkeypatch):
    monkeypatch.setattr(app.identity_store, "resolve", lambda platform, uid: None)
    monkeypatch.setattr(app, "get_user", lambda uid: {"address": "rLEGACY"})
    assert _run(app._resolve_wallet("telegram", "55")) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_service_platform_resolve.py -v`
Expected: FAIL — `AttributeError: module 'lfg_service.app' has no attribute '_resolve_wallet'`.

- [ ] **Step 3: Implement helpers + route the three sites through them**

In `lfg_service/app.py`, add near the other helpers (after `require_wallet` or above `handle_me`):

```python
def _platform(user: dict[str, Any]) -> str:
    return user.get("platform", "discord")


async def _resolve_wallet(platform: str, uid: str) -> str | None:
    wallet = await asyncio.to_thread(identity_store.resolve, platform, uid)
    if wallet is None and platform == "discord":
        record = await asyncio.to_thread(get_user, uid)
        wallet = record["address"] if record else None
    return wallet
```

Rewrite `handle_events_me` (lines ~104-107):

```python
    payload = verify_session_token(request.query.get("token", ""))
    if not payload:
        return web.json_response({"error": "unauthorized", "code": "bad_session"}, status=401)
    wallet = await _resolve_wallet(_platform(payload), payload["id"])
```

Rewrite `handle_me` (lines ~262-266):

```python
    user = request["user"]
    wallet = await _resolve_wallet(_platform(user), user["id"])
```

Rewrite the resolution inside `require_wallet`'s wrapper (lines ~181-184):

```python
        wallet = await _resolve_wallet(_platform(request["user"]), request["user"]["id"])
        if not wallet:
            return web.json_response({"error": "no wallet registered"}, status=400)
        request["wallet"] = wallet
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_service_platform_resolve.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Verify no Discord regression**

Run: `.venv/bin/pytest tests/ -k "events_me or require_wallet or handle_me or identity" -v`
Expected: PASS — existing Discord/webapp resolution unchanged (default platform `discord` + legacy fallback preserved).

- [ ] **Step 6: Commit**

```bash
git add lfg_service/app.py tests/test_service_platform_resolve.py
git commit -m "feat(service): platform-aware wallet resolution (me/events_me/require_wallet)"
```

---

### Task A3: Platform-aware identity link (`handle_register`, `handle_signin_status`)

**Files:**
- Modify: `lfg_service/app.py` (`handle_register` ~270-289, `handle_signin_status` ~480-492)
- Test: `tests/test_service_platform_register.py` (create)

**Interfaces:**
- Consumes: `_platform` from Task A2.
- Produces: `handle_register` links identities under the session's real platform; `handle_signin_status` converted for consistency (webapp path stays `discord` via default).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_service_platform_register.py
import asyncio

import lfg_service.app as app
from lfg_service.app import make_session_token


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _Req:
    def __init__(self, token, body):
        self.headers = {"Authorization": f"Bearer {token}"}
        self._body = body
        self._store: dict = {}

    async def json(self):
        return self._body

    def __getitem__(self, k):
        return self._store[k]

    def __setitem__(self, k, v):
        self._store[k] = v


def test_register_links_under_token_platform(monkeypatch):
    linked = {}
    monkeypatch.setattr(app, "is_valid_classic_address", lambda w: True)
    monkeypatch.setattr(app, "register_user", lambda uid, name, w: True)

    def fake_link(platform, uid, name, wallet):
        linked["args"] = (platform, uid, wallet)
        return True

    monkeypatch.setattr(app.identity_store, "link", fake_link)
    monkeypatch.setattr(app.config, "WEBAPP_DEV_MODE", False)

    token = make_session_token({"id": "55", "name": "tg", "platform": "telegram"})
    resp = _run(app.handle_register(_Req(token, {"wallet": "rXRPL"})))
    assert resp.status == 200
    assert linked["args"] == ("telegram", "55", "rXRPL")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_service_platform_register.py -v`
Expected: FAIL — `linked["args"]` platform is `"discord"`, not `"telegram"`.

- [ ] **Step 3: Implement — use `_platform` at the link sites**

In `handle_register`, replace the hardcoded link (lines ~281-283):

```python
    linked = await asyncio.to_thread(
        identity_store.link, _platform(user), user["id"], user["name"], wallet
    )
    if not linked:
        logging.error(
            "identity.link failed for %s:%s — /events/me may 403 until restart-migrate",
            _platform(user),
            user["id"],
        )
```

In `handle_signin_status`, replace the hardcoded `"discord"` in the `identity_store.link(...)` call (line ~492) with `_platform(request["user"])`:

```python
            identity_store.link, _platform(request["user"]), rec["discord_id"], rec["name"], s["account"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_service_platform_register.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add lfg_service/app.py tests/test_service_platform_register.py
git commit -m "feat(service): platform-aware identity link (register/signin)"
```

---

### Task A4: `MintSession.platform` + platform-aware mint publish

**Files:**
- Modify: `lfg_core/mint_flow.py` (`MintSession.__init__` ~42-49, `to_dict` ~132)
- Modify: `lfg_service/app.py` (`handle_mint_start` ~315-319, `handle_mint_status` publish ~419-424)
- Test: `tests/test_service_mint_platform.py` (create)

**Interfaces:**
- Consumes: `_platform` from A2.
- Produces: `MintSession(discord_id, wallet_address, return_url=None, platform="discord")` with `self.platform`; `to_dict()` includes `"platform"`. `handle_mint_start` sets `platform=_platform(user)`; the publish emits `{"platform": session.platform, "platform_user_id": session.discord_id}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_service_mint_platform.py
from lfg_core.mint_flow import MintSession


def test_mint_session_defaults_platform_discord():
    s = MintSession(discord_id="9", wallet_address="rA")
    assert s.platform == "discord"
    assert s.to_dict()["platform"] == "discord"


def test_mint_session_accepts_platform():
    s = MintSession(discord_id="55", wallet_address="rB", platform="telegram")
    assert s.platform == "telegram"
    assert s.to_dict()["platform"] == "telegram"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_service_mint_platform.py -v`
Expected: FAIL — `MintSession.__init__` has no `platform` param / `to_dict` has no `platform` key.

- [ ] **Step 3: Implement the field**

In `lfg_core/mint_flow.py`, extend `__init__`:

```python
    def __init__(
        self,
        discord_id: str,
        wallet_address: str,
        return_url: dict[str, str] | None = None,
        platform: str = "discord",
    ) -> None:
        self.id = uuid.uuid4().hex
        self.discord_id = discord_id
        self.platform = platform
        self.wallet_address = wallet_address
```

In `to_dict()` (~132), add the key (place it next to `"id"`):

```python
            "id": self.id,
            "platform": self.platform,
```

- [ ] **Step 4: Wire the service mint path**

In `lfg_service/app.py` `handle_mint_start`, set the platform on construction (~315-319):

```python
    session = mint_flow.MintSession(
        discord_id=user["id"],
        wallet_address=request["wallet"],
        return_url=await _request_return_url(request),
        platform=_platform(user),
    )
```

In `handle_mint_status`, change the publish payload (~421):

```python
        await publish_event(
            "mint.completed" if ok else "mint.failed",
            {"platform": session.platform, "platform_user_id": session.discord_id},
            session.wallet_address,
            session.to_dict(),
        )
```

- [ ] **Step 5: Run tests to verify pass + no regression**

Run: `.venv/bin/pytest tests/test_service_mint_platform.py tests/ -k "mint" -v`
Expected: PASS — new tests pass; existing mint tests unaffected (default `discord`).

- [ ] **Step 6: Commit**

```bash
git add lfg_core/mint_flow.py lfg_service/app.py tests/test_service_mint_platform.py
git commit -m "feat(service): MintSession carries platform; mint events publish real platform"
```

---

### Task A5: Full-suite gate + finish PR A

**Files:** none (verification + branch finish)

- [ ] **Step 1: Run the full suite**

Run: `.venv/bin/pytest tests/ -q`
Expected: PASS (all green — prior count + the new platform tests).

- [ ] **Step 2: Lint + type-check**

Run: `.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy .`
Expected: clean. (Fix any `warn_return_any`/format issues inline, re-run.)

- [ ] **Step 3: Discord-regression spot check**

Run: `.venv/bin/pytest tests/test_discord_events.py tests/test_discord_mint.py tests/test_discord_sourcetag_invariant.py -v`
Expected: PASS — Discord surface behavior is byte-identical (defaults to `discord`).

- [ ] **Step 4: Push + open draft PR**

```bash
git push -u origin feat/spine-plan4a-platform-aware
gh pr create --draft --repo Team-Hamsa/LFG \
  --title "feat(service): platform-aware spine (Plan 4a — Telegram prerequisite)" \
  --body "Threads the originating platform through the session token and replaces hardcoded \`\"discord\"\` in identity resolve/link + mint publish. Backward-compatible (defaults to discord; Discord+webapp unchanged). Precursor to the Telegram adapter (#43)."
```

- [ ] **Step 5: Flip ready when settled, route through CodeRabbit/Greptile**

```bash
gh pr ready <number> --repo Team-Hamsa/LFG
```
Wait for review; resolve findings; merge only after review is handled. **PR B depends on this being merged to `main`.**

---

# PR B — `surfaces/telegram_bot/` adapter

Branch: `feat/spine-plan4b-telegram` **off `main` after PR A merges**. New package `surfaces/telegram_bot/` mirroring `surfaces/discord_bot/`.

### Task B1: Dependency, mypy config, package config

**Files:**
- Modify: `requirements.txt`
- Modify: `pyproject.toml` (mypy overrides ~24 and ~55)
- Create: `surfaces/telegram_bot/__init__.py`
- Create: `surfaces/telegram_bot/config.py`
- Test: `tests/test_telegram_config.py` (create)

**Interfaces:**
- Produces: `surfaces.telegram_bot.config` exposing `TELEGRAM_BOT_TOKEN`, `LFG_SERVICE_URL`, `SERVICE_TOKEN_TELEGRAM`, `TELEGRAM_ANNOUNCE_CHAT_ID` (int), `RETRY_MAX_ATTEMPTS`, `RETRY_BASE_DELAY`.

- [ ] **Step 1: Add the dependency + mypy entries**

Append to `requirements.txt`:

```
python-telegram-bot>=21
```

In `pyproject.toml`, add `"telegram.*",` to the `ignore_missing_imports` module list (~24-37), and add `"surfaces.telegram_bot.*"` to the relaxed-production module list (~55):

```toml
module = ["main", "lfg_service.app", "webapp.server", "db_helpers", "user_db", "surfaces.discord_bot.*", "surfaces.telegram_bot.*"]
```

- [ ] **Step 2: Install the dependency**

Run: `.venv/bin/pip install "python-telegram-bot>=21"`
Expected: installs PTB v21+.

- [ ] **Step 3: Write the failing test**

```python
# tests/test_telegram_config.py
import importlib


def test_config_reads_env(monkeypatch):
    for k, v in {
        "TELEGRAM_BOT_TOKEN": "tg-tok",
        "LFG_SERVICE_URL": "http://svc",
        "SERVICE_TOKEN_TELEGRAM": "s",
        "TELEGRAM_ANNOUNCE_CHAT_ID": "12345",
    }.items():
        monkeypatch.setenv(k, v)
    import surfaces.telegram_bot.config as cfg

    importlib.reload(cfg)
    assert cfg.TELEGRAM_BOT_TOKEN == "tg-tok"
    assert cfg.SERVICE_TOKEN_TELEGRAM == "s"
    assert cfg.TELEGRAM_ANNOUNCE_CHAT_ID == 12345
```

- [ ] **Step 4: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_telegram_config.py -v`
Expected: FAIL — `ModuleNotFoundError: surfaces.telegram_bot`.

- [ ] **Step 5: Create the package + config**

`surfaces/telegram_bot/__init__.py`:

```python
# surfaces/telegram_bot/ — Telegram adapter on the shared lfg_service backend.
```

`surfaces/telegram_bot/config.py`:

```python
# surfaces/telegram_bot/config.py
# Environment-derived settings for the Telegram adapter. SERVICE_TOKEN_TELEGRAM
# auto-registers the surface server-side (lfg_service/auth.py) — no service code
# change needed for auth.
import logging
import os

from dotenv import load_dotenv

load_dotenv()


def _require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise ValueError(f"{name} not found in environment variables")
    return value


TELEGRAM_BOT_TOKEN = _require("TELEGRAM_BOT_TOKEN")
LFG_SERVICE_URL = _require("LFG_SERVICE_URL")
SERVICE_TOKEN_TELEGRAM = _require("SERVICE_TOKEN_TELEGRAM")

TELEGRAM_ANNOUNCE_CHAT_ID = int(os.getenv("TELEGRAM_ANNOUNCE_CHAT_ID", "0"))
if not TELEGRAM_ANNOUNCE_CHAT_ID:
    raise ValueError("TELEGRAM_ANNOUNCE_CHAT_ID not found in environment variables")

RETRY_MAX_ATTEMPTS = int(os.getenv("RETRY_MAX_ATTEMPTS", "5"))
RETRY_BASE_DELAY = float(os.getenv("RETRY_BASE_DELAY", "1.0"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()],
)
```

- [ ] **Step 6: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_telegram_config.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add requirements.txt pyproject.toml surfaces/telegram_bot/__init__.py surfaces/telegram_bot/config.py tests/test_telegram_config.py
git commit -m "feat(telegram): scaffold package + config + PTB dependency"
```

---

### Task B2: `render.py` — pure caption/photo builders

**Files:**
- Create: `surfaces/telegram_bot/render.py`
- Test: `tests/test_telegram_render.py` (create)

**Interfaces:**
- Produces: `payment_caption(payment_link: str) -> str`, `offer_caption(final: dict[str, Any]) -> str`, `error_caption(message: str) -> str`, `photo_input(data: bytes, filename: str) -> telegram.InputFile`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_telegram_render.py
from surfaces.telegram_bot import render


def test_payment_caption_has_link():
    cap = render.payment_caption("https://xumm.app/sign/abc")
    assert "https://xumm.app/sign/abc" in cap
    assert "1 token" in cap.lower() or "pay" in cap.lower()


def test_offer_caption_has_number_and_link():
    cap = render.offer_caption({"nft_number": 3600, "accept_deeplink": "https://xumm.app/sign/xyz"})
    assert "3600" in cap
    assert "https://xumm.app/sign/xyz" in cap


def test_error_caption_passthrough():
    assert "boom" in render.error_caption("boom")


def test_photo_input_builds_inputfile():
    f = render.photo_input(b"\x89PNG", "x.png")
    assert f.filename == "x.png"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_telegram_render.py -v`
Expected: FAIL — `ModuleNotFoundError` / attributes missing.

- [ ] **Step 3: Implement**

`surfaces/telegram_bot/render.py`:

```python
# surfaces/telegram_bot/render.py
# Pure caption + photo builders for the Telegram mint flow. Telegram has no
# embeds; plain-text captions (no parse_mode, to avoid MarkdownV2 escaping
# pitfalls) carry the links, and photos are sent as InputFile bytes. Trivially
# unit-testable with no SDK/XRPL involvement.
import io
from typing import Any

from telegram import InputFile


def payment_caption(payment_link: str) -> str:
    return (
        "💰 Token Payment Required\n\n"
        "Pay 1 token to mint your NFT:\n"
        "1. Scan the QR with your XRPL wallet (XUMM/Xaman)\n"
        "2. Approve the payment\n"
        "3. Wait for confirmation\n\n"
        f"Open payment link: {payment_link}\n"
        "(expires in 5 minutes)"
    )


def offer_caption(final: dict[str, Any]) -> str:
    number = final.get("nft_number", "?")
    accept_link = final.get("accept_deeplink", "")
    return (
        "🎨 NFT Minted Successfully!\n\n"
        f"NFT Number: #{number}\n\n"
        "To claim it:\n"
        "1. Scan the QR with XUMM\n"
        "2. Review and accept the offer\n"
        "3. Your NFT appears in your wallet\n\n"
        f"Open in XUMM: {accept_link}\n"
        "(offer expires in 24 hours)"
    )


def error_caption(message: str) -> str:
    return f"⚠️ {message}"


def photo_input(data: bytes, filename: str) -> InputFile:
    return InputFile(io.BytesIO(data), filename=filename)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_telegram_render.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add surfaces/telegram_bot/render.py tests/test_telegram_render.py
git commit -m "feat(telegram): pure caption/photo render builders"
```

---

### Task B3: `mint_view.py` — `handle_mint`

**Files:**
- Create: `surfaces/telegram_bot/mint_view.py`
- Test: `tests/test_telegram_mint.py` (create)

**Interfaces:**
- Consumes: `render` (B2); `LFGServiceClient.start_mint/qr_png/wait_for_mint`; `surfaces._client.errors.{ServiceError,BadRequest}`.
- Produces: `async handle_mint(svc, update, context) -> None`. Sends payment QR photo → polls → sends offer QR (hosted `accept_qr_url` URL if present, else rendered from `accept_deeplink`). Module constants `MINT_OK_STATES`, `_BAD_STATE_MESSAGES`, `_friendly(err)` (duplicated from the Discord adapter — kept local so Telegram never imports `discord`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_telegram_mint.py
import asyncio
from types import SimpleNamespace

from surfaces._client.errors import BadRequest
from surfaces.telegram_bot import mint_view


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
        self.photos.append((chat_id, caption))

    async def send_message(self, chat_id, text):
        self.messages.append((chat_id, text))


def _update_ctx(bot, uid="55"):
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=int(uid), username="tg", full_name="TG User"),
        effective_chat=SimpleNamespace(id=999),
    )
    ctx = SimpleNamespace(bot=bot, args=[])
    return update, ctx


class _Svc:
    def __init__(self, start, final, qr=b"PNG"):
        self._start = start
        self._final = final
        self._qr = qr

    async def start_mint(self, user_id, *, username=""):
        if isinstance(self._start, Exception):
            raise self._start
        return self._start

    async def qr_png(self, data):
        return self._qr

    async def wait_for_mint(self, user_id, session_id):
        return self._final


def test_happy_path_sends_payment_and_offer_qr():
    bot = _Bot()
    update, ctx = _update_ctx(bot)
    svc = _Svc(
        start={"id": "sid", "payment_link": "https://pay"},
        final={"state": "offer_ready", "nft_number": 3600, "accept_deeplink": "https://accept"},
    )
    _run(mint_view.handle_mint(svc, update, ctx))
    # two photos: payment QR + offer QR
    assert len(bot.photos) == 2
    assert "3600" in bot.photos[1][1]


def test_hosted_qr_url_used_directly():
    bot = _Bot()
    update, ctx = _update_ctx(bot)
    svc = _Svc(
        start={"id": "sid", "payment_link": "https://pay"},
        final={"state": "done", "nft_number": 7, "accept_qr_url": "https://cdn/qr.png"},
    )
    _run(mint_view.handle_mint(svc, update, ctx))
    # offer photo sent with the hosted URL as the photo arg
    assert bot.photos[1][0] == 999


def test_no_wallet_sends_register_hint():
    bot = _Bot()
    update, ctx = _update_ctx(bot)
    svc = _Svc(start=BadRequest("no wallet registered", status=400), final={})
    _run(mint_view.handle_mint(svc, update, ctx))
    assert bot.messages and "register" in bot.messages[0][1].lower()


def test_bad_terminal_state_reports_failure():
    bot = _Bot()
    update, ctx = _update_ctx(bot)
    svc = _Svc(
        start={"id": "sid", "payment_link": "https://pay"},
        final={"state": "payment_timeout"},
    )
    _run(mint_view.handle_mint(svc, update, ctx))
    assert any("timed out" in m[1].lower() for m in bot.messages)
```

> Note: confirm the `BadRequest(...)` constructor signature in `surfaces/_client/errors.py` before running; if it does not accept `status=`, construct it as that file requires (the Discord mint tests show the canonical construction).

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_telegram_mint.py -v`
Expected: FAIL — module/handle_mint missing.

- [ ] **Step 3: Implement**

`surfaces/telegram_bot/mint_view.py`:

```python
# surfaces/telegram_bot/mint_view.py
# Inverted mint handler for Telegram: start_mint -> payment QR -> wait_for_mint
# -> offer-accept QR. ALL XRPL/CDN work happens in lfg_service (which stamps the
# Make Waves SourceTag); this module only orchestrates SDK calls and sends
# Telegram photos/messages. handle_mint(svc, update, context) is standalone so
# tests can drive it with fakes.
#
# _friendly / MINT_OK_STATES / _BAD_STATE_MESSAGES are intentionally duplicated
# from surfaces.discord_bot.mint_view: keeping them local means the Telegram
# package never imports `discord`. They are ~15 trivial lines.
import logging
from typing import Any

from surfaces._client import LFGServiceClient
from surfaces._client.errors import BadRequest, ServiceError
from surfaces.telegram_bot import render

MINT_OK_STATES = frozenset({"offer_ready", "done"})

_BAD_STATE_MESSAGES = {
    "payment_timeout": "Payment request timed out. Please try again.",
    "failed": "The mint failed. Please try again or contact an admin.",
}


def _friendly(err: ServiceError) -> str:
    code = (err.code or "").lower()
    message = (err.message or "").lower()
    if isinstance(err, BadRequest) and ("wallet" in code or "wallet" in message):
        return "Please register your wallet first using /register."
    if err.status == 409 or "in_progress" in code or "already" in message:
        return "You already have a mint in progress — finish or wait for it to time out."
    return err.message or "The mint service is unavailable. Please try again shortly."


async def handle_mint(svc: LFGServiceClient, update: Any, context: Any) -> None:
    bot = context.bot
    chat_id = update.effective_chat.id
    user = update.effective_user
    user_id = str(user.id)
    username = user.username or getattr(user, "full_name", "") or ""

    try:
        session = await svc.start_mint(user_id, username=username)
    except ServiceError as e:
        await bot.send_message(chat_id, render.error_caption(_friendly(e)))
        return

    session_id = session["id"]
    payment_link = session.get("payment_link", "")

    try:
        qr_png = await svc.qr_png(payment_link)
    except ServiceError as e:
        logging.error(f"payment QR render failed: {e}")
        await bot.send_message(chat_id, render.error_caption(_friendly(e)))
        return
    await bot.send_photo(
        chat_id,
        photo=render.photo_input(qr_png, "payment_qr.png"),
        caption=render.payment_caption(payment_link),
    )

    try:
        final = await svc.wait_for_mint(user_id, session_id)
    except ServiceError as e:
        await bot.send_message(chat_id, render.error_caption(_friendly(e)))
        return

    state = str(final.get("state") or "")
    if state not in MINT_OK_STATES:
        reason = _BAD_STATE_MESSAGES.get(state, "Mint did not complete. Please try again.")
        await bot.send_message(chat_id, render.error_caption(reason))
        return

    hosted_qr = final.get("accept_qr_url")
    if hosted_qr:
        await bot.send_photo(chat_id, photo=hosted_qr, caption=render.offer_caption(final))
        return

    accept_link = final.get("accept_deeplink", "")
    try:
        qr_png = await svc.qr_png(accept_link)
    except ServiceError:
        # Mint succeeded; only the QR render failed. Still surface the offer link.
        await bot.send_message(chat_id, render.offer_caption(final))
        return
    await bot.send_photo(
        chat_id,
        photo=render.photo_input(qr_png, "offer_qr.png"),
        caption=render.offer_caption(final),
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_telegram_mint.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add surfaces/telegram_bot/mint_view.py tests/test_telegram_mint.py
git commit -m "feat(telegram): inverted mint handler on the SDK"
```

---

### Task B4: `events.py` — firehose consumer (Telegram-gated)

**Files:**
- Create: `surfaces/telegram_bot/events.py`
- Test: `tests/test_telegram_events.py` (create)

**Interfaces:**
- Consumes: `svc.events(types=...)`, `lfg_service.events.Event`.
- Produces: `make_announcement(ev) -> str`; `async run_event_loop(svc, announce, dm_user=None) -> None`. DM gated on `ev.type == "mint.completed"` AND `identity.platform == "telegram"`. `aclose()` runs in `finally`. `_MINT_EVENT_TYPES == ["mint.completed", "mint.failed"]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_telegram_events.py
import asyncio

from lfg_service.events import Event
from surfaces.telegram_bot import events as ev_mod


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _tg_identity(uid):
    return {"platform": "telegram", "platform_user_id": uid}


class _FakeAgen:
    def __init__(self, items):
        self._items = list(items)
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._items:
            return self._items.pop(0)
        raise StopAsyncIteration

    async def aclose(self):
        self.closed = True


class _FakeSvc:
    def __init__(self, agen):
        self._agen = agen

    def events(self, types=None):
        self.types = types
        return self._agen


def test_announce_and_dm_on_telegram_completed():
    agen = _FakeAgen([
        Event(type="mint.completed", ts=0, identity=_tg_identity("55"), wallet=None,
              data={"nft_number": 3600}),
    ])
    svc = _FakeSvc(agen)
    sent, dmed = [], []

    async def announce(m):
        sent.append(m)

    async def dm(uid, m):
        dmed.append((uid, m))

    _run(ev_mod.run_event_loop(svc, announce, dm))
    assert svc.types == ["mint.completed", "mint.failed"]
    assert sent and "3600" in sent[0]
    assert dmed == [("55", sent[0])]
    assert agen.closed is True


def test_no_dm_for_failed_or_non_telegram():
    agen = _FakeAgen([
        Event(type="mint.failed", ts=0, identity=_tg_identity("55"), wallet=None,
              data={"nft_number": 1}),
        Event(type="mint.completed", ts=0,
              identity={"platform": "discord", "platform_user_id": "9"}, wallet=None,
              data={"nft_number": 2}),
    ])
    sent, dmed = [], []

    async def announce(m):
        sent.append(m)

    async def dm(uid, m):
        dmed.append((uid, m))

    _run(ev_mod.run_event_loop(_FakeSvc(agen), announce, dm))
    assert len(sent) == 2
    assert dmed == []


def test_loop_survives_handler_error():
    agen = _FakeAgen([
        Event(type="mint.completed", ts=0, identity=_tg_identity("1"), wallet=None,
              data={"nft_number": 1}),
        Event(type="mint.completed", ts=0, identity=_tg_identity("2"), wallet=None,
              data={"nft_number": 2}),
    ])
    calls = {"n": 0}

    async def announce(m):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")

    _run(ev_mod.run_event_loop(_FakeSvc(agen), announce, None))
    assert calls["n"] == 2
    assert agen.closed is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_telegram_events.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement**

`surfaces/telegram_bot/events.py`:

```python
# surfaces/telegram_bot/events.py
# Background firehose consumer: announces mint.completed / mint.failed to the
# configured channel and DMs the minter on success. The /events firehose is
# cross-surface, so the DM is gated on identity.platform == "telegram".
import logging
from collections.abc import Awaitable, Callable

from lfg_service.events import Event
from surfaces._client import LFGServiceClient

_MINT_EVENT_TYPES = ["mint.completed", "mint.failed"]


def _is_telegram(ev: Event) -> bool:
    return (ev.identity or {}).get("platform") == "telegram"


def make_announcement(ev: Event) -> str:
    data = ev.data or {}
    number = data.get("nft_number", "?")
    if ev.type == "mint.completed":
        return f"🎨 NFT #{number} minted for a user."
    return f"❌ Mint failed for a user (#{number})."


async def run_event_loop(
    svc: LFGServiceClient,
    announce: Callable[[str], Awaitable[None]],
    dm_user: Callable[[str, str], Awaitable[None]] | None = None,
) -> None:
    """Consume the service firehose forever. The SDK reconnects internally;
    cancel the enclosing task to stop (finally aclose()s the generator)."""
    agen = svc.events(types=_MINT_EVENT_TYPES)
    try:
        async for ev in agen:
            try:
                message = make_announcement(ev)
                await announce(message)
                if dm_user is not None and ev.type == "mint.completed" and _is_telegram(ev):
                    uid = (ev.identity or {}).get("platform_user_id")
                    if uid:
                        await dm_user(uid, message)
            except Exception as e:  # never let one bad event kill the loop
                logging.error(f"event handler error: {e}")
    finally:
        await agen.aclose()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_telegram_events.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add surfaces/telegram_bot/events.py tests/test_telegram_events.py
git commit -m "feat(telegram): firehose consumer (announce + minter DM, telegram-gated)"
```

---

### Task B5: `commands.py` — `/mint`, `/register`, `/start`

**Files:**
- Create: `surfaces/telegram_bot/commands.py`
- Test: `tests/test_telegram_commands.py` (create)

**Interfaces:**
- Consumes: `svc` (from `bot.py`, imported lazily via the handlers); `handle_mint` (B3); `ServiceError`.
- Produces: `async mint(update, context)`, `async register(update, context)`, `async start(update, context)`, and `async _register_impl(update, context, *, _svc=None)` (testable seam). `_register_impl` reads `context.args[0]` as the wallet; usage hint when missing.

> **Import note:** `commands.py` imports `svc` from `surfaces.telegram_bot.bot` at module top. `bot.py` (Task B6) defines `svc` at module top and imports `commands` lazily inside `build_application()`, so there is no circular-import cycle at load time (mirrors the Discord adapter's pattern). The test injects `_svc` and never touches the real `bot.svc`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_telegram_commands.py
import asyncio
from types import SimpleNamespace

import pytest

from surfaces._client.errors import ServiceError


@pytest.fixture
def cmds(monkeypatch):
    for k, v in {
        "TELEGRAM_BOT_TOKEN": "t",
        "LFG_SERVICE_URL": "http://svc",
        "SERVICE_TOKEN_TELEGRAM": "s",
        "TELEGRAM_ANNOUNCE_CHAT_ID": "1",
    }.items():
        monkeypatch.setenv(k, v)
    import surfaces.telegram_bot.commands as c

    return c


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _update():
    sent = []

    async def reply_text(msg):
        sent.append(msg)

    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=55, username="tg", full_name="TG"),
        message=SimpleNamespace(reply_text=reply_text),
    )
    return update, sent


class _OkSvc:
    def __init__(self):
        self.calls = []

    async def register(self, uid, name, wallet):
        self.calls.append((uid, name, wallet))
        return {"ok": True}


def test_register_happy_path(cmds):
    update, sent = _update()
    ctx = SimpleNamespace(args=["rWALLET"])
    svc = _OkSvc()
    _run(cmds._register_impl(update, ctx, _svc=svc))
    assert svc.calls == [("55", "tg", "rWALLET")]
    assert "registered" in sent[0].lower()


def test_register_missing_arg_shows_usage(cmds):
    update, sent = _update()
    ctx = SimpleNamespace(args=[])
    _run(cmds._register_impl(update, ctx, _svc=_OkSvc()))
    assert "/register" in sent[0]


def test_register_service_error_surfaced(cmds):
    update, sent = _update()
    ctx = SimpleNamespace(args=["rW"])

    class _ErrSvc:
        async def register(self, *a):
            raise ServiceError("nope")

    _run(cmds._register_impl(update, ctx, _svc=_ErrSvc()))
    assert "nope" in sent[0]
```

> Note: confirm `ServiceError("nope")` exposes `.message == "nope"`; the Discord `_register_impl` surfaces `e.message`. If `ServiceError`'s constructor differs, match `surfaces/_client/errors.py`.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_telegram_commands.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement**

`surfaces/telegram_bot/commands.py`:

```python
# surfaces/telegram_bot/commands.py
# Telegram command handlers: /mint (interactive), /register <wallet>, /start.
# Mirrors surfaces.discord_bot.commands. _register_impl takes an injectable _svc
# so tests can drive it without the real shared client.
from typing import Any

from surfaces._client.errors import ServiceError
from surfaces.telegram_bot.bot import svc
from surfaces.telegram_bot.mint_view import handle_mint


async def mint(update: Any, context: Any) -> None:
    await handle_mint(svc, update, context)


async def _register_impl(update: Any, context: Any, *, _svc: Any = None) -> None:
    client = _svc if _svc is not None else svc
    user = update.effective_user
    uid = str(user.id)
    name = user.username or getattr(user, "full_name", "") or ""
    args = getattr(context, "args", None) or []
    if not args:
        await update.message.reply_text("Usage: /register <wallet>")
        return
    wallet = args[0]
    try:
        await client.register(uid, name, wallet)
    except ServiceError as e:
        await update.message.reply_text(e.message or "There was an error registering your wallet.")
        return
    await update.message.reply_text("Your wallet has been registered!")


async def register(update: Any, context: Any) -> None:
    await _register_impl(update, context)


async def start(update: Any, context: Any) -> None:
    await update.message.reply_text(
        "Welcome to LFG! Register your wallet with /register <wallet>, then /mint to mint an NFT."
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_telegram_commands.py -v`
Expected: PASS (3 tests).

> If import fails because `bot.py` does not exist yet, implement Task B6 first then return — the test only needs `commands` importable, and `bot.py` defines `svc`. (Subagent-driven execution: do B6 before B5's test run, or stub `svc` — prefer doing B6 first.)

- [ ] **Step 5: Commit**

```bash
git add surfaces/telegram_bot/commands.py tests/test_telegram_commands.py
git commit -m "feat(telegram): /mint /register /start command handlers"
```

---

### Task B6: `bot.py` — PTB application lifecycle

**Files:**
- Create: `surfaces/telegram_bot/bot.py`
- Test: `tests/test_telegram_bot_lifecycle.py` (create)

**Interfaces:**
- Consumes: `config` (B1), `LFGServiceClient`, `run_event_loop` (B4), `commands` (B5, imported lazily).
- Produces: module-level `svc = LFGServiceClient(config.LFG_SERVICE_URL, config.SERVICE_TOKEN_TELEGRAM, "telegram")`; `build_application()` returning a configured PTB `Application`; `async _post_init(application)` / `async _post_shutdown(application)` (events task started/cancelled, svc entered/closed in the correct order); `main()`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_telegram_bot_lifecycle.py
import asyncio

import pytest


@pytest.fixture
def bot_mod(monkeypatch):
    for k, v in {
        "TELEGRAM_BOT_TOKEN": "t",
        "LFG_SERVICE_URL": "http://svc",
        "SERVICE_TOKEN_TELEGRAM": "s",
        "TELEGRAM_ANNOUNCE_CHAT_ID": "1",
    }.items():
        monkeypatch.setenv(k, v)
    import importlib

    import surfaces.telegram_bot.config as cfg

    importlib.reload(cfg)
    import surfaces.telegram_bot.bot as b

    importlib.reload(b)
    return b


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_svc_configured_for_telegram(bot_mod):
    assert bot_mod.svc._surface == "telegram"
    assert bot_mod.svc._service_token == "s"


def test_post_shutdown_cancels_events_and_closes_svc(bot_mod, monkeypatch):
    closed = {"svc": False}

    async def fake_aenter():
        return bot_mod.svc

    async def fake_close():
        closed["svc"] = True

    monkeypatch.setattr(bot_mod.svc, "__aenter__", fake_aenter)
    monkeypatch.setattr(bot_mod.svc, "close", fake_close)

    # Fake an Application whose bot has an async send_message
    class _Bot:
        async def send_message(self, **kw):
            pass

    app = type("App", (), {"bot": _Bot()})()

    # post_init should enter svc and start a (cancellable) events task
    async def never_ending(svc, announce, dm):
        ev = asyncio.Event()
        await ev.wait()

    monkeypatch.setattr(bot_mod, "run_event_loop", never_ending)

    async def scenario():
        await bot_mod._post_init(app)
        assert bot_mod._events_task is not None
        await bot_mod._post_shutdown(app)
        assert bot_mod._events_task is None
        assert closed["svc"] is True

    _run(scenario())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_telegram_bot_lifecycle.py -v`
Expected: FAIL — module/attrs missing.

- [ ] **Step 3: Implement**

`surfaces/telegram_bot/bot.py`:

```python
# surfaces/telegram_bot/bot.py
# python-telegram-bot v21 application lifecycle for the Telegram surface. One
# shared LFGServiceClient drives every handler. The firehose consumer runs as a
# cancellable task started in post_init and stopped BEFORE svc.close() in
# post_shutdown, so the generator's aclose() releases the WebSocket on a live
# aiohttp session (mirrors the Discord adapter's cleanup ordering).
import asyncio
import logging

from telegram.ext import Application, CommandHandler

from surfaces._client import LFGServiceClient
from surfaces.telegram_bot import config
from surfaces.telegram_bot.events import run_event_loop

svc = LFGServiceClient(config.LFG_SERVICE_URL, config.SERVICE_TOKEN_TELEGRAM, "telegram")

_events_task: asyncio.Task[None] | None = None


async def _post_init(application: Application) -> None:
    global _events_task
    await svc.__aenter__()

    async def _announce(message: str) -> None:
        await application.bot.send_message(chat_id=config.TELEGRAM_ANNOUNCE_CHAT_ID, text=message)

    async def _dm(uid: str, message: str) -> None:
        try:
            await application.bot.send_message(chat_id=int(uid), text=message)
        except Exception as e:
            logging.warning(f"DM to {uid} failed: {e}")

    _events_task = asyncio.create_task(run_event_loop(svc, _announce, _dm))


async def _post_shutdown(application: Application) -> None:
    global _events_task
    if _events_task is not None:
        _events_task.cancel()
        await asyncio.gather(_events_task, return_exceptions=True)
        _events_task = None
    try:
        await svc.close()
    except Exception as e:
        logging.error(f"Error closing service client: {e}")


def build_application() -> Application:
    application = (
        Application.builder()
        .token(config.TELEGRAM_BOT_TOKEN)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )
    from surfaces.telegram_bot import commands as cmds

    application.add_handler(CommandHandler("mint", cmds.mint))
    application.add_handler(CommandHandler("register", cmds.register))
    application.add_handler(CommandHandler(["start", "help"], cmds.start))
    return application


def main() -> None:
    application = build_application()
    application.run_polling()


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_telegram_bot_lifecycle.py tests/test_telegram_commands.py -v`
Expected: PASS — lifecycle + commands import cleanly (no circular import).

- [ ] **Step 5: Commit**

```bash
git add surfaces/telegram_bot/bot.py tests/test_telegram_bot_lifecycle.py
git commit -m "feat(telegram): PTB application lifecycle (events task + svc cleanup ordering)"
```

---

### Task B7: SourceTag-clean invariant, docs, pm2, finish PR B

**Files:**
- Create: `tests/test_telegram_sourcetag_invariant.py`
- Modify: `CLAUDE.md` (env + pm2 docs)
- Test: full suite + lint + mypy

**Interfaces:**
- Produces: a test asserting the Telegram package builds zero inline XRPL/XUMM transactions (SourceTag-clean by construction).

- [ ] **Step 1: Write the invariant test**

```python
# tests/test_telegram_sourcetag_invariant.py
# The Telegram surface builds NO inline XRPL/XUMM transactions — all minting
# goes through lfg_service, which stamps the Make Waves SourceTag (covered by
# test_xrpl_source_tag.py + test_xumm_source_tag.py). This test pins that
# invariant: no TransactionType / source_tag / NFToken construction appears in
# the Telegram package source.
import pathlib

_PKG = pathlib.Path(__file__).resolve().parent.parent / "surfaces" / "telegram_bot"

_FORBIDDEN = ("TransactionType", "NFTokenMint", "NFTokenBurn", "TrustSet", "submit_and_wait")


def test_no_inline_xrpl_tx_in_telegram_package():
    offenders = []
    for py in _PKG.glob("*.py"):
        text = py.read_text()
        for needle in _FORBIDDEN:
            if needle in text:
                offenders.append((py.name, needle))
    assert offenders == [], f"unexpected inline tx tokens in telegram package: {offenders}"
```

- [ ] **Step 2: Run it**

Run: `.venv/bin/pytest tests/test_telegram_sourcetag_invariant.py -v`
Expected: PASS (package is tx-free by construction).

- [ ] **Step 3: Document env + pm2**

In `CLAUDE.md`, add the Telegram env vars to the `.env` block:

```
TELEGRAM_BOT_TOKEN=<telegram-bot-token>
SERVICE_TOKEN_TELEGRAM=<telegram-surface-token>
TELEGRAM_ANNOUNCE_CHAT_ID=<telegram-channel-id>
```

And add a one-line deployment note near the other pm2 services: the Telegram surface runs as pm2 process `lfg-telegram` → `python -m surfaces.telegram_bot.bot`.

- [ ] **Step 4: Full gate**

Run: `.venv/bin/pytest tests/ -q && .venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy .`
Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add tests/test_telegram_sourcetag_invariant.py CLAUDE.md
git commit -m "test(telegram): SourceTag-clean invariant + deployment docs"
```

- [ ] **Step 6: Push + draft PR (closes #43)**

```bash
git push -u origin feat/spine-plan4b-telegram
gh pr create --draft --repo Team-Hamsa/LFG \
  --title "feat(telegram): Telegram surface (Spine Plan 4 of 4)" \
  --body "Closes #43. Adds surfaces/telegram_bot/ (PTB v21) on the shared lfg_service via the Surface SDK: interactive /mint, /register, mint announcements + minter DM. Depends on the merged platform-aware spine (Plan 4a). SourceTag-clean by construction (no inline tx)."
```

- [ ] **Step 7: Flip ready when settled, route through CodeRabbit/Greptile**

```bash
gh pr ready <number> --repo Team-Hamsa/LFG
```
Resolve review findings; merge only after review is handled.

---

## Post-merge (both PRs landed)

- [ ] Start the pm2 process on the host: `pm2 start "python -m surfaces.telegram_bot.bot" --name lfg-telegram` (after setting the new env vars), `pm2 save`.
- [ ] Link this plan + the spec back to issue #43 (per LFG CLAUDE.md): `gh issue comment 43 --repo Team-Hamsa/LFG --body "Spec: <blob-url>\nPlan: <blob-url>"` with permalinks at the merge commit SHA.

---

## Self-Review

**Spec coverage:**
- Part A (platform-awareness): token (A1), resolve sites me/events_me/require_wallet (A2), link sites register/signin (A3), mint publish + MintSession (A4). ✓ All five hardcoded-`"discord"` sites + `require_wallet` covered.
- Part B (adapter): config (B1), render (B2), mint_view (B3), events (B4), commands (B5), bot lifecycle (B6), SourceTag invariant + docs + pm2 (B7). ✓ Mirrors `surfaces/discord_bot/`.
- Two-PR delivery: PR A (A1–A5) merges before PR B (B1–B7). ✓
- `discord_id` kept; `platform` added separately. ✓ (A4)
- SourceTag: no inline tx + invariant test. ✓ (Global constraint + B7)
- Library PTB v21+. ✓ (B1)
- Admin out of scope. ✓ (no admin tasks)

**Placeholder scan:** No TBD/TODO/"add error handling". Two explicit "confirm constructor signature" notes (B3, B5) point the implementer at `surfaces/_client/errors.py` with the canonical Discord test as reference — these are verification cues, not placeholders; the code shown is complete and runnable as written if the signatures match (they do per the Discord adapter).

**Type consistency:** `handle_mint(svc, update, context)`, `run_event_loop(svc, announce, dm_user=None)`, `_register_impl(update, context, *, _svc=None)`, `make_session_token(user)`, `_resolve_wallet(platform, uid)`, `_platform(user)`, `MintSession(..., platform="discord")` — names/signatures match across all tasks and against the existing Discord adapter + SDK + service code that was read to write this plan.
