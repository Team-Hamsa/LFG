# Share-ceiling Cap in the Rarity Engine — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an automatic, self-adapting share-ceiling to the rarity engine so
an over-represented trait plateaus instead of running away — replacing the manual
`enabled=0` park-off stopgap. Gated by `RARITY_CAP_MULTIPLE` (0 = off; identical
to today until opted in).

**Architecture:** One pure-math change in `lfg_core/rarity.py::effective_weight`
(new `candidate_count` + `cap_multiple` args, clamp `base` to
`max(cap_multiple × 1/candidate_count, floor_weight)`), threaded through the three
call sites that must agree — `weighted_pick` (the picker),
`get_odds`/`scripts/trait_dashboard.py` (admin views) — plus one config knob. No
DB migration, no on-ledger transaction, no new dependency.

**Tech Stack:** Python 3 / sqlite3 / stdlib / pytest; the dashboard is aiohttp +
vanilla no-build JS.

## Global Constraints

- **No transaction is built by this change** (mint-selection math only), so
  SourceTag=2606160021 and provenance memos are not touched — but the invariant
  stands for any tx path: never omit them.
- **Pre-push gate must pass** (ruff `--fix`, ruff-format, mypy from the project
  `.venv`, gitleaks, pytest, validate-trait-config). Never `--no-verify`.
- **Backwards-compatible gate:** `RARITY_CAP_MULTIPLE` default `0` ⇒ byte-for-byte
  identical behavior to today; every existing `effective_weight`/`weighted_pick`
  test must keep passing untouched.
- **`effective_weight` stays pure** (no new I/O); `now`/`rng` remain injectable.
- No `app.js` / client-cache-buster change is involved (the dashboard page is a
  standalone script, not the Activity client).

---

### Task 1: Config knob `RARITY_CAP_MULTIPLE`

**Files:**
- Modify: `lfg_core/config.py`
- Test: `tests/test_rarity.py` (asserts the constant exists / default 0)

**Interfaces:** Produces `config.RARITY_CAP_MULTIPLE: float`.

- [ ] **Step 1: Write the failing test** — in `tests/test_rarity.py` (env-guard preamble already at module top: `os.environ.setdefault` for `DISCORD_BOT_TOKEN`, `XUMM_*`, `SEED`, `TOKEN_*`, `XRPL_NETWORK`, `BUNNY_PULL_ZONE`, `LAYER_SOURCE` etc.):
  ```python
  def test_rarity_cap_multiple_defaults_off():
      from lfg_core import config
      assert config.RARITY_CAP_MULTIPLE == 0.0
  ```
- [ ] **Step 2: Run to verify it fails** — `.venv/bin/python -m pytest tests/test_rarity.py::test_rarity_cap_multiple_defaults_off -q` (expect `AttributeError: RARITY_CAP_MULTIPLE`).
- [ ] **Step 3: Implement** — after `RARITY_BOOST_STEP_HOURS` in `lfg_core/config.py`:
  ```python
  RARITY_CAP_MULTIPLE = float(os.getenv("RARITY_CAP_MULTIPLE", "0"))  # 0/unset = no share ceiling
  ```
  Add a matching line to the `.env` documentation block in `CLAUDE.md` (optional, docs-only).
- [ ] **Step 4: Run to verify it passes** — same pytest command, green.
- [ ] **Step 5: Wider run** — `.venv/bin/python -m pytest tests/test_rarity.py -q`.
- [ ] **Step 6: Commit** — `feat(rarity): add RARITY_CAP_MULTIPLE config knob (#198)`.

---

### Task 2: The clamp in `effective_weight`

**Files:**
- Modify: `lfg_core/rarity.py` (`effective_weight`)
- Test: `tests/test_rarity.py`

**Interfaces:**
Produces `effective_weight(..., population_size=0, candidate_count=0, cap_multiple=0.0)`.
Consumes nothing new. New keyword args are optional and default to the no-op path.

- [ ] **Step 1: Write the failing tests** — add to `tests/test_rarity.py`:
  ```python
  def test_cap_below_ceiling_unchanged():
      # share 10/100 = 0.10 smoothed? use population_size=0 path for exact math
      uncapped = rarity.effective_weight(30, 100, 0.005, None, 24, None, NOW)
      capped = rarity.effective_weight(
          30, 100, 0.005, None, 24, None, NOW,
          candidate_count=40, cap_multiple=3.0,  # ceiling 3*(1/40)=0.075 > 0.30? no -> clamps
      )
      # 0.30 share IS above the 0.075 ceiling -> clamps
      assert capped == pytest.approx(0.075)
      # a share below ceiling is untouched:
      low = rarity.effective_weight(2, 100, 0.005, None, 24, None, NOW,
                                    candidate_count=40, cap_multiple=3.0)
      assert low == pytest.approx(0.02)  # 2/100 = 0.02 < 0.075

  def test_cap_never_below_floor():
      # candidate_count large -> fair_share tiny -> ceiling would be sub-floor
      w = rarity.effective_weight(50, 100, 0.005, None, 24, None, NOW,
                                  candidate_count=1000, cap_multiple=3.0)
      assert w == pytest.approx(0.005)  # clamps to floor, not 3*(1/1000)=0.003

  def test_cap_off_when_multiple_zero():
      assert rarity.effective_weight(30, 100, 0.005, None, 24, None, NOW,
                                     candidate_count=40, cap_multiple=0.0) == pytest.approx(0.30)

  def test_cap_off_when_no_candidate_count():
      assert rarity.effective_weight(30, 100, 0.005, None, 24, None, NOW,
                                     candidate_count=0, cap_multiple=3.0) == pytest.approx(0.30)

  def test_cap_does_not_touch_boost():
      started = (NOW).isoformat()
      # capped base 0.075 * active boost multiplier
      w = rarity.effective_weight(30, 100, 0.005, 7.0, 24, started, NOW,
                                  candidate_count=40, cap_multiple=3.0)
      assert w == pytest.approx(0.075 * rarity.boost_multiplier(7.0, 24, started, NOW))
  ```
- [ ] **Step 2: Run to verify they fail** — `.venv/bin/python -m pytest tests/test_rarity.py -k cap -q` (expect `TypeError: unexpected keyword argument`).
- [ ] **Step 3: Implement** — in `effective_weight`, after `base = max(share, floor_weight)` and before the boost multiply:
  ```python
  if cap_multiple and candidate_count:
      ceiling = max(cap_multiple / candidate_count, floor_weight)
      base = min(base, ceiling)
  ```
  Add the two keyword params (`candidate_count: int = 0, cap_multiple: float = 0.0`) to the signature and extend the docstring to describe the plateau + the "ceiling never below floor" invariant.
- [ ] **Step 4: Run to verify they pass** — `.venv/bin/python -m pytest tests/test_rarity.py -k cap -q`.
- [ ] **Step 5: Wider run** — `.venv/bin/python -m pytest tests/test_rarity.py -q` (all existing `effective_weight`/`weighted_pick` tests still green — the no-op default guarantees it).
- [ ] **Step 6: Commit** — `feat(rarity): clamp share term to a fair-share ceiling in effective_weight (#198)`.

---

### Task 3: Thread the cap through `weighted_pick`

**Files:**
- Modify: `lfg_core/rarity.py` (`weighted_pick`)
- Test: `tests/test_rarity.py`

**Interfaces:** `weighted_pick` passes `candidate_count=len(rows)` and
`cap_multiple=config.RARITY_CAP_MULTIPLE` into each `effective_weight` call.

- [ ] **Step 1: Write the failing integration tests** — add to `tests/test_rarity.py`:
  ```python
  def test_weighted_pick_bounds_runaway(conn, monkeypatch):
      # seed one dominant trait + several small ones in the same (body, category)
      # (reuse the seeding style of test_weighted_pick_respects_weights)
      monkeypatch.setattr(rarity.config, "RARITY_CAP_MULTIPLE", 3.0)
      rng = _CountingRng(...)  # or a seeded random.Random for a statistical bound
      picks = [rarity.weighted_pick(conn, body, category, available,
                                    network="testnet", now=NOW, rng=random.Random(i))
               for i in range(400)]
      runaway_rate = picks.count(RUNAWAY) / len(picks)
      assert runaway_rate < UNCAPPED_RATE  # materially lower than without the cap

  def test_weighted_pick_cap_off_unchanged(conn, monkeypatch):
      monkeypatch.setattr(rarity.config, "RARITY_CAP_MULTIPLE", 0.0)
      # distribution matches today's proportional-with-floor result
  ```
  Model the assertion on the existing `test_weighted_pick_respects_weights` / `test_weighted_pick_denominator_spans_whole_category` fixtures; pick a concrete seeded distribution so the bound is deterministic.
- [ ] **Step 2: Run to verify they fail** — `.venv/bin/python -m pytest tests/test_rarity.py -k "runaway or cap_off_unchanged" -q` (fails: cap not yet wired into the picker, runaway rate too high).
- [ ] **Step 3: Implement** — in `weighted_pick`, change the weights comprehension to:
  ```python
  weights = [
      effective_weight(
          r[1], total, r[2], r[3], r[4], r[5], now,
          population_size=population,
          candidate_count=len(rows),
          cap_multiple=config.RARITY_CAP_MULTIPLE,
      )
      for r in rows
  ]
  ```
  (`config` is already imported in `rarity.py`.)
- [ ] **Step 4: Run to verify they pass** — same pytest command.
- [ ] **Step 5: Wider run** — `.venv/bin/python -m pytest tests/test_rarity.py -q`.
- [ ] **Step 6: Commit** — `feat(rarity): apply share ceiling in weighted_pick (#198)`.

---

### Task 4: Admin visibility (get_odds + trait_dashboard)

**Files:**
- Modify: `lfg_core/rarity.py` (`get_odds`)
- Modify: `scripts/trait_dashboard.py` (`/api/traits` row builder)
- Test: `tests/test_rarity.py` (get_odds) and `tests/test_trait_dashboard.py` (dashboard field)

**Interfaces:** `get_odds` computes weights with the cap so its displayed weight
matches the picker; `/api/traits` rows gain a `ceiling` (+ `capped: bool`) field
for a UI badge. `candidate_count` for admin views = number of **enabled** rows in
the `(body, category)` group (approximation, since the layer-store `available`
list isn't known here).

- [ ] **Step 1: Write the failing tests** —
  - `tests/test_rarity.py`: seed a group with one high-share enabled trait; with `RARITY_CAP_MULTIPLE` monkeypatched to a value that caps it, assert `get_odds` returns the plateaued weight (equal to the picker's), and that a below-ceiling trait's weight is unchanged.
  - `tests/test_trait_dashboard.py` (env-guard preamble at module top per the dashboard test convention): assert `GET /api/traits` rows include `ceiling` and `capped`, and that with the cap set a known over-represented seeded row reports `capped: true`.
- [ ] **Step 2: Run to verify they fail** — `.venv/bin/python -m pytest tests/test_rarity.py tests/test_trait_dashboard.py -k "cap or ceiling or capped" -q`.
- [ ] **Step 3: Implement** —
  - `get_odds`: compute `enabled_n = sum(1 for r in rows if r[6])` (the `enabled` column) and pass `candidate_count=enabled_n, cap_multiple=config.RARITY_CAP_MULTIPLE` to its `effective_weight` call. Keep the returned 5-tuple shape (weight now reflects the cap).
  - `scripts/trait_dashboard.py`: per `(body, category)` group compute `enabled_n`; pass `candidate_count=enabled_n, cap_multiple=config.RARITY_CAP_MULTIPLE` to `effective_weight`; add `"ceiling": max(config.RARITY_CAP_MULTIPLE / enabled_n, floor) if config.RARITY_CAP_MULTIPLE and enabled_n else None` and `"capped": bool(...)` to each row dict; render a "capped" badge client-side in the inline JS grid/list.
- [ ] **Step 4: Run to verify they pass** — same pytest command.
- [ ] **Step 5: Wider run** — `.venv/bin/python -m pytest tests/test_rarity.py tests/test_trait_dashboard.py -q`.
- [ ] **Step 6: Commit** — `feat(rarity): surface share ceiling in get_odds + trait dashboard (#198)`.

---

### Final Task: Full gate + PR

- [ ] Run the full suite: `.venv/bin/python -m pytest -q`.
- [ ] Run lint/format/type: `ruff check --fix . && ruff format . && .venv/bin/mypy lfg_core scripts` (or invoke the pre-push config directly). Fix everything; never `--no-verify`.
- [ ] Push the feature branch (the orchestrator/git owner performs git ops; do not push from an unrelated worktree without confirming `git branch --show-current`).
- [ ] `gh pr create` **non-draft** against `main`, body referencing #198 and summarizing the plateau design + the `RARITY_CAP_MULTIPLE=0` gate. **No AI attribution** in the commit trailers or PR body.
- [ ] Wait for **Greptile** and **CodeRabbit**; per repo rules, resolve every actionable finding — fix in code **and** reply on its thread naming the fixing commit — before merge. Re-review triggers: `@greptile-apps please re-review`, `@coderabbitai review`.
- [ ] Note in the PR: go-live is `RARITY_CAP_MULTIPLE=3.0` in the **mainnet** stack `.env` (proposed), tunable; leaving it unset keeps today's behavior. Whether to re-enable the manually-parked traits is a separate ops decision (open question in the spec).
