# Sponsored Step 0 Auto-Recertification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Campaign start (`POST /api/admin/sponsored-mint/start`) kicks a deterministic background archive re-verification, so the manual Step 0 CLI run is only ever needed once per network (the human baseline attestation).

**Architecture:** Extract the account_tx sweep + baseline-certify core from `scripts/backfill_history.py` into a new `lfg_core/archive_reverify.py`; add `reverify_archive()` which re-runs the certified sweep from stored `archive_state` provenance (inheriting the original human attestation verbatim); `lfg_service/app.py` runs it as a single-flight background job on campaign start, waits for the listener heartbeat, audits the outcome into `free_mint_audit`, and exposes a `reverify` block on the status endpoint. The fail-closed `archive_is_usable` gate stays the sole admission authority — no campaign-state changes.

**Tech Stack:** Python 3.10 (staging box — `asyncio.TimeoutError` ≠ builtin `TimeoutError` there, see #339), aiohttp service, xrpl-py AsyncWebsocketClient, sqlite3, pytest.

**Spec:** `docs/superpowers/specs/2026-08-03-sponsored-step0-auto-recert-design.md` (issue #340).

## Global Constraints

- Run everything from the repo checkout with `.venv/bin/python` / `.venv/bin/pytest` (pre-push gate uses the project venv; worktrees need the `.venv` symlink or the gate silently skips).
- No Claude/AI attribution in commits or PR bodies (user's global rule — overrides tool defaults).
- All new code mypy-clean and ruff-clean (pre-push gate blocks otherwise).
- `SourceTag` / memo rules are untouched — this feature performs **no XRPL transactions**, only read RPCs (`ledger`, `account_tx`).
- Do not weaken any fail-closed check: `archive_is_usable`, `validate_baseline_endpoint`, `_is_validated_entry`, and the gap-clearing CASE logic in `record_archive_baseline` are used as-is, never modified.
- Original human attestation must survive verbatim inside every auto-reverify provenance string; nesting must not grow on repeated re-verifies.
- Existing CLI behavior of `scripts/backfill_history.py` (including `--complete-audited-baseline`) must not change; existing tests in `tests/test_backfill_history.py` and `tests/test_sponsored_acceptance.py` must keep passing without edits.

---

### Task 1: Extract sweep + baseline helpers into `lfg_core/archive_reverify.py`

Pure move-and-re-export refactor. The service cannot import from `scripts/` (they insert repo root on `sys.path` at import time and are not a package the service loads), so the sweep core moves into `lfg_core` and the CLI imports it back under the same names.

**Files:**
- Create: `lfg_core/archive_reverify.py`
- Modify: `scripts/backfill_history.py`
- Test: existing `tests/test_backfill_history.py`, `tests/test_sponsored_acceptance.py` (no edits — regression only)

**Interfaces:**
- Produces (in `lfg_core/archive_reverify.py`, moved verbatim from `scripts/backfill_history.py` with their current signatures and docstrings):
  - `PAGE_LIMIT`, `REQUEST_TIMEOUT`, `THROTTLE_SECONDS`, `RETRYABLE_ERRORS`, `RETRY_MAX`, `RETRY_BASE_DELAY`
  - `REQUIRED_BASELINE_SOURCES`, `validate_baseline_source_coverage(sources) -> None`
  - `baseline_account_coverage(sources, *, distributor, nft_issuer=None, brix_issuer=None) -> dict[str, str]`
  - `baseline_coverage_document(accounts, *, source_tag, ledger_min, ledger_max) -> dict[str, Any]`
  - `validate_baseline_endpoint(snapshot, *, claimed_genesis_hash, baseline_ledger_min, baseline_ledger_max) -> None`
  - `_is_validated_entry(page, entry) -> bool`, `_warn_if_unvalidated(source, page, skipped) -> None`
  - `store_raw_tx(conn, tx, *, network=None) -> bool`
  - `backfill_account_tx(conn, request_fn, account, source, *, network=None, ledger_min=-1, ledger_max=-1) -> int`
  - `make_request_fn(client) -> Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]` — the retry/throttle closure currently defined inline in `_amain()` (the `request_fn` at `scripts/backfill_history.py:349-383`), lifted out with `client` as a parameter, body unchanged (keep BOTH `TimeoutError` and `asyncio.TimeoutError` in the except tuple — Python 3.10).

- [ ] **Step 1: Create `lfg_core/archive_reverify.py` with the moved code**

Module docstring:

```python
"""Deterministic archive sweep + certification core.

Shared by the one-time manual baseline certification CLI
(scripts/backfill_history.py --complete-audited-baseline) and the automated
re-verification job the sponsored-mint campaign start kicks (#340). The
functions here perform read-only XRPL RPCs and history-DB writes; they never
sign or submit transactions.
"""
```

Move the items listed in **Interfaces** out of `scripts/backfill_history.py` verbatim (imports they need: `asyncio`, `json`, `logging`, `re`, `time`, `sqlite3`, `dataclasses`, `typing.Any`, `websockets.exceptions.WebSocketException`, `xrpl.asyncio.clients.AsyncWebsocketClient`, `xrpl.asyncio.clients.exceptions.XRPLWebsocketException`, `xrpl.models.requests.Request`, `from lfg_core import history_events, history_store`; import `sponsored_mint` lazily inside `store_raw_tx` to avoid an import cycle — `sponsored_mint` imports nothing from this module, but keep it lazy anyway to match `lfg_core` convention for cross-module heft). `make_request_fn` is the inline closure re-homed:

```python
def make_request_fn(
    client: AsyncWebsocketClient,
) -> Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]:
    """Bounded-retry, throttled request wrapper over one websocket client."""

    async def request_fn(req: dict[str, Any]) -> dict[str, Any]:
        delay = RETRY_BASE_DELAY
        for attempt in range(RETRY_MAX):
            await asyncio.sleep(THROTTLE_SECONDS)
            try:
                r = await asyncio.wait_for(
                    client.request(Request.from_dict(req)), timeout=REQUEST_TIMEOUT
                )
            except (
                TimeoutError,
                asyncio.TimeoutError,
                ConnectionError,
                OSError,
                WebSocketException,
                XRPLWebsocketException,
            ) as e:
                if attempt < RETRY_MAX - 1:
                    logging.warning(f"{req['method']}: {e!r}; backing off {delay:.0f}s")
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 120.0)
                    continue
                raise
            if r.is_successful():
                return r.result
            error = r.result.get("error") if isinstance(r.result, dict) else None
            if error in RETRYABLE_ERRORS and attempt < RETRY_MAX - 1:
                logging.warning(f"{req['method']}: {error}; backing off {delay:.0f}s")
                await asyncio.sleep(delay)
                delay = min(delay * 2, 120.0)
                continue
            raise RuntimeError(f"{req['method']} failed: {r.result}")
        raise RuntimeError(f"{req['method']} failed after {RETRY_MAX} attempts")

    return request_fn
```

- [ ] **Step 2: Rewire `scripts/backfill_history.py` to import the moved names**

Replace the moved definitions with re-exports so every existing caller and test keeps working:

```python
from lfg_core.archive_reverify import (  # noqa: E402,F401
    PAGE_LIMIT,
    REQUEST_TIMEOUT,
    RETRY_BASE_DELAY,
    RETRY_MAX,
    RETRYABLE_ERRORS,
    REQUIRED_BASELINE_SOURCES,
    THROTTLE_SECONDS,
    _is_validated_entry,
    _warn_if_unvalidated,
    backfill_account_tx,
    baseline_account_coverage,
    baseline_coverage_document,
    make_request_fn,
    store_raw_tx,
    validate_baseline_endpoint,
    validate_baseline_source_coverage,
)
```

In `_amain()`, replace the inline `request_fn` closure with `request_fn = make_request_fn(client)`. `backfill_nft_history` stays in the script (nft_history is not part of certification).

- [ ] **Step 3: Run the regression suite**

Run: `.venv/bin/pytest tests/test_backfill_history.py tests/test_sponsored_acceptance.py tests/test_history_store.py -q`
Expected: PASS with zero test-file edits. Also run `.venv/bin/python -c "import lfg_core.archive_reverify"` and `.venv/bin/python scripts/backfill_history.py --help` (exit 0).

- [ ] **Step 4: Commit**

```bash
git add lfg_core/archive_reverify.py scripts/backfill_history.py
git commit -m "refactor(history): extract archive sweep/certify core into lfg_core.archive_reverify (#340)"
```

---

### Task 2: `reverify_archive()` — the deterministic re-certification

**Files:**
- Modify: `lfg_core/archive_reverify.py`
- Test: Create `tests/test_archive_reverify.py`

**Interfaces:**
- Consumes: Task 1's `backfill_account_tx`, `baseline_coverage_document`, `validate_baseline_source_coverage`; `history_store.get_archive_state`, `history_store.fetch_endpoint_snapshot`, `history_store.record_archive_baseline`, `history_store.EARLIEST_AVAILABLE_LEDGER`.
- Produces:
  - `@dataclass(frozen=True) class ReverifyResult: ok: bool; reason: str | None; ledger_max: int | None; provenance: str | None`
  - `inherit_attestation(provenance: str) -> str`
  - `async def reverify_archive(conn, request_fn, *, network: str, now: int | None = None) -> ReverifyResult`
  - Failure `reason` values (closed set, exact strings): `"baseline_never_certified"`, `"genesis_mismatch"`, `"coverage_unbound"`, `"missing_required_sources"`, `"sweep_failed: <exc>"`.

- [ ] **Step 1: Write the failing tests**

`tests/test_archive_reverify.py`. Copy the env-guard preamble convention from `tests/test_history_store.py` (module-top env vars before `lfg_core` imports — repo convention, see CLAUDE.md "Test env-guard convention"). Fake `request_fn` answering `ledger` (genesis + validated tip) and `account_tx` (empty validated pages, no marker):

```python
import asyncio
import json

import pytest

from lfg_core import archive_reverify, history_store

GENESIS = "ABC123GENESISHASH"


def _fake_request_fn(tip=500_000, genesis=GENESIS, account_pages=None, fail_account_tx=False):
    async def request_fn(req):
        if req["method"] == "ledger":
            if req["ledger_index"] == history_store.EARLIEST_AVAILABLE_LEDGER:
                return {
                    "ledger_index": history_store.EARLIEST_AVAILABLE_LEDGER,
                    "ledger_hash": genesis,
                }
            return {"ledger_index": tip, "ledger_hash": "TIPHASH"}
        if req["method"] == "account_tx":
            if fail_account_tx:
                raise RuntimeError("account_tx failed: boom")
            return {
                "account": req["account"],
                "ledger_index_min": req["ledger_index_min"],
                "ledger_index_max": req["ledger_index_max"],
                "validated": True,
                "transactions": (account_pages or {}).get(req["account"], []),
            }
        raise AssertionError(f"unexpected method {req['method']}")

    return request_fn


def _certified_conn(tmp_path, *, provenance="hamsa manual audit 2026-08-01", coverage=True):
    conn = history_store.init_history_db(str(tmp_path / "history_testnet.db"))
    doc = archive_reverify.baseline_coverage_document(
        {"token_issuer": "rTOKEN", "signing": "rSIGN"},
        source_tag=2606160021,
        ledger_min=history_store.EARLIEST_AVAILABLE_LEDGER,
        ledger_max=400_000,
    )
    history_store.record_archive_baseline(
        conn,
        network="testnet",
        genesis_hash=GENESIS,
        ledger_min=history_store.EARLIEST_AVAILABLE_LEDGER,
        ledger_max=400_000,
        provenance=provenance,
        source_tag=2606160021,
        coverage=json.dumps(doc, sort_keys=True, separators=(",", ":")) if coverage else None,
    )
    return conn


def test_inherit_attestation_passthrough_and_unnesting():
    assert archive_reverify.inherit_attestation("hamsa audit") == "hamsa audit"
    wrapped = "auto-reverify at 2026-08-03T14:00:00Z (baseline: hamsa audit)"
    assert archive_reverify.inherit_attestation(wrapped) == "hamsa audit"
    # double-wrap never nests
    rewrapped = f"auto-reverify at 2026-08-04T00:00:00Z (baseline: {archive_reverify.inherit_attestation(wrapped)})"
    assert archive_reverify.inherit_attestation(rewrapped) == "hamsa audit"


def test_reverify_refuses_without_prior_baseline(tmp_path):
    conn = history_store.init_history_db(str(tmp_path / "history_testnet.db"))
    result = asyncio.run(
        archive_reverify.reverify_archive(conn, _fake_request_fn(), network="testnet")
    )
    assert result == archive_reverify.ReverifyResult(False, "baseline_never_certified", None, None)


def test_reverify_refuses_on_genesis_mismatch(tmp_path):
    conn = _certified_conn(tmp_path)
    result = asyncio.run(
        archive_reverify.reverify_archive(
            conn, _fake_request_fn(genesis="OTHERCHAIN"), network="testnet"
        )
    )
    assert (result.ok, result.reason) == (False, "genesis_mismatch")


def test_reverify_refuses_on_unbound_coverage(tmp_path):
    conn = _certified_conn(tmp_path, coverage=False)
    result = asyncio.run(
        archive_reverify.reverify_archive(conn, _fake_request_fn(), network="testnet")
    )
    assert (result.ok, result.reason) == (False, "coverage_unbound")


def test_reverify_certifies_to_tip_and_inherits_attestation(tmp_path):
    conn = _certified_conn(tmp_path, provenance="hamsa manual audit 2026-08-01")
    result = asyncio.run(
        archive_reverify.reverify_archive(
            conn, _fake_request_fn(tip=600_000), network="testnet", now=1_800_000_000
        )
    )
    assert result.ok and result.reason is None
    state = history_store.get_archive_state(conn, "testnet")
    assert state is not None and state.baseline_complete
    assert state.baseline_ledger_max == 600_000 == result.ledger_max
    assert state.baseline_provenance is not None
    assert "(baseline: hamsa manual audit 2026-08-01)" in state.baseline_provenance
    assert state.baseline_provenance.startswith("auto-reverify at ")
    # coverage doc rebuilt against the new range, same accounts
    doc = json.loads(state.baseline_coverage or "{}")
    assert doc["ledger_max"] == 600_000
    assert doc["accounts"] == {"signing": "rSIGN", "token_issuer": "rTOKEN"}
    # certification clears the heartbeat by design
    assert state.heartbeat_at is None and state.validated_ledger_index is None


def test_second_reverify_does_not_nest_provenance(tmp_path):
    conn = _certified_conn(tmp_path, provenance="hamsa manual audit 2026-08-01")
    asyncio.run(
        archive_reverify.reverify_archive(conn, _fake_request_fn(tip=600_000), network="testnet")
    )
    asyncio.run(
        archive_reverify.reverify_archive(conn, _fake_request_fn(tip=700_000), network="testnet")
    )
    state = history_store.get_archive_state(conn, "testnet")
    assert state is not None and state.baseline_provenance is not None
    assert state.baseline_provenance.count("auto-reverify at") == 1
    assert state.baseline_provenance.endswith("(baseline: hamsa manual audit 2026-08-01)")


def test_reverify_heals_bounded_continuity_gap(tmp_path):
    conn = _certified_conn(tmp_path)
    history_store.invalidate_archive_continuity(
        conn, network="testnet", gap_after=450_000, reason="listener disconnect"
    )
    state = history_store.get_archive_state(conn, "testnet")
    assert state is not None and not state.baseline_complete
    result = asyncio.run(
        archive_reverify.reverify_archive(conn, _fake_request_fn(tip=600_000), network="testnet")
    )
    assert result.ok
    state = history_store.get_archive_state(conn, "testnet")
    assert state is not None and state.baseline_complete
    assert state.continuity_gap_at is None


def test_reverify_reports_sweep_failure(tmp_path):
    conn = _certified_conn(tmp_path)
    result = asyncio.run(
        archive_reverify.reverify_archive(
            conn, _fake_request_fn(fail_account_tx=True), network="testnet"
        )
    )
    assert not result.ok
    assert result.reason is not None and result.reason.startswith("sweep_failed: ")
```

Note: check `history_store.invalidate_archive_continuity`'s real signature before using it in the gap test (`grep -n "def invalidate_archive_continuity" lfg_core/history_store.py`) and adapt the keyword names to it — the test intent (bounded gap below the new tip heals) is what matters.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_archive_reverify.py -q`
Expected: FAIL — `AttributeError: module 'lfg_core.archive_reverify' has no attribute 'reverify_archive'` (and `ReverifyResult`, `inherit_attestation`).

- [ ] **Step 3: Implement in `lfg_core/archive_reverify.py`**

```python
_ATTESTATION_RE = re.compile(r"^auto-reverify at \S+ \(baseline: (?P<orig>.*)\)$", re.DOTALL)


@dataclass(frozen=True)
class ReverifyResult:
    """Outcome of one automated re-certification attempt."""

    ok: bool
    reason: str | None
    ledger_max: int | None
    provenance: str | None


def inherit_attestation(provenance: str) -> str:
    """Return the original human attestation, unwrapping one auto-reverify layer.

    Repeated re-verifies must carry the SAME baseline attestation forever, not
    a nested chain of wrappers."""
    match = _ATTESTATION_RE.match(provenance.strip())
    return match.group("orig") if match else provenance.strip()


async def reverify_archive(
    conn: sqlite3.Connection,
    request_fn: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
    *,
    network: str,
    now: int | None = None,
) -> ReverifyResult:
    """Deterministically re-certify a previously human-certified archive.

    Preconditions are read from archive_state: a prior baseline with a
    non-empty provenance and a bound coverage document. The sweep re-pages
    account_tx over exactly the accounts the original certification covered,
    from EARLIEST_AVAILABLE_LEDGER to the live validated tip, then certifies
    through record_archive_baseline (whose gap-clearing CASE logic is the
    single authority on whether a continuity gap is healed). Never raises on
    expected failures — returns a ReverifyResult with a machine-readable
    reason instead."""

    state = history_store.get_archive_state(conn, network)
    if state is None or not (state.baseline_provenance or "").strip():
        return ReverifyResult(False, "baseline_never_certified", None, None)
    try:
        coverage_doc = json.loads(state.baseline_coverage or "")
        accounts = dict(coverage_doc["accounts"])
    except (ValueError, TypeError, KeyError):
        return ReverifyResult(False, "coverage_unbound", None, None)
    if not accounts:
        return ReverifyResult(False, "coverage_unbound", None, None)
    try:
        validate_baseline_source_coverage(set(accounts))
    except ValueError:
        return ReverifyResult(False, "missing_required_sources", None, None)

    try:
        snapshot = await history_store.fetch_endpoint_snapshot(request_fn)
    except Exception as exc:  # endpoint identity unreadable — nothing to certify against
        return ReverifyResult(False, f"sweep_failed: {exc}", None, None)
    if snapshot.genesis_hash != state.genesis_hash:
        return ReverifyResult(False, "genesis_mismatch", None, None)

    ledger_min = history_store.EARLIEST_AVAILABLE_LEDGER
    ledger_max = snapshot.validated_ledger_index
    try:
        for source, account in sorted(accounts.items()):
            await backfill_account_tx(
                conn,
                request_fn,
                account,
                f"{source}_tx",
                network=network,
                ledger_min=ledger_min,
                ledger_max=ledger_max,
            )
    except Exception as exc:
        # Cursors persisted per page; a retry resumes, exactly like a Ctrl-C'd
        # manual backfill. Nothing was certified, so fail-closed is preserved.
        return ReverifyResult(False, f"sweep_failed: {exc}", None, None)

    from lfg_core import config

    timestamp = int(time.time()) if now is None else int(now)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(timestamp))
    provenance = f"auto-reverify at {stamp} (baseline: {inherit_attestation(state.baseline_provenance or '')})"
    doc = baseline_coverage_document(
        accounts, source_tag=config.SOURCE_TAG, ledger_min=ledger_min, ledger_max=ledger_max
    )
    history_store.record_archive_baseline(
        conn,
        network=network,
        genesis_hash=snapshot.genesis_hash,
        ledger_min=ledger_min,
        ledger_max=ledger_max,
        provenance=provenance,
        source_tag=config.SOURCE_TAG,
        coverage=json.dumps(doc, sort_keys=True, separators=(",", ":")),
        completed_at=timestamp,
    )
    refreshed = history_store.get_archive_state(conn, network)
    if refreshed is None or not refreshed.baseline_complete:
        # A gap whose bound lies past the swept tip survives certification by
        # design (record_archive_baseline's CASE). Report it, don't mask it.
        return ReverifyResult(False, "gap_not_covered", ledger_max, provenance)
    return ReverifyResult(True, None, ledger_max, provenance)
```

Add `"gap_not_covered"` to the documented reason set in the module docstring. Imports to add: `re`, `time`, `sqlite3`, `dataclasses.dataclass`, `typing.Awaitable/Callable`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_archive_reverify.py tests/test_backfill_history.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add lfg_core/archive_reverify.py tests/test_archive_reverify.py
git commit -m "feat(sponsored): deterministic archive reverify inheriting the human baseline attestation (#340)"
```

---

### Task 3: Heartbeat wait + reverify audit record

**Files:**
- Modify: `lfg_core/archive_reverify.py` (wait helper), `lfg_core/sponsored_mint.py` (audit writer)
- Test: `tests/test_archive_reverify.py` (wait), `tests/test_sponsored_mint_store.py` (audit)

**Interfaces:**
- Consumes: `sponsored_mint.archive_is_usable(history_path, *, network=None, now=None) -> bool`; `sponsored_mint._audit(conn, *, network, actor, action, at, campaign_id, result, details=None)` (existing private writer — the new public function wraps it).
- Produces:
  - `async def wait_for_archive_usable(history_path: str, *, network: str, timeout: float = 90.0, poll: float = 5.0, now_fn: Callable[[], float] = time.time, sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep) -> bool` in `archive_reverify`
  - `def audit_archive_reverify(db_path: str, *, network: str, actor: str, result: str, now: int | None = None) -> None` in `sponsored_mint`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_archive_reverify.py`:

```python
def test_wait_for_archive_usable_polls_until_true(monkeypatch, tmp_path):
    from lfg_core import sponsored_mint

    calls = {"n": 0}

    def fake_usable(path, *, network=None, now=None):
        calls["n"] += 1
        return calls["n"] >= 3

    monkeypatch.setattr(sponsored_mint, "archive_is_usable", fake_usable)
    clock = {"t": 0.0}
    slept: list[float] = []

    async def fake_sleep(seconds):
        clock["t"] += seconds
        slept.append(seconds)

    ok = asyncio.run(
        archive_reverify.wait_for_archive_usable(
            str(tmp_path / "h.db"),
            network="testnet",
            timeout=90.0,
            poll=5.0,
            now_fn=lambda: clock["t"],
            sleep_fn=fake_sleep,
        )
    )
    assert ok and calls["n"] == 3 and slept == [5.0, 5.0]


def test_wait_for_archive_usable_times_out(monkeypatch, tmp_path):
    from lfg_core import sponsored_mint

    monkeypatch.setattr(sponsored_mint, "archive_is_usable", lambda *a, **k: False)
    clock = {"t": 0.0}

    async def fake_sleep(seconds):
        clock["t"] += seconds

    ok = asyncio.run(
        archive_reverify.wait_for_archive_usable(
            str(tmp_path / "h.db"),
            network="testnet",
            timeout=20.0,
            poll=5.0,
            now_fn=lambda: clock["t"],
            sleep_fn=fake_sleep,
        )
    )
    assert not ok
```

Append to `tests/test_sponsored_mint_store.py` (reuse that file's existing app-DB tmp fixture/helpers for a `db_path` — read its top to match local conventions):

```python
def test_audit_archive_reverify_writes_row(tmp_path):
    db = str(tmp_path / "app.db")
    sponsored_mint.audit_archive_reverify(
        db, network="testnet", actor="admin:42", result="ok", now=1_800_000_000
    )
    sponsored_mint.audit_archive_reverify(
        db, network="testnet", actor="admin:42", result="failed: genesis_mismatch"
    )
    import sqlite3

    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT actor, action, result FROM free_mint_audit ORDER BY at"
        ).fetchall()
    assert ("admin:42", "archive_reverify", "ok") in rows
    assert ("admin:42", "archive_reverify", "failed: genesis_mismatch") in rows
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_archive_reverify.py tests/test_sponsored_mint_store.py -q -k "wait_for_archive or audit_archive"`
Expected: FAIL with AttributeError on both new names.

- [ ] **Step 3: Implement**

In `archive_reverify.py`:

```python
async def wait_for_archive_usable(
    history_path: str,
    *,
    network: str,
    timeout: float = 90.0,
    poll: float = 5.0,
    now_fn: Callable[[], float] = time.time,
    sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> bool:
    """Wait for the listener to restamp the heartbeat after certification.

    record_archive_baseline clears heartbeat/validated fields by design; the
    streaming listener restamps them on the next validated ledger it applies.
    Bounded so a listener with no archive identity (env genesis hash unset and
    no prior row at ITS startup) turns into a clear failure, not a hang."""
    from lfg_core import sponsored_mint

    deadline = now_fn() + timeout
    while True:
        if sponsored_mint.archive_is_usable(history_path, network=network):
            return True
        if now_fn() >= deadline:
            return False
        await sleep_fn(poll)
```

In `sponsored_mint.py` (near `start_campaign`):

```python
def audit_archive_reverify(
    db_path: str, *, network: str, actor: str, result: str, now: int | None = None
) -> None:
    """Durable free_mint_audit row for an automated archive re-verification."""
    _require_supported_network(network)
    timestamp = _timestamp(now)
    ensure_schema(db_path)
    with _connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        _audit(
            conn,
            network=network,
            actor=actor.strip() or "system",
            action="archive_reverify",
            at=timestamp,
            campaign_id=None,
            result=result,
        )
```

(Match `_audit`'s real keyword list — it also takes `details`; pass nothing extra unless required.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_archive_reverify.py tests/test_sponsored_mint_store.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add lfg_core/archive_reverify.py lfg_core/sponsored_mint.py tests/test_archive_reverify.py tests/test_sponsored_mint_store.py
git commit -m "feat(sponsored): heartbeat wait helper + archive_reverify audit rows (#340)"
```

---

### Task 4: Service integration — start kicks the job, status reports it

**Files:**
- Modify: `lfg_service/app.py` (`handle_sponsored_mint_start`, `handle_sponsored_mint_status`, new job runner + state)
- Test: `tests/test_sponsored_admin.py`

**Interfaces:**
- Consumes: `archive_reverify.reverify_archive`, `archive_reverify.make_request_fn`, `archive_reverify.wait_for_archive_usable`, `sponsored_mint.audit_archive_reverify`, `config.CLIO_WS_URL`, `history_store.init_history_db`, `_sponsored_mint_paths()`.
- Produces (module-level in `lfg_service/app.py`):
  - `_reverify_state: dict[str, dict[str, Any]]` — per-network `{"state": "idle"|"running"|"ok"|"failed", "error": str | None, "finished_at": int | None}`
  - `_reverify_tasks: dict[str, asyncio.Task]` — single-flight guard
  - `def _reverify_status(network: str) -> dict[str, Any]`
  - `async def run_archive_reverify(network: str, actor: str) -> None`
  - `def kick_archive_reverify(network: str, actor: str) -> None`

- [ ] **Step 1: Write the failing tests**

`tests/test_sponsored_admin.py` already builds `_Request` objects and calls the handlers directly (see its `handle_sponsored_mint_start` usages around lines 106-130) — follow the same auth-header and `_run()` conventions. Add:

```python
def test_start_kicks_single_flight_reverify(monkeypatch, ...):  # match file's fixture params
    kicked: list[tuple[str, str]] = []
    monkeypatch.setattr(server, "kick_archive_reverify", lambda net, actor: kicked.append((net, actor)))
    headers = {"Authorization": "Bearer tok-d"}  # match the file's working auth fixture
    _run(server.handle_sponsored_mint_start(_Request(headers, {"actor": "admin:42"})))
    assert kicked == [(server.config.XRPL_NETWORK, "admin:42")]


def test_kick_archive_reverify_is_single_flight(monkeypatch):
    started = {"n": 0}

    async def fake_job(network, actor):
        started["n"] += 1
        await asyncio.sleep(3600)

    monkeypatch.setattr(server, "run_archive_reverify", fake_job)

    async def scenario():
        server.kick_archive_reverify("testnet", "admin:42")
        server.kick_archive_reverify("testnet", "admin:42")  # joins, doesn't double-run
        await asyncio.sleep(0)
        assert started["n"] == 1
        server._reverify_tasks["testnet"].cancel()

    _run(scenario())


def test_status_exposes_reverify_block(monkeypatch, ...):
    server._reverify_state["testnet"] = {
        "state": "failed",
        "error": "genesis_mismatch",
        "finished_at": 1_800_000_000,
    }
    headers = {"Authorization": "Bearer tok-d"}
    resp = _run(server.handle_sponsored_mint_status(_Request(headers, {})))
    body = json.loads(resp.text)
    assert body["reverify"] == {
        "state": "failed",
        "error": "genesis_mismatch",
        "finished_at": 1_800_000_000,
    }


def test_run_archive_reverify_success_path(monkeypatch, tmp_path):
    class _Result:
        ok = True
        reason = None
        ledger_max = 600_000
        provenance = "auto-reverify …"

    async def fake_reverify(conn, request_fn, *, network, now=None):
        return _Result()

    async def fake_wait(path, *, network, **kw):
        return True

    audits: list[str] = []
    monkeypatch.setattr(server.archive_reverify, "reverify_archive", fake_reverify)
    monkeypatch.setattr(server.archive_reverify, "wait_for_archive_usable", fake_wait)
    monkeypatch.setattr(
        server.sponsored_mint,
        "audit_archive_reverify",
        lambda db, *, network, actor, result, now=None: audits.append(result),
    )
    monkeypatch.setattr(server, "_reverify_client", _fake_ws_client_factory())  # see Step 3
    _run(server.run_archive_reverify("testnet", "admin:42"))
    assert server._reverify_state["testnet"]["state"] == "ok"
    assert audits == ["ok"]


def test_run_archive_reverify_heartbeat_timeout(monkeypatch, tmp_path):
    # same monkeypatching, but fake_wait returns False
    ...
    assert server._reverify_state["testnet"]["state"] == "failed"
    assert "listener" in server._reverify_state["testnet"]["error"]
    assert audits and audits[0].startswith("failed: ")
```

(Fill the two elided tests concretely when writing them — same shape as the success test with `fake_wait` returning `False`, and with `fake_reverify` returning `ok=False, reason="genesis_mismatch"` asserting `error == "genesis_mismatch"`.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_sponsored_admin.py -q -k reverify`
Expected: FAIL — `AttributeError` on `kick_archive_reverify` / `_reverify_state`.

- [ ] **Step 3: Implement in `lfg_service/app.py`**

Add imports `from lfg_core import archive_reverify` (the module already imports `sponsored_mint`, `history_store`, `config`). Near the sponsored-burn worker section:

```python
# --- Sponsored archive auto-reverify (#340) -------------------------------
# Campaign start kicks one background re-certification per network. The
# fail-closed archive_is_usable gate stays the admission authority; this job
# only repairs the archive so the gate can open. State is in-memory — a
# restart forgets a finished job, which is harmless: the next start re-kicks.
_reverify_state: dict[str, dict[str, Any]] = {}
_reverify_tasks: dict[str, asyncio.Task] = {}


def _reverify_status(network: str) -> dict[str, Any]:
    return dict(
        _reverify_state.get(network) or {"state": "idle", "error": None, "finished_at": None}
    )


def _reverify_client() -> Any:
    """Seam for tests: one websocket client on the configured clio endpoint."""
    from xrpl.asyncio.clients import AsyncWebsocketClient

    return AsyncWebsocketClient(config.CLIO_WS_URL)


async def run_archive_reverify(network: str, actor: str) -> None:
    _, history_db = _sponsored_mint_paths()
    campaign_db, _ = _sponsored_mint_paths()
    _reverify_state[network] = {"state": "running", "error": None, "finished_at": None}
    error: str | None = None
    try:
        conn = history_store.init_history_db(history_db)
        try:
            async with _reverify_client() as client:
                request_fn = archive_reverify.make_request_fn(client)
                result = await archive_reverify.reverify_archive(
                    conn, request_fn, network=network
                )
        finally:
            conn.close()
        if not result.ok:
            error = result.reason or "reverify_failed"
        elif not await archive_reverify.wait_for_archive_usable(history_db, network=network):
            error = (
                "listener never restamped the heartbeat — it has no archive identity; set "
                "SPONSORED_MINT_*_GENESIS_HASH and restart the listener (not during a live campaign)"
            )
    except Exception:
        logging.error(f"archive reverify crashed: {traceback.format_exc()}")
        error = "internal_error"
    _reverify_state[network] = {
        "state": "failed" if error else "ok",
        "error": error,
        "finished_at": int(time.time()),
    }
    try:
        sponsored_mint.audit_archive_reverify(
            campaign_db,
            network=network,
            actor=actor,
            result=f"failed: {error}" if error else "ok",
        )
    except Exception:
        logging.error(f"archive reverify audit write failed: {traceback.format_exc()}")


def kick_archive_reverify(network: str, actor: str) -> None:
    task = _reverify_tasks.get(network)
    if task is not None and not task.done():
        return  # single-flight: join the running job
    _reverify_tasks[network] = asyncio.get_event_loop().create_task(
        run_archive_reverify(network, actor)
    )
```

In `handle_sponsored_mint_start`, after the `start_campaign` call succeeds, add:

```python
    kick_archive_reverify(config.XRPL_NETWORK, actor)
```

In `handle_sponsored_mint_status`, add to the response dict before returning:

```python
    status["reverify"] = _reverify_status(config.XRPL_NETWORK)
```

(If the handler returns `dataclasses.asdict(...)` inline, bind it to `status` first — it already does for the balance-state branch.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_sponsored_admin.py tests/test_archive_reverify.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add lfg_service/app.py tests/test_sponsored_admin.py
git commit -m "feat(sponsored): campaign start kicks single-flight archive reverify; status exposes it (#340)"
```

---

### Task 5: Runbook + env docs

**Files:**
- Modify: `docs/ops/sponsored-free-mint.md` (Step 0 section), `docs/ops/env.staging.example` (genesis-hash vars, if absent), `CLAUDE.md` env-var block (add `SPONSORED_MINT_MAINNET_GENESIS_HASH` / `SPONSORED_MINT_TESTNET_GENESIS_HASH` lines if absent)

**Interfaces:**
- Consumes: behavior implemented in Tasks 2-4 (describe it, don't restate code).

- [ ] **Step 1: Rewrite Step 0 in `docs/ops/sponsored-free-mint.md`**

Split it into:
- **Step 0a — one-time baseline certification (manual, unchanged):** the existing CLI command and constraints, explicitly labeled "run once per network / per testnet reset; the `--baseline-provenance` attestation is the human audit claim."
- **Step 0b — automatic re-verification (no action needed):** clicking Start Sponsored Mint kicks a background job that re-sweeps to the live tip, inherits the original attestation (`auto-reverify at <ts> (baseline: …)`), waits for the listener heartbeat, and writes an `archive_reverify` row to `free_mint_audit`. The status endpoint's `reverify` block (`idle/running/ok/failed` + `error`) says why eligibility is unavailable. Failure reasons table: `baseline_never_certified` → run Step 0a; `genesis_mismatch` → wrong endpoint or testnet reset (re-run Step 0a); `coverage_unbound`/`missing_required_sources` → re-run Step 0a with the required `--sources`; `gap_not_covered`, `sweep_failed: …`, heartbeat message → transient/ops, retry by pressing Start again.
- **Prerequisite:** set `SPONSORED_MINT_MAINNET_GENESIS_HASH` / `SPONSORED_MINT_TESTNET_GENESIS_HASH` in each stack's `.env` so the listener always has an archive identity at startup (removes the certify→restart→certify-again bootstrap). Keep the existing warning: never restart during a live campaign.

- [ ] **Step 2: Add the two genesis env vars to `docs/ops/env.staging.example` and the CLAUDE.md env block** (one line each, with a `# optional; listener archive identity at startup (#340)` comment). Skip whichever file already has them.

- [ ] **Step 3: Verify docs build nothing (markdown only), run full test suite once**

Run: `.venv/bin/pytest -q`
Expected: PASS (full-suite order matters — env-guard convention; a failure only in full-suite order is a real bug, not a flake).

- [ ] **Step 4: Commit**

```bash
git add docs/ops/sponsored-free-mint.md docs/ops/env.staging.example CLAUDE.md
git commit -m "docs(sponsored): Step 0 split into one-time baseline + automatic reverify (#340)"
```

---

### Task 6: PR

- [ ] **Step 1: Push the branch and open a ready (non-draft) PR against `main`**

```bash
git push -u origin HEAD
gh pr create --repo Team-Hamsa/LFG --title "feat(sponsored): auto-recertify the eligibility archive on campaign start (#340)" --body "Closes #340.

Campaign start now kicks a single-flight background re-verification of the eligibility archive: re-sweeps account_tx over the accounts the original certification covered (from ledger 32570 to the live validated tip), re-certifies through the existing gap-clearing record_archive_baseline, inherits the one-time human baseline attestation verbatim, waits for the listener heartbeat, and audits the outcome to free_mint_audit. The fail-closed archive_is_usable gate remains the sole admission authority — no campaign-state changes. The sweep/certify core moved from scripts/backfill_history.py into lfg_core/archive_reverify.py (CLI re-exports, behavior unchanged).

Spec: docs/superpowers/specs/2026-08-03-sponsored-step0-auto-recert-design.md"
```

No AI attribution in the body. Wait for Greptile + CodeRabbit; close every actionable finding on its own thread (fix + reply naming the commit, or decline with reasoning) before merge. Remember: a clean Greptile verdict lives only in the `Greptile Review` check-run summary.

---

## Self-review notes

- Spec coverage: concept split (T1/T2), provenance inheritance + no nesting (T2), gap healing via existing CASE (T2), tip handling (T2 — the sweep is pinned to the snapshot tip read in the same session, so the manual flow's "tip moved" race cannot occur; the spec's retry loop is therefore unnecessary and deliberately dropped — document this in the PR body if a reviewer asks), heartbeat wait + listener-identity error (T3/T4), single-flight start trigger + status block + audit rows (T4), no new campaign states (T4 — start path unchanged apart from the kick), runbook/env prerequisite (T5), out-of-scope items untouched.
- Deviation from spec, intentional: spec's "retry read-sweep-certify up to 3 times on tip-moved" is dead logic under the implementation (min/max are passed from the same snapshot the validation would compare against); `gap_not_covered` reason added instead to surface the one real residual failure. Everything else matches.
