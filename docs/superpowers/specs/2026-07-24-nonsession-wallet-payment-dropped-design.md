# Non-session-wallet mint payment — detect & surface, don't silently drop — design

**Date:** 2026-07-24
**Status:** shipped — PR #348, merged 2026-08-06 · historical draft, see §0
**Issue:** #275 (closed 2026-08-06)

## 0. What actually shipped (reviewed 2026-08-19)

Drafted 2026-07-24 during a backlog triage sweep and never merged before the
work shipped. Kept as the design record linked from #275.

- **Shipped in:** PR #348 "fix(mint): fail loudly on payment signed by
  non-session wallet" (`cf603dd`), merged 2026-08-06.
- **Followed this design:** closely. The guard sits exactly where this doc put
  it — in `lfg_core/mint_flow.py::update_scan_state`, inside the existing
  `AWAITING_PAYMENT`/`payment_uuid`/`not payment_signed` branch and ahead of the
  `qr_scanned` / `payment_signed` / `_capture_issued_token` updates — sets
  `FAILED` plus the new `MintSession.reason = "signer_mismatch"` and a
  signer-naming `error`, logs the WARNING carrying signer and txid, and calls
  `session.task.cancel()`. `to_dict()` exposes `reason`. Open questions 1 and 2
  were resolved as recommended: `FAILED` + `reason` rather than a new terminal
  state, and no auto-remediation of a same-identity signer (reconciliation stays
  manual).
- **Diverged:**
  - The shipped guard also unwinds reservations this draft does not mention:
    `release_sponsored_reservation(session, "signer_mismatch")` and
    `settle_headroom(session)` are called directly, because a task cancelled
    before it ever ran skips `run_mint_session`'s `finally`. Both are idempotent
    with the dying task's own settle. The covering test
    (`test_mismatched_signer_releases_headroom_reservation`) was added in
    response to CodeRabbit on PR #348.
  - The draft's inner `if session.state == AWAITING_PAYMENT:` re-check was
    dropped. The enclosing branch already establishes it and there is no `await`
    in between, so the re-check was dead code.
  - The plan's Task 2 — a service-level test asserting `GET /api/mint/{id}`
    returns `state=failed` / `reason=signer_mismatch` — was not written. All
    five tests in `tests/test_mint_signer_mismatch.py` drive
    `update_scan_state` directly. Two cases the draft did not call for were
    added instead: the headroom release above, and an unsigned payload carrying
    a foreign `account` that must not trip the guard.
  - Bulk mint stayed out of scope as drafted: `lfg_core/bulk_mint_flow.py` still
    relies solely on the #314 payload pin (`account=self.wallet_address`) with
    no signer poll of its own.
- **Live behaviour is now documented in:** the `#275` comment block in
  `lfg_core/mint_flow.py::update_scan_state` and PR #348's description.
  CLAUDE.md is not updated for this path — its only `signer_mismatch` text
  covers the marketplace buy flow.

## Problem

A user started an XRP mint in the Activity signed in as wallet `A`, but approved
the XUMM payment with a *different* Xaman account `B` active. 10 XRP landed on the
mint wallet (`tesSUCCESS`, validated), but the mint never fired and no error or
log line appeared — the session sat in `awaiting_payment` until the user
cancelled ~3.5 min later. Money taken, nothing minted, silently.

Root cause: the single-mint flow verifies payment purely by watching the ledger
for a Payment matching `(amount, source == session wallet, destination)`
(`lfg_core/mint_flow.py::run_mint_session` → `xrpl_ops.wait_for_payment(
expected_sender=session.wallet_address, ...)`). A payment from any other Xaman
account is invisible to that watcher, so the session can only ever
`payment_timeout` — with no signal that money arrived.

**What #314 already fixed (merged 2026-07-22):** `xumm_ops.create_payment_payload`
gained an `account` param and `mint_flow.prepare_payment` now passes
`account=self.wallet_address` (mint_flow.py:177). With `Account` set in the
txjson, Xaman *refuses to sign from any other account*, so the exact incident
cannot recur through the normal payload path.

**What is still missing (this issue's explicit "fix direction"):** the mint flow
never inspects the XUMM payload result. It relies *solely* on Account-pinning at
Xaman and on the on-ledger sender match. It does not do what the market/bid
flows already do — poll the payload, compare `response.account` to the session
wallet, and **fail loudly + log** on mismatch. So if a wrong-sender payment ever
does slip through (a regression that drops the `account` pin, a stale client
holding a pre-#314 payload, an ops/manual payment, or any Xaman edge case), the
mint flow still silently times out with zero server-side record that money
arrived. The mint flow is the odd one out: `lfg_core/market_flow.py` fails
`signer_mismatch` at lines 357, 436, 636, 693 *even though its payloads pin
`Account` too* — belt-and-suspenders is the established house pattern.

## Constraints discovered

- **Account-pinning is primary, not sufficient by itself.** The market/bid flows
  keep an independent signer check behind their pinned payloads; the mint flow
  should match that posture. This is defense-in-depth + an ops signal, not a
  replacement for #314.
- **The signer is already in hand.** `mint_flow.update_scan_state` already fetches
  the signed payload status (`xumm_ops.get_payload_status(session.payment_uuid)`)
  and reads `s["account"]`, `s["signed"]`, `s["user_token"]` — but only uses
  `account` inside `_capture_issued_token` to gate push-token capture, never to
  fail the session. The txid (`s.get("txid")`) is also available for ops
  reconciliation. `get_payload_status` returns `signed/opened/expired/account/
  txid/user_token` (see market_flow's usage).
- **Two writers to `session.state`.** The background `run_mint_session` task is
  blocked in `wait_for_payment` while the client-driven `update_scan_state`
  (called from `handle_mint_status`) polls the payload. A mismatched-signer
  payment never satisfies `wait_for_payment(expected_sender=wallet)`, so the
  background task will only ever `payment_timeout`. Detection must therefore
  happen in the poll path and *also stop the background wait* — mirroring
  `MintSession.cancel()`, which sets a terminal state and calls
  `self.task.cancel()` synchronously (no await between check and assignment, so
  on the single event loop it cannot race the task's own transitions).
- **Terminal-state guard.** The check must only fire while
  `state == AWAITING_PAYMENT and payment_uuid and not payment_signed` (exactly
  the existing `update_scan_state` guard), so it can never clobber a session that
  already matched its payment and left `awaiting_payment`.
- **No auto-refund in scope.** The 2026-07-17 incident is reconciled manually
  (mint/refund). The minimum bar per the issue is *detect + surface + log* so ops
  can reconcile; auto-refund/credit is a possible extension (see Out of scope).
- **SourceTag / memos / no-custody unchanged.** This change reads payload status
  and sets session state only — it builds no new transaction, so
  `SourceTag = 2606160021` and the provenance memos are untouched.
- **`payment_timeout` vs `failed`.** `TERMINAL_STATES` already includes both
  `FAILED` and `PAYMENT_TIMEOUT` (mint_flow.py:50); the client's
  `TERMINAL_MINT_STATES` (webapp/client/mint_pure.js:41) already renders `failed`
  and surfaces `session.error`. So a `FAILED` + human-readable `error` needs **no
  client change and no cache-buster bump**.

## Design

Add a signer-mismatch guard to the mint payment poll, mirroring
`market_flow.advance_*`:

1. **`MintSession.reason` field + `to_dict`** (mint_flow.py). Add
   `self.reason: str | None = None` next to `self.error` (line ~79), and expose
   `"reason": self.reason` in `to_dict()` (line ~243) — parity with
   `market_flow`'s `session.reason` for telemetry/clients that key on it.

2. **Detect in `update_scan_state`** (mint_flow.py:605). In the
   `AWAITING_PAYMENT` branch, after `s = get_payload_status(...)` resolves with
   `s["signed"]` true, before/instead of only capturing the token:

   ```python
   if s and s["signed"] and s.get("account") != session.wallet_address:
       # #275: Xaman signed the mint payment from a different account than the
       # session wallet. Account-pinning (#314) should prevent this; if it ever
       # slips through, the wrong wallet's money has already moved on-ledger and
       # wait_for_payment (expected_sender=wallet) will never match it. Fail
       # loudly + log the txid so ops can reconcile, instead of silently timing
       # out. Mirrors market_flow's signer_mismatch guard.
       signer = s.get("account")
       txid = s.get("txid")
       logging.warning(
           "mint session %s: payment signed by %s, not session wallet %s "
           "(txid=%s) — reconcile manually",
           session.id, signer, session.wallet_address, txid,
       )
       if session.state == AWAITING_PAYMENT:   # no await since the guard above
           session.state = FAILED
           session.reason = "signer_mismatch"
           session.error = (
               f"Payment was signed by a different Xaman account ({signer}) "
               f"than your session wallet. Your session wallet was not charged; "
               f"if another account paid, contact support to reconcile."
           )
           if session.task is not None:
               session.task.cancel()   # stop the doomed wait_for_payment
       return
   ```

   Keep the existing `qr_scanned` / `payment_signed` / `_capture_issued_token`
   updates for the matching-signer path unchanged (they already gate token
   capture on `s["account"] == session.wallet_address`).

3. **Background-task safety.** `session.task.cancel()` raises `CancelledError`
   (a `BaseException`) inside `wait_for_payment`; `run_mint_session`'s
   `except Exception` cannot catch it, so it propagates to the `finally` that
   settles the headroom reservation (release-only, since no mint landed) and the
   `FAILED`/`signer_mismatch` state set by the poll stands. This is exactly the
   proven `MintSession.cancel()` mechanism.

4. **Service surface.** No new endpoint. `handle_mint_status`
   (lfg_service/app.py:4103) already calls `update_scan_state` then returns
   `session.to_dict()`, so the client's next poll receives
   `{"state": "failed", "reason": "signer_mismatch", "error": "..."}` and renders
   the message via its existing `failed`-state path.

### On-ledger tx shape

None. This change builds and submits no transaction — it reads XUMM payload
status and mutates in-memory session state. `SourceTag` / memos untouched.

## Out of scope

- **Auto-refund or auto-credit of a wrong-account payment.** Reconciliation stays
  manual (per the issue's ops note). A future extension could, on a detected
  mismatch, look up whether the signer is another registered wallet of the same
  `identity` and either fulfil the mint or issue a `mint_credits` credit
  (`lfg_core/mint_credits.py`) — an open question below.
- **Widening `wait_for_payment` to accept any sender.** Detecting a wrong-*sender*
  payment that landed without a matching payload signer (e.g. a manual DEX/CLI
  payment) is a larger design; the payload-signer poll covers the Xaman flow that
  caused the incident.
- **Bulk mint (#215).** `bulk_mint_flow` pins `Account` the same way; a parallel
  signer-poll guard there is a follow-up, not this issue.

## Open questions / decisions for maintainer

1. **Auto-remediate same-identity signers?** The issue offers as optional:
   "accept the payment if the signer is a registered wallet of the same
   identity." Do we want the mismatch handler to resolve `identity` for the
   signer and, if it's the same user, fulfil/credit rather than fail? Or keep the
   minimum bar (fail + log, manual reconcile) for now?
2. **`failed` vs a new `signer_mismatch` state?** This design reuses `FAILED`
   with `reason="signer_mismatch"` (matches market_flow, needs no client change).
   Acceptable, or do you want a distinct terminal state for dashboards?
3. **Given #314 pins `Account`, is even this defense-in-depth worth shipping?**
   Recommendation: yes — it closes the mint/market inconsistency, and turns any
   future slip-through from a silent money-loss into a loud, logged, reconcilable
   failure. But it's a maintainer call whether to ship or close #275 as
   "prevented by #314."

## Testing

- **Unit (`tests/test_mint_signer_mismatch.py`):**
  - `update_scan_state` on a session in `AWAITING_PAYMENT` whose
    `get_payload_status` returns `{signed: True, account: "rOTHER...", txid: ...}`
    (monkeypatched) transitions the session to `FAILED`, sets
    `reason == "signer_mismatch"`, populates a human-readable `error` naming the
    signer, and cancels `session.task`.
  - Matching-signer path (`account == session.wallet_address`) leaves the session
    in `AWAITING_PAYMENT`, sets `payment_signed`, and captures the push token
    (existing behavior unregressed).
  - `to_dict()` includes `"reason"`.
- **Integration:** `handle_mint_status` for a session whose payload polls as
  mismatched-signer returns `200` with
  `{"state": "failed", "reason": "signer_mismatch"}` and a non-empty `error`.
- **Regression:** full `pytest` (esp. `tests/test_mint_cancel.py`,
  `tests/test_mint_active_resume.py`) — the task-cancel path is shared machinery.
- **Manual smoke (testnet):** start a mint as wallet A, approve the payment with a
  second Xaman account B active — expect the pay screen to flip to a clear
  "signed by a different account" failure (not a 5-minute timeout) and a
  `WARNING` log line carrying B's address and the payment txid.
