# Bulk Mint UI (Activity) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose the merged bulk-mint backend (#215) in the Activity client — quantity stepper, K× payment, live fulfillment progress with per-unit accept — behind a server-side `BULK_MINT_UI_ENABLED` flag so staging can test before prod.

**Architecture:** Server side: one new env flag surfaced through `GET /api/config`, plus one new endpoint that lazily builds a XUMM accept payload for a single offered unit. Client side (no-build vanilla JS, `webapp/client/app.js`): a stepper on the mint home routes qty ≥ 2 to `POST /api/mint/bulk`, a new `bulk-panel` renders job progress + a per-unit accept list from the job's `units[]`, and boot resume checks `GET /api/mint/bulk/active` before the existing single-mint resume.

**Tech Stack:** Python 3 / aiohttp (`lfg_service/app.py`), pytest, vanilla JS no-build client (tests are source-assertion style, see `tests/test_app_js_boot.py`).

**Spec:** `docs/superpowers/specs/2026-07-17-bulk-mint-ui-design.md`

## Global Constraints

- Flag default **off**: with `BULK_MINT_UI_ENABLED` unset, the client renders exactly today's UI — no stepper, no bulk calls.
- Accept payloads are created **on click only, never eagerly** (XUMM open-payload cap, #260). All payload plumbing goes through existing `xumm_ops.create_accept_offer_payload` — do not build txjson by hand.
- Qty 1 must hit the existing `POST /api/mint` path byte-for-byte unchanged.
- Bulk routes must stay registered **before** the `/api/mint/{session_id}` wildcard (existing test `test_bulk_route_registered_before_mint_session_wildcard` guards this — keep the new accept route in the same block).
- Pre-push gate (ruff/ruff-format/mypy/gitleaks/pytest) must pass; never `--no-verify`.
- Run tests with `.venv/bin/python -m pytest` from the repo root (worktree root).

---

### Task 1: `BULK_MINT_UI_ENABLED` flag + `/api/config` fields

**Files:**
- Modify: `lfg_core/config.py` (next to `ECONOMY_ENABLED`, ~line 159)
- Modify: `lfg_service/app.py` — `handle_config` (~line 3855)
- Test: `tests/test_bulk_mint_ui_flag.py` (create)

**Interfaces:**
- Produces: `config.BULK_MINT_UI_ENABLED: bool`; `/api/config` JSON gains `"bulk_mint_ui": bool` and `"bulk_mint_max": int`. Task 3's client reads exactly these two keys.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_bulk_mint_ui_flag.py
"""BULK_MINT_UI_ENABLED flag (#215 UI): default off; surfaced via /api/config
so the no-build client can gate the quantity stepper without a deploy."""
import asyncio
import json

from aiohttp.test_utils import make_mocked_request

from lfg_core import config
from lfg_service import app as server


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_flag_defaults_off():
    assert config.BULK_MINT_UI_ENABLED is False


def test_config_endpoint_carries_bulk_fields(monkeypatch):
    monkeypatch.setattr(server.config, "BULK_MINT_UI_ENABLED", True)
    resp = _run(server.handle_config(make_mocked_request("GET", "/api/config")))
    body = json.loads(resp.body)
    assert body["bulk_mint_ui"] is True
    assert body["bulk_mint_max"] == server.config.BULK_MINT_MAX


def test_config_endpoint_bulk_ui_off_by_default():
    resp = _run(server.handle_config(make_mocked_request("GET", "/api/config")))
    body = json.loads(resp.body)
    assert body["bulk_mint_ui"] is False
```

Note: if the test module import fails on frozen config constants when run in full-suite order, copy the env-guard preamble from an existing test (e.g. top of `tests/test_bulk_mint_service.py`) — the `BUNNY_PULL_ZONE`/`LAYER_SOURCE` guard convention.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_bulk_mint_ui_flag.py -v`
Expected: FAIL — `AttributeError: module 'lfg_core.config' has no attribute 'BULK_MINT_UI_ENABLED'`

- [ ] **Step 3: Implement**

In `lfg_core/config.py`, directly below the `BULK_MINT_MAX` validation block (~line 122):

```python
# Bulk mint UI flag (#215 follow-up): gates the Activity's quantity stepper /
# bulk flow client-side via /api/config. Server bulk endpoints stay live
# regardless (they're quantity-capped and auth'd on their own). Off by
# default; staging sets it first (docs/ops/env.staging.example).
BULK_MINT_UI_ENABLED = os.getenv("BULK_MINT_UI_ENABLED", "0") not in ("0", "false", "False")
```

In `lfg_service/app.py` `handle_config`, add to the returned dict next to `"economy_enabled"`:

```python
            "bulk_mint_ui": config.BULK_MINT_UI_ENABLED,
            "bulk_mint_max": config.BULK_MINT_MAX,
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest tests/test_bulk_mint_ui_flag.py tests/test_bulk_mint_service.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add lfg_core/config.py lfg_service/app.py tests/test_bulk_mint_ui_flag.py
git commit -m "feat: BULK_MINT_UI_ENABLED flag surfaced via /api/config (#215 UI)"
```

---

### Task 2: per-unit accept endpoint (lazy XUMM payload)

**Files:**
- Modify: `lfg_service/app.py` — new handler next to `handle_bulk_mint_status` (~line 3046), route in the bulk block (~line where `add_post("/api/mint/bulk", ...)` lives)
- Test: `tests/test_bulk_mint_service.py` (append)

**Interfaces:**
- Consumes: `bulk_sessions` dict, `bulk_mint_flow.OFFERED`, `xumm_ops.create_accept_offer_payload(offer_id, return_url=, user_token=, platform=)` → `{"qr_url","xumm_url","uuid","pushed","push"} | None`, `memos.platform_for_surface(surface)`, `_platform(user)`, `_request_return_url(request)`.
- Produces: `POST /api/mint/bulk/{session_id}/units/{index}/accept` → 200 `{"qr": str, "link": str, "push": "sent"|"failed"|null}`; 404 unknown/foreign job; 400 bad index; 409 `{"error":"unit_not_offered"}` (also for the claimed-while-offline `offer_id=None` case); 502 `{"error":"payload_failed"}`. Task 4's client calls this.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_bulk_mint_service.py`; reuse its `dev_auth` fixture, `_run`, `_StatusReq` — but the accept route needs a POST stand-in with two match_info keys, so add a tiny variant)

```python
class _AcceptReq(_StatusReq):
    def __init__(self, session_id, index):
        super().__init__(session_id)
        self.match_info = {"session_id": session_id, "index": index}

    async def json(self):
        return {}


def _offered_job(sessions):
    job = bulk_mint_flow.BulkMintJob(
        discord_id="dev", wallet_address="rDEV", requested_qty=2, platform="discord-activity"
    )
    job.quantity = 2
    job.units = [bulk_mint_flow.Unit(index=0), bulk_mint_flow.Unit(index=1)]
    job.units[0].state = bulk_mint_flow.OFFERED
    job.units[0].offer_id = "OFFERIDX0"
    job.state = bulk_mint_flow.FULFILLING
    sessions[job.id] = job
    return job


def test_bulk_unit_accept_route_registered():
    routes = {
        (r.method, r.resource.canonical)
        for r in server.create_app().router.routes()
        if r.resource is not None
    }
    assert ("POST", "/api/mint/bulk/{session_id}/units/{index}/accept") in routes


def test_bulk_unit_accept_happy_path(dev_auth, monkeypatch):
    job = _offered_job(dev_auth)
    seen = {}

    async def fake_payload(offer_id, return_url=None, user_token=None, platform=None, **kw):
        seen.update(offer_id=offer_id, user_token=user_token, platform=platform)
        return {"qr_url": "QR", "xumm_url": "LINK", "uuid": "U", "pushed": False, "push": None}

    monkeypatch.setattr(server.xumm_ops, "create_accept_offer_payload", fake_payload)
    resp = _run(server.handle_bulk_mint_unit_accept(_AcceptReq(job.id, "0")))
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body["link"] == "LINK" and body["qr"] == "QR"
    assert seen["offer_id"] == "OFFERIDX0"
    assert seen["platform"] == server.memos.platform_for_surface("discord-activity")


def test_bulk_unit_accept_rejects_non_offered_unit(dev_auth):
    job = _offered_job(dev_auth)
    resp = _run(server.handle_bulk_mint_unit_accept(_AcceptReq(job.id, "1")))  # unit 1 is PENDING
    assert resp.status == 409


def test_bulk_unit_accept_rejects_bad_index(dev_auth):
    job = _offered_job(dev_auth)
    for bad in ("7", "-1", "zero"):
        resp = _run(server.handle_bulk_mint_unit_accept(_AcceptReq(job.id, bad)))
        assert resp.status == 400, bad


def test_bulk_unit_accept_unknown_job_404(dev_auth):
    resp = _run(server.handle_bulk_mint_unit_accept(_AcceptReq("nope", "0")))
    assert resp.status == 404


def test_bulk_unit_accept_already_claimed_unit_409(dev_auth):
    # offered with offer_id None = gift offer already accepted while the
    # service was down (see BulkMintJob.to_dict contract) — nothing to sign.
    job = _offered_job(dev_auth)
    job.units[0].offer_id = None
    resp = _run(server.handle_bulk_mint_unit_accept(_AcceptReq(job.id, "0")))
    assert resp.status == 409


def test_bulk_unit_accept_payload_failure_502(dev_auth, monkeypatch):
    job = _offered_job(dev_auth)

    async def none_payload(*a, **kw):
        return None

    monkeypatch.setattr(server.xumm_ops, "create_accept_offer_payload", none_payload)
    resp = _run(server.handle_bulk_mint_unit_accept(_AcceptReq(job.id, "0")))
    assert resp.status == 502
```

Check the file's existing imports — it already imports `bulk_mint_flow` and `server`; add `from lfg_core.bulk_mint_flow import Unit`-style access via `bulk_mint_flow.Unit` (no new import needed) and `json` if absent. Verify `server.memos` and `server.xumm_ops` are importable attributes of the app module (they are — `lfg_service/app.py` imports both); if `_AcceptReq` needs `headers`, it inherits `_StatusReq.headers = {}`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_bulk_mint_service.py -k unit_accept -v`
Expected: FAIL — `AttributeError: ... no attribute 'handle_bulk_mint_unit_accept'`

- [ ] **Step 3: Implement the handler** (in `lfg_service/app.py`, directly after `handle_bulk_mint_status`)

```python
@require_auth
async def handle_bulk_mint_unit_accept(request):
    """Build a XUMM accept payload for ONE offered bulk unit (#215 UI), on
    click only — a 10-unit job must never open 10 XUMM payloads up front
    (open-payload cap, #260). Repeat clicks mint a fresh payload; the previous
    one expires via _create_xumm_payload's standard 15-min expire."""
    job = bulk_sessions.get(request.match_info["session_id"])
    if (
        not job
        or job.discord_id != request["user"]["id"]
        or getattr(job, "platform", "discord") != _platform(request["user"])
    ):
        return web.json_response({"error": "not found"}, status=404)
    raw_index = request.match_info["index"]
    if not raw_index.isdigit():  # rejects "-1", "zero"; only plain non-negative ints
        return web.json_response({"error": "invalid_unit"}, status=400)
    index = int(raw_index)
    if index >= len(job.units):
        return web.json_response({"error": "invalid_unit"}, status=400)
    unit = job.units[index]
    # offer_id None while OFFERED = the gift offer was already accepted
    # (delivered while the service was down) — treat as claimed, nothing to sign.
    if unit.state != bulk_mint_flow.OFFERED or not unit.offer_id:
        return web.json_response({"error": "unit_not_offered"}, status=409)
    return_url = await _request_return_url(request)
    payload = await xumm_ops.create_accept_offer_payload(
        unit.offer_id,
        return_url=return_url,
        user_token=job.push_user_token,
        platform=memos.platform_for_surface(job.platform),
    )
    if not payload:
        return web.json_response({"error": "payload_failed"}, status=502)
    return web.json_response(
        {"qr": payload["qr_url"], "link": payload["xumm_url"], "push": payload.get("push")}
    )
```

Register the route inside the existing bulk block (order within the block doesn't matter for the wildcard test, but keep it grouped):

```python
    app.router.add_post(
        "/api/mint/bulk/{session_id}/units/{index}/accept", handle_bulk_mint_unit_accept
    )
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest tests/test_bulk_mint_service.py -v`
Expected: all PASS (including the pre-existing route-order test)

- [ ] **Step 5: Commit**

```bash
git add lfg_service/app.py tests/test_bulk_mint_service.py
git commit -m "feat: lazy per-unit accept endpoint for bulk mint jobs (#215 UI)"
```

---

### Task 3: quantity stepper on mint home (flag-gated)

**Files:**
- Modify: `webapp/client/index.html` — inside `#mint-panel .actions` (~line 51)
- Modify: `webapp/client/app.js` — config read (~line 2386), `mint-btn` wiring (~line 2350)
- Modify: `webapp/client/style.css` — stepper styles
- Test: `tests/test_app_js_bulk.py` (create; source-assertion style like `tests/test_app_js_boot.py`)

**Interfaces:**
- Consumes: `/api/config` `bulk_mint_ui` / `bulk_mint_max` (Task 1).
- Produces: module-level `let bulkCfg = { enabled: false, max: 1 }` and `let mintQty = 1` in `app.js`; `mint-btn` click routes to `startBulkMint(mintQty)` (defined in Task 4) when `mintQty > 1`, else the untouched `startMint()`. DOM ids: `mint-qty`, `qty-minus`, `qty-plus`, `qty-value`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_app_js_bulk.py
# The webapp client is no-build vanilla JS (no JS test harness) — guard the
# bulk-mint UI (#215) the same way tests/test_app_js_boot.py guards boot:
# assert the source contains the flag gate, the stepper, and the routing,
# and that the single-mint path survives unchanged.
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLIENT = os.path.join(ROOT, "webapp", "client")


def _read(name: str) -> str:
    with open(os.path.join(CLIENT, name), encoding="utf-8") as f:
        return f.read()


def test_index_has_qty_stepper_hidden_by_default():
    html = _read("index.html")
    assert 'id="mint-qty"' in html and "hidden" in html.split('id="mint-qty"')[1][:120]
    assert 'id="qty-minus"' in html
    assert 'id="qty-plus"' in html
    assert 'id="qty-value"' in html


def test_app_js_gates_stepper_on_config_flag():
    src = _read("app.js")
    assert "bulk_mint_ui" in src
    assert "bulk_mint_max" in src
    assert "bulkCfg" in src


def test_app_js_routes_qty_to_bulk_and_preserves_single_mint():
    src = _read("app.js")
    assert "startBulkMint" in src
    # single-mint path untouched: startMint still POSTs /api/mint
    assert "api('/api/mint', { method: 'POST'" in src
    assert "'/api/mint/bulk'" in src
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_app_js_bulk.py -v`
Expected: FAIL on all three (no stepper markup, no flag read, no routing)

- [ ] **Step 3: Implement**

`webapp/client/index.html` — replace the mint button line inside `.actions`:

```html
        <div class="mint-row">
          <div id="mint-qty" class="qty-stepper" hidden>
            <button id="qty-minus" class="qty-btn" aria-label="Fewer">−</button>
            <span id="qty-value" aria-live="polite">1</span>
            <button id="qty-plus" class="qty-btn" aria-label="More">+</button>
          </div>
          <button id="mint-btn" class="primary big">⛏️ Mint NFT</button>
        </div>
```

`webapp/client/style.css` — append:

```css
/* Bulk mint quantity stepper (#215 UI) */
.mint-row { display: flex; align-items: center; gap: 8px; justify-content: center; }
.qty-stepper { display: flex; align-items: center; gap: 4px; }
.qty-stepper .qty-btn {
  width: 32px; height: 32px; border-radius: 8px; font-size: 18px; line-height: 1;
  border: 1px solid var(--border, #444); background: transparent; color: inherit; cursor: pointer;
}
.qty-stepper .qty-btn:disabled { opacity: 0.4; cursor: default; }
#qty-value { min-width: 24px; text-align: center; font-weight: 700; }
```

(Check `style.css` for an existing `--border`-style custom property and reuse whatever the file actually uses for button borders — match the surrounding idiom rather than inventing a variable.)

`webapp/client/app.js` — near the other module-level mint state (`let currentMintId = null;`, ~line 773), add:

```js
// Bulk mint UI (#215): server-flagged via /api/config so staging can test
// before prod. Qty 1 = the untouched single-mint path.
let bulkCfg = { enabled: false, max: 1 };
let mintQty = 1;

function renderQty() {
  el('qty-value').textContent = String(mintQty);
  el('qty-minus').disabled = mintQty <= 1;
  el('qty-plus').disabled = mintQty >= bulkCfg.max;
}

function setupBulkStepper(cfg) {
  bulkCfg = { enabled: !!cfg.bulk_mint_ui, max: Math.max(1, cfg.bulk_mint_max || 1) };
  if (!bulkCfg.enabled) return; // flag off: stepper stays hidden, today's UI
  el('mint-qty').hidden = false;
  el('qty-minus').onclick = () => { mintQty = Math.max(1, mintQty - 1); renderQty(); };
  el('qty-plus').onclick = () => { mintQty = Math.min(bulkCfg.max, mintQty + 1); renderQty(); };
  renderQty();
}
```

At the boot site that already fetches config (`const cfg = await api('/api/config');`, ~line 2386), add after it:

```js
    setupBulkStepper(cfg);
```

There is a second `/api/config` fetch (~line 210, Discord client_id bootstrap) — check whether the ~2386 site runs on every boot path (Discord AND Telegram AND web). If any boot path skips it, call `setupBulkStepper` after whichever config fetch every path shares instead. The stepper must appear on all three surfaces or none.

Change the `mint-btn` wiring (~line 2350) from `el('mint-btn').onclick = startMint;` to:

```js
  el('mint-btn').onclick = () => (mintQty > 1 ? startBulkMint(mintQty) : startMint());
```

Add a temporary stub so this task stands alone (Task 4 replaces it):

```js
async function startBulkMint(quantity) {
  // Replaced by the real bulk flow in the next task.
  showError(`Bulk mint (${quantity}) coming right up — not wired yet.`);
}
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest tests/test_app_js_bulk.py tests/test_app_js_boot.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add webapp/client/index.html webapp/client/app.js webapp/client/style.css tests/test_app_js_bulk.py
git commit -m "feat: flag-gated quantity stepper on mint home (#215 UI)"
```

---

### Task 4: bulk payment + fulfillment screen + resume

**Files:**
- Modify: `webapp/client/index.html` — new `bulk-panel` section after `#flow-panel`'s section
- Modify: `webapp/client/app.js` — real `startBulkMint`, `pollBulk`, `renderBulkJob`, `bulkAccept`, `cancelBulkMint`, `resumeBulkMint`; boot hook
- Modify: `webapp/client/style.css` — unit-list styles
- Test: `tests/test_app_js_bulk.py` (extend)

**Interfaces:**
- Consumes: `POST /api/mint/bulk` `{quantity}` → job dict (`id`, `state`, `quantity`, `pay_with`, `pay_amount`, `payment_link` (MAY be null while `awaiting_payment` = still preparing), `units[]` each `{index, state, nft_number, nft_id, image_url, offer_id, error}`, `minted`, `offered`); `GET /api/mint/bulk/{id}`; `POST /api/mint/bulk/{id}/cancel` (409 once paid); `GET /api/mint/bulk/active` → `{session: job|null}`; `POST /api/mint/bulk/{id}/units/{index}/accept` → `{qr, link, push}` (Task 2). Existing helpers: `showFlow`, `showPanel`, `qrUrl`, `signText`, `openExternal`, `showError`, `showMintHome`, `discordCtx`, `el`.
- Produces: complete client bulk flow; boot calls `resumeBulkMint()` before `resumeMint()`.

- [ ] **Step 1: Extend the source-assertion tests** (append to `tests/test_app_js_bulk.py`)

```python
def test_index_has_bulk_panel():
    html = _read("index.html")
    assert 'id="bulk-panel"' in html
    assert 'id="bulk-progress"' in html
    assert 'id="bulk-units"' in html
    assert 'id="bulk-done-btn"' in html


def test_app_js_bulk_flow_wiring():
    src = _read("app.js")
    assert "function pollBulk(" in src
    assert "function renderBulkJob(" in src
    assert "async function bulkAccept(" in src
    assert "async function resumeBulkMint(" in src
    assert "'/api/mint/bulk/active'" in src
    assert "/units/" in src and "/accept" in src
    # accept payloads are lazy: exactly the one endpoint call site, no
    # eager loop over units[]
    assert src.count("/accept`") == 1


def test_app_js_bulk_resume_runs_before_single_resume():
    src = _read("app.js")
    # every boot path that resumes single mint checks bulk first: the two
    # call sites use the combined guard, so the counts must match
    assert src.count("await resumeBulkMint()") == src.count("await resumeMint()")
    assert "await resumeBulkMint()) && !(await resumeMint()" in src
```

- [ ] **Step 2: Run to verify the new tests fail**

Run: `.venv/bin/python -m pytest tests/test_app_js_bulk.py -v`
Expected: the three new tests FAIL; Task 3's still PASS

- [ ] **Step 3: Implement — markup + styles**

`webapp/client/index.html`, a new sibling section after the flow-panel section:

```html
    <section id="bulk-panel" class="card sticker" hidden>
      <h2 id="bulk-title">🏗️ Bulk mint</h2>
      <p id="bulk-progress" class="card-sub"></p>
      <div id="bulk-spinner" class="spinner" role="img" aria-label="Working…" hidden></div>
      <div id="bulk-units"></div>
      <p><button id="bulk-done-btn" class="primary" hidden>Done</button></p>
    </section>
```

`webapp/client/style.css`, append:

```css
/* Bulk fulfillment unit list (#215 UI) */
#bulk-units { display: flex; flex-direction: column; gap: 8px; margin: 12px 0; }
.bulk-unit { display: flex; align-items: center; gap: 10px; padding: 8px; border-radius: 10px; }
.bulk-unit img.thumb { width: 56px; height: 56px; border-radius: 8px; object-fit: cover; }
.bulk-unit .u-label { flex: 1; text-align: left; }
.bulk-unit.pending { opacity: 0.55; }
.bulk-unit .u-error { color: var(--danger, #e66); font-size: 0.85em; }
.bulk-unit img.u-qr { width: 120px; height: 120px; }
```

(Again: check `style.css` for the real border/danger token names and match them.)

**Panel registration:** find how `showPanel(id)` knows the set of panels (~line 384) — if it iterates a hardcoded list of section ids, add `'bulk-panel'`; if it queries `section.card` generically, nothing to do. Verify by reading the function before assuming.

- [ ] **Step 4: Implement — client flow** (replace Task 3's `startBulkMint` stub; add near the single-mint flow functions)

```js
// ---- Bulk mint flow (#215 UI) ----
let currentBulkId = null;
let bulkPollTimer = null;
let bulkPollGen = 0;

function bulkPayView(j) {
  const xrp = j.pay_with === 'XRP';
  return {
    title: `💰 Pay for ${j.quantity} builds`,
    text: j.payment_link
      ? (xrp
        ? `Pay ${j.pay_amount} XRP to mint ${j.quantity} avatars — no trustline needed. Scan with Xaman, approve, and hang tight here.`
        : `Pay ${j.pay_amount} LFGO — burned on mint. One payment covers all ${j.quantity}. Scan with Xaman, approve, and hang tight here.`)
      : 'Preparing your payment request…',
    pill: j.pay_with ? { kind: xrp ? 'xrp' : 'lfgo', text: `Paying with ${xrp ? 'XRP' : 'LFGO'}` } : null,
    qrData: j.payment_link,
    link: j.payment_link,
    spinner: !j.payment_link, // payment_link may be null = still preparing (see to_dict contract)
    cancel: () => cancelBulkMint(),
  };
}

async function startBulkMint(quantity) {
  try {
    const j = await api('/api/mint/bulk', {
      method: 'POST',
      body: JSON.stringify({ ...discordCtx(), quantity }),
    });
    currentBulkId = j.id;
    showFlow(bulkPayView(j));
    pollBulk(j.id);
  } catch (e) {
    showError(e.message === 'collection_full'
      ? 'The collection is full — no room left to mint.' : e.message);
  }
}

async function cancelBulkMint() {
  if (!currentBulkId) { showMintHome(); return; }
  const btn = el('flow-cancel-btn');
  btn.disabled = true;
  try {
    await api(`/api/mint/bulk/${currentBulkId}/cancel`, {
      method: 'POST', body: JSON.stringify(discordCtx()),
    });
    clearTimeout(bulkPollTimer);
    bulkPollGen++;
    currentBulkId = null;
    showMintHome();
  } catch (e) {
    // 409 = already paid: fulfillment must run — keep polling, don't dump home.
  } finally {
    btn.disabled = false;
  }
}

function unitRow(j, u) {
  const row = document.createElement('div');
  row.className = `bulk-unit ${u.state}`;
  const label = document.createElement('span');
  label.className = 'u-label';
  if (u.state === 'pending') label.textContent = `#${u.index + 1} — waiting…`;
  else if (u.state === 'minted') label.textContent = `#${u.nft_number ?? u.index + 1} — creating offer…`;
  else if (u.state === 'failed') {
    label.innerHTML = '';
    label.textContent = `#${u.index + 1} — didn't mint. `;
    const err = document.createElement('span');
    err.className = 'u-error';
    err.textContent = 'Your payment is saved as a mint credit — nothing is lost.';
    label.appendChild(err);
  } else label.textContent = `#${u.nft_number}`;
  if (u.image_url) {
    const img = document.createElement('img');
    img.className = 'thumb';
    img.src = u.image_url;
    img.alt = `NFT #${u.nft_number}`;
    row.appendChild(img);
  }
  row.appendChild(label);
  if (u.state === 'offered' && u.offer_id) {
    const btn = document.createElement('button');
    btn.className = 'secondary';
    btn.textContent = 'Accept';
    btn.onclick = () => bulkAccept(j.id, u.index, row, btn);
    row.appendChild(btn);
  } else if (u.state === 'offered' && !u.offer_id) {
    const done = document.createElement('span');
    done.textContent = '✅ claimed';
    row.appendChild(done);
  }
  return row;
}

// Accept payloads are built ON CLICK only (XUMM open-payload cap, #260) —
// never pre-created for the whole list.
async function bulkAccept(jobId, index, row, btn) {
  btn.disabled = true;
  try {
    const r = await api(`/api/mint/bulk/${jobId}/units/${index}/accept`, {
      method: 'POST', body: JSON.stringify(discordCtx()),
    });
    let qrWrap = row.querySelector('.u-accept');
    if (!qrWrap) {
      qrWrap = document.createElement('div');
      qrWrap.className = 'u-accept';
      row.appendChild(qrWrap);
    }
    qrWrap.replaceChildren();
    const note = document.createElement('p');
    note.className = 'card-sub';
    note.textContent = signText(r.push, 'Scan to claim this one to your wallet.');
    qrWrap.appendChild(note);
    const img = document.createElement('img');
    img.className = 'u-qr';
    img.src = qrUrl(r.link);
    img.alt = 'Accept QR — scan with Xaman';
    qrWrap.appendChild(img);
    const open = document.createElement('button');
    open.className = 'link';
    open.textContent = 'Open in Xaman ↗';
    open.onclick = () => openExternal(r.link);
    qrWrap.appendChild(open);
  } catch (e) {
    showError(e.message);
  } finally {
    btn.disabled = false; // repeat click = fresh payload (old one expires in 15 min)
  }
}

function renderBulkJob(j) {
  showPanel('bulk-panel');
  const total = j.quantity;
  if (j.state === 'done') {
    el('bulk-progress').textContent = j.offered === total
      ? `All ${total} minted — accept your NFTs below. Offers never expire.`
      : `Finished: ${j.offered}/${total} ready to accept below.`;
  } else if (j.state === 'failed') {
    el('bulk-progress').textContent = j.error || 'Something went wrong.';
  } else {
    el('bulk-progress').textContent = `Minting ${Math.min(j.minted + 1, total)} / ${total}…`;
  }
  el('bulk-spinner').hidden = j.state === 'done' || j.state === 'failed';
  el('bulk-done-btn').hidden = !(j.state === 'done' || j.state === 'failed');
  const list = el('bulk-units');
  // Preserve any open accept QR across re-renders: only rebuild rows whose
  // state changed. Keyed by unit index on the row element.
  const prev = new Map([...list.children].map((n) => [n.dataset.idx, n]));
  list.replaceChildren(...j.units.map((u) => {
    const old = prev.get(String(u.index));
    if (old && old.dataset.state === u.state) return old;
    const row = unitRow(j, u);
    row.dataset.idx = String(u.index);
    row.dataset.state = u.state;
    return row;
  }));
}

function pollBulk(jobId) {
  clearTimeout(bulkPollTimer);
  const gen = ++bulkPollGen;
  const tick = async () => {
    if (gen !== bulkPollGen) return;
    let j;
    try {
      j = await api(`/api/mint/bulk/${jobId}`);
    } catch (e) {
      if (gen === bulkPollGen) bulkPollTimer = setTimeout(tick, 3000);
      return;
    }
    if (gen !== bulkPollGen) return;
    if (j.state === 'awaiting_payment') {
      showFlow(bulkPayView(j));
    } else if (j.state === 'payment_timeout') {
      showFlow({ title: '⏰ Payment timed out', text: 'No payment came through in time. Give it another go.', done: true });
      return;
    } else if (j.state === 'cancelled') {
      showMintHome();
      return;
    } else {
      renderBulkJob(j); // paid / fulfilling / done / failed
      if (j.state === 'done' || j.state === 'failed') return; // final render, stop polling
    }
    bulkPollTimer = setTimeout(tick, 3000);
  };
  bulkPollTimer = setTimeout(tick, 1000);
}

// Boot resume (#216 pattern): a live bulk job survives the Activity webview
// being killed while the user app-switches to Xaman. Checked BEFORE the
// single-mint resume — a user can't have both, and bulk is the costlier
// flow to strand. Returns true when a job resumed.
async function resumeBulkMint() {
  let active = null;
  try {
    active = await api('/api/mint/bulk/active');
  } catch (_) { return false; }
  const j = active && active.session;
  if (!j) return false;
  currentBulkId = j.id;
  if (j.state === 'awaiting_payment') showFlow(bulkPayView(j));
  else renderBulkJob(j);
  pollBulk(j.id);
  return true;
}
```

Wire the Done button where the other panel buttons are wired (~line 2350):

```js
  el('bulk-done-btn').onclick = () => { clearTimeout(bulkPollTimer); bulkPollGen++; currentBulkId = null; showMintHome(); };
```

Boot hook: at BOTH existing resume call sites (~lines 2406 and 2428, `if (!(await resumeMint())) showMintHome();`), change to:

```js
        if (!(await resumeBulkMint()) && !(await resumeMint())) showMintHome();
```

**`api()` error-shape check:** `startBulkMint` matches `e.message === 'collection_full'` — read the `api()` helper first to see what it throws for a JSON error body (`{"error": "collection_full"}`). If it surfaces the body's `error` field as `message`, the check is right; if it throws a generic HTTP message, match on that instead. Do not guess — read the helper.

- [ ] **Step 5: Run tests**

Run: `.venv/bin/python -m pytest tests/test_app_js_bulk.py tests/test_app_js_boot.py tests/test_bulk_mint_service.py -v`
Expected: all PASS

- [ ] **Step 6: Manual smoke in dev mode**

Run: `WEBAPP_DEV_MODE=1 BULK_MINT_UI_ENABLED=1 .venv/bin/python -m webapp.server` (check `lfg-services-pm2` memory / `webapp/server.py` header for the exact dev-mode launch idiom if this doesn't start), open the local client, verify: stepper visible, qty clamps at 1 and `bulk_mint_max`, qty 1 click starts the normal single mint, qty 2 click POSTs `/api/mint/bulk`. Then restart WITHOUT `BULK_MINT_UI_ENABLED` and verify no stepper renders.

- [ ] **Step 7: Commit**

```bash
git add webapp/client/index.html webapp/client/app.js webapp/client/style.css tests/test_app_js_bulk.py
git commit -m "feat: bulk mint payment + fulfillment UI with lazy per-unit accept (#215 UI)"
```

---

### Task 5: docs + staging flag line

**Files:**
- Modify: `docs/ops/env.staging.example` — add `BULK_MINT_UI_ENABLED=1`
- Modify: `CLAUDE.md` — env-var list gains one line

**Interfaces:** none (docs only).

- [ ] **Step 1: Edit `docs/ops/env.staging.example`** — next to `ECONOMY_ENABLED=1`:

```dotenv
BULK_MINT_UI_ENABLED=1                # bulk mint UI (#215) — staging-first; prod stays unset until promoted
```

- [ ] **Step 2: Edit `CLAUDE.md`** — in the Environment Variables block, after the `BULK_MINT_MAX`-related lines (search for `BULK_MINT_JOBS_DIR` mentions; the env example block is the big fenced list — add near `TELEGRAM_MINI_APP_URL`-style optional entries):

```dotenv
BULK_MINT_UI_ENABLED=0                                      # optional (#215); Activity bulk-mint stepper — off = today's UI, server endpoints stay live
```

- [ ] **Step 3: Run the full test suite once** (pre-PR gate)

Run: `.venv/bin/python -m pytest -q`
Expected: full suite green (same count as main plus the new tests)

- [ ] **Step 4: Commit**

```bash
git add docs/ops/env.staging.example CLAUDE.md
git commit -m "docs: BULK_MINT_UI_ENABLED staging flag + env docs (#215 UI)"
```

---

## Final verification

- [ ] `.venv/bin/python -m pytest -q` — green
- [ ] `ruff check . && ruff format --check .` (or just let the pre-push hook run) — clean
- [ ] Dev-mode smoke: flag off = no stepper; flag on = stepper, qty routing, bulk panel renders a mocked job
- [ ] Push branch `bulk-mint-ui`, open PR (ready, not draft — per repo convention), wait for Greptile + CodeRabbit, resolve findings before merge
