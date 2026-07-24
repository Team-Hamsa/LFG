# Cancel/expire the XUMM payload on mint-session cancel — design

**Date:** 2026-07-24
**Status:** draft (triage — needs maintainer review)
**Issue:** #152

## Problem

Follow-up from #141 / PR #148 (mint pay-screen cancel). When a user backs out of
the mint pay screen, `POST /api/mint/{session_id}/cancel`
(`lfg_service/app.py::handle_mint_cancel`) calls `session.cancel()`
(`lfg_core/mint_flow.py::MintSession.cancel`), which marks the session
`CANCELLED`, cancels the background payment-wait task, and settles the headroom
reservation — but it **never touches the open XUMM payload**. The payment
payload created in `MintSession.prepare_payment` (stored on
`session.payment_uuid`, line ~181) stays **live and signable in the user's Xaman
app** until it self-expires via `_create_xumm_payload`'s standard 15-minute
`expire` (`DEFAULT_EXPIRE_MINUTES`, `lfg_core/xumm_ops.py`).

Consequence: a user who cancels and *later* signs the stale payload pays with
**no session attached**. The next mint's payment backfill window
(`wait_for_payment`, `not_before = created_at − 10s`) only rescues that payment
if a new session happens to start within ~10 seconds; otherwise the payment
lands unmatched and support reconciles it by hand.

The cancel *mechanism* already exists: `lfg_core/xumm_ops.py::cancel_xumm_payload(uuid)`
(added by the XUMM-429 hardening, PR #260 — `DELETE /platform/payload/{uuid}`,
returns `True` only when XUMM confirms `cancelled`, swallows transport/429 errors
and returns `False`, never raises). It is used today only by
`scripts/cancel_xumm_payloads.py` for backlog cleanup. **It is not wired to any
session cancel path.** This issue is that wiring.

## Constraints discovered

- **Fail-safe: cancel must never be blocked by the payload delete.** PR #148's
  contract is that backing out releases the per-user mint lock *immediately*
  (`session.cancel()` flips state synchronously so `_active_session` frees the
  lock in the same tick). A slow or failing `DELETE /payload` must not delay or
  block that. `cancel_xumm_payload` already returns `False` (never raises) on
  transport error / 429 / already-resolved, so the caller just ignores the
  result.
- **Response latency.** `cancel_xumm_payload` does a blocking
  `requests.delete(..., timeout=10)` on a worker thread. Awaiting it inline
  before returning the HTTP response would add up to 10s to a *cancel* click.
  The lock is already released synchronously, so the delete should run
  **fire-and-forget** (background task), not inline.
- **`payment_uuid` may be `None`.** If `prepare_payment` failed to create a
  payload (backoff/outage — `session.payment_uuid is None`, the same guard at
  app.py ~3411), there is nothing to cancel. Skip when `None`.
- **Idempotency / double-cancel.** `handle_mint_cancel` on an already-terminal
  session is a no-op (returns early at `session.state in TERMINAL_STATES`), so
  the payload delete only fires on the *first, real* cancel — no double DELETE.
- **Rate-limit budget.** `cancel_xumm_payload` self-checks `rate_limited()` and
  no-ops during a XUMM 429 cooldown, so a burst of cancels can't deepen a
  rate-limit hole. Nothing extra needed here.
- **No new tx / no SourceTag concern.** `DELETE /platform/payload` is a XUMM
  control-plane call, not an XRPL transaction — no SourceTag, no provenance
  memos. (SourceTag=2606160021 + memos remain untouched; no ledger tx is built.)
- **No custody change.** Delivery/cancel only; the no-custody signing model is
  unchanged.

## Design

Small, single-seam change. No new module, no data-model change, no config.

### 1. A tiny fire-and-forget helper in `lfg_service/app.py`

Add a module-level helper next to the other background-task spawns:

```python
# Retain references so fire-and-forget tasks aren't GC'd mid-flight.
_payload_cancel_tasks: set[asyncio.Task] = set()

def _spawn_payload_cancel(uuid: str | None) -> None:
    """Best-effort background cancel of an open XUMM payload (#152). No-op
    when uuid is None (payload was never created). Never blocks the caller:
    xumm_ops.cancel_xumm_payload swallows all errors and returns a bool, so
    a failure here can never stop a session cancel / lock release."""
    if not uuid:
        return
    task = asyncio.get_event_loop().create_task(xumm_ops.cancel_xumm_payload(uuid))
    _payload_cancel_tasks.add(task)
    task.add_done_callback(_payload_cancel_tasks.discard)
```

(Mirrors the existing `create_task` + set-discard pattern already used for
sweeps and `_run_and_close` in app.py.)

### 2. Wire it into `handle_mint_cancel`

After the session transitions to `CANCELLED` and the lock is released, spawn the
delete:

```python
    if not session.cancel():
        return web.json_response({"error": "session is past payment"}, status=409)
    _spawn_payload_cancel(session.payment_uuid)   # #152
    session.mark_published()
    return web.json_response(session.to_dict())
```

Placement is *after* `session.cancel()` (lock already free) and *before* the
response — the spawn itself is O(1) and non-blocking.

### 3. Parallel wiring (same helper, in-scope siblings)

The issue explicitly calls out reusing the helper wherever a session is
deliberately torn down with a live payload. Two are trivial and safe:

- **Bulk mint** — `handle_bulk_mint_cancel` (app.py ~3601). `BulkMintJob` stores
  `self.payment_uuid` (`lfg_core/bulk_mint_flow.py:101`). Add
  `_spawn_payload_cancel(job.payment_uuid)` after `job.cancel()`.
- **Swap** — `handle_swap_cancel` (app.py ~4257). ⚠️ **`SwapSession` does not
  currently persist the fee-payload uuid** — `_collect_modify_fee` /
  `prepare_payment` in `lfg_core/swap_flow.py` reads `payload["xumm_url"]` and
  `payload.get("push")` but never stores `payload.get("uuid")`. Wiring swap
  therefore requires first adding `self.payment_uuid = payload.get("uuid")` in
  swap_flow (and to `to_dict`/`from_dict` if the session is persisted). This is
  a one-line capture plus the spawn.

Market list/buy/cancel and the trait-sell wizard are noted in the issue as
"worth wiring" but are **out of scope** for this change (see below) — their
payloads are built lazily on click and already carry short expiries, and their
cancel paths differ.

### Data flow (mint)

```
Xaman shows live payload  ──prepare_payment──▶ session.payment_uuid = <uuid>
user taps "Cancel"        ──POST /mint/{id}/cancel──▶ handle_mint_cancel
                                                       session.cancel()  (lock freed, task killed)
                                                       _spawn_payload_cancel(uuid)  ─background─▶
                                                          xumm_ops.cancel_xumm_payload(uuid)
                                                          DELETE /platform/payload/{uuid}
                                                       return 200 immediately
Xaman now shows the payload as no-longer-signable.
```

## Out of scope

- Market (`ListSession`/`BuySession`/`CancelSession`) and trait-sell wizard
  payload cancellation — separate cancel semantics; track as a follow-up if
  desired.
- Changing the 15-minute default `expire` on payloads (the backstop stays).
- Cancelling payloads on *non-user* terminal transitions (payment timeout,
  pipeline failure) — those payloads expire naturally and there is no stale-QR
  risk because the wallet either paid (session ran to completion) or the wait
  timed out on the same expiry clock.
- The accept-offer payload — a cancel is only legal `AWAITING_PAYMENT`, before
  any accept payload exists, so `accept_uuid` is always `None` at cancel time.

## Open questions / decisions for maintainer

1. **Fire-and-forget vs. inline await.** Design picks fire-and-forget (snappy
   cancel response). Acceptable that in the rare case the process dies within
   ~10s of a cancel the DELETE is lost and the payload expires on its own 15-min
   clock instead? (Recommended: yes — the 15-min expire is the existing
   backstop and this is strictly better than today.)
2. **Include swap + bulk now, or mint-only?** Bulk is free (uuid already
   stored). Swap needs the extra one-line uuid capture in `swap_flow`. Ship all
   three, or keep this PR mint-only and file swap/bulk as a fast-follow? (Design
   assumes: mint + bulk in this PR; swap in the same PR with the uuid-capture
   line, since it's small and the issue asks for it.)
3. **Any need to record the cancel outcome?** `cancel_xumm_payload` already logs
   `cancelled=… reason=…`. Sufficient, or does ops want a metric? (Recommended:
   log-only, no metric.)

## Testing

**Unit (`tests/test_mint_cancel.py`, extend):**
- `handle_mint_cancel` on an `AWAITING_PAYMENT` session with
  `session.payment_uuid = "PAYUUID"` calls `xumm_ops.cancel_xumm_payload`
  exactly once with `"PAYUUID"` (monkeypatch a fake capturing the arg; drain the
  spawned task with `await asyncio.sleep(0)`).
- `session.payment_uuid is None` → `cancel_xumm_payload` is **not** called (no
  payload existed).
- Payload-cancel raising/returning `False` does **not** change the 200 response
  or leave the lock held (monkeypatch fake that raises; assert response.status
  == 200, `_active_session(...) is None`, `session.state == CANCELLED`).
- Already-terminal cancel (`OFFER_READY`) and mid-pipeline 409 do **not** spawn
  a payload cancel (existing tests + assert fake not called).

**Unit (bulk, `tests/test_bulk_mint_*` or a focused file):**
- `handle_bulk_mint_cancel` spawns `cancel_xumm_payload(job.payment_uuid)`;
  `None` uuid → not called.

**Unit (swap, if included):**
- `SwapSession.prepare_payment` (or `_collect_modify_fee`) stores
  `payment_uuid` from the payload; `handle_swap_cancel` spawns the cancel.

**Manual smoke (testnet):**
1. Start a mint, get the pay QR/deeplink, open it in Xaman (don't sign).
2. Tap Cancel in the Activity → 200, lock freed (a new mint starts).
3. In Xaman, confirm the pending sign request is gone / no longer signable.
4. Repeat with XUMM briefly unreachable (or a bad uuid) → cancel still returns
   200 instantly and the lock frees.
