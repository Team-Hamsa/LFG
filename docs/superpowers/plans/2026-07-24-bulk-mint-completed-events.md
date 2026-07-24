# Bulk-mint `mint.completed` Events Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish one parity `mint.completed` firehose event per bulk-minted
unit (#215) so the X auto-poster (#41), Telegram announce, and Discord announce
fire for bulk editions exactly as they do for single mints — durably idempotent
across bulk-job resume.

**Architecture:** Three independent seams:
1. **Data-model** (`lfg_core/bulk_mint_flow.py`): persist `traits`/`body_type`/
   `video_url`/`published` on `Unit`, capture them in `_fulfill_unit`.
2. **Emission seam** (`lfg_core/bulk_mint_flow.py`): `emit_completed(job)` helper
   + an injected runtime `job.on_unit_complete` callback, called from
   `run_bulk_mint_job`.
3. **Service wiring** (`lfg_service/app.py`): `_publish_bulk_unit` builds the
   parity event via the existing `publish_event`/`enrich_minter_identity`, set
   as `job.on_unit_complete` at both `run_bulk_mint_job` launch sites
   (`handle_bulk_mint_start`, `resume_bulk_jobs`).

Consumers (`surfaces/x_bot/`, `surfaces/telegram_bot/events.py`,
`surfaces/discord_bot/events.py`) are UNCHANGED — the event type is the same
`mint.completed` they already consume.

**Tech Stack:** Python 3 / aiohttp / asyncio / pytest. No client (`app.js`)
changes. No new env vars. No XRPL transaction.

## Global Constraints

- **No XRPL tx is built here** — this only publishes an in-process bus event for
  an already-landed mint. The underlying mint tx already carries
  `SourceTag = 2606160021` and provenance memos via `mint_flow.mint_one_unit`;
  nothing in this plan touches the ledger, so SourceTag/memos are preserved by
  not being touched.
- **Pre-push gate must pass** (ruff `--fix`, ruff-format, mypy from `.venv`,
  gitleaks, pytest, validate-trait-config). Never `--no-verify`. In a worktree
  ensure the `.venv` symlink exists or the gate silently skips (see memory:
  worktree `.venv` trap).
- **No `app.js` change** in this work, so no `webapp/client/index.html`
  cache-buster bump is required. (If any client file is touched, bump it in the
  same commit.)
- **New test files** importing `lfg_core` at module top MUST copy the env-guard
  preamble (`os.environ.setdefault` for `XUMM_API_KEY`, `XUMM_API_SECRET`,
  `SEED`, `TOKEN_ISSUER_ADDRESS`, `TOKEN_CURRENCY_HEX`, `BUNNY_CDN_ACCESS_KEY`,
  `BUNNY_CDN_STORAGE_ZONE`, `LAYER_SOURCE`, `BUNNY_PULL_ZONE`) verbatim from
  `tests/test_bulk_mint_flow.py`, or full-suite collection order strands frozen
  config constants.
- **Publish failure must never break fulfillment** — mirror
  `_run_mint_session_and_publish` (log + continue).
- **No AI attribution** on commits or the PR (no `Co-Authored-By`, no
  "Generated with" footer).

---

### Task 1: Persist trait payload + published flag on `Unit`

**Files:**
- Modify: `lfg_core/bulk_mint_flow.py` (`Unit` dataclass; `_fulfill_unit`)
- Test: `tests/test_bulk_mint_events.py` (new)

**Interfaces:**
- Produces: `Unit` with defaulted `traits: dict[str,str]|None`,
  `body_type: str|None`, `video_url: str|None`, `published: bool=False`.
- Consumes: `mint_flow.UnitResult.{traits,body_type,video_url}`.

- [ ] **Step 1: Write the failing test(s)** — in `tests/test_bulk_mint_events.py`
  (env-guard preamble at top): a round-trip test that builds a `Unit(index=0,
  state=OFFERED, nft_id="X", traits={"Hat":"Wizard"}, body_type="ape",
  video_url=None, published=True)`, runs it through
  `BulkMintJob.serialize()`/`from_serialized()` and asserts the fields survive;
  and a backward-compat test that `Unit(**{"index":0,"state":"offered"})`
  yields `traits is None, published is False`.

  ```python
  from lfg_core import bulk_mint_flow
  def test_unit_new_fields_round_trip():
      u = bulk_mint_flow.Unit(index=0, state=bulk_mint_flow.OFFERED, nft_id="000...",
                              traits={"Hat": "Wizard"}, body_type="ape", published=True)
      # asdict → Unit(**...) is what serialize/from_serialized do
      from dataclasses import asdict
      u2 = bulk_mint_flow.Unit(**asdict(u))
      assert u2.traits == {"Hat": "Wizard"} and u2.body_type == "ape" and u2.published is True
  def test_unit_old_record_defaults():
      u = bulk_mint_flow.Unit(**{"index": 1, "state": "offered", "nft_id": "Y"})
      assert u.traits is None and u.body_type is None and u.video_url is None and u.published is False
  ```

- [ ] **Step 2: Run to verify they fail** — `.venv/bin/python -m pytest
  tests/test_bulk_mint_events.py -q` → expect `TypeError`/`AttributeError`
  (fields don't exist yet).

- [ ] **Step 3: Implement** — add the four defaulted fields to the `Unit`
  dataclass. In `_fulfill_unit`, in the `if res.nft_id:` branch, add
  `unit.traits = res.traits`, `unit.body_type = res.body_type`,
  `unit.video_url = res.video_url` alongside the existing assignments. No
  change to `serialize`/`from_serialized` (they use `asdict`/`Unit(**u)`).

- [ ] **Step 4: Run to verify they pass** — `.venv/bin/python -m pytest
  tests/test_bulk_mint_events.py -q`.

- [ ] **Step 5: Wider suite / regression run** — `.venv/bin/python -m pytest
  tests/test_bulk_mint_flow.py tests/test_bulk_mint_durability.py
  tests/test_bulk_mint_service.py -q` (Unit round-trips must still pass).

- [ ] **Step 6: Commit** — `feat(bulk): persist traits/body_type/video_url +
  published on bulk Unit (#253)`.

---

### Task 2: `emit_completed` helper + `on_unit_complete` seam in `run_bulk_mint_job`

**Files:**
- Modify: `lfg_core/bulk_mint_flow.py` (`BulkMintJob.__init__`; new
  `emit_completed`; `run_bulk_mint_job`)
- Test: `tests/test_bulk_mint_events.py`

**Interfaces:**
- Produces: `BulkMintJob.on_unit_complete: Callable[[Unit], Awaitable[None]] |
  None = None` (runtime-only, not serialized); `async def
  emit_completed(job) -> None`.
- Consumes: the injected `job.on_unit_complete`; `persist(job)`.

- [ ] **Step 1: Write the failing test(s)** — with a fake async callback that
  records units:
  - `emit_completed` publishes once per `OFFERED` unit with `nft_id`, sets
    `unit.published=True`; a `MINTED` (not OFFERED) unit is skipped.
  - Idempotency: a second `emit_completed` call publishes nothing (all
    `published`).
  - A callback that raises leaves `unit.published is False` and other units
    still publish.
  - Job-level: drive `run_bulk_mint_job` with `mint_flow.mint_one_unit`
    monkeypatched to return OFFERED `UnitResult`s and `on_unit_complete` set to
    a recorder — assert N recorded units; re-run (resume) records zero more.

  ```python
  import asyncio
  def test_emit_completed_once_per_offered_and_idempotent(monkeypatch, tmp_path):
      job = _make_job_with_units([  # helper: two OFFERED, one MINTED
          ("offered", "A"), ("offered", "B"), ("minted", "C")])
      seen = []
      async def cb(u): seen.append(u.nft_id)
      job.on_unit_complete = cb
      monkeypatch.setattr(bulk_mint_flow, "persist", lambda j: True)
      asyncio.get_event_loop().run_until_complete(bulk_mint_flow.emit_completed(job))
      assert sorted(seen) == ["A", "B"]
      asyncio.get_event_loop().run_until_complete(bulk_mint_flow.emit_completed(job))
      assert sorted(seen) == ["A", "B"]  # no re-emit
  ```

- [ ] **Step 2: Run to verify they fail** — `.venv/bin/python -m pytest
  tests/test_bulk_mint_events.py -q` → `AttributeError: emit_completed` /
  `on_unit_complete`.

- [ ] **Step 3: Implement** —
  - Add `self.on_unit_complete: Any = None` to `BulkMintJob.__init__` (with the
    other runtime-only attrs near `self.task`); do NOT add it to `serialize`/
    `from_serialized`.
  - Add `emit_completed(job)` as specified in the design: no-op when callback is
    None; loop units, skip unless `state == OFFERED and nft_id and not
    published`; `try: await job.on_unit_complete(unit) except Exception: log +
    continue`; on success `unit.published = True; persist(job)`.
  - In `run_bulk_mint_job`, call `await emit_completed(job)` (a) after
    `persist(job)` at the tail of the main `for unit in job.units:` fulfill
    loop, and (b) once after the bounded final re-offer pass. Keep it OUT of the
    early-park `return` paths (those units emit on the next resume, which is
    correct).

- [ ] **Step 4: Run to verify they pass** — `.venv/bin/python -m pytest
  tests/test_bulk_mint_events.py -q`.

- [ ] **Step 5: Wider suite / regression run** — `.venv/bin/python -m pytest
  tests/test_bulk_mint_flow.py tests/test_bulk_mint_durability.py -q`
  (fulfillment ordering + durability unaffected).

- [ ] **Step 6: Commit** — `feat(bulk): per-unit emit_completed seam with durable
  published guard (#253)`.

---

### Task 3: Service publisher + wiring at both launch sites

**Files:**
- Modify: `lfg_service/app.py` (new `_publish_bulk_unit`; set
  `job.on_unit_complete` in `handle_bulk_mint_start` and `resume_bulk_jobs`)
- Test: `tests/test_bulk_mint_publish.py` (new; pattern from
  `tests/test_mint_terminal_publish.py`)

**Interfaces:**
- Produces: `async def _publish_bulk_unit(job, unit) -> None` — publishes
  `mint.completed` with `data = {id, platform, state, nft_number, nft_id,
  image_url, video_url, traits, body_type, bulk: True}` via `publish_event(...,
  enrich_minter_identity(job.platform, job.discord_id, job.wallet_address),
  job.wallet_address, data)`.
- Consumes: existing `publish_event`, `enrich_minter_identity`,
  `bulk_mint_flow.run_bulk_mint_job`.

- [ ] **Step 1: Write the failing test(s)** — monkeypatch `server.publish_event`
  to capture calls (copy `_record_publishes` from
  `tests/test_mint_terminal_publish.py`). Build a `BulkMintJob` with OFFERED
  units carrying `traits`/`body_type`, call
  `server._publish_bulk_unit(job, unit)`, and assert exactly one captured event
  with `type == "mint.completed"`, `data["nft_id"]`, `data["traits"]`,
  `data["body_type"]`, `data["bulk"] is True`, and a non-None enriched
  `identity`. Also assert `handle_bulk_mint_start`/`resume_bulk_jobs` set
  `job.on_unit_complete` (drive the start handler in dev-mode auth like
  `test_bulk_mint_service.py` and assert the created job's callback is
  `server._publish_bulk_unit`).

- [ ] **Step 2: Run to verify they fail** — `.venv/bin/python -m pytest
  tests/test_bulk_mint_publish.py -q` → `AttributeError: _publish_bulk_unit`.

- [ ] **Step 3: Implement** — add `_publish_bulk_unit` near
  `_publish_mint_terminal` in `lfg_service/app.py`. In `handle_bulk_mint_start`,
  set `job.on_unit_complete = _publish_bulk_unit` immediately before
  `job.task = asyncio.create_task(bulk_mint_flow.run_bulk_mint_job(job))`. Do the
  same in `resume_bulk_jobs` before its `create_task`.

- [ ] **Step 4: Run to verify they pass** — `.venv/bin/python -m pytest
  tests/test_bulk_mint_publish.py -q`.

- [ ] **Step 5: Wider suite / regression run** — `.venv/bin/python -m pytest
  tests/test_bulk_mint_service.py tests/test_mint_terminal_publish.py
  tests/test_x_poster.py tests/test_telegram_events.py tests/test_discord_events.py
  -q` (consumer parity + single-mint publish unaffected).

- [ ] **Step 6: Commit** — `feat(events): publish per-unit mint.completed for bulk
  mints (#253)`.

---

### Task 4: Consumer-parity assertions

**Files:**
- Test: extend `tests/test_x_poster.py`, `tests/test_telegram_events.py`,
  `tests/test_discord_events.py`

- [ ] **Step 1: Write the tests** — feed each consumer a bulk-shaped
  `mint.completed` event (`data` incl. `"bulk": True`, `nft_id`, `nft_number`,
  `traits`, `body_type`, `image_url`) and assert parity: `poster.should_post`
  returns `f"mint:{nft_id}"`; `poster.compose` renders the header + traits line;
  TG/Discord `make_announcement` returns the `🎨 NFT #N minted by ...` string and
  `announcement_image` returns `video_url or image_url`. The `bulk` marker must
  not change any output (consumers ignore unknown keys).

- [ ] **Step 2: Run to verify** — `.venv/bin/python -m pytest tests/test_x_poster.py
  tests/test_telegram_events.py tests/test_discord_events.py -q`.

- [ ] **Step 3: Commit** — `test(events): consumer parity for bulk mint.completed
  (#253)`.

---

### Final Task: Full gate + PR

- [ ] Run the full suite: `.venv/bin/python -m pytest -q` (green).
- [ ] Run lint/type gate: `.venv/bin/ruff check . && .venv/bin/ruff format
  --check . && .venv/bin/mypy lfg_core lfg_service surfaces` (or trigger the
  pre-push hook by pushing; never `--no-verify`).
- [ ] Push the feature branch.
- [ ] `gh pr create` against `main` (Team-Hamsa/LFG), **non-draft**, no AI
  attribution in title/body. Reference #253, #215, #41.
- [ ] Wait for **Greptile** (check the `Greptile Review` check-run's
  `output.summary` — a clean review posts no comment) **and CodeRabbit**.
  Resolve every actionable finding: fix in code AND reply on its thread naming
  the fixing commit. Do not merge until both are triaged.
- [ ] Note ops decisions for the maintainer from the spec's open questions
  (deploy-time backlog behavior, X budget spike acceptance).
