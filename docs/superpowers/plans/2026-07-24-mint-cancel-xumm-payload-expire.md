# Cancel/expire the XUMM payload on mint-session cancel — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a user cancels a mint (and bulk-mint, and swap) pay screen, the
open XUMM payment payload is best-effort cancelled so a stale QR/deeplink can no
longer be signed in Xaman. A payload-cancel failure must never block the session
cancel / per-user lock release.

**Architecture:** Two independent seams.
1. **Service helper + mint/bulk wiring** — `lfg_service/app.py`: a fire-and-forget
   `_spawn_payload_cancel(uuid)` reusing the existing
   `xumm_ops.cancel_xumm_payload` (PR #260), called from `handle_mint_cancel` and
   `handle_bulk_mint_cancel`. `MintSession.payment_uuid` and
   `BulkMintJob.payment_uuid` already exist.
2. **Swap uuid capture + wiring** — `lfg_core/swap_flow.py` must first *store* the
   fee-payload uuid (it doesn't today), then `handle_swap_cancel` spawns the same
   helper.

**Tech Stack:** Python 3 / aiohttp / asyncio / pytest. No client change (no
app.js touch), no data-model change, no new config.

## Global Constraints

- **No XRPL tx is built** — `DELETE /platform/payload/{uuid}` is a XUMM
  control-plane call. SourceTag=2606160021 and provenance memos are irrelevant
  here and remain untouched everywhere else.
- **Fail-safe:** `xumm_ops.cancel_xumm_payload` swallows all errors and returns a
  bool (never raises); the wiring ignores the result. The session cancel / lock
  release path is unchanged and must remain synchronous and immediate.
- **Fire-and-forget:** the delete runs in a retained background task; never await
  it inline (it does a 10s-timeout blocking `requests.delete` on a worker thread).
- Pre-push gate (ruff `--fix`, ruff-format, mypy from `.venv`, gitleaks, pytest,
  validate-trait-config) must pass. Never `--no-verify`.
- No `app.js` / client change → **no cache-buster bump needed** for this work.
- Every new test file (or new module-top import of `lfg_core`) carries the
  tests/ env-guard preamble (`os.environ.setdefault` for `XUMM_API_KEY`,
  `XUMM_API_SECRET`, `SEED`, `TOKEN_ISSUER_ADDRESS`, `TOKEN_CURRENCY_HEX`,
  `BUNNY_CDN_ACCESS_KEY`, `BUNNY_CDN_STORAGE_ZONE`, `LAYER_SOURCE=local`,
  `BUNNY_PULL_ZONE`) — extending the existing `tests/test_mint_cancel.py` inherits
  it already.

---

### Task 1: Service helper + mint cancel wiring

**Files:**
- Modify: `lfg_service/app.py` (add `_spawn_payload_cancel`; call it in
  `handle_mint_cancel`)
- Test: `tests/test_mint_cancel.py` (extend)

**Interfaces:**
- Produces: `_spawn_payload_cancel(uuid: str | None) -> None` (module-level in
  app.py; no-op when `uuid` falsy; spawns `xumm_ops.cancel_xumm_payload(uuid)`
  into a retained `set[asyncio.Task]`).
- Consumes: existing `lfg_core.xumm_ops.cancel_xumm_payload(uuid) -> bool`,
  `MintSession.payment_uuid`.

- [ ] **Step 1: Write the failing test(s)** — in `tests/test_mint_cancel.py`
  (env-guard preamble already present at module top). Add, using the existing
  `_MockRequest`/`_run`/`_token` helpers:
  ```python
  def test_mint_cancel_cancels_open_payload(monkeypatch):
      monkeypatch.setattr(app.config, "WEBAPP_DEV_MODE", False)
      seen = []
      async def fake_cancel(uuid):
          seen.append(uuid); return True
      monkeypatch.setattr(app.xumm_ops, "cancel_xumm_payload", fake_cancel)

      async def scenario():
          s = mint_flow.MintSession("55", "rA", platform="discord")
          s.payment_uuid = "PAYUUID"
          app.mint_sessions[s.id] = s
          try:
              resp = await app.handle_mint_cancel(_MockRequest(s.id, _token()))
              assert resp.status == 200
              await asyncio.sleep(0)  # let the fire-and-forget task run
              assert seen == ["PAYUUID"]
          finally:
              app.mint_sessions.pop(s.id, None)
      _run(scenario())

  def test_mint_cancel_no_payload_uuid_skips_cancel(monkeypatch):
      monkeypatch.setattr(app.config, "WEBAPP_DEV_MODE", False)
      seen = []
      async def fake_cancel(uuid):
          seen.append(uuid); return True
      monkeypatch.setattr(app.xumm_ops, "cancel_xumm_payload", fake_cancel)
      s = mint_flow.MintSession("55", "rA", platform="discord")
      s.payment_uuid = None
      app.mint_sessions[s.id] = s
      try:
          resp = _run(app.handle_mint_cancel(_MockRequest(s.id, _token())))
          assert resp.status == 200
          assert seen == []
      finally:
          app.mint_sessions.pop(s.id, None)

  def test_mint_cancel_payload_failure_does_not_block(monkeypatch):
      monkeypatch.setattr(app.config, "WEBAPP_DEV_MODE", False)
      async def boom(uuid):
          raise RuntimeError("xumm down")
      monkeypatch.setattr(app.xumm_ops, "cancel_xumm_payload", boom)
      s = mint_flow.MintSession("55", "rA", platform="discord")
      s.payment_uuid = "PAYUUID"
      app.mint_sessions[s.id] = s
      try:
          resp = _run(app.handle_mint_cancel(_MockRequest(s.id, _token())))
          assert resp.status == 200
          assert s.state == mint_flow.CANCELLED
          assert app._active_session(app.mint_sessions, mint_flow.TERMINAL_STATES, "55", "discord") is None
      finally:
          app.mint_sessions.pop(s.id, None)
  ```
  (`app.xumm_ops` is the module already imported in app.py — confirm the import
  name with `grep -n "import.*xumm_ops" lfg_service/app.py` and adjust the
  monkeypatch target if it's aliased.)
- [ ] **Step 2: Run to verify they fail** —
  `.venv/bin/python -m pytest tests/test_mint_cancel.py -k "payload" -q`
  Expect: `test_mint_cancel_cancels_open_payload` fails (`seen == []`, helper not
  wired). The no-uuid and failure tests may pass trivially before wiring; they
  lock in behavior after.
- [ ] **Step 3: Implement** — in `lfg_service/app.py`:
  - Add near the other background-task spawns:
    ```python
    _payload_cancel_tasks: set[asyncio.Task] = set()

    def _spawn_payload_cancel(uuid: str | None) -> None:
        """Best-effort background cancel of an open XUMM payload (#152). No-op
        when uuid is None. Never blocks the caller: cancel_xumm_payload
        swallows all errors and returns a bool, so a failure here can never
        stop a session cancel / lock release."""
        if not uuid:
            return
        task = asyncio.get_event_loop().create_task(xumm_ops.cancel_xumm_payload(uuid))
        _payload_cancel_tasks.add(task)
        task.add_done_callback(_payload_cancel_tasks.discard)
    ```
    (If a test monkeypatches `cancel_xumm_payload` to *raise* synchronously
    before returning a coroutine, `create_task` would raise — but the helper is
    `async def`, so calling it returns a coroutine and any raise happens inside
    the task, surfaced only via the done-callback. To be safe against a raising
    fake, the done-callback may `task.exception()`-swallow; simplest is to keep
    the fake `async def` as in Step 1.)
  - In `handle_mint_cancel`, after the `if not session.cancel(): return 409`
    guard and before `session.mark_published()`:
    ```python
    _spawn_payload_cancel(session.payment_uuid)  # #152
    ```
- [ ] **Step 4: Run to verify they pass** —
  `.venv/bin/python -m pytest tests/test_mint_cancel.py -q`
- [ ] **Step 5: Wider suite / regression run** —
  `.venv/bin/python -m pytest tests/test_mint_cancel.py tests/test_mint_terminal_publish.py tests/test_xumm_open_payload_cap.py -q`
- [ ] **Step 6: Commit** —
  `feat(mint): cancel the open XUMM payload on mint-session cancel (#152)`

---

### Task 2: Bulk-mint cancel wiring

**Files:**
- Modify: `lfg_service/app.py` (`handle_bulk_mint_cancel`)
- Test: `tests/test_mint_cancel.py` (or a bulk-focused test file with the
  env-guard preamble)

**Interfaces:** Consumes `_spawn_payload_cancel` (Task 1) and
`BulkMintJob.payment_uuid` (`lfg_core/bulk_mint_flow.py:101`, already stored).

- [ ] **Step 1: Write the failing test(s)** — spawn a bulk job in
  `AWAITING_PAYMENT`, set `job.payment_uuid = "BULKUUID"`, register it in
  `app.bulk_sessions`, monkeypatch `app.xumm_ops.cancel_xumm_payload`, call
  `app.handle_bulk_mint_cancel`, `await asyncio.sleep(0)`, assert the fake saw
  `"BULKUUID"`. Add a `payment_uuid=None` → not-called case. (Use
  `bulk_mint_flow.BulkMintJob(...)`; confirm its ctor args with
  `grep -n "def __init__" lfg_core/bulk_mint_flow.py`.)
- [ ] **Step 2: Run to verify they fail** —
  `.venv/bin/python -m pytest tests/test_mint_cancel.py -k "bulk and payload" -q`
- [ ] **Step 3: Implement** — in `handle_bulk_mint_cancel`, after
  `if not job.cancel(): return 409` and before `job.mark_published()`:
  `_spawn_payload_cancel(job.payment_uuid)  # #152`
- [ ] **Step 4: Run to verify they pass** — same `-k` command.
- [ ] **Step 5: Wider suite / regression run** —
  `.venv/bin/python -m pytest tests/test_mint_cancel.py tests/test_bulk_mint_supply_cap.py -q`
- [ ] **Step 6: Commit** —
  `feat(mint): cancel the open XUMM payload on bulk-mint cancel (#152)`

---

### Task 3: Swap fee-payload uuid capture + cancel wiring

**Files:**
- Modify: `lfg_core/swap_flow.py` (store `payment_uuid` from the fee payload;
  add it to `to_dict`/`from_dict` if the session is serialized)
- Modify: `lfg_service/app.py` (`handle_swap_cancel`)
- Test: `tests/test_swap_*` (a focused test with the env-guard preamble) or
  extend an existing swap test

**Interfaces:**
- Produces: `SwapSession.payment_uuid: str | None` (new attribute, set in
  `prepare_payment`/`_collect_modify_fee` from `payload.get("uuid")`).
- Consumes: `_spawn_payload_cancel` (Task 1).

- [ ] **Step 1: Write the failing test(s)** — (a) drive
  `SwapSession.prepare_payment` (or the fee-collect path) with a monkeypatched
  `xumm_ops.create_payment_payload` returning a dict incl.
  `{"uuid": "SWAPUUID", "xumm_url": "...", "push": None}` and assert
  `session.payment_uuid == "SWAPUUID"`; (b) `handle_swap_cancel` on an
  `AWAITING_PAYMENT` swap session with `payment_uuid` set spawns
  `cancel_xumm_payload("SWAPUUID")`. Confirm the exact fee-payload build site
  with `grep -n "create_payment_payload" lfg_core/swap_flow.py` (line ~155).
- [ ] **Step 2: Run to verify they fail** —
  `.venv/bin/python -m pytest tests/test_swap_cancel_payload.py -q`
  (or whichever file the tests land in). Expect: `payment_uuid` is unset →
  AttributeError/None, and the cancel isn't spawned.
- [ ] **Step 3: Implement** —
  - In `lfg_core/swap_flow.py`: initialize `self.payment_uuid: str | None = None`
    in `__init__`, and in the fee-payload build (`if payload:` block, ~line 167)
    add `self.payment_uuid = payload.get("uuid")`. If the swap session is
    persisted (check for `to_dict`/`from_dict`/journal), thread `payment_uuid`
    through both like `payment_push` is; if not persisted, in-memory is enough.
  - In `lfg_service/app.py::handle_swap_cancel`, after
    `if not session.cancel(): return 409` and before `session.mark_published()`:
    `_spawn_payload_cancel(session.payment_uuid)  # #152`
- [ ] **Step 4: Run to verify they pass** — the swap test command from Step 2.
- [ ] **Step 5: Wider suite / regression run** —
  `.venv/bin/python -m pytest tests/ -k "swap or mint_cancel" -q`
- [ ] **Step 6: Commit** —
  `feat(swap): capture + cancel the fee payload on swap cancel (#152)`

---

### Final Task: Full gate + PR

- [ ] Run the full suite: `.venv/bin/python -m pytest -q`
- [ ] Run linters/types locally as the pre-push gate will:
  `.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy lfg_core lfg_service`
- [ ] Push the branch (pre-push hook runs the full gate — never `--no-verify`).
- [ ] `gh pr create` against `Team-Hamsa/LFG`, **non-draft**, body referencing
  #152 (and #148/#260 for context). **No AI attribution** in the commit trailers
  or PR body (per repo convention).
- [ ] Wait for **Greptile** and **CodeRabbit**; resolve every actionable finding
  — fix in code *and* reply on the finding's thread naming the fixing commit —
  before merge. (Greptile "clean" shows as no comment; verify via the
  `Greptile Review` check-run summary.)
