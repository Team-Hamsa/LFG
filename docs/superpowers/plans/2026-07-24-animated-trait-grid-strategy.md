# Animated-trait Grid Rendering Strategy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Serve a truly **static first-frame** image in every dense trait grid
(Closet, trait strip, shop/market trait tiles) with IntersectionObserver
lazy-load, and upgrade to a small hardware-decoded WebM `<video>` **only** in
the focused/detail view under a concurrency budget (iOS-safe). No compose/mint
change; no on-ledger change.

**Architecture:** Three independent seams —
1. **Static tier** `layers/.stills/` (pure path logic in
   `lfg_core/layer_thumbs.py`, generation in `scripts/make_layer_thumbs.py`,
   serving in `lfg_service/app.py::handle_layer` + `_trait_image_url`).
2. **Lazy static grid tiles** (client: pure `webapp/client/layer_media_pure.js`,
   wired into `renderCloset`/`renderTraitStrip` in `webapp/client/app.js`).
3. **Budgeted detail-view video upgrade** (client: video-budget LRU in the pure
   module, wired into `renderCanvas`).

Seams 1 and 2/3 are independent (server tier can land first, degrades to the
existing GIF thumb until the client asks for `still=1`). Cross-references #204,
which reuses seam 2/3 primitives for the composite mint/swap reveal.

**Tech Stack:** Python 3 / aiohttp / asyncio / pytest; ffmpeg (generator only);
vanilla no-build ES-module JS client with node-harness pure-logic tests.

## Global Constraints

- **No transaction is built here** — this is display/asset plumbing only.
  SourceTag `2606160021` and provenance memos are N/A; nothing on the
  mint/swap/economy on-ledger path is touched. Do not add or alter any tx code.
- **Pre-push gate must pass** (ruff `--fix`, ruff-format, mypy from `.venv`,
  gitleaks, pytest, validate-trait-config). Never `--no-verify`. In worktrees,
  ensure the `.venv` symlink exists so the gate actually runs.
- **`/api/layer` never introduces a new 404:** every new param degrades through
  still → thumb → full. Verify with a test that a `still=1` request for art with
  no still on disk still returns the thumb/full bytes, not 404.
- **Cache-buster:** any edit to `webapp/client/app.js` bumps `app.js?v=<n>` in
  `webapp/client/index.html` (currently `?v=32`) in the **same commit**. A new
  ES-module import (`layer_media_pure.js`) is itself a cache key — add it with
  its own `?v=` and bump in lockstep.
- **Dot-prefixed tier:** `.stills` must stay hidden so `LocalLayerStore` never
  enumerates it as a body / mint-pool source.

---

### Task 1: `layers/.stills/` static-frame tier — path logic + generator

**Files:**
- Modify `lfg_core/layer_thumbs.py` (add `STILLS_DIR`, `STILL_SIZE`,
  `still_path_for`, `scan_stills`; factor the winner-by-priority walk into a
  shared helper reused by `scan` and `scan_stills`).
- Modify `scripts/make_layer_thumbs.py` (also generate the `.stills/` tree:
  static PNG → 512 lanczos; animated → first-frame PNG, `libvpx-vp9` decode on
  `.webm`, alpha preserved; mtime-idempotent + `--check`).
- Test `tests/test_layer_thumbs.py`.

**Interfaces:**
- Produces `layer_thumbs.still_path_for(src_path: str, base_dir: str) -> str | None`
  and `layer_thumbs.scan_stills(base_dir: str) -> tuple[list[tuple[str,str]], list[str]]`.
- Consumes the existing `_SOURCES_FOR_THUMB` / `LAYER_EXTENSIONS` priority.

- [ ] **Step 1: Write the failing test(s)** — in `tests/test_layer_thumbs.py`
  (module already exists; keep its env-guard posture — it imports `lfg_core`,
  so ensure the preamble
  `import os; os.environ.setdefault("BUNNY_PULL_ZONE", "z"); os.environ.setdefault("LAYER_SOURCE", "local")`
  is at module top if not already present). Add:
  ```python
  def test_still_path_maps_every_format_to_png(tmp_path):
      base = str(tmp_path)
      assert layer_thumbs.still_path_for(f"{base}/ape/Hat/Wiz.png", base) == \
          f"{base}/.stills/ape/Hat/Wiz.png"
      assert layer_thumbs.still_path_for(f"{base}/ape/Body/Diamond.webm", base) == \
          f"{base}/.stills/ape/Body/Diamond.png"

  def test_still_path_rejects_outside_and_own_tier(tmp_path):
      base = str(tmp_path)
      assert layer_thumbs.still_path_for(f"{base}/.stills/ape/Hat/X.png", base) is None
      assert layer_thumbs.still_path_for("/etc/passwd", base) is None
      assert layer_thumbs.still_path_for(f"{base}/ape/Hat/notes.txt", base) is None

  def test_scan_stills_reports_missing_and_orphan(tmp_path):
      # build a layers tree with one png + one webm; no .stills yet -> both stale
      ...
      stale, orphans = layer_thumbs.scan_stills(str(tmp_path))
      assert {s for s, _ in stale} == {png_src, webm_src}
      assert orphans == []
  ```
- [ ] **Step 2: Run to verify they fail** —
  `.venv/bin/python -m pytest tests/test_layer_thumbs.py -q`
  (expect `AttributeError: module 'lfg_core.layer_thumbs' has no attribute 'still_path_for'`).
- [ ] **Step 3: Implement** — add `STILLS_DIR = ".stills"`, `STILL_SIZE = 512`,
  `still_path_for` (mirror `thumb_path_for` guards but map any layer ext → `.png`),
  and `scan_stills` (reuse the extension-priority winner walk; a `.stills/<stem>.png`
  is stale if missing or older than its winning source). In
  `scripts/make_layer_thumbs.py`, add a stills pass: for each stale
  `(src, still)`, `ffmpeg -i src -frames:v 1` (prefixing `-c:v libvpx-vp9` for
  `.webm`), `scale=512:512:flags=lanczos`, RGBA/alpha preserved, write the PNG;
  prune orphans; honor `--check`.
- [ ] **Step 4: Run to verify they pass** —
  `.venv/bin/python -m pytest tests/test_layer_thumbs.py -q`.
- [ ] **Step 5: Generator smoke** — on a fixture tree (`.png` + `.gif` + alpha
  `.webm`), run `make_layer_thumbs.py`; assert each `.stills/*.png` is 512×512
  and the webm still has a non-opaque alpha channel (first frame, not black).
- [ ] **Step 6: Commit** — `feat(layers): add .stills first-frame tier for grid rendering (#298)`.

---

### Task 2: Serve `still=1` from `/api/layer` and `_trait_image_url`

**Files:**
- Modify `lfg_service/app.py` (`handle_layer` ~L5079: still→thumb→full chain;
  `_trait_image_url` ~L972: append `&still=1`).
- Test `webapp/test_smoke.py`.

**Interfaces:**
- Consumes `layer_thumbs.still_path_for`. Produces `/api/layer?...&thumb=1&still=1`.

- [ ] **Step 1: Write the failing test(s)** — in `webapp/test_smoke.py` (keep
  the existing env-guard preamble at module top). Add, alongside
  `test_layer_handler_bad_params`:
  ```python
  async def test_layer_handler_prefers_still_then_falls_back(tmp_path, monkeypatch):
      # local store rooted at tmp_path with an animated Body.webm + a .stills png
      # -> still=1 serves the .stills/*.png; delete the still -> serves .thumbs gif;
      # delete that -> serves the full .webm. Never 404 while the full asset exists.
      ...
  def test_trait_image_url_carries_still(monkeypatch):
      url = app._trait_image_url(cfg, "Hat", "Wizard Hat")
      assert "thumb=1" in url and "still=1" in url
  ```
- [ ] **Step 2: Run to verify they fail** —
  `.venv/bin/python -m pytest webapp/test_smoke.py -q -k "still or trait_image"`.
- [ ] **Step 3: Implement** — in `handle_layer`, when
  `request.query.get("still") == "1"` and `thumb == "1"` and the store is
  `LocalLayerStore`, try `layer_thumbs.still_path_for(path, store.base_dir)`
  first (use it if it exists on disk), else the existing `thumb_path_for`, else
  keep `path` (full). Append `&still=1` in `_trait_image_url` next to
  `&thumb=1`.
- [ ] **Step 4: Run to verify they pass** — same `-k` selection, then the full
  `webapp/test_smoke.py`.
- [ ] **Step 5: Wider suite / regression run** —
  `.venv/bin/python -m pytest tests/test_layer_thumbs.py webapp/test_smoke.py -q`.
- [ ] **Step 6: Commit** — `feat(service): /api/layer still=1 static-frame serving (#298)`.

---

### Task 3: Pure client module + lazy static grid tiles

**Files:**
- Create `webapp/client/layer_media_pure.js` (ES module, pure logic:
  `layerParams(...)`, and the video-budget LRU used in Task 4).
- Create `tests/test_layer_media_pure_js.py` (node harness, mirror
  `tests/test_build_pure_js.py`).
- Modify `webapp/client/app.js` (`layerSrc` delegates to `layerParams`;
  new `layerStillEl` + shared `observeLazy`; `renderCloset` ~L2216,
  `renderTraitStrip` ~L2285 switch to lazy static tiles).
- Modify `webapp/client/index.html` (bump `app.js?v=`, add
  `layer_media_pure.js?v=1` import key).

**Interfaces:**
- Produces `layerParams(body, trait, value, {still, full}) -> string` (query
  string). Consumed by `layerSrc`.

- [ ] **Step 1: Write the failing test(s)** — `tests/test_layer_media_pure_js.py`
  (node subprocess harness; no `lfg_core` import → no env-guard needed):
  ```python
  def test_layer_params_grid_is_static_thumb():
      out = run_js('M.layerParams("ape","Hat","Wizard Hat",{still:true})')
      assert "thumb=1" in out and "still=1" in out
  def test_layer_params_full_has_no_thumb():
      out = run_js('M.layerParams("ape","Body","Diamond",{full:true})')
      assert "thumb=1" not in out and "still=1" not in out
  ```
- [ ] **Step 2: Run to verify they fail** —
  `.venv/bin/python -m pytest tests/test_layer_media_pure_js.py -q`
  (fails: module/function absent; skips only if node is missing).
- [ ] **Step 3: Implement** — write `layer_media_pure.js::layerParams` (URL-encode
  body/trait/value; append `thumb=1&still=1` for grids, `thumb=1` for non-still
  animated, nothing for `full`). In `app.js`: `layerSrc(body,trait,value,opts={})`
  delegates the query to `layerParams`; add `layerStillEl(src, alt, onMissing)`
  (plain `<img>`, no client `<video>` fallback — server chain handles
  still→thumb→full; `onMissing` only on genuine 404); add a single shared
  `observeLazy(img, src)` IntersectionObserver (`rootMargin: '200px'`, assigns
  `.src`, unobserves). Switch `renderCloset` and `renderTraitStrip` tiles from
  `layerMediaEl(layerSrc(...))` to `observeLazy(layerStillEl(...), layerSrc(..., {still:true}))`.
  Bump `app.js?v=` and add the `layer_media_pure.js?v=1` import in `index.html`.
- [ ] **Step 4: Run to verify they pass** —
  `.venv/bin/python -m pytest tests/test_layer_media_pure_js.py -q`.
- [ ] **Step 5: Wider suite / regression run** —
  `.venv/bin/python -m pytest tests/ webapp/ -q -k "pure or layer or dom"`
  (incl. `tests/test_market_panel_dom.py` to catch DOM-shape regressions).
- [ ] **Step 6: Commit** — `feat(activity): static lazy trait grid tiles (#298)`
  (single commit incl. the `index.html` cache-buster bump).

---

### Task 4: Budgeted detail-view WebM upgrade

**Files:**
- Modify `webapp/client/layer_media_pure.js` (video-budget LRU:
  `makeVideoBudget(max)` → `acquire(key)`/`release(key)` returning evictions).
- Modify `tests/test_layer_media_pure_js.py` (budget LRU cases).
- Modify `webapp/client/app.js` (`renderCanvas` ~L1920: still `<img>` per
  animated slot, upgrade to a `layerSrc(..., {full:true})` `<video muted autoplay
  loop playsinline>` through the budget; iOS budget 0 via
  `!video.canPlayType('video/webm; codecs="vp9"')`, reusing the L1891 probe).
- Modify `webapp/client/index.html` (bump `app.js?v=`, `layer_media_pure.js?v=`).

**Interfaces:**
- Produces `makeVideoBudget(max)` with `acquire(key) -> evictedKeys[]` and
  `release(key)`. Pure; DOM wiring lives in `app.js`.

- [ ] **Step 1: Write the failing test(s)** — in `tests/test_layer_media_pure_js.py`:
  ```python
  def test_video_budget_evicts_least_recent():
      out = run_js('(()=>{const b=M.makeVideoBudget(2);'
                   'b.acquire("a");b.acquire("b");'
                   'const ev=b.acquire("c");return ev;})()')
      assert out == ["a"]
  def test_video_budget_zero_never_grants():
      out = run_js('(()=>{const b=M.makeVideoBudget(0);return b.acquire("a");})()')
      assert out == ["a"]  # over budget immediately -> the just-acquired key evicts itself
  ```
- [ ] **Step 2: Run to verify they fail** —
  `.venv/bin/python -m pytest tests/test_layer_media_pure_js.py -q -k budget`.
- [ ] **Step 3: Implement** — `makeVideoBudget(max)`: an LRU `Map`; `acquire`
  bumps recency, evicts (and returns) keys beyond `max`. In `app.js`
  `renderCanvas`: build each animated slot as a still `<img>`, then, if the
  webview can play VP9-WebM and the budget grants a slot, replace with a full
  WebM `<video>`; on eviction, pause + revert that video back to its still and
  free the decoder. iOS/no-VP9 → budget constructed with `max=0` (or reuse a
  detected cap), so tiles stay static/GIF — never a dead `<video>`. Bump the
  cache-busters.
- [ ] **Step 4: Run to verify they pass** —
  `.venv/bin/python -m pytest tests/test_layer_media_pure_js.py -q`.
- [ ] **Step 5: Wider suite / regression run** —
  `.venv/bin/python -m pytest tests/ webapp/ -q`.
- [ ] **Step 6: Commit** — `feat(activity): budgeted WebM detail-view upgrade (#298)`.

---

### Final Task: Full gate + PR

- [ ] Run the full suite: `.venv/bin/python -m pytest -q`.
- [ ] Run lint/format/type: `.venv/bin/ruff check --fix . && .venv/bin/ruff format . && .venv/bin/mypy .`.
- [ ] Generate the stills tier on staging + prod layer trees
      (`make_layer_thumbs.py`) as an ops step, and add `.stills/` regeneration to
      the CLAUDE.md "Adding a New Trait Layer" checklist (alongside the existing
      `make_layer_thumbs.py` step). `.stills/` is gitignored (inside `layers/`).
- [ ] **Manual staging smoke:** 200-trait Closet scrolls smoothly; network panel
      confirms off-screen tiles don't fetch (lazy); Build canvas character
      animates; **iOS device** check for VP9-alpha (spec open question 1) — if it
      renders on black, leave iOS on the static/GIF path (already the default).
- [ ] Push the branch and open a **non-draft** PR (`gh pr create`) to
      `Team-Hamsa/LFG`. **No AI attribution** in commits or PR body. Cross-link
      #298 and note the shared primitives available to #204.
- [ ] Wait for **Greptile** + **CodeRabbit**; resolve every actionable finding
      (fix in code **and** reply on its thread naming the fixing commit) before
      merge. Greptile clean = check-run summary only (no comment); confirm the
      run, don't assume a no-show.
