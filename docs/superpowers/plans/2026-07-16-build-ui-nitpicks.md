# Build UI Nitpicks Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Five UX fixes to the Discord Activity's Dressing Room panel: rename the entry button to "🏗️ Build", add a back button, stop default-landing on unindexed GOs, replace the bottom roster strip with a labeled GO-picker overlay, and hide Closet tiles whose art can't render on the selected GO.

**Architecture:** All changes live in `webapp/client/` (no-build vanilla JS). Decision logic that can be unit-tested goes in a new pure ES module `webapp/client/build_pure.js` (same pattern as `mint_pure.js` / `market_pure.js`, tested under Node via pytest). DOM wiring stays in `app.js`.

**Tech Stack:** Vanilla JS (ES modules, no build step), pytest + Node harness for pure modules.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-07-16-build-ui-nitpicks-design.md`
- No service/API changes; `webapp/client/` only (plus tests).
- Entry button copy is exactly `🏗️ Build`.
- Back button copy is exactly `← Back to the job site` (matches the swap panel).
- Pre-push gate (ruff/ruff-format/mypy/gitleaks/pytest/validate-trait-config) must stay green; never `--no-verify`.
- Discord caches `app.js` — final manual verification requires a full Activity relaunch.

---

### Task 1: Pure helpers — `pickDefaultCharacter` and `goTileState`

**Files:**
- Create: `webapp/client/build_pure.js`
- Test: `tests/test_build_pure_js.py`

**Interfaces:**
- Consumes: nothing (pure module).
- Produces:
  - `pickDefaultCharacter(characters) -> string | null` — nft_id of the first character with a truthy `body`, else the first character's nft_id, else `null` for an empty/missing list.
  - `goTileState(char, activeNftId) -> {label: string, sub: string, state: 'active'|'selectable'|'indexing'}` — label is `#<edition>` (`#?` when edition is null/undefined); sub is the body name or `indexing…`; state is `active` when `char.nft_id === activeNftId`, else `indexing` when body is falsy, else `selectable`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_build_pure_js.py`:

```python
# tests/test_build_pure_js.py
# Build (Dressing Room) panel decision logic, kept in the pure module
# webapp/client/build_pure.js and executed here under Node — same harness as
# tests/test_mint_pure_js.py / tests/test_market_pure_js.py.
#
# No lfg_core import at module top -> no env-guard preamble needed.
import json
import os
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULE_REL = "./webapp/client/build_pure.js"

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed on this host")


def run_js(expr: str):
    """Run `expr` (a JS expression referencing the imported module as `M`)
    inside a small Node ES-module script, executed with cwd=ROOT so the
    relative import resolves; returns the JSON-decoded result."""
    script = (
        f"import * as M from {json.dumps(MODULE_REL)};\n"
        f"const result = ({expr});\n"
        f"console.log(JSON.stringify(result === undefined ? null : result));\n"
    )
    proc = subprocess.run(
        [NODE, "--input-type=module"],
        input=script,
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=15,
    )
    assert proc.returncode == 0, f"node script failed:\n{script}\n--- stderr ---\n{proc.stderr}"
    return json.loads(proc.stdout)


# --- pickDefaultCharacter ---------------------------------------------------
# The Build panel must never default-land on an unindexed token ("#null ·
# still indexing…"): prefer the first character whose metadata has a body.


def test_default_skips_unindexed_leading_character():
    chars = (
        "[{nft_id: 'A', body: ''}, {nft_id: 'B', body: 'male'}, {nft_id: 'C', body: 'ape'}]"
    )
    assert run_js(f"M.pickDefaultCharacter({chars})") == "B"


def test_default_keeps_first_when_it_is_indexed():
    chars = "[{nft_id: 'A', body: 'milady'}, {nft_id: 'B', body: ''}]"
    assert run_js(f"M.pickDefaultCharacter({chars})") == "A"


def test_default_falls_back_to_first_when_none_indexed():
    chars = "[{nft_id: 'A', body: ''}, {nft_id: 'B', body: null}]"
    assert run_js(f"M.pickDefaultCharacter({chars})") == "A"


def test_default_empty_roster_is_null():
    assert run_js("M.pickDefaultCharacter([])") is None
    assert run_js("M.pickDefaultCharacter(null)") is None


# --- goTileState -------------------------------------------------------------


def test_tile_active():
    out = run_js("M.goTileState({nft_id: 'A', edition: 3521, body: 'male'}, 'A')")
    assert out == {"label": "#3521", "sub": "male", "state": "active"}


def test_tile_selectable():
    out = run_js("M.goTileState({nft_id: 'B', edition: 398, body: 'ape'}, 'A')")
    assert out == {"label": "#398", "sub": "ape", "state": "selectable"}


def test_tile_unindexed_is_disabled_and_labeled():
    out = run_js("M.goTileState({nft_id: 'C', edition: null, body: ''}, 'A')")
    assert out == {"label": "#?", "sub": "indexing…", "state": "indexing"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_build_pure_js.py -v`
Expected: FAIL (every test) — node script fails with `Cannot find module ... build_pure.js`.

- [ ] **Step 3: Write the implementation**

Create `webapp/client/build_pure.js`:

```js
// webapp/client/build_pure.js
// Pure decision logic for the Build (Dressing Room) panel, kept free of
// DOM/network code so it can be executed and unit-tested under Node
// (tests/test_build_pure_js.py) — same split as mint_pure.js/market_pure.js.

// Default GO selection: never land on an unindexed token ("#null · still
// indexing…"). Prefer the first character whose metadata has a body; fall
// back to the first character only when none are indexed yet.
export function pickDefaultCharacter(characters) {
  if (!characters || !characters.length) return null;
  const indexed = characters.find((c) => Boolean(c.body));
  return (indexed || characters[0]).nft_id;
}

// Presentation state for one GO-picker tile.
//   label — '#<edition>' ('#?' while the edition is unknown/unindexed)
//   sub   — body name, or 'indexing…' while metadata is incomplete
//   state — 'active' (currently selected) | 'selectable' | 'indexing'
//           (disabled: no body means every layer fetch would 400)
export function goTileState(char, activeNftId) {
  const indexed = Boolean(char.body);
  return {
    label: `#${char.edition == null ? '?' : char.edition}`,
    sub: indexed ? char.body : 'indexing…',
    state: char.nft_id === activeNftId ? 'active' : indexed ? 'selectable' : 'indexing',
  };
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_build_pure_js.py -v`
Expected: 7 passed (or skipped if node missing — it isn't on this host).

- [ ] **Step 5: Commit**

```bash
git add webapp/client/build_pure.js tests/test_build_pure_js.py
git commit -m "feat(activity): pure Build-panel helpers (default GO pick, picker tile state)"
```

---

### Task 2: Rename entry button + Build-panel back button

**Files:**
- Modify: `webapp/client/index.html` (line 34 button; `dressup-panel` section ~line 94)
- Modify: `webapp/client/app.js` (init/wiring section, near the existing `el('swap-btn').onclick` at ~line 2139)

**Interfaces:**
- Consumes: existing `showMintHome()` (app.js ~line 190).
- Produces: DOM ids `dressup-back-btn` (no JS API for later tasks).

- [ ] **Step 1: Rename the entry button**

In `webapp/client/index.html` line 34, change:

```html
<button id="swap-btn" class="secondary">👗 Dress Up</button>
```

to:

```html
<button id="swap-btn" class="secondary">🏗️ Build</button>
```

(The `swap-btn` id stays — it is referenced from app.js and renaming it buys nothing.)

- [ ] **Step 2: Add the back button to the Build panel**

In `webapp/client/index.html`, inside `<section id="dressup-panel" ...>`, add as the FIRST child (before `closet-gate`):

```html
      <p><button id="dressup-back-btn" class="back">← Back to the job site</button></p>
```

- [ ] **Step 3: Wire the back button**

In `webapp/client/app.js`, next to the existing `el('swap-btn').onclick = () => openDressup();` (~line 2139), add:

```js
  el('dressup-back-btn').onclick = () => showMintHome();
```

- [ ] **Step 4: Verify no stray user-visible "Dress Up" copy remains**

Run: `grep -rn -i "dress up" webapp/client/index.html webapp/client/app.js`
Expected: no user-visible button/heading copy (code comments like "hide the Dress Up entry point" may remain; update the comment wording to "Build" while there).

- [ ] **Step 5: Run the webapp test suite for regressions**

Run: `.venv/bin/python -m pytest tests/test_market_panel_dom.py webapp/ -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add webapp/client/index.html webapp/client/app.js
git commit -m "feat(activity): rename Dress Up to Build; add back button to the Build panel"
```

---

### Task 3: Default GO selection uses `pickDefaultCharacter`

**Files:**
- Modify: `webapp/client/app.js` (`openDressup` ~line 1277; `harvestActive` ~line 1520; imports ~line 15)

**Interfaces:**
- Consumes: `pickDefaultCharacter(characters)` from Task 1.
- Produces: module import alias `buildPure` used by Task 4.

- [ ] **Step 1: Import the pure module**

In `webapp/client/app.js`, after the `mintPure` import (~line 15), add:

```js
// Build-panel decision logic lives in its own pure module so it's
// Node-testable too (tests/test_build_pure_js.py).
import * as buildPure from './build_pure.js';
```

- [ ] **Step 2: Replace both blind `characters[0]` picks**

In `openDressup` (~line 1277), change:

```js
    activeNftId = economyState.characters[0] ? economyState.characters[0].nft_id : null;
```

to:

```js
    activeNftId = buildPure.pickDefaultCharacter(economyState.characters);
```

In `harvestActive` (~line 1520), make the identical replacement:

```js
    activeNftId = buildPure.pickDefaultCharacter(economyState.characters);
```

- [ ] **Step 3: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_build_pure_js.py tests/test_market_panel_dom.py webapp/ -q`
Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add webapp/client/app.js
git commit -m "fix(activity): Build panel defaults to the first indexed GO, not #null"
```

---

### Task 4: GO picker overlay replaces the bottom roster strip

**Files:**
- Modify: `webapp/client/index.html` (remove `roster-strip` div ~line 114; add Switch GO button in `dressup-stage`; add overlay markup next to `confirm-overlay` ~line 218)
- Modify: `webapp/client/app.js` (replace `renderRoster` with `renderGoPicker` + open/close; update `selectCharacter`, `openDressup`, `harvestActive`)
- Modify: `webapp/client/style.css` (remove `.roster-*` rules ~lines 704-717; add `.go-*` rules)

**Interfaces:**
- Consumes: `buildPure.goTileState(char, activeNftId)` (Task 1), existing `imgUrl`/`layerSrc`/`layerComplete`/`BLANK_IMG`/`selectCharacter`/`openAssemble`.
- Produces: `renderGoPicker()`, `openGoPicker()`, `closeGoPicker()`, module-level `goAssembleEnabled` flag. `renderRoster` is deleted.

- [ ] **Step 1: HTML — Switch GO button + overlay, remove the strip**

In `webapp/client/index.html`, inside `dressup-stage` after `<p id="dressup-id" class="cap"></p>` (~line 103), add:

```html
          <button id="go-switch-btn" class="secondary">🔄 Switch GO</button>
```

Delete the roster strip line (~line 114):

```html
      <div id="roster-strip" class="roster-strip"></div>
```

Before the `confirm-overlay` div (~line 218), add:

```html
  <div id="go-picker-overlay" class="confirm-overlay" role="dialog" aria-modal="true"
       aria-labelledby="go-picker-title" hidden>
    <div class="confirm-box card go-picker-box">
      <div class="go-picker-head">
        <h2 id="go-picker-title">Your GOs</h2>
        <button id="go-picker-close" class="secondary" aria-label="Close">✕</button>
      </div>
      <div id="go-picker-grid" class="go-picker-grid"></div>
    </div>
  </div>
```

- [ ] **Step 2: app.js — picker render + open/close**

In `webapp/client/app.js`, replace the whole `renderRoster` function (~lines 1146-1182) with:

```js
// --- GO picker (overlay) ---
// Replaces the old unlabeled bottom roster strip: a full-panel overlay grid
// of labeled tiles (#edition · body), opened from the Switch GO button.
let goAssembleEnabled = true;

function renderGoPicker() {
  const grid = el('go-picker-grid');
  grid.replaceChildren();
  for (const char of economyState.characters) {
    const t = buildPure.goTileState(char, activeNftId);
    const tile = document.createElement('button');
    tile.className = 'go-tile'
      + (t.state === 'active' ? ' active' : '')
      + (t.state === 'indexing' ? ' indexing' : '');
    const img = document.createElement('img');
    img.loading = 'lazy';
    const imgSrc = imgUrl(char.image_url, THUMB_W);
    const bodyVal = (char.attributes.find((a) => a.trait_type === 'Body') || {}).value;
    if (imgSrc) {
      img.src = imgSrc;
    } else if (layerComplete(char.body, bodyVal)) {
      img.src = layerSrc(char.body, 'Body', bodyVal);
    } else {
      // No CDN image and incomplete metadata: a layer fetch would 400.
      img.src = BLANK_IMG;
    }
    img.alt = t.label;
    const cap = document.createElement('span');
    cap.className = 'go-tile-label';
    cap.textContent = t.state === 'active' ? `✓ ${t.label}` : t.label;
    const sub = document.createElement('span');
    sub.className = 'go-tile-sub';
    sub.textContent = t.sub;
    tile.replaceChildren(img, cap, sub);
    if (t.state === 'indexing') {
      tile.disabled = true; // no body -> every layer fetch would 400
    } else {
      tile.onclick = () => { closeGoPicker(); selectCharacter(char.nft_id); };
    }
    grid.appendChild(tile);
  }
  const add = document.createElement('button');
  add.className = 'go-tile assemble';
  add.textContent = '＋';
  add.title = goAssembleEnabled ? 'Assemble new' : 'Create your Closet first';
  if (goAssembleEnabled) add.onclick = () => { closeGoPicker(); openAssemble(); };
  else add.disabled = true;
  grid.appendChild(add);
}

function openGoPicker() {
  renderGoPicker();
  const overlay = el('go-picker-overlay');
  overlay.hidden = false;
  const onKey = (e) => { if (e.key === 'Escape') closeGoPicker(); };
  overlay._onKey = onKey; // stashed so closeGoPicker can remove it
  document.addEventListener('keydown', onKey);
  el('go-picker-close').onclick = () => closeGoPicker();
  overlay.onclick = (e) => { if (e.target === overlay) closeGoPicker(); };
}

function closeGoPicker() {
  const overlay = el('go-picker-overlay');
  overlay.hidden = true;
  overlay.onclick = null;
  if (overlay._onKey) {
    document.removeEventListener('keydown', overlay._onKey);
    overlay._onKey = null;
  }
}
```

- [ ] **Step 3: app.js — update the call sites**

In `selectCharacter` (~lines 1184-1190), replace `renderRoster();` with nothing (the picker re-renders on open); the function becomes:

```js
function selectCharacter(nftId) {
  activeNftId = nftId;
  const char = economyState.characters.find((c) => c.nft_id === nftId);
  if (char) renderCanvas(char);
  renderCloset();
}
```

In `openDressup`, gated branch (~line 1266), replace:

```js
      // Render roster (no-op visually) but don't wire assemble tile
      renderRoster(/* assembleEnabled= */ false);
```

with:

```js
      goAssembleEnabled = false;
```

In `openDressup`, active branch (~lines 1277-1280), replace:

```js
    activeNftId = buildPure.pickDefaultCharacter(economyState.characters);
    renderRoster(/* assembleEnabled= */ true);
    if (activeNftId) selectCharacter(activeNftId);
    else { el('dressup-canvas').replaceChildren(); renderCloset(); }
```

with:

```js
    goAssembleEnabled = true;
    activeNftId = buildPure.pickDefaultCharacter(economyState.characters);
    if (activeNftId) selectCharacter(activeNftId);
    else { el('dressup-canvas').replaceChildren(); renderCloset(); }
```

In `harvestActive` (~lines 1521-1523), replace:

```js
    showPanel('dressup-panel');
    if (activeNftId) selectCharacter(activeNftId);
    else { renderRoster(); renderCloset(); el('dressup-canvas').replaceChildren(); }
```

with:

```js
    showPanel('dressup-panel');
    if (activeNftId) selectCharacter(activeNftId);
    else { renderCloset(); el('dressup-canvas').replaceChildren(); }
```

Wire the Switch GO button next to the other wiring (~line 2139 area, where Task 2 added `dressup-back-btn`):

```js
  el('go-switch-btn').onclick = () => openGoPicker();
```

- [ ] **Step 4: CSS — remove `.roster-*`, add `.go-*`**

In `webapp/client/style.css`, delete the `.roster-strip` / `.roster-tile` block (~lines 704-717, including `.roster-tile.incomplete`). Add in its place:

```css
/* --- GO picker overlay (replaces the old roster strip) --- */
.go-picker-box { width: min(520px, 100%); text-align: left; }
.go-picker-head { display: flex; justify-content: space-between; align-items: center; }
.go-picker-head h2 { margin: 0; }
.go-picker-grid {
  display: grid; grid-template-columns: repeat(auto-fill, minmax(96px, 1fr));
  gap: 10px; margin-top: 12px; max-height: 60vh; overflow-y: auto;
}
.go-tile {
  display: flex; flex-direction: column; align-items: center; gap: 2px;
  padding: 8px 4px; border: 2px solid var(--ink); border-radius: 10px;
  background: var(--surface-2); cursor: pointer;
}
.go-tile img { width: 72px; height: 72px; object-fit: contain; }
.go-tile.active { box-shadow: 0 0 0 3px var(--blue); }
.go-tile.indexing {
  opacity: .55; cursor: default;
  background: repeating-linear-gradient(
    45deg, var(--surface-2), var(--surface-2) 6px, var(--surface) 6px, var(--surface) 12px);
}
.go-tile-label { font-weight: 700; font-size: .8rem; }
.go-tile-sub { font-size: .7rem; color: var(--muted); }
.go-tile.assemble { justify-content: center; font-size: 1.6rem; min-height: 96px; }
```

- [ ] **Step 5: Verify no dangling references**

Run: `grep -n "renderRoster\|roster-strip\|roster-tile" webapp/client/app.js webapp/client/index.html webapp/client/style.css`
Expected: no matches.

- [ ] **Step 6: Run the test suite**

Run: `.venv/bin/python -m pytest tests/test_build_pure_js.py tests/test_market_panel_dom.py webapp/ -q`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add webapp/client/index.html webapp/client/app.js webapp/client/style.css
git commit -m "feat(activity): labeled GO picker overlay replaces the roster strip"
```

---

### Task 5: Hide Closet tiles / trait chips that can't render on the selected GO

**Files:**
- Modify: `webapp/client/app.js` (`renderCloset` ~lines 1307-1350; `renderTraitStrip` ~lines 1352-1383)

**Interfaces:**
- Consumes: existing `layerComplete`, `BLANK_IMG`, `activeChar()`.
- Produces: nothing new — behavior change only.

Behavior: when a GO **is** selected, a tile whose art is incomplete for it is skipped, and a tile whose layer fetch 404s (art missing for that body) removes itself via `img.onerror`. When **no** GO is selected (empty roster), tiles keep the `BLANK_IMG` placeholder so the Closet contents stay visible.

- [ ] **Step 1: `renderCloset` — skip incomplete, self-remove on 404**

In `renderCloset`, replace the img block (~lines 1325-1330):

```js
    const img = document.createElement('img');
    // Guard: a missing active body or empty asset value would 400 the layer fetch.
    img.src = (char && layerComplete(char.body, asset.value))
      ? layerSrc(char.body, asset.slot, asset.value)
      : BLANK_IMG;
    img.alt = `${asset.slot}: ${asset.value}`;
```

with:

```js
    // With a GO selected, a trait that can't render on its body is hidden
    // entirely (it reappears on a GO whose body has the art). With no GO
    // selected, keep a blank placeholder so the Closet contents stay visible.
    if (char && !layerComplete(char.body, asset.value)) continue;
    const img = document.createElement('img');
    if (char) {
      img.src = layerSrc(char.body, asset.slot, asset.value);
      // Art missing for this body (layer fetch 404s): drop the whole tile
      // instead of rendering a broken image.
      img.onerror = () => item.remove();
    } else {
      img.src = BLANK_IMG;
    }
    img.alt = `${asset.slot}: ${asset.value}`;
```

(Note: the `continue` must come before the `item` DOM node is appended — it already is, since `grid.appendChild(item)` is the last line of the loop; `item` is declared above, which is fine.)

- [ ] **Step 2: `renderTraitStrip` — same rule for tradeable-trait chips**

In `renderTraitStrip`, replace the chip img block (~lines 1366-1372):

```js
    const chip = document.createElement('div');
    chip.className = 'trait-chip';
    const img = document.createElement('img');
    img.src = (char && layerComplete(char.body, t.value))
      ? layerSrc(char.body, t.slot, t.value)
      : BLANK_IMG;
    img.alt = `${t.slot}: ${t.value}`;
```

with:

```js
    if (char && !layerComplete(char.body, t.value)) continue;
    const chip = document.createElement('div');
    chip.className = 'trait-chip';
    const img = document.createElement('img');
    if (char) {
      img.src = layerSrc(char.body, t.slot, t.value);
      img.onerror = () => chip.remove();
    } else {
      img.src = BLANK_IMG;
    }
    img.alt = `${t.slot}: ${t.value}`;
```

- [ ] **Step 3: Run the test suite**

Run: `.venv/bin/python -m pytest tests/ webapp/ -q`
Expected: all pass (full local suite, same set the pre-push gate runs).

- [ ] **Step 4: Commit**

```bash
git add webapp/client/app.js
git commit -m "fix(activity): hide Closet tiles/trait chips that can't render on the selected GO"
```

---

### Task 6: Manual verification pass (dev mode)

**Files:** none (verification only).

- [ ] **Step 1: Launch the dev-mode harness**

Run: `WEBAPP_DEV_MODE=1 .venv/bin/python -m webapp.server` (background; port 8176 — stop any pm2 `lfg-activity` conflict is NOT needed since this box's prod runs from `~/LFG`, use a different port if 8176 is bound: `WEBAPP_PORT=8199`).
Then: `curl -s localhost:8199/ | grep -o '🏗️ Build'` — expected: `🏗️ Build`.

- [ ] **Step 2: Manual checklist (browser or curl-level where possible)**

- Entry button reads "🏗️ Build".
- Build panel shows "← Back to the job site" and it returns home.
- With a mixed roster (mock economy has indexed + unindexed), landing GO is indexed (caption never "#null").
- Switch GO opens the overlay; tiles labeled `#edition · body`; unindexed tile greyed/disabled; picking one re-renders; ✕ / Esc / backdrop close it.
- Closet tiles with art missing for the selected body disappear (no broken images).

- [ ] **Step 3: Record results**

Note any deviations; fix before the branch is finished (superpowers:verification-before-completion).
