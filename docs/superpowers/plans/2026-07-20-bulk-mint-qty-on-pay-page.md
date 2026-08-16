# Bulk-mint quantity on the Mint pay page — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the bulk-mint quantity stepper off the home screen and onto the Mint pay page, where changing quantity cancels the live payload and only a highlighted Regenerate button rebuilds the QR; relabel the home action buttons.

**Architecture:** Pure client change in the vanilla-JS no-build Activity (`webapp/client/`). Core decisions (is the shown QR stale? which endpoint does the selected quantity target?) become pure functions in `mint_pure.js`, unit-tested under Node via `tests/test_mint_pure_js.py`. DOM wiring in `app.js` is thin and guarded by source-assertion tests in the same file. No server/endpoint changes — reuses `/api/mint`, `/api/mint/bulk`, and their `/cancel` + `/regenerate` routes.

**Tech Stack:** ES-module vanilla JS (`webapp/client/app.js`, `mint_pure.js`), HTML (`index.html`), CSS (`style.css`), pytest+Node test harness (`tests/test_mint_pure_js.py`).

## Global Constraints

- Feature is server-flagged: the stepper renders only when `bulkCfg.enabled` (server `/api/config` → `bulk_mint_ui` true). Flag off ⇒ pay page is byte-for-byte today's single-mint page. (verbatim from spec "Pay-page quantity control")
- No server-side changes. Reuse `POST /api/mint`, `POST /api/mint/bulk`, `POST /api/mint/{id}/cancel`, `POST /api/mint/bulk/{id}/cancel`, `POST /api/mint/{id}/regenerate`. (spec "Non-goals")
- Quantity 1 ⇒ single-mint `MintSession`; quantity >1 ⇒ bulk `BulkMintJob`. Crossing the 1↔N boundary always cancels + requires Regenerate. (spec "Why 1↔N always needs a Regenerate tap")
- Changing quantity cancels the live session/payload **immediately** (frees the XUMM payload). (spec goal 4)
- Home buttons keep their emoji icons; only the text label changes. (spec "Home-screen changes")
- The stepper never appears on swap/market flows, nor on the QR-scanned ("Approve in Xaman") mint sub-view. (spec "Pay-page quantity control")
- Respect `prefers-reduced-motion`: static highlight instead of a pulse. (spec "Styling")
- The no-build client has no bundler; new pure logic lives in `mint_pure.js` and is tested with the existing Node-under-pytest harness. Never bypass the pre-push gate.

---

### Task 1: Pure quantity-decision helpers in `mint_pure.js`

**Files:**
- Modify: `webapp/client/mint_pure.js` (append new exports at end)
- Test: `tests/test_mint_pure_js.py` (append)

**Interfaces:**
- Produces:
  - `clampQty(q, max) -> number` — integer clamped to `[1, max]`; non-finite ⇒ 1.
  - `qtyStale(selectedQty, liveQty) -> boolean` — true when `liveQty === null` (no live session) OR `selectedQty !== liveQty`.
  - `qtyMintTarget(selectedQty) -> 'single' | 'bulk'` — `'bulk'` iff `selectedQty > 1`, else `'single'`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_mint_pure_js.py`:

```python
# ---------------------------------------------------------------------------
# Bulk-mint pay-page quantity helpers (#215 UX revision)
#   clampQty(q, max)                 -> int in [1, max]
#   qtyStale(selectedQty, liveQty)   -> bool (shown QR no longer matches qty)
#   qtyMintTarget(selectedQty)       -> 'single' | 'bulk'
# ---------------------------------------------------------------------------


def test_clamp_qty_bounds():
    assert run_js("M.clampQty(1, 10)") == 1
    assert run_js("M.clampQty(0, 10)") == 1
    assert run_js("M.clampQty(-5, 10)") == 1
    assert run_js("M.clampQty(10, 10)") == 10
    assert run_js("M.clampQty(11, 10)") == 10
    assert run_js("M.clampQty(3, 10)") == 3


def test_clamp_qty_non_finite_is_one():
    assert run_js("M.clampQty(NaN, 10)") == 1
    assert run_js("M.clampQty(undefined, 10)") == 1


def test_qty_stale_no_live_session_is_stale():
    # liveQty null == no live payload backs the shown QR
    assert run_js("M.qtyStale(1, null)") is True
    assert run_js("M.qtyStale(3, null)") is True


def test_qty_stale_matching_qty_is_fresh():
    assert run_js("M.qtyStale(1, 1)") is False
    assert run_js("M.qtyStale(3, 3)") is False


def test_qty_stale_changed_qty_is_stale():
    assert run_js("M.qtyStale(3, 1)") is True
    assert run_js("M.qtyStale(1, 3)") is True


def test_qty_mint_target():
    assert run_js("M.qtyMintTarget(1)") == "single"
    assert run_js("M.qtyMintTarget(2)") == "bulk"
    assert run_js("M.qtyMintTarget(10)") == "bulk"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd ~/LFG-wt/bulk-mint-qty && .venv/bin/python -m pytest tests/test_mint_pure_js.py -k "qty or clamp" -v`
Expected: FAIL — `node script failed` / `M.clampQty is not a function`.

- [ ] **Step 3: Implement the helpers**

Append to `webapp/client/mint_pure.js`:

```javascript
// --- Bulk-mint pay-page quantity helpers (#215 UX revision) ---------------
// The quantity stepper lives on the pay page. Two questions drive the UI and
// keep the DOM wiring dumb: does the currently-shown QR still match the
// selected quantity, and which endpoint does that quantity target?

// Clamp a stepper value to the allowed range. Non-finite -> 1 (defensive:
// never let a bad value unhide/enable a control out of range).
export function clampQty(q, max) {
  const n = Math.trunc(Number(q));
  if (!Number.isFinite(n)) return 1;
  return Math.min(Math.max(n, 1), Math.max(1, Math.trunc(Number(max)) || 1));
}

// Is the shown QR stale relative to the selected quantity? liveQty is the
// quantity the live session/job was created for, or null when no live payload
// backs the screen (e.g. just after a qty change cancelled it). A stale QR is
// dimmed and Regenerate is highlighted.
export function qtyStale(selectedQty, liveQty) {
  return liveQty === null || selectedQty !== liveQty;
}

// Which mint endpoint does the selected quantity commit to? 1 = single-mint
// session; >1 = bulk job (deliberately separate paths, #215).
export function qtyMintTarget(selectedQty) {
  return selectedQty > 1 ? 'bulk' : 'single';
}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd ~/LFG-wt/bulk-mint-qty && .venv/bin/python -m pytest tests/test_mint_pure_js.py -k "qty or clamp" -v`
Expected: PASS (all new tests green).

- [ ] **Step 5: Commit**

```bash
cd ~/LFG-wt/bulk-mint-qty
git add webapp/client/mint_pure.js tests/test_mint_pure_js.py
git commit -m "feat(mint): pure quantity helpers for pay-page bulk stepper (#215)"
```

---

### Task 2: Home-screen markup — remove stepper, relabel buttons

**Files:**
- Modify: `webapp/client/index.html` (mint-panel, lines ~52–62)
- Modify: `webapp/client/index.html` (flow-panel, add `#flow-qty` block near line ~233)
- Test: `tests/test_mint_pure_js.py` (append markup assertions)

**Interfaces:**
- Produces (DOM contract consumed by Task 3/4):
  - Home `.mint-row` contains only `#mint-btn` (no `#mint-qty`).
  - Home buttons text: `#mint-btn` → `⛏️ Mint`, `#swap-btn` → `🏗️ Build`, `#swapper-btn` → `🔁 Swap`, `#market-btn` → `🛒 Trade`.
  - Flow panel contains a hidden stepper `#flow-qty` with children `#flow-qty-minus`, `#flow-qty-value`, `#flow-qty-plus`.

- [ ] **Step 1: Write the failing markup tests**

Append to `tests/test_mint_pure_js.py`:

```python
# ---------------------------------------------------------------------------
# #215 UX revision: home stepper removed + buttons relabelled; the quantity
# stepper markup now lives in the flow (pay) panel.
# ---------------------------------------------------------------------------

INDEX_HTML = os.path.join(ROOT, "webapp", "client", "index.html")


def test_home_has_no_qty_stepper():
    html = open(INDEX_HTML).read()
    mint_panel = html.split('id="mint-panel"', 1)[1].split("</section>", 1)[0]
    assert 'id="mint-qty"' not in mint_panel  # stepper moved off the home screen


def test_home_buttons_relabelled():
    html = open(INDEX_HTML).read()
    assert 'id="mint-btn" class="primary big">⛏️ Mint<' in html
    assert 'id="swap-btn" class="secondary">🏗️ Build<' in html
    assert 'id="swapper-btn" class="secondary">🔁 Swap<' in html
    assert 'id="market-btn" class="secondary">🛒 Trade<' in html


def test_flow_panel_has_qty_stepper():
    html = open(INDEX_HTML).read()
    flow_panel = html.split('id="flow-panel"', 1)[1].split("</section>", 1)[0]
    assert 'id="flow-qty"' in flow_panel
    assert 'id="flow-qty-minus"' in flow_panel
    assert 'id="flow-qty-value"' in flow_panel
    assert 'id="flow-qty-plus"' in flow_panel
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd ~/LFG-wt/bulk-mint-qty && .venv/bin/python -m pytest tests/test_mint_pure_js.py -k "home or flow_panel_has_qty" -v`
Expected: FAIL (`id="mint-qty"` still in mint-panel; new labels/ids absent).

- [ ] **Step 3: Edit the home mint-panel**

In `webapp/client/index.html`, replace the mint-row + button block (currently lines ~52–62):

```html
        <div class="mint-row">
          <div id="mint-qty" class="qty-stepper" hidden>
            <button id="qty-minus" class="qty-btn" aria-label="Fewer">−</button>
            <span id="qty-value" aria-live="polite">1</span>
            <button id="qty-plus" class="qty-btn" aria-label="More">+</button>
          </div>
          <button id="mint-btn" class="primary big">⛏️ Mint NFT</button>
        </div>
        <button id="swap-btn" class="secondary">🏗️ Build</button>
        <button id="swapper-btn" class="secondary">🔁 Trait Swapper</button>
        <button id="market-btn" class="secondary">🛒 Marketplace</button>
```

with (stepper gone, labels changed, `.mint-row` kept for layout):

```html
        <div class="mint-row">
          <button id="mint-btn" class="primary big">⛏️ Mint</button>
        </div>
        <button id="swap-btn" class="secondary">🏗️ Build</button>
        <button id="swapper-btn" class="secondary">🔁 Swap</button>
        <button id="market-btn" class="secondary">🛒 Trade</button>
```

- [ ] **Step 4: Add the stepper to the flow panel**

In `webapp/client/index.html`, inside `#flow-panel` → `.flow-copy`, add the stepper immediately **above** the `#flow-regen-btn` line (currently line ~233):

```html
          <div id="flow-qty" class="qty-stepper" hidden>
            <span class="qty-lbl">Quantity</span>
            <button id="flow-qty-minus" class="qty-btn" aria-label="Fewer">−</button>
            <span id="flow-qty-value" aria-live="polite">1</span>
            <button id="flow-qty-plus" class="qty-btn" aria-label="More">+</button>
          </div>
          <p><button id="flow-regen-btn" class="link" hidden>↻ Regenerate QR</button></p>
```

(The `<p><button id="flow-regen-btn" ...></p>` line already exists — add only the `<div id="flow-qty">` block above it.)

- [ ] **Step 5: Run to verify they pass**

Run: `cd ~/LFG-wt/bulk-mint-qty && .venv/bin/python -m pytest tests/test_mint_pure_js.py -k "home or flow_panel_has_qty" -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
cd ~/LFG-wt/bulk-mint-qty
git add webapp/client/index.html tests/test_mint_pure_js.py
git commit -m "feat(mint): move qty stepper to pay page, relabel home buttons (#215)"
```

---

### Task 3: Styling — stale-QR dim + Regenerate highlight

**Files:**
- Modify: `webapp/client/style.css` (append)

**Interfaces:**
- Consumes: DOM ids `#flow-qr`, `#flow-regen-btn`, `#flow-qty` from Task 2.
- Produces: CSS classes `.qr-stale` (on `#flow-qr`) and `.needs-regen` (on `#flow-regen-btn`) that Task 4 toggles; visible styling for `#flow-qty` on the pay page.

- [ ] **Step 1: Confirm the existing stepper style exists (reuse, don't duplicate)**

Run: `cd ~/LFG-wt/bulk-mint-qty && grep -n "qty-stepper\|qty-btn" webapp/client/style.css`
Expected: existing `.qty-stepper` / `.qty-btn` rules (from #272) are present — the moved stepper reuses them, so no new base-stepper CSS is needed. If absent, add minimal flex rules in Step 2.

- [ ] **Step 2: Append the stale/highlight styles**

Append to `webapp/client/style.css`:

```css
/* #215 UX revision: pay-page quantity stepper + stale-QR affordance.
   Changing quantity cancels the live payload; the shown QR dims and the
   Regenerate button pulses until the user rebuilds it. */
#flow-qty {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  margin: 0.25rem 0 0.5rem;
}
#flow-qty .qty-lbl {
  font-size: 0.85rem;
  opacity: 0.8;
}

/* Stale QR: the shown code no longer matches the selected quantity. */
#flow-qr.qr-stale {
  opacity: 0.35;
  filter: blur(2px) grayscale(0.4);
  transition: opacity 0.15s ease, filter 0.15s ease;
}

/* Regenerate needs attention while the QR is stale. */
#flow-regen-btn.needs-regen {
  animation: regen-pulse 1.1s ease-in-out infinite;
  font-weight: 700;
}
@keyframes regen-pulse {
  0%, 100% { opacity: 1; text-shadow: 0 0 0 transparent; }
  50%      { opacity: 0.65; text-shadow: 0 0 8px currentColor; }
}
@media (prefers-reduced-motion: reduce) {
  #flow-regen-btn.needs-regen { animation: none; text-decoration: underline; }
  #flow-qr.qr-stale { transition: none; }
}
```

- [ ] **Step 3: Sanity-check the CSS parses (no test framework for CSS — visual/lint only)**

Run: `cd ~/LFG-wt/bulk-mint-qty && grep -c "needs-regen\|qr-stale" webapp/client/style.css`
Expected: `>= 3` (rules present).

- [ ] **Step 4: Commit**

```bash
cd ~/LFG-wt/bulk-mint-qty
git add webapp/client/style.css
git commit -m "style(mint): pay-page qty stepper + stale-QR/regenerate highlight (#215)"
```

---

### Task 4: Wire the pay-page stepper into `app.js`

**Files:**
- Modify: `webapp/client/app.js` (showFlow, mintPayView, bulkPayView, startMint, startBulkMint, resumeMint, resumeBulkMint, setupBulkStepper, onFlowRegen, cancelLiveMintSilently, onQtyChange, init wiring)
- Test: `tests/test_mint_pure_js.py` (append app.js wiring assertions)

**Interfaces:**
- Consumes: `clampQty`, `qtyStale`, `qtyMintTarget` (Task 1); DOM ids `#flow-qty*`, `.qr-stale`, `.needs-regen` (Tasks 2–3); existing `showFlow`, `pollMint` (`pollTimer`/`pollGen`), `pollBulk` (`bulkPollTimer`/`bulkPollGen`), `mintPayView`, `bulkPayView`, `startMint`, `startBulkMint`, `cancelMint`, `cancelBulkMint`, `regeneratePaymentQr`.
- Produces: module state `mintQty` (selected, reused), `liveQty` (new; 1 for single, `job.quantity` for bulk, `null` when no live payload); `showFlow` gains a `qtyStepper` boolean option; `onFlowRegen`, `onQtyChange(delta)`, `cancelLiveMintSilently()`.

- [ ] **Step 1: Write the failing wiring tests**

Append to `tests/test_mint_pure_js.py`:

```python
# ---------------------------------------------------------------------------
# #215 UX revision: app.js wiring for the pay-page quantity stepper.
# Source-assertion style (same as the boot/cancel wiring tests above).
# ---------------------------------------------------------------------------


def test_app_js_mint_btn_always_single():
    """Home Mint button no longer branches on qty — it always starts a single
    mint; quantity is chosen on the pay page now."""
    src = open(APP_JS).read()
    assert "mintQty > 1 ? startBulkMint" not in src
    assert "el('mint-btn').onclick = () => startMint();" in src


def test_app_js_regen_uses_qty_aware_handler():
    src = open(APP_JS).read()
    assert "el('flow-regen-btn').onclick = onFlowRegen;" in src


def test_app_js_regen_routes_by_qty_target():
    """onFlowRegen commits the selected quantity to the right endpoint."""
    src = open(APP_JS).read()
    body = src.split("async function onFlowRegen", 1)[1].split("\n}\n", 1)[0]
    assert "qtyMintTarget" in body
    assert "startBulkMint" in body
    assert "regeneratePaymentQr" in body  # same-qty expired refresh keeps the session


def test_app_js_qty_change_cancels_live_payload():
    """Changing quantity must cancel the live session server-side immediately."""
    src = open(APP_JS).read()
    body = src.split("function onQtyChange", 1)[1].split("\n}\n", 1)[0]
    assert "cancelLiveMintSilently" in body
    assert "needs-regen" in body  # highlights regenerate
    assert "qr-stale" in body     # dims the QR


def test_app_js_cancel_silent_hits_both_endpoints_and_stops_polls():
    src = open(APP_JS).read()
    body = src.split("async function cancelLiveMintSilently", 1)[1].split("\n}\n", 1)[0]
    assert "/api/mint/" in body and "/cancel" in body
    assert "/api/mint/bulk/" in body
    assert "++pollGen" in body        # stop the single-mint poll chain
    assert "++bulkPollGen" in body    # stop the bulk poll chain
    assert "liveQty = null" in body


def test_app_js_showflow_renders_qty_stepper_flag():
    src = open(APP_JS).read()
    body = src.split("function showFlow(", 1)[1].split("\n}\n", 1)[0]
    assert "qtyStepper" in body
    assert "el('flow-qty').hidden" in body
    # a fresh render clears any leftover stale visuals
    assert "classList.remove('qr-stale')" in body
    assert "classList.remove('needs-regen')" in body


def test_app_js_pay_views_request_stepper():
    """Both the single and bulk pay views opt into the stepper."""
    src = open(APP_JS).read()
    mint_body = src.split("function mintPayView", 1)[1].split("\n}\n", 1)[0]
    bulk_body = src.split("function bulkPayView", 1)[1].split("\n}\n", 1)[0]
    assert "qtyStepper: true" in mint_body
    assert "qtyStepper: true" in bulk_body


def test_app_js_start_paths_set_live_qty():
    src = open(APP_JS).read()
    single = src.split("async function startMint", 1)[1].split("\n}\n", 1)[0]
    bulk = src.split("async function startBulkMint", 1)[1].split("\n}\n", 1)[0]
    assert "liveQty = 1" in single
    assert "liveQty = quantity" in bulk
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd ~/LFG-wt/bulk-mint-qty && .venv/bin/python -m pytest tests/test_mint_pure_js.py -k "app_js and (qty or regen or silent or showflow or pay_views or start_paths or mint_btn)" -v`
Expected: FAIL (handlers/state not present yet).

- [ ] **Step 3: Import the new pure helpers**

In `webapp/client/app.js`, the top already has `import * as mintPure from './mint_pure.js';` (line ~15). No new import line needed — reference `mintPure.clampQty`, `mintPure.qtyStale`, `mintPure.qtyMintTarget`.

- [ ] **Step 4: Add `liveQty` state and repurpose the stepper wiring**

Replace the block at lines ~788–804 (`let bulkCfg …` through the end of `setupBulkStepper`):

```javascript
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

with (stepper now on the pay page; `liveQty` tracks the live payload's quantity):

```javascript
// Bulk mint UI (#215, pay-page revision): server-flagged via /api/config so
// staging can test before prod. Quantity is chosen on the PAY page now, not
// the home screen. Qty 1 = the untouched single-mint path.
let bulkCfg = { enabled: false, max: 1 };
let mintQty = 1;              // selected quantity on the pay-page stepper
let liveQty = null;           // quantity the live session/job was built for; null = none

function renderFlowQty() {
  el('flow-qty-value').textContent = String(mintQty);
  el('flow-qty-minus').disabled = mintQty <= 1;
  el('flow-qty-plus').disabled = mintQty >= bulkCfg.max;
}

function setupBulkStepper(cfg) {
  bulkCfg = { enabled: !!cfg.bulk_mint_ui, max: Math.max(1, cfg.bulk_mint_max || 1) };
  if (!bulkCfg.enabled) return; // flag off: stepper never renders, today's UI
  el('flow-qty-minus').onclick = () => onQtyChange(-1);
  el('flow-qty-plus').onclick = () => onQtyChange(1);
}

// Pay-page stepper press. Changing quantity invalidates the shown QR: cancel
// the live payload immediately (frees the XUMM slot), dim the QR, and pulse
// Regenerate — a new QR is built only when the user taps it.
function onQtyChange(delta) {
  const next = mintPure.clampQty(mintQty + delta, bulkCfg.max);
  if (next === mintQty) return;
  mintQty = next;
  renderFlowQty();
  if (mintPure.qtyStale(mintQty, liveQty)) {
    cancelLiveMintSilently(); // fire-and-forget: cancel whatever is live
    el('flow-qr').classList.add('qr-stale');
    el('flow-link-btn').hidden = true;               // no accept while stale
    el('flow-regen-btn').hidden = false;
    el('flow-regen-btn').classList.add('needs-regen');
  }
}

// Cancel whichever mint payload is live without navigating home (used when a
// qty change supersedes it). Stops both poll chains and clears liveQty.
async function cancelLiveMintSilently() {
  const singleId = currentMintId;
  const bulkId = currentBulkId;
  currentMintId = null;
  currentBulkId = null;
  liveQty = null;
  clearTimeout(pollTimer); pollGen++;             // stop single-mint poll
  clearTimeout(bulkPollTimer); bulkPollGen++;     // stop bulk poll
  if (singleId) {
    try {
      await api(`/api/mint/${singleId}/cancel`, {
        method: 'POST', body: JSON.stringify(discordCtx()),
      });
    } catch (_) { /* 409 already-paid etc.: superseded anyway, ignore */ }
  }
  if (bulkId) {
    try {
      await api(`/api/mint/bulk/${bulkId}/cancel`, {
        method: 'POST', body: JSON.stringify(discordCtx()),
      });
    } catch (_) { /* ignore */ }
  }
}

// Regenerate = the commit gate. Same quantity + a live single session that
// merely expired -> refresh that session's payload (keeps its state). Any qty
// change (liveQty null) -> build a fresh session on the endpoint the selected
// quantity targets.
async function onFlowRegen() {
  if (!mintPure.qtyStale(mintQty, liveQty) && liveQty === 1 && currentMintId) {
    return regeneratePaymentQr(); // classic same-session expired-QR refresh
  }
  if (mintPure.qtyMintTarget(mintQty) === 'bulk') return startBulkMint(mintQty);
  return startMint();
}
```

- [ ] **Step 5: Render the stepper in `showFlow`**

In `webapp/client/app.js`, add `qtyStepper` to the `showFlow` destructured options (line ~631) and render it. Change the signature line:

```javascript
function showFlow({ title, text, qrData, link, image, video, done, stage, spinner, celebrate, pill, regen, cancel, share }) {
```

to:

```javascript
function showFlow({ title, text, qrData, link, image, video, done, stage, spinner, celebrate, pill, regen, cancel, share, qtyStepper }) {
```

Then, immediately after the `el('flow-regen-btn').hidden = !regen;` line (line ~654), insert:

```javascript
  // #215: pay-page quantity stepper. Only mint pay views pass qtyStepper, and
  // only when the server flag is on. A fresh render is never stale — clear the
  // dim/pulse a prior qty change may have left on the reused elements.
  const showQty = !!qtyStepper && bulkCfg.enabled;
  el('flow-qty').hidden = !showQty;
  if (showQty) renderFlowQty();
  el('flow-qr').classList.remove('qr-stale');
  el('flow-regen-btn').classList.remove('needs-regen');
```

- [ ] **Step 6: Opt the pay views into the stepper**

In `mintPayView` (line ~693, the **unscanned** return — NOT the `qr_scanned` spinner branch), add `qtyStepper: true`:

```javascript
  return {
    title: '💰 Pay to build',
    text: signText(s.payment_push, xrp
      ? `Pay ${s.pay_amount} XRP to mint your avatar — no trustline needed. Scan with Xaman, approve, and hang tight here.`
      : `Pay ${s.pay_amount || 1} LFGO — burned on mint. Scan with Xaman, approve, and hang tight here.`),
    pill,
    qrData: s.payment_link,
    link: s.payment_link,
    stage: s.state,
    regen: true,
    qtyStepper: true,
    // Unscanned QR: nothing can be signed yet — cancel without the warning.
    cancel: () => cancelMint(false),
  };
```

In `bulkPayView` (line ~811), add `qtyStepper: true` and `regen: true` to its returned object (bulk currently has no regen affordance; the stepper needs it):

```javascript
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
    regen: true,
    qtyStepper: true,
    spinner: !j.payment_link, // payment_link may be null = still preparing (see to_dict contract)
    cancel: () => cancelBulkMint(),
  };
```

- [ ] **Step 7: Set `liveQty` / reset `mintQty` in the start + resume paths**

In `startMint` (line ~1016), set `mintQty` and `liveQty` when the single session is created:

```javascript
async function startMint() {
  try {
    const s = await api('/api/mint', { method: 'POST', body: JSON.stringify(discordCtx()) });
    currentMintId = s.id;
    mintQty = 1;
    liveQty = 1;
    showFlow(mintPayView(s));
    pollMint(s.id);
  } catch (e) {
    showError(e.message);
  }
}
```

In `startBulkMint` (line ~828), set `liveQty` to the job quantity:

```javascript
async function startBulkMint(quantity) {
  try {
    const j = await api('/api/mint/bulk', {
      method: 'POST',
      body: JSON.stringify({ ...discordCtx(), quantity }),
    });
    currentBulkId = j.id;
    mintQty = quantity;
    liveQty = quantity;
    showFlow(bulkPayView(j));
    pollBulk(j.id);
  } catch (e) {
    showError(e.message === 'collection_full'
      ? 'The collection is full — no room left to mint.' : e.message);
  }
```

In `resumeMint` (line ~1032), initialise the stepper to 1 after `currentMintId = id;`:

```javascript
  currentMintId = id;
  mintQty = 1;
  liveQty = 1;
```

In `resumeBulkMint` (line ~1002), after `currentBulkId = j.id;`, mirror the job quantity:

```javascript
  currentBulkId = j.id;
  mintQty = j.quantity;
  liveQty = j.quantity;
```

- [ ] **Step 8: Update the init wiring**

In `webapp/client/app.js` init (lines ~2900–2901), change:

```javascript
  el('mint-btn').onclick = () => (mintQty > 1 ? startBulkMint(mintQty) : startMint());
  el('flow-regen-btn').onclick = regeneratePaymentQr;
```

to:

```javascript
  el('mint-btn').onclick = () => startMint();
  el('flow-regen-btn').onclick = onFlowRegen;
```

- [ ] **Step 9: Run the wiring tests to verify they pass**

Run: `cd ~/LFG-wt/bulk-mint-qty && .venv/bin/python -m pytest tests/test_mint_pure_js.py -v`
Expected: PASS (all new + pre-existing tests in the file green).

- [ ] **Step 10: Commit**

```bash
cd ~/LFG-wt/bulk-mint-qty
git add webapp/client/app.js tests/test_mint_pure_js.py
git commit -m "feat(mint): pay-page qty stepper wiring — cancel-on-change + regenerate gate (#215)"
```

---

### Task 5: Regression sweep + boot-wiring guard

**Files:**
- Test: `tests/test_mint_pure_js.py` (append one boot guard)
- Verify only: full webapp/client test surface

**Interfaces:**
- Consumes: everything from Tasks 1–4.

- [ ] **Step 1: Add a flag-off regression assertion**

Append to `tests/test_mint_pure_js.py`:

```python
def test_setup_bulk_stepper_noops_when_flag_off():
    """Flag off: setupBulkStepper returns before wiring any stepper handler, so
    the pay page is exactly today's single-mint page."""
    src = open(APP_JS).read()
    body = src.split("function setupBulkStepper", 1)[1].split("\n}\n", 1)[0]
    # The early return guards all stepper wiring.
    ret_idx = body.index("if (!bulkCfg.enabled) return;")
    wire_idx = body.index("flow-qty-minus")
    assert ret_idx < wire_idx
```

- [ ] **Step 2: Run the whole mint-pure test file**

Run: `cd ~/LFG-wt/bulk-mint-qty && .venv/bin/python -m pytest tests/test_mint_pure_js.py -v`
Expected: PASS (all).

- [ ] **Step 3: Run the webapp test suite (no regressions)**

Run: `cd ~/LFG-wt/bulk-mint-qty && .venv/bin/python -m pytest webapp/ tests/test_mint_pure_js.py tests/test_build_pure_js.py -q`
Expected: PASS (existing smoke/mock tests unaffected — this change is client-only DOM/JS).

- [ ] **Step 4: Full pre-push gate dry run**

Run: `cd ~/LFG-wt/bulk-mint-qty && .venv/bin/python -m pytest -q`
Expected: PASS. (ruff/mypy/gitleaks run at push time; this change touches only JS/HTML/CSS + one Python test file, so mypy/ruff scope is the test file — keep it clean.)

- [ ] **Step 5: Commit (if Step 1 added anything uncommitted)**

```bash
cd ~/LFG-wt/bulk-mint-qty
git add tests/test_mint_pure_js.py
git commit -m "test(mint): guard flag-off no-op for pay-page stepper (#215)"
```

---

## Self-Review

**Spec coverage:**
- Home reverted / no stepper — Task 2 (`test_home_has_no_qty_stepper`). ✅
- Quantity stepper on pay page — Tasks 2 (markup), 4 (render/wiring). ✅
- Initial QR is qty-1 single mint — Task 4 Step 7 (`startMint` sets `liveQty=1`), `mint-btn` → `startMint` (Task 4 Step 8). ✅
- Qty change cancels payload immediately + dims QR + highlights regen — Task 4 Step 4 (`onQtyChange`, `cancelLiveMintSilently`). ✅
- Regenerate is the commit gate, qty-aware — Task 4 Step 4 (`onFlowRegen`), Task 1 (`qtyMintTarget`). ✅
- Same-qty expired refresh preserved — `onFlowRegen` early-returns `regeneratePaymentQr()`. ✅
- Button relabels — Task 2 (`test_home_buttons_relabelled`). ✅
- Stepper hidden on swap/market + qr_scanned sub-view — only `mintPayView` (unscanned) / `bulkPayView` pass `qtyStepper` (Task 4 Step 6); swap/market never do. ✅
- Flag-off = today's UI — Task 5 (`test_setup_bulk_stepper_noops_when_flag_off`), `showFlow` gates on `bulkCfg.enabled`. ✅
- prefers-reduced-motion — Task 3 CSS. ✅
- Resume reflects in-flight qty — Task 4 Step 7 (resume paths). ✅

**Placeholder scan:** No TBD/TODO/"handle edge cases"; every code step shows full code. ✅

**Type consistency:** `clampQty`/`qtyStale`/`qtyMintTarget` signatures match between Task 1 definition and Task 4 usage. State names `mintQty`/`liveQty`, DOM ids `flow-qty`/`flow-qty-minus`/`flow-qty-value`/`flow-qty-plus`, classes `qr-stale`/`needs-regen` consistent across Tasks 2–4. Poll vars `pollTimer`/`pollGen` (single) and `bulkPollTimer`/`bulkPollGen` (bulk) match existing `app.js`. ✅
