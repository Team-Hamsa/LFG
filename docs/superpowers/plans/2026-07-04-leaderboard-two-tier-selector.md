# Leaderboard Two-Tier Board Selector Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the leaderboard's horizontally-scrolling 8-chip board row with a two-tier selector (3 category tabs → 2-3 sub-board chips), and fix the suite-wide `ECONOMY_ENABLED` test env-guard so pre-push tests pass with the mainnet `.env`.

**Architecture:** Frontend-only change in `webapp/client/` (no-build vanilla JS — no JS test harness; UI regressions are guarded by Python source-assertion tests, per `tests/test_app_js_boot.py`). Sub-board chips are rendered by JS from a single `CATEGORIES` map so HTML and JS cannot drift. The env-guard fix is a new root `conftest.py` that pins `ECONOMY_ENABLED=1` before any `lfg_core` import (pytest imports conftest before test modules; `load_dotenv()` in `lfg_core/config.py:9` does not override already-set env vars).

**Tech Stack:** Vanilla JS/HTML/CSS, pytest (source-assertion tests).

**Spec:** `docs/superpowers/specs/2026-07-04-leaderboard-two-tier-selector-design.md`

## Global Constraints

- Board keys sent to `/api/leaderboard` are UNCHANGED: `users_nfts`, `users_swaps`, `users_builds`, `nft_swaps`, `nft_rarity`, `brix_rich`, `brix_lp`, `brix_earned`. No backend/API changes.
- Labels: Users → Holders / Swappers / Builders; NFTs → **Swaps** (was "Hot NFTs") / Rarest; BRIX → Richlist / LP / Earned. Category tabs: `Users`, `NFTs`, `BRIX`.
- Default selection unchanged: category `users`, board `users_nfts`.
- Work on a feature branch; open a **draft** PR (CodeRabbit workflow). Do not push to `main`.
- This machine's `.env` has `ECONOMY_ENABLED=0`; until Task 1 lands, plain `pytest` shows 3 pre-existing failures — run Task 1 first.

---

### Task 1: Root conftest env-guard for `ECONOMY_ENABLED`

Three tests fail suite-wide because `.env` now carries `ECONOMY_ENABLED=0` (mainnet cutover, #113) and `lfg_core/config.py` freezes the flag at first import: `tests/test_economy_feature_flag.py::test_config_default_is_enabled`, `webapp/test_smoke.py::test_economy_dev_mode_read`, `webapp/test_smoke.py::test_equip_missing_body_field_returns_400`. Per-file preambles can't fix this — an earlier test file importing `lfg_core` freezes the flag first. A root `conftest.py` runs before any test module is imported, and `load_dotenv()` respects already-set vars.

**Files:**
- Create: `conftest.py` (repo root)

**Interfaces:**
- Produces: test runs where `lfg_core.config.ECONOMY_ENABLED` is `True` regardless of `.env` (unless the shell explicitly exports it).

- [ ] **Step 1: Reproduce the failures**

Run: `.venv/bin/python -m pytest tests/test_economy_feature_flag.py::test_config_default_is_enabled webapp/test_smoke.py::test_economy_dev_mode_read webapp/test_smoke.py::test_equip_missing_body_field_returns_400 -v`
Expected: 3 FAILED (assert False is True / 403 vs 200 / 403 vs 400).

- [ ] **Step 2: Create root `conftest.py`**

```python
# conftest.py — repo-root pytest env guard.
# lfg_core/config.py freezes constants from the environment (via load_dotenv)
# at first import, and the machine's .env is the LIVE deployment config — e.g.
# it sets ECONOMY_ENABLED=0 after the mainnet cutover (#113), which broke the
# tests that assert the enabled default. pytest imports this file before any
# test module, and load_dotenv() never overrides an already-set variable, so
# setdefault here pins the test default suite-wide. Explicit shell exports
# still win (setdefault), so a run can force a value when needed.
import os

os.environ.setdefault("ECONOMY_ENABLED", "1")
```

- [ ] **Step 3: Verify the 3 tests pass**

Run: `.venv/bin/python -m pytest tests/test_economy_feature_flag.py webapp/test_smoke.py -v`
Expected: PASS (all; no failures).

- [ ] **Step 4: Run the full suite**

Run: `.venv/bin/python -m pytest`
Expected: 757 passed (0 failed).

- [ ] **Step 5: Commit**

```bash
git add conftest.py
git commit -m "test: pin ECONOMY_ENABLED=1 in root conftest so live .env can't break the suite"
```

---

### Task 2: Two-tier selector — markup + JS

**Files:**
- Modify: `webapp/client/index.html:51-60` (the `#lb-boards` block)
- Modify: `webapp/client/app.js:175-176` (constants/state), `:232-285` (`loadLeaderboard`), `:287-311` (`setupLeaderboard`)
- Modify: `tests/test_app_js_boot.py:75` (old assertion on static chips)
- Test: `tests/test_leaderboard_selector.py` (new)

**Interfaces:**
- Consumes: existing `el()`, `loadLeaderboard()`, `lbState`, `NFT_BOARDS` in `app.js`.
- Produces: `CATEGORIES` map (`{users|nfts|brix: [{board, label}, ...]}`), `renderLbBoards()` (no args, renders `#lb-boards` from `CATEGORIES[lbState.cat]`), `lbState.cat` (string, default `'users'`). Task 3 styles the new `.lb-cats` row / `.lb-cat` chips.

- [ ] **Step 1: Write the failing source-assertion tests**

Create `tests/test_leaderboard_selector.py` (reads source files only — no `lfg_core` import, so no env-guard preamble needed):

```python
# tests/test_leaderboard_selector.py
# The webapp client is no-build vanilla JS (no JS test harness), so the
# two-tier leaderboard selector (spec: 2026-07-04-leaderboard-two-tier-
# selector-design.md) is guarded by source assertions, like test_app_js_boot.
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLIENT = os.path.join(ROOT, "webapp", "client")


def _read(name: str) -> str:
    with open(os.path.join(CLIENT, name), encoding="utf-8") as f:
        return f.read()


def test_index_has_category_row():
    html = _read("index.html")
    assert 'id="lb-cats"' in html
    for cat in ("users", "nfts", "brix"):
        assert f'data-cat="{cat}"' in html
    # Sub-board chips are JS-rendered from CATEGORIES; none hardcoded in HTML.
    assert 'data-board=' not in html
    assert 'id="lb-boards"' in html


def test_app_js_categories_map_covers_all_8_boards():
    src = _read("app.js")
    m = re.search(r"const CATEGORIES = \{.*?\n\};", src, re.S)
    assert m, "CATEGORIES map missing from app.js"
    block = m.group(0)
    for board in (
        "users_nfts", "users_swaps", "users_builds",
        "nft_swaps", "nft_rarity",
        "brix_rich", "brix_lp", "brix_earned",
    ):
        assert board in block, f"{board} missing from CATEGORIES"
    for label in ("Holders", "Swappers", "Builders", "Swaps", "Rarest",
                  "Richlist", "LP", "Earned"):
        assert f"'{label}'" in block, f"label {label} missing"
    assert "Hot" not in block  # renamed to Swaps


def test_app_js_category_switch_behavior():
    src = _read("app.js")
    # Sub-row renders from the map; category click selects its first board
    # and reloads.
    assert "function renderLbBoards()" in src
    assert "CATEGORIES[lbState.cat][0].board" in src
    assert "cat: 'users'" in src  # default category in lbState
```

- [ ] **Step 2: Run new tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_leaderboard_selector.py -v`
Expected: 3 FAILED (`lb-cats` not in html, CATEGORIES missing, renderLbBoards missing).

- [ ] **Step 3: Replace the chip row in `index.html`**

Replace lines 51-59 (the `#lb-boards` div and its 8 buttons) with:

```html
        <div id="lb-cats" class="lb-chips lb-cats" role="tablist" aria-label="Category">
          <button class="lb-chip lb-cat active" role="tab" aria-selected="true" data-cat="users">Users</button>
          <button class="lb-chip lb-cat" role="tab" aria-selected="false" data-cat="nfts">NFTs</button>
          <button class="lb-chip lb-cat" role="tab" aria-selected="false" data-cat="brix">BRIX</button>
        </div>
        <div id="lb-boards" class="lb-chips lb-boards" role="tablist" aria-label="Board"></div>
```

- [ ] **Step 4: Add `CATEGORIES` map, `cat` state, and `renderLbBoards()` in `app.js`**

At `app.js:175-176`, replace:

```js
const NFT_BOARDS = ['nft_swaps', 'nft_rarity'];
const lbState = { period: 'week', board: 'users_nfts', anchor: null };
```

with:

```js
const NFT_BOARDS = ['nft_swaps', 'nft_rarity'];
// Two-tier board selector: category tabs → sub-board chips. The sub-row is
// rendered from this map so HTML and JS can't drift. Board keys match the
// /api/leaderboard contract and are unchanged.
const CATEGORIES = {
  users: [
    { board: 'users_nfts', label: 'Holders' },
    { board: 'users_swaps', label: 'Swappers' },
    { board: 'users_builds', label: 'Builders' },
  ],
  nfts: [
    { board: 'nft_swaps', label: 'Swaps' },
    { board: 'nft_rarity', label: 'Rarest' },
  ],
  brix: [
    { board: 'brix_rich', label: 'Richlist' },
    { board: 'brix_lp', label: 'LP' },
    { board: 'brix_earned', label: 'Earned' },
  ],
};
const lbState = { period: 'week', cat: 'users', board: 'users_nfts', anchor: null };

function renderLbBoards() {
  const row = el('lb-boards');
  row.replaceChildren(
    ...CATEGORIES[lbState.cat].map(({ board, label }) => {
      const btn = document.createElement('button');
      btn.className = 'lb-chip';
      btn.setAttribute('role', 'tab');
      btn.dataset.board = board;
      btn.textContent = label;
      return btn;
    })
  );
}
```

- [ ] **Step 5: Mark the active category tab in `loadLeaderboard()`**

In `loadLeaderboard()` (app.js:232), immediately before the existing `for (const btn of el('lb-boards')...)` loop, add:

```js
  for (const btn of el('lb-cats').querySelectorAll('.lb-chip')) {
    const active = btn.dataset.cat === lbState.cat;
    btn.classList.toggle('active', active);
    btn.setAttribute('aria-selected', String(active));
  }
```

(The existing `#lb-boards` active-state loop and the rest of the function are unchanged; it operates on the JS-rendered chips. Note the rendered chips don't set `aria-selected` at creation — this loop sets it on every load, including the first.)

- [ ] **Step 6: Wire category clicks and initial render in `setupLeaderboard()`**

In `setupLeaderboard()` (app.js:287), add before the existing `el('lb-boards')` listener:

```js
  renderLbBoards();
  el('lb-cats').addEventListener('click', (e) => {
    const btn = e.target.closest('.lb-chip');
    if (!btn || btn.dataset.cat === lbState.cat) return;
    lbState.cat = btn.dataset.cat;
    lbState.board = CATEGORIES[lbState.cat][0].board;
    renderLbBoards();
    loadLeaderboard();
  });
```

The existing `el('lb-boards')` click listener is unchanged (event delegation on the container keeps working across re-renders).

- [ ] **Step 7: Update the stale assertion in `tests/test_app_js_boot.py:75`**

Replace:

```python
    assert 'id="leaderboard"' in html and 'data-board="brix_rich"' in html
```

with:

```python
    assert 'id="leaderboard"' in html and 'data-cat="brix"' in html
```

- [ ] **Step 8: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_leaderboard_selector.py tests/test_app_js_boot.py -v`
Expected: PASS (all).

- [ ] **Step 9: Commit**

```bash
git add webapp/client/index.html webapp/client/app.js tests/test_leaderboard_selector.py tests/test_app_js_boot.py
git commit -m "feat(leaderboard): two-tier category/board selector; rename Hot NFTs to Swaps"
```

---

### Task 3: CSS — no-scroll wrap + category-tab weight

**Files:**
- Modify: `webapp/client/style.css:762-774` (`.lb-chips`, `.lb-boards`, `.lb-chip`)

**Interfaces:**
- Consumes: `.lb-cats` row and `.lb-cat` chip classes produced by Task 2.

- [ ] **Step 1: Replace scroll styling with wrap and style category tabs**

Replace style.css lines 762-767:

```css
.lb-chips {
  display: flex; gap: .4rem; overflow-x: auto; padding-bottom: 4px;
  -ms-overflow-style: none; scrollbar-width: none;
}
.lb-chips::-webkit-scrollbar { display: none; }
.lb-boards { margin-top: 6px; }
```

with:

```css
.lb-chips { display: flex; flex-wrap: wrap; gap: .4rem; padding-bottom: 4px; }
.lb-boards { margin-top: 6px; }
.lb-cat { font-size: .85rem; font-weight: 700; }
```

(Max 3 chips per row, so nothing scrolls; category tabs read heavier than sub-chips. `.lb-chip`'s `flex: 0 0 auto` and the `.active` fill are unchanged and shared by both rows.)

- [ ] **Step 2: Verify the full suite is green**

Run: `.venv/bin/python -m pytest`
Expected: 760 passed (757 + 3 new), 0 failed.

- [ ] **Step 3: Visual smoke check via dev mode**

Run the Activity locally with `WEBAPP_DEV_MODE=1` (mock harness) and confirm in the served HTML/behavior: category row renders 3 tabs; default shows Holders/Swappers/Builders; clicking NFTs swaps to Swaps/Rarest and loads `nft_swaps`; no horizontal scrollbar. (Headless box — verify via `curl` of the page plus the source-assertion tests; the user can eyeball in Discord after deploy.)

- [ ] **Step 4: Commit**

```bash
git add webapp/client/style.css
git commit -m "style(leaderboard): wrap chip rows, weight category tabs"
```

---

### Task 4: Draft PR

- [ ] **Step 1: Push branch and open a draft PR**

```bash
git push -u origin feat/leaderboard-two-tier-selector
gh pr create --draft --repo Team-Hamsa/LFG \
  --title "feat(leaderboard): two-tier category/board selector" \
  --body "Implements docs/superpowers/specs/2026-07-04-leaderboard-two-tier-selector-design.md

- Replaces the horizontally scrolling 8-chip board row with category tabs (Users / NFTs / BRIX) + JS-rendered sub-board chips from a single CATEGORIES map
- Renames 'Hot NFTs' → 'Swaps' (board key nft_swaps unchanged; no API changes)
- Root conftest.py pins ECONOMY_ENABLED=1 for tests so the live .env (ECONOMY_ENABLED=0, #113) can't break the suite

🤖 Generated with [Claude Code](https://claude.com/claude-code)"
```

- [ ] **Step 2: Mark ready for CodeRabbit when settled**

Run `gh pr ready <number>` once the branch is final; wait for CodeRabbit review and resolve findings before merge (post-merge hook auto-restarts `lfg-activity`).
