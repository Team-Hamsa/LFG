# Bulk-mint `mint.completed` events — design

**Date:** 2026-07-24
**Status:** draft (triage — needs maintainer review)
**Issue:** #253

## Problem

Bulk mints (#215) publish **no** firehose events. `handle_bulk_mint_start` /
`handle_bulk_mint_status` and `lfg_core/bulk_mint_flow.py` contain zero
`publish_event` calls, so every bulk-minted edition is invisible to every
firehose consumer:

- **X auto-poster** (`surfaces/x_bot/`) never tweets bulk editions —
  `poster.should_post` only fires on `mint.completed` with a non-empty `nft_id`.
- **Telegram announce** (`surfaces/telegram_bot/events.py`) and **Discord
  announce** (`surfaces/discord_bot/events.py`) never announce them —
  `run_event_loop` consumes `mint.completed`/`mint.failed` and nothing emits
  those for bulk units.

Single mints are covered: `lfg_service/app.py::_run_mint_session_and_publish`
wraps `mint_flow.run_mint_session` and calls `_publish_mint_terminal(session)`
server-side, so even a killed Activity client can't suppress the event.
`_publish_mint_terminal` builds `mint.completed` from `MintSession.to_dict()`
and is idempotent via the session's `_published` flag. Bulk was deliberately
left out pending the product decision below.

## Constraints discovered

- **The bus lives in the service, not `lfg_core`.** `publish_event` /
  `publish_terminal` / `enrich_minter_identity` are defined in
  `lfg_service/app.py`; `BUS.publish(Event(...))` is service-owned.
  `lfg_core/bulk_mint_flow.py` cannot import them (layering). The existing
  pattern for this is the `on_mint` async callback that `mint_flow.mint_one_unit`
  already accepts — dependency injection from the service down into `lfg_core`.
- **Event-shape parity is required.** Consumers read `data["nft_number"]`,
  `data["nft_id"]`, `data["image_url"]`, `data["video_url"]`, `data["traits"]`
  (dict, LFG naming — `Head`→`Hat` already applied in `mint_one_unit`), and
  `data["body_type"]`, plus `event.identity` / `event.wallet`. The X poster's
  `compose()` needs `traits` + `body_type` to render the tweet body; the TG/
  Discord `announcement_image` prefers `video_url` then `image_url`.
- **The persisted `Unit` dataclass currently drops the trait data.**
  `mint_flow.UnitResult` carries `traits` / `body_type` / `video_url` (added by
  #41 PR-1), but `bulk_mint_flow.Unit` only stores `index, state, nft_number,
  nft_id, image_url, offer_id, error`. `_fulfill_unit` discards
  `res.traits`/`res.body_type`/`res.video_url`. A parity event — and any event
  re-emitted on resume — therefore needs those fields **persisted on the Unit**,
  because the resume path (`_ensure_offer`) has only the on-disk Unit, not the
  in-memory `UnitResult`.
- **Bulk jobs resume; double-emit must be prevented durably.** `run_bulk_mint_job`
  is (re)launched from both `handle_bulk_mint_start` and `resume_bulk_jobs`
  (`load_all_resumable` picks up `AWAITING_PAYMENT`/`PAID`/`FULFILLING`). The X
  poster dedups per `nft_id` via `x_state.db`, but TG/Discord consumers do
  **not** dedup — a re-emit is a visibly duplicated announcement. Idempotency
  must therefore be a **durable per-unit flag** in the job JSON record, mirroring
  the single-mint `session._published` guard (set only after a successful
  publish).
- **Publish failure must never break fulfillment.** Same rule as
  `_run_mint_session_and_publish` ("publish failure is logged and never breaks
  the mint task"). A bus error on one unit must not abort the job or skip
  minting the rest.
- **X budget gate is intentional.** A quantity-10 job = up to 10
  `mint.completed` events = up to 10 X budget slots (`X_MONTHLY_POST_BUDGET`,
  poster budget-gates and dedups). This is accepted for option 1.
- **No SourceTag / memo work.** This feature builds **no XRPL transaction** — it
  only publishes an in-process bus event describing an already-landed mint. The
  underlying mint tx already carries `SourceTag = 2606160021` and provenance
  memos via `mint_one_unit`. Nothing here touches the ledger.

## Design

Adopt **Option 1 (per-unit events)** from the issue: each bulk unit that reaches
`OFFERED` publishes one `mint.completed` event shaped exactly like a single
mint's, so every existing consumer works unchanged with zero consumer edits.

### 1. Persist trait data + a published flag on `Unit` (`lfg_core/bulk_mint_flow.py`)

Extend the `Unit` dataclass with defaulted fields (default-valued so old on-disk
records deserialize via `Unit(**u)` unchanged, and every existing `Unit(...)`
call site stays valid):

```python
@dataclass
class Unit:
    index: int
    state: str = PENDING
    nft_number: int | None = None
    nft_id: str | None = None
    image_url: str | None = None
    offer_id: str | None = None
    error: str | None = None
    # #253: parity payload for the mint.completed firehose event, captured
    # from UnitResult so a resume can re-emit without the in-memory result.
    traits: dict[str, str] | None = None
    body_type: str | None = None
    video_url: str | None = None
    # durable idempotency guard — set only after a successful publish.
    published: bool = False
```

`serialize()` already does `[asdict(u) for u in self.units]` and
`from_serialized` does `Unit(**u)`, so the new fields round-trip with **no
serializer change**. `to_dict()` (client-facing) is left as-is.

In `_fulfill_unit`, where `res.nft_id` is truthy, capture the trait data onto
the unit alongside the existing assignments:

```python
if res.nft_id:
    unit.nft_id = res.nft_id
    unit.nft_number = res.nft_number
    unit.image_url = res.image_url
    unit.traits = res.traits
    unit.body_type = res.body_type
    unit.video_url = res.video_url
    ...
```

(`_ensure_offer` needs no capture — a unit reaching it was already MINTED with
its trait data persisted from the original `_fulfill_unit` run.)

### 2. Inject a per-unit publish callback (service → `lfg_core`)

Add a runtime-only attribute `job.on_unit_complete: Callable[[Unit],
Awaitable[None]] | None = None` on `BulkMintJob.__init__` (never serialized, same
posture as `job.task`). Add a module helper in `bulk_mint_flow.py`:

```python
async def emit_completed(job: BulkMintJob) -> None:
    """Publish mint.completed for every OFFERED unit not yet announced.
    Idempotent via unit.published; the service-provided job.on_unit_complete
    does the actual publish, and we mark+persist only after it returns so a
    crash before persist re-emits on resume (accepted sub-tick window; X
    dedups on nft_id). A callback exception is logged and leaves the unit
    unpublished — never aborts the job (mirrors _run_mint_session_and_publish)."""
    if job.on_unit_complete is None:
        return
    for unit in job.units:
        if unit.state != OFFERED or not unit.nft_id or unit.published:
            continue
        try:
            await job.on_unit_complete(unit)
        except Exception as e:
            logging.error("bulk job %s unit %d publish failed: %s", job.id, unit.index, e)
            continue
        unit.published = True
        persist(job)
```

Call `await emit_completed(job)` inside `run_bulk_mint_job`:
- right after `persist(job)` at the end of each iteration of the main fulfill
  loop (prompt per-unit emission as each lands), and
- once after the bounded final re-offer pass (catches units that self-heal
  MINTED→OFFERED late in the same run).

Because `emit_completed` scans **all** units and gates on `published`, the
resume path is covered for free: a resumed `FULFILLING` job re-enters the loop
(which `continue`s over already-OFFERED units) and the trailing
`emit_completed` publishes any OFFERED-but-unpublished unit exactly once. A job
that parks early (durability-degraded `return`, or the exception handler that
keeps a paid job `FULFILLING`) simply emits those units on the next resume.

### 3. Service-side publisher + wiring (`lfg_service/app.py`)

Define the callback that builds the parity event and set it before every
`run_bulk_mint_job` launch:

```python
async def _publish_bulk_unit(job: Any, unit: Any) -> None:
    """Publish one bulk unit's mint.completed, shaped like a single mint's
    MintSession.to_dict payload so x_bot / TG / Discord consumers read it
    with no branching. Raises on publish failure (bulk_mint_flow.emit_completed
    catches + leaves the unit unpublished for retry)."""
    data = {
        "id": job.id,
        "platform": job.platform,
        "state": bulk_mint_flow.OFFERED,
        "nft_number": unit.nft_number,
        "nft_id": unit.nft_id,
        "image_url": unit.image_url,
        "video_url": unit.video_url,
        "traits": unit.traits,
        "body_type": unit.body_type,
        "bulk": True,  # informational provenance marker; consumers ignore it
    }
    await publish_event(
        "mint.completed",
        enrich_minter_identity(job.platform, job.discord_id, job.wallet_address),
        job.wallet_address,
        data,
    )
```

At both launch sites — `handle_bulk_mint_start` (before
`asyncio.create_task(bulk_mint_flow.run_bulk_mint_job(job))`) and
`resume_bulk_jobs` (before the equivalent `create_task`) — set
`job.on_unit_complete = _publish_bulk_unit`. This mirrors how `on_mint` and
`_publish_mint_terminal` are wired from the service.

### Ordering decision: emit at OFFERED, not at MINTED

A unit emits its `mint.completed` when it reaches `OFFERED` (the NFT is minted
**and** the gift offer to the buyer exists), never at bare `MINTED`. Rationale:
`MINTED`-without-offer is the delivered-pending-offer / re-offer-on-resume state;
announcing it as a completed mint before the user can claim it would be
premature, and a unit that never gets offered (permanent offer failure, stays
`MINTED`) is intentionally never announced. `OFFERED` is the durable resolved
success state, the bulk analog of the single-mint terminal.

## Out of scope

- **Option 2 / summary `mint.bulk_completed` event** and any X-side coalescing
  window (Option 3). Deferred — they need new compose paths in both consumers
  and a product call on collapsing N tweets into one.
- **Claim-later UX** (#218) — bulk offers carry no `Expiration`; acceptance is
  already decoupled from fulfillment. Unchanged here.
- **X budget rework.** 10 units consuming 10 budget slots is accepted;
  `X_MONTHLY_POST_BUDGET` tuning is an ops knob, not a code change.
- **`mint.failed` for bulk units.** A `UNIT_FAILED` unit converts to a durable
  `mint_credits` row, not a lost mint; emitting a per-unit failure event is not
  requested by #253 and is left out (open question below).

## Open questions / decisions for maintainer

1. **Failure events?** Should a `UNIT_FAILED` unit emit `mint.failed`? Single
   mints do (on `FAILED`/`PAYMENT_TIMEOUT`). #253 asks only for the success
   path; proposal is to skip bulk failure events for now (a credited unit isn't
   really a "failed mint" from the user's side). Confirm.
2. **Deploy-time backlog.** On first deploy, an in-flight `FULFILLING` job whose
   units are already `OFFERED` (published defaults to `False` on the old record)
   will emit `mint.completed` for those already-offered editions on the next
   resume. This set is tiny (only genuinely-in-flight jobs; `DONE` jobs are
   never resumed) and arguably correct (they were minted but never announced),
   but it is a burst of possibly-minutes-old announcements. Accept, or add a
   one-shot "treat pre-existing OFFERED units as already published" migration?
3. **X budget spikes.** A single quantity-`BULK_MINT_MAX` job can consume up to
   10 budget slots in a burst. Acceptable, or should the X poster gain a
   per-`bulk`-job cap (would use the new `data["bulk"]` marker)? Proposal:
   accept for now; revisit under Option 3 if it's a problem.
4. **`bulk` marker.** Keep the informational `data["bulk"]: True` field? It's
   free provenance for future analytics/coalescing and consumers ignore unknown
   keys.

## Testing

**Unit (`tests/test_bulk_mint_events.py`, new; copy the env-guard preamble
from `tests/test_bulk_mint_flow.py`):**
- A job whose units reach `OFFERED` calls `on_unit_complete` **once per
  OFFERED unit** with the right `nft_id`/`traits`/`body_type`.
- `emit_completed` is idempotent: calling it twice (simulating a resume)
  publishes each unit exactly once (`published` gates the second pass).
- A `MINTED`-but-not-`OFFERED` unit does **not** emit.
- A callback that raises leaves `unit.published is False` and does not abort
  the loop / other units still emit.
- `Unit` round-trips `traits`/`body_type`/`video_url`/`published` through
  `serialize()` → `from_serialized()`; an old record dict missing those keys
  deserializes with defaults (`published=False`).

**Integration (extend `tests/test_bulk_mint_service.py` or new
`test_bulk_mint_publish.py`, patterned on `tests/test_mint_terminal_publish.py`
which monkeypatches `server.publish_event` to capture calls):**
- Drive `run_bulk_mint_job` on a job with `on_unit_complete = _publish_bulk_unit`
  and a fake `mint_one_unit` returning OFFERED units; assert N
  `mint.completed` events with parity payloads and correct
  `enrich_minter_identity` wiring.
- Re-run the same job (resume): assert **no** additional events (durable
  `published` guard).

**Consumer parity (`tests/test_x_poster.py`, `tests/test_telegram_events.py`,
`tests/test_discord_events.py`):** feed a bulk-shaped `mint.completed` event
(with `data["bulk"] = True`) and assert `should_post` returns `mint:<nft_id>`,
`compose` renders the traits line, and `make_announcement`/`announcement_image`
produce the same output as a single-mint event.

**Manual smoke (testnet/staging):** run a quantity-2 bulk mint via the Activity;
confirm two `🎨 LFGO #N just minted!` announcements in the TG/Discord channel and
(with `X_ENABLED=1` on staging) two dedup'd/budgeted tweet attempts; kill the
service mid-job and confirm resume emits only the not-yet-announced unit.
