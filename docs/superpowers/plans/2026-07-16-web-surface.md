# Standalone Web Surface Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The Activity interface loads and works in a plain browser at `build.letseffinggo.com` (GitHub Pages front-end + CORS'd prod API over the existing Tailscale Funnel), authenticated by Xaman wallet sign-in as a 4th surface (`platform="web"`).

**Architecture:** Service gains a dark-by-default CORS middleware (`WEB_ALLOWED_ORIGINS`) and two client-callable web-signin endpoints that bootstrap a session from a XUMM SignIn (wallet = identity). Client gains a `config.js`-driven API base and an `insideWeb` boot branch reusing the existing register-panel QR UI. A GitHub Actions workflow publishes `webapp/client/` to Pages on every push to `deploy`.

**Tech Stack:** aiohttp middleware/handlers, existing `xumm_ops` signin payloads, vanilla JS client, GitHub Pages via `actions/deploy-pages`.

**Spec:** `docs/superpowers/specs/2026-07-16-web-surface-design.md`

## Global Constraints

- No behavior change for Discord/Telegram/dev surfaces when `WEB_ALLOWED_ORIGINS` is unset and `window.LFG_WEB` is null.
- New test files importing `lfg_core` at module top MUST copy the env-guard preamble (BUNNY_PULL_ZONE/LAYER_SOURCE) used by existing tests (repo convention; full-suite ordering breaks otherwise).
- Session tokens: `make_session_token({"id": <wallet>, "name": …, "platform": "web"})`; wallet is the `platform_user_id`.
- SignIn payloads carry no SourceTag/memos (existing exemption — pseudo-tx).
- Pre-push gate (ruff/ruff-format/mypy/gitleaks/pytest/validate-trait-config) must pass; never `--no-verify`.
- Work happens in worktree `/home/hamsa/LFG/.claude/worktrees/web-surface` on branch `feat/web-surface`.

---

### Task 1: Config + memos plumbing

**Files:**
- Modify: `lfg_core/config.py` (near `TELEGRAM_INITDATA_MAX_AGE`)
- Modify: `lfg_core/memos.py:48-56` (`_SURFACE_TO_PLATFORM`)
- Test: `tests/test_web_surface_config.py`

**Interfaces:**
- Produces: `config.WEB_ALLOWED_ORIGINS: tuple[str, ...]` (parsed, stripped, empty entries dropped); `memos.platform_for_surface("web") == memos.PLATFORM_WEBAPP`.

- [ ] **Step 1: failing test**

```python
# tests/test_web_surface_config.py
# WEB_ALLOWED_ORIGINS parsing + memos surface mapping for the web surface.
import os

os.environ.setdefault("BUNNY_PULL_ZONE", "test.b-cdn.net")
os.environ.setdefault("LAYER_SOURCE", "local")

from lfg_core import memos


def test_web_allowed_origins_default_empty():
    from lfg_core import config
    assert isinstance(config.WEB_ALLOWED_ORIGINS, tuple)


def test_parse_allowed_origins_strips_and_drops_empties():
    from lfg_core.config import _parse_allowed_origins
    got = _parse_allowed_origins(" https://a.example ,, https://b.example ")
    assert got == ("https://a.example", "https://b.example")


def test_memos_web_surface_maps_to_webapp():
    assert memos.platform_for_surface("web") == memos.PLATFORM_WEBAPP
```

- [ ] **Step 2: run, expect FAIL** — `pytest tests/test_web_surface_config.py -v` → `_parse_allowed_origins` undefined; web maps to backend.

- [ ] **Step 3: implement**

`lfg_core/config.py` (after the TELEGRAM block):

```python
def _parse_allowed_origins(raw: str) -> tuple[str, ...]:
    return tuple(o.strip() for o in raw.split(",") if o.strip())


# Standalone web surface (build.letseffinggo.com): exact Origin values allowed
# to call the API cross-origin. Empty (default) = CORS middleware inert.
WEB_ALLOWED_ORIGINS = _parse_allowed_origins(os.getenv("WEB_ALLOWED_ORIGINS", ""))
```

`lfg_core/memos.py`: add `"web": PLATFORM_WEBAPP,` to `_SURFACE_TO_PLATFORM`.

- [ ] **Step 4: run, expect PASS** — same command.
- [ ] **Step 5: commit** — `git commit -m "feat(web-surface): WEB_ALLOWED_ORIGINS config + web→webapp memo platform"`

### Task 2: CORS middleware

**Files:**
- Modify: `lfg_service/app.py` (new `cors_mw` next to `no_cache_mw:3545`; register in `create_app():3557`)
- Test: `tests/test_web_cors.py`

**Interfaces:**
- Produces: `lfg_service.app.cors_mw` (aiohttp middleware). Reads `config.WEB_ALLOWED_ORIGINS` at request time (monkeypatch-able).

- [ ] **Step 1: failing test**

```python
# tests/test_web_cors.py
# CORS middleware: dark by default; allowlisted Origins get ACAO + preflight.
import asyncio
import os

os.environ.setdefault("BUNNY_PULL_ZONE", "test.b-cdn.net")
os.environ.setdefault("LAYER_SOURCE", "local")

from aiohttp import web
from aiohttp.test_utils import make_mocked_request

import lfg_service.app as app


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _ok(request):
    return web.json_response({"ok": True})


def _req(method="GET", origin=None):
    headers = {"Origin": origin} if origin else {}
    return make_mocked_request(method, "/api/config", headers=headers)


def test_no_allowlist_no_headers(monkeypatch):
    monkeypatch.setattr(app.config, "WEB_ALLOWED_ORIGINS", ())
    resp = _run(app.cors_mw(_req(origin="https://evil.example"), _ok))
    assert "Access-Control-Allow-Origin" not in resp.headers


def test_allowed_origin_gets_acao(monkeypatch):
    monkeypatch.setattr(app.config, "WEB_ALLOWED_ORIGINS", ("https://build.letseffinggo.com",))
    resp = _run(app.cors_mw(_req(origin="https://build.letseffinggo.com"), _ok))
    assert resp.headers["Access-Control-Allow-Origin"] == "https://build.letseffinggo.com"
    assert "Origin" in resp.headers["Vary"]


def test_foreign_origin_gets_nothing(monkeypatch):
    monkeypatch.setattr(app.config, "WEB_ALLOWED_ORIGINS", ("https://build.letseffinggo.com",))
    resp = _run(app.cors_mw(_req(origin="https://evil.example"), _ok))
    assert "Access-Control-Allow-Origin" not in resp.headers


def test_preflight_short_circuits(monkeypatch):
    monkeypatch.setattr(app.config, "WEB_ALLOWED_ORIGINS", ("https://build.letseffinggo.com",))
    resp = _run(app.cors_mw(_req("OPTIONS", origin="https://build.letseffinggo.com"), _ok))
    assert resp.status == 204
    assert "Authorization" in resp.headers["Access-Control-Allow-Headers"]
    assert "POST" in resp.headers["Access-Control-Allow-Methods"]
```

- [ ] **Step 2: run, expect FAIL** — `pytest tests/test_web_cors.py -v` → no `cors_mw`.
- [ ] **Step 3: implement** (next to `no_cache_mw`):

```python
@web.middleware
async def cors_mw(request, handler):
    # Standalone web surface (spec 2026-07-16): the Pages-hosted client calls
    # this API cross-origin. Dark by default — with WEB_ALLOWED_ORIGINS unset
    # nothing changes for Discord/Telegram (same-origin, no Origin match).
    origin = request.headers.get("Origin", "")
    allowed = origin and origin in config.WEB_ALLOWED_ORIGINS
    if allowed and request.method == "OPTIONS":
        resp = web.Response(status=204)
    else:
        resp = await handler(request)
    if allowed:
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers.add("Vary", "Origin")
        if request.method == "OPTIONS":
            resp.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
            resp.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
            resp.headers["Access-Control-Max-Age"] = "3600"
    return resp
```

Register: `web.Application(middlewares=[cors_mw, no_cache_mw])`. Preflight short-circuit is inside the middleware, so no OPTIONS routes are needed.

- [ ] **Step 4: run, expect PASS**; also `pytest webapp/test_smoke.py -q` still green.
- [ ] **Step 5: commit** — `git commit -m "feat(web-surface): CORS middleware gated on WEB_ALLOWED_ORIGINS"`

### Task 3: Web signin endpoints

**Files:**
- Modify: `lfg_service/app.py` (new section next to `handle_signin_start:3147`; routes in `create_app()`)
- Test: `tests/test_web_signin_endpoint.py`

**Interfaces:**
- Consumes: `xumm_ops.create_signin_payload(return_url=…)`, `xumm_ops.get_payload_status(uuid)`, `identity_store.link/set_user_token/handle_for_wallet`, `make_session_token`.
- Produces: `POST /api/web/signin` → `{uuid, signin_link}` | 429 | 502; `GET /api/web/signin/{payload_uuid}` → `{state}` / on signed `{state:"signed", wallet, session_token, user:{id,username}}`.

- [ ] **Step 1: failing tests**

```python
# tests/test_web_signin_endpoint.py
# Client-callable wallet signin for the standalone web surface: bootstraps a
# platform="web" session from a XUMM SignIn (wallet = platform_user_id).
import asyncio
import json
import os

os.environ.setdefault("BUNNY_PULL_ZONE", "test.b-cdn.net")
os.environ.setdefault("LAYER_SOURCE", "local")

import lfg_service.app as app
from lfg_service.app import verify_session_token

WALLET = "rN7n7otQDd6FczFgLdSqtcsAUxDkw6fzRH"


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _Req:
    def __init__(self, body=None, headers=None, match=None, remote="1.2.3.4"):
        self._body = body or {}
        self.headers = headers or {}
        self.match_info = match or {}
        self.remote = remote
        self._store: dict = {}

    async def json(self):
        return self._body

    def __getitem__(self, k):
        return self._store[k]

    def __setitem__(self, k, v):
        self._store[k] = v


def setup_function(_fn):
    app.web_signin_payloads.clear()
    app._web_signin_hits.clear()


def test_start_creates_payload(monkeypatch):
    async def fake_create(return_url=None):
        return {"uuid": "u-1", "xumm_url": "https://xumm.app/sign/u-1"}

    monkeypatch.setattr(app.xumm_ops, "create_signin_payload", fake_create)
    resp = _run(app.handle_web_signin_start(_Req()))
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body == {"uuid": "u-1", "signin_link": "https://xumm.app/sign/u-1"}
    assert "u-1" in app.web_signin_payloads


def test_start_rate_limited(monkeypatch):
    async def fake_create(return_url=None):
        return {"uuid": "u-x", "xumm_url": "x"}

    monkeypatch.setattr(app.xumm_ops, "create_signin_payload", fake_create)
    for _ in range(app.WEB_SIGNIN_RATE_MAX):
        assert _run(app.handle_web_signin_start(_Req())).status == 200
    resp = _run(app.handle_web_signin_start(_Req()))
    assert resp.status == 429


def test_status_signed_issues_web_session(monkeypatch):
    app.web_signin_payloads["u-2"] = {"created_at": 0}
    linked = {}

    async def fake_status(uuid):
        return {"signed": True, "account": WALLET, "expired": False,
                "opened": True, "user_token": "push-tok"}

    monkeypatch.setattr(app.xumm_ops, "get_payload_status", fake_status)
    monkeypatch.setattr(app.identity_store, "handle_for_wallet", lambda w: None)
    monkeypatch.setattr(
        app.identity_store, "link",
        lambda p, uid, name, wallet: linked.update(p=p, uid=uid, name=name, w=wallet) or True,
    )
    tokens = {}
    monkeypatch.setattr(
        app.identity_store, "set_user_token",
        lambda p, uid, tok: tokens.update({(p, uid): tok}),
    )
    resp = _run(app.handle_web_signin_status(_Req(match={"payload_uuid": "u-2"})))
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body["state"] == "signed"
    assert body["wallet"] == WALLET
    decoded = verify_session_token(body["session_token"])
    assert decoded["platform"] == "web"
    assert decoded["id"] == WALLET
    assert linked == {"p": "web", "uid": WALLET, "name": body["user"]["username"], "w": WALLET}
    assert tokens[("web", WALLET)] == "push-tok"
    assert "u-2" not in app.web_signin_payloads


def test_status_unknown_uuid_404():
    resp = _run(app.handle_web_signin_status(_Req(match={"payload_uuid": "nope"})))
    assert resp.status == 404


def test_status_expired(monkeypatch):
    app.web_signin_payloads["u-3"] = {"created_at": 0}

    async def fake_status(uuid):
        return {"signed": False, "account": None, "expired": True, "opened": False}

    monkeypatch.setattr(app.xumm_ops, "get_payload_status", fake_status)
    resp = _run(app.handle_web_signin_status(_Req(match={"payload_uuid": "u-3"})))
    assert json.loads(resp.body)["state"] == "expired"
    assert "u-3" not in app.web_signin_payloads
```

- [ ] **Step 2: run, expect FAIL** — handlers undefined.
- [ ] **Step 3: implement** (below the existing signin section):

```python
# --- Standalone web surface signin (spec 2026-07-16) ---------------------
# Client-callable (like /api/telegram/auth): bootstraps a session where the
# wallet IS the identity — platform="web", platform_user_id=<classic address>.
# The payload uuid (128-bit, single-use, short-TTL) is the bearer secret; no
# pre-auth ownership check is possible, same trust model as the XUMM deep link.

web_signin_payloads: dict[str, Any] = {}
WEB_SIGNIN_RATE_MAX = 5           # payload creations…
WEB_SIGNIN_RATE_WINDOW = 60.0     # …per IP per window (XUMM API protection)
_web_signin_hits: dict[str, list[float]] = {}


def _client_ip(request) -> str:
    # Funnel/tailscale serve fronts the service; the peer addr is localhost.
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.remote or "?"


def _web_rate_limited(ip: str) -> bool:
    now = time.time()
    hits = [t for t in _web_signin_hits.get(ip, []) if now - t < WEB_SIGNIN_RATE_WINDOW]
    if len(hits) >= WEB_SIGNIN_RATE_MAX:
        _web_signin_hits[ip] = hits
        return True
    hits.append(now)
    _web_signin_hits[ip] = hits
    return False


def _prune_web_signin_payloads():
    cutoff = time.time() - SIGNIN_TTL
    for uuid, rec in list(web_signin_payloads.items()):
        if rec["created_at"] < cutoff:
            del web_signin_payloads[uuid]


async def handle_web_signin_start(request):
    if _web_rate_limited(_client_ip(request)):
        return web.json_response(
            {"error": "too many sign-in attempts", "code": "rate_limited"}, status=429
        )
    _prune_web_signin_payloads()
    origin = request.headers.get("Origin", "")
    return_url = (
        {"app": origin, "web": origin} if origin in config.WEB_ALLOWED_ORIGINS else None
    )
    payload = await xumm_ops.create_signin_payload(return_url=return_url)
    if not payload:
        return web.json_response({"error": "could not reach Xaman"}, status=502)
    web_signin_payloads[payload["uuid"]] = {"created_at": time.time()}
    return web.json_response({"uuid": payload["uuid"], "signin_link": payload["xumm_url"]})


async def handle_web_signin_status(request):
    uuid = request.match_info["payload_uuid"]
    if uuid not in web_signin_payloads:
        return web.json_response({"error": "not found"}, status=404)
    s = await xumm_ops.get_payload_status(uuid)
    if not s:
        return web.json_response({"error": "could not reach Xaman"}, status=502)
    if s["signed"] and s["account"] and is_valid_classic_address(s["account"]):
        wallet = s["account"]
        handle = await asyncio.to_thread(identity_store.handle_for_wallet, wallet)
        name = handle or f"{wallet[:6]}…{wallet[-4:]}"
        if not await asyncio.to_thread(identity_store.link, "web", wallet, name, wallet):
            return web.json_response({"error": "identity link failed"}, status=500)
        if s.get("user_token"):
            await asyncio.to_thread(identity_store.set_user_token, "web", wallet, s["user_token"])
        del web_signin_payloads[uuid]
        token = make_session_token({"id": wallet, "name": name, "platform": "web"})
        return web.json_response(
            {"state": "signed", "wallet": wallet, "session_token": token,
             "user": {"id": wallet, "username": name}}
        )
    if s["expired"]:
        del web_signin_payloads[uuid]
        return web.json_response({"state": "expired"})
    return web.json_response({"state": "opened" if s["opened"] else "pending"})
```

(Reuse the existing `SIGNIN_TTL` constant used by `_prune_signin_payloads`; if it's named differently, match it.) Routes in `create_app()` next to the signin pair:

```python
    app.router.add_post("/api/web/signin", handle_web_signin_start)
    app.router.add_get("/api/web/signin/{payload_uuid}", handle_web_signin_status)
```

- [ ] **Step 4: run, expect PASS** — plus `pytest tests/test_service_signin_platform.py tests/test_telegram_auth_endpoint.py -q` untouched.
- [ ] **Step 5: commit** — `git commit -m "feat(web-surface): client-callable /api/web/signin wallet auth arm"`

### Task 4: Client — config.js, API base, web boot branch

**Files:**
- Create: `webapp/client/config.js` (`window.LFG_WEB = null;` + comment)
- Modify: `webapp/client/index.html:233` (script tag before `app.js`)
- Modify: `webapp/client/app.js` (API base; `insideWeb`; `setupWeb()`; signin reuse; boot)

**Interfaces:**
- Consumes: `POST /api/web/signin` / `GET /api/web/signin/{uuid}` from Task 3.
- Produces: web-mode boot path; `localStorage["lfg_web_session"]`.

- [ ] **Step 1: implement** (no-build client; Node-testable pure modules don't cover DOM boot — verification is Task 6 e2e + existing smoke tests):

`config.js`:
```js
// Deploy-time surface config. The repo default (null) means "not the
// standalone web surface" — Discord/Telegram/dev behave exactly as before.
// The GitHub Pages deploy overwrites this file with
//   window.LFG_WEB = { apiBase: 'https://…/lfg' };
window.LFG_WEB = null;
```

`index.html`: `<script src="config.js?v=1"></script>` immediately before the `telegram-web-app.js` tag.

`app.js` (top, after `insideTelegram`):
```js
// Standalone web surface (build.letseffinggo.com): config.js sets LFG_WEB
// when this client is served from GitHub Pages; the API then lives on
// another origin (the funnel) and auth is a wallet sign-in, not Discord/TG.
const webCfg = window.LFG_WEB || null;
const insideWeb = !!webCfg && !insideDiscord && !insideTelegram;
const API_BASE = (webCfg && webCfg.apiBase) || '';
const WEB_SESSION_KEY = 'lfg_web_session';
```
- `api()`: `fetch(API_BASE + path, …)`; on 401 in web mode clear `localStorage[WEB_SESSION_KEY]`.
- `qrUrl()`/`imgUrl()`: prefix `API_BASE`.
- `renderSignin`/`pollSignin`: parameterize endpoint + completion:

```js
async function startWebSignin() {
  clearTimeout(signinPollTimer);
  showPanel('register-panel');
  renderSignin({ sub: 'Setting up your Xaman sign-in…', spinner: true });
  try {
    const s = await api('/api/web/signin', { method: 'POST', body: '{}' });
    renderSignin({
      sub: 'Scan with Xaman and approve the sign-in — your wallet is your login.',
      qrLink: s.signin_link,
    });
    pollWebSignin(s.uuid);
  } catch (e) {
    showError(e.message);
    renderSignin({ sub: 'Could not start the Xaman sign-in.', retry: true });
  }
}

function pollWebSignin(uuid) {
  clearTimeout(signinPollTimer);
  const tick = async () => {
    if (el('register-panel').hidden) return;
    let s;
    try { s = await api(`/api/web/signin/${uuid}`); }
    catch (e) { signinPollTimer = setTimeout(tick, 3000); return; }
    if (s.state === 'signed') {
      sessionToken = s.session_token;
      try { localStorage.setItem(WEB_SESSION_KEY, s.session_token); } catch (_) {}
      me = { ...s.user, wallet: s.wallet };
      showMintHome();
      return;
    }
    if (s.state === 'expired') { renderSignin({ sub: 'The sign-in request expired.', retry: true }); return; }
    if (s.state === 'opened') renderSignin({ sub: 'QR scanned — approve the sign-in in Xaman…', spinner: true });
    signinPollTimer = setTimeout(tick, 3000);
  };
  signinPollTimer = setTimeout(tick, 3000);
}

async function setupWeb() {
  let stored = null;
  try { stored = localStorage.getItem(WEB_SESSION_KEY); } catch (_) {}
  if (stored) {
    sessionToken = stored;
    try { return await api('/api/me'); }           // still valid → straight in
    catch (_) { sessionToken = null; }             // expired → fresh sign-in
  }
  await startWebSignin();
  return null; // signin flow drives the UI from here
}
```
- Boot: web branch before the degraded-mode return; `register-retry-btn`/`change-wallet-btn` route to `startWebSignin` when `insideWeb`; dev-reload EventSource skipped when `API_BASE` is set.

```js
  if (insideWeb) {
    try {
      const user = await setupWeb();
      if (user) { me = user; if (!(await resumeMint())) showMintHome(); }
    } catch (e) { console.error(e); status(`Failed to connect: ${e.message}`); }
    return;
  }
```

- [ ] **Step 2: verify** — `pytest webapp/ tests/test_market_pure_js.py tests/test_mint_pure_js.py -q` green; `node --check webapp/client/app.js` parses (module syntax: use `node --input-type=module --check` equivalent or skip if unsupported).
- [ ] **Step 3: commit** — `git commit -m "feat(web-surface): client web boot branch + configurable API base"`

### Task 5: GitHub Pages workflow + docs

**Files:**
- Create: `.github/workflows/pages.yml`
- Modify: `CLAUDE.md` (env var), `docs/ops/` note optional

```yaml
name: Deploy web surface to GitHub Pages
on:
  push:
    branches: [deploy]          # prod parity: client matches the API the funnel serves
    paths: ['webapp/client/**', '.github/workflows/pages.yml']
  workflow_dispatch:
permissions:
  contents: read
  pages: write
  id-token: write
concurrency: { group: pages, cancel-in-progress: true }
jobs:
  deploy:
    runs-on: ubuntu-latest
    environment: { name: github-pages, url: '${{ steps.deployment.outputs.page_url }}' }
    steps:
      - uses: actions/checkout@v4
      - name: Assemble site
        run: |
          mkdir -p _site
          cp -r webapp/client/. _site/
          cat > _site/config.js <<'EOF'
          window.LFG_WEB = { apiBase: 'https://letseffinggo.tail82fcc6.ts.net/lfg' };
          EOF
      - uses: actions/configure-pages@v5
      - uses: actions/upload-pages-artifact@v3
        with: { path: _site }
      - id: deployment
        uses: actions/deploy-pages@v4
```

- [ ] Enable Pages (one-time, after merge): `gh api -X POST repos/Team-Hamsa/LFG/pages -f build_type=workflow` then `gh api -X PUT repos/Team-Hamsa/LFG/pages -f cname=build.letseffinggo.com -f build_type=workflow`.
- [ ] CLAUDE.md env block: `WEB_ALLOWED_ORIGINS=https://build.letseffinggo.com,https://team-hamsa.github.io   # optional; standalone web surface CORS allowlist (empty = off)`.
- [ ] Commit — `git commit -m "feat(web-surface): GitHub Pages deploy workflow + docs"`

### Task 6: Gate, PR, review, merge, deploy, e2e

- [ ] Full gate in worktree: `.venv` exists? worktrees share repo — run `~/LFG/.venv/bin/python -m pytest` if worktree venv absent; then push (pre-push runs everything).
- [ ] Open ready PR → wait Greptile + CodeRabbit → resolve all actionable findings → merge.
- [ ] Staging auto-deploys; smoke `curl http://127.0.0.1:8177/api/config`.
- [ ] Prod: add `WEB_ALLOWED_ORIGINS` to `~/LFG/.env`, run `scripts/promote.sh`; Pages workflow fires on `deploy` push.
- [ ] e2e: Pages URL loads (`curl -s https://team-hamsa.github.io/LFG/ | grep app.js`), preflight OPTIONS against funnel returns ACAO, `POST /api/web/signin` returns a payload.

## Self-Review

- Spec coverage: auth arm (T3), CORS (T2), config/memos (T1), client (T4), Pages+ops (T5-6), DNS handoff = post-plan ops. ✔
- No placeholders; names consistent (`web_signin_payloads`, `WEB_SIGNIN_RATE_MAX`, `handle_web_signin_start/status`, `LFG_WEB`, `lfg_web_session`). ✔
- `SIGNIN_TTL` name must be verified against app.py at execution (noted inline). ✔
