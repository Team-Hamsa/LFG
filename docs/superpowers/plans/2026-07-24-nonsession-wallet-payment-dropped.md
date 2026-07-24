# Non-session-wallet mint payment — detect & surface Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a mint's XUMM payment payload is signed by a Xaman account other
than the session wallet, fail the mint session loudly (`failed` /
`reason="signer_mismatch"`) with a human-readable error and a server-side
`WARNING` carrying the signer + payment txid — instead of silently timing out.
Defense-in-depth behind the #314 `Account`-pin, matching `market_flow`'s existing
`signer_mismatch` posture.

**Architecture:** One seam — `lfg_core/mint_flow.py`. The detection lives in the
already-existing payload poll (`update_scan_state`), which the service's
`handle_mint_status` already calls; the terminal state + task-cancel reuse the
proven `MintSession.cancel()` mechanism. No new endpoint, no transaction, no
client change (the client already renders `failed` + `error`).

**Tech Stack:** Python 3 / aiohttp / asyncio / pytest; no client change (no
cache-buster bump required).

## Global Constraints

- `SourceTag = 2606160021` + provenance memos: this change builds **no**
  transaction, so both are untouched — do not add or alter any tx path.
- The pre-push gate (ruff --fix, ruff-format, mypy from `.venv`, gitleaks,
  pytest, validate-trait-config) must pass; **never** `--no-verify`.
- No `app.js` / client change is expected. If any `webapp/client/*` file is
  edited, bump the cache-buster in `webapp/client/index.html` in the SAME commit
  (not expected here).
- New test files that import `lfg_core` at module top MUST carry the env-guard
  preamble (`os.environ.setdefault(...)` for `LAYER_SOURCE`, `BUNNY_PULL_ZONE`,
  `XUMM_*`, `SEED`, `TOKEN_*`, `BUNNY_CDN_*`) — copy verbatim from
  `tests/test_mint_cancel.py` lines 11-24.

---

### Task 1: Signer-mismatch detection in the mint payment poll

**Files:**
- Modify: `lfg_core/mint_flow.py` (`MintSession.__init__`, `to_dict`,
  `update_scan_state`)
- Test: `tests/test_mint_signer_mismatch.py` (new)

**Interfaces:**
- Produces: `MintSession.reason: str | None` (new attr; `"signer_mismatch"` on a
  foreign-signed payment), exposed in `to_dict()` as `"reason"`.
- Consumes: `xumm_ops.get_payload_status(uuid)` →
  `{signed, opened, expired, account, txid, user_token}` (existing shape, same as
  `market_flow` consumes).

- [ ] **Step 1: Write the failing test(s)** — `tests/test_mint_signer_mismatch.py`
  with the env-guard preamble from `tests/test_mint_cancel.py`. Build a
  `MintSession` (discord_id, wallet_address="rSESSION...", platform="discord"),
  force `state = mint_flow.AWAITING_PAYMENT`, set `payment_uuid = "uuid-1"`,
  attach a dummy cancellable `session.task`. Monkeypatch
  `mint_flow.xumm_ops.get_payload_status` (async) to return
  `{"signed": True, "opened": True, "expired": False, "account": "rOTHER...",
  "txid": "TX123", "user_token": None}`. Assert after
  `await mint_flow.update_scan_state(session)`:
  - `session.state == mint_flow.FAILED`
  - `session.reason == "signer_mismatch"`
  - `session.error` is truthy and contains `"rOTHER"`
  - the dummy task received `.cancel()`
  Second test — matching signer: `account == "rSESSION..."` →
  `session.state == mint_flow.AWAITING_PAYMENT`, `session.payment_signed is True`,
  task NOT cancelled. Third test — `to_dict()` contains key `"reason"`.
  Snippet:
  ```python
  import asyncio
  import lfg_core.mint_flow as mint_flow

  class _DummyTask:
      def __init__(self): self.cancelled = False
      def cancel(self): self.cancelled = True

  def _mk_session(wallet="rSESSION"):
      s = mint_flow.MintSession(discord_id="d1", wallet_address=wallet,
                                platform="discord")
      s.state = mint_flow.AWAITING_PAYMENT
      s.payment_uuid = "uuid-1"
      s.task = _DummyTask()
      return s

  def test_foreign_signer_fails_loudly(monkeypatch):
      s = _mk_session()
      async def fake_status(uuid):
          return {"signed": True, "opened": True, "expired": False,
                  "account": "rOTHER", "txid": "TX123", "user_token": None}
      monkeypatch.setattr(mint_flow.xumm_ops, "get_payload_status", fake_status)
      asyncio.get_event_loop().run_until_complete(mint_flow.update_scan_state(s))
      assert s.state == mint_flow.FAILED
      assert s.reason == "signer_mismatch"
      assert "rOTHER" in (s.error or "")
      assert s.task.cancelled is True
  ```
  (Check the real `MintSession.__init__` signature with `grep -n "def __init__"
  lfg_core/mint_flow.py` and adjust kwargs to match.)

- [ ] **Step 2: Run to verify they fail** —
  `.venv/bin/python -m pytest tests/test_mint_signer_mismatch.py -q`
  Expect failures: `AttributeError: reason` / state still `awaiting_payment`
  (guard not yet implemented).

- [ ] **Step 3: Implement** — in `lfg_core/mint_flow.py`:
  - In `MintSession.__init__`, next to `self.error = None`, add
    `self.reason: str | None = None`.
  - In `to_dict()`, add `"reason": self.reason,` next to `"error"`.
  - In `update_scan_state`, inside the `AWAITING_PAYMENT` branch, after
    `s = await xumm_ops.get_payload_status(session.payment_uuid)` and `if s:`,
    add a foreign-signer guard BEFORE the existing `qr_scanned`/`payment_signed`
    assignment:
    ```python
    if s.get("signed") and s.get("account") != session.wallet_address:
        signer, txid = s.get("account"), s.get("txid")
        logging.warning(
            "mint session %s: payment signed by %s, not session wallet %s "
            "(txid=%s) — reconcile manually",
            session.id, signer, session.wallet_address, txid,
        )
        # AWAITING_PAYMENT guard + synchronous assignment => no race with the
        # background wait_for_payment (same invariant as MintSession.cancel()).
        if session.state == AWAITING_PAYMENT:
            session.state = FAILED
            session.reason = "signer_mismatch"
            session.error = (
                f"Payment was signed by a different Xaman account ({signer}) "
                f"than your session wallet. Your session wallet was not charged; "
                f"contact support if another account paid."
            )
            if session.task is not None:
                session.task.cancel()
        return
    ```
    Leave the existing matching-signer updates (`qr_scanned`, `payment_signed`,
    `_capture_issued_token`) unchanged below it. Confirm `logging` is already
    imported at the top of the module (it is — used by `run_mint_session`).

- [ ] **Step 4: Run to verify they pass** —
  `.venv/bin/python -m pytest tests/test_mint_signer_mismatch.py -q` → all green.

- [ ] **Step 5: Wider suite / regression run** —
  `.venv/bin/python -m pytest tests/test_mint_cancel.py tests/test_mint_active_resume.py tests/test_service_mint_platform.py -q`
  (task-cancel + status-poll machinery is shared). Expect all pass.

- [ ] **Step 6: Commit** — `fix(mint): fail loudly on a non-session-wallet
  payment signer instead of silent timeout (#275)`

---

### Task 2: Service-level surfacing test

**Files:**
- Test: `tests/test_mint_signer_mismatch.py` (extend) OR
  `tests/test_service_mint_platform.py` (follow its harness)

**Interfaces:**
- Consumes: `GET /api/mint/{session_id}` (`handle_mint_status`) — already calls
  `update_scan_state` then returns `to_dict()`.

- [ ] **Step 1: Write the failing test** — following the auth/session harness in
  `tests/test_service_mint_platform.py` (uses `make_session_token`,
  `lfg_service.app`), register a mint session in `app.mint_sessions` in
  `AWAITING_PAYMENT` with `payment_uuid` set, monkeypatch
  `mint_flow.xumm_ops.get_payload_status` to a foreign-signer response, then hit
  `handle_mint_status` (via the test client / direct handler call the other
  tests use). Assert the JSON body has `state == "failed"`,
  `reason == "signer_mismatch"`, and a non-empty `error`.

- [ ] **Step 2: Run to verify it fails** —
  `.venv/bin/python -m pytest tests/test_mint_signer_mismatch.py -q -k service`

- [ ] **Step 3: Implement** — no new production code expected; the Task 1 change
  flows through `handle_mint_status` unchanged. If the assertion fails only on a
  serialization gap (e.g. `reason` missing from `to_dict`), fix there.

- [ ] **Step 4: Run to verify it passes** — same command → green.

- [ ] **Step 5: Wider suite** — `.venv/bin/python -m pytest tests/ -q -k mint`

- [ ] **Step 6: Commit** — `test(mint): assert /api/mint status surfaces
  signer_mismatch (#275)`

---

### Final Task: Full gate + PR

- [ ] Run the full suite: `.venv/bin/python -m pytest -q`
- [ ] Run linters/type check: `.venv/bin/ruff check . && .venv/bin/ruff format
  --check . && .venv/bin/mypy lfg_core lfg_service` (or let the pre-push hook run
  them). Never `--no-verify`.
- [ ] Confirm no `webapp/client/*` change was made (so no cache-buster bump is
  owed); if one crept in, bump `webapp/client/index.html` in the same commit.
- [ ] Push the branch and open a **non-draft** PR to `Team-Hamsa/LFG` per repo
  rules — **no AI attribution** in the commit trailer or PR body. Body: what/why,
  link #275, note this is defense-in-depth behind #314's `Account`-pin and the
  `market_flow` signer-mismatch parity, and that reconciliation stays manual.
- [ ] Wait for **Greptile** and **CodeRabbit**; resolve every actionable finding
  (fix in code AND reply on its thread naming the fixing commit) before merge.
  Re-review triggers: `@greptile-apps please re-review` / `@coderabbitai review`.
