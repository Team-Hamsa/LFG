# Closet — Standalone Issuance + Bucket→Closet Rename — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rename the per-wallet trait container Bucket→Closet end-to-end and make its issuance a standalone, up-front, accepted-by-user step that gates Harvest/Assemble.

**Architecture:** A mechanical, behavior-preserving rename lands first (Task 1), then new behavior is layered on with TDD: a `none → pending_accept → active` state machine on the renamed `closet_tokens` table, an `ensure_closet`/`confirm_accept` lifecycle, a hard "closet must be active" precondition on the economy flows, listener accept-handling, service endpoints + register-path issuance, dev-mode mock parity, frontend states, and a re-mint migration script. Ships as a **single PR** with internally-phased tasks.

**Tech Stack:** Python 3 (aiohttp service, sqlite3, xrpl-py), vanilla JS (Discord Activity), pytest (repo-native sync style: `new_event_loop` + direct call, NOT pytest-asyncio).

**Spec:** `docs/superpowers/specs/2026-06-27-closet-issuance-and-rename-design.md`

## Global Constraints

- **Build on PR #104** (merged first): `ensure_bucket` already takes an Optional `exists_fn`; `EconomyDeps` has `bucket_exists_fn`; `scripts/_economy_deps.py` has `_bucket_exists`. This plan renames those to `closet_*` and extends them.
- **SourceTag `2606160021`** on every XRPL tx / XUMM payload — already stamped by `lfg_core/xrpl_ops.py` builders; the migration script reuses those builders, so do not add inline tx dicts.
- **Soulbound model unchanged:** Closet flags `CLOSET_NFT_FLAGS = 16` (mutable-only, non-transferable, non-burnable).
- **DB authoritative for accounting; the Closet NFToken metadata mirrors it.** Modify the token before the DB.
- **mypy:** `lfg_core` is under full strict typing; `surfaces/_client/*` is full `--strict`; `tests/*` is `ignore_errors=true`. Run `.venv/bin/mypy .` (full, not per-file) before claiming mypy-clean — per-file misses cross-method `warn_return_any`.
- **Test style:** repo-native (`loop = asyncio.new_event_loop()` helper), parametrize all generics (`dict[str, Any]`).
- **Status string constants** (defined in Task 2, `lfg_core/closet_token.py`, imported elsewhere): `PENDING_ACCEPT = "pending_accept"`, `ACTIVE = "active"`. Absence of a `closet_tokens` row ⇒ status `none`.
- **Run the full suite green** after each task: `.venv/bin/python -m pytest -q`.

---

### Task 1: Mechanical rename (Bucket → Closet), behavior-preserving

The entire import graph renames together so the suite stays green. New `closet_tokens` columns (`status`, `offer_id`) are added to the schema now but left unused until Task 3. `CLOSET_TAXON` is a NEW value; `LEGACY_BUCKET_TAXON` preserves the read path.

**Files (modify):**
- `lfg_core/config.py`
- `lfg_core/economy_store.py`
- `lfg_core/bucket_token.py` → rename to `lfg_core/closet_token.py`
- `lfg_core/economy_flow.py`
- `lfg_core/nft_listener.py`
- `scripts/_economy_deps.py`, `scripts/economy_harvest.py`, `scripts/economy_assemble.py`, `scripts/economy_equip.py`, `scripts/audit_trait_economy.py`, `scripts/audit_layer_coverage.py`, `scripts/onchain_listener.py`
- `lfg_service/app.py`, `webapp/economy_api.py`, `webapp/mock_economy.py`
- `surfaces/discord_bot/events.py`, `surfaces/telegram_bot/events.py`
- `webapp/client/app.js`, `webapp/client/index.html`, `webapp/client/style.css`
- **Tests (rename in place):** `tests/test_bucket_token.py` → `tests/test_closet_token.py`, `tests/test_bucket_token_lifecycle.py` → `tests/test_closet_token_lifecycle.py`, and update identifiers across `tests/test_economy_flow_*.py`, `tests/test_economy_store*.py`, `tests/test_economy_listener.py`, `tests/test_onchain_listener.py`, `tests/test_trait_economy*.py`, `tests/test_xrpl_source_tag.py`, `tests/test_service_firehose.py`, `tests/test_audit_layer_coverage.py`, `webapp/test_economy_api.py`, `webapp/test_mock_economy.py`.

**Identifier rename map (apply verbatim, whole-word):**

| Old | New |
|---|---|
| `bucket_token` (module) | `closet_token` |
| `parse_bucket_metadata` | `parse_closet_metadata` |
| `build_bucket_metadata` | `build_closet_metadata` |
| `ensure_bucket` | `ensure_closet` |
| `sync_bucket` | `sync_closet` |
| `BucketRef` | `ClosetRef` |
| `BucketError` | `ClosetError` |
| `bucket_assets` (table) | `closet_assets` |
| `bucket_bodies` (table) | `closet_bodies` |
| `bucket_tokens` (table) | `closet_tokens` |
| `read_bucket_assets` | `read_closet_assets` |
| `read_bucket_bodies` | `read_closet_bodies` |
| `set_bucket_contents` | `set_closet_contents` |
| `set_bucket_token` | `set_closet_token` |
| `get_bucket_token` | `get_closet_token` |
| `bucket_upload_fn / bucket_mint_fn / bucket_offer_fn / bucket_accept_fn / bucket_modify_fn` | `closet_upload_fn / closet_mint_fn / closet_offer_fn / closet_accept_fn / closet_modify_fn` |
| `bucket_exists_fn` (EconomyDeps, from #104) | `closet_exists_fn` |
| `_bucket_exists` (scripts) | `_closet_exists` |
| `_apply_bucket` | `_apply_closet` |
| `BUCKET_TAXON` | `CLOSET_TAXON` |
| `BUCKET_NFT_FLAGS` | `CLOSET_NFT_FLAGS` |
| `BUCKET_IMAGE_URL` | `CLOSET_IMAGE_URL` |
| `lfg_bucket` (on-chain metadata key) | `lfg_closet` (emit); **read both** (Task 2) |
| user strings "Bucket" / "bucket" | "Closet" / "closet" (UI, announce, Xaman prompts) |
| CSS/ids `dressup-bucket`,`bucket-grid`,`bucket-item`,`bucket-head`,`bucket-filter`,`bucket-*` | `dressup-closet`,`closet-grid`,`closet-item`,`closet-head`,`closet-filter`,`closet-*` |

- [ ] **Step 1: `config.py`** — rename the three constants. Set `CLOSET_TAXON` to a NEW distinct value and keep the legacy taxon for transition reads:

```python
# Closet (per-user soulbound trait container; formerly "Bucket").
LEGACY_BUCKET_TAXON = int(os.getenv("BUCKET_TAXON", "1761"))
CLOSET_TAXON = int(os.getenv("CLOSET_TAXON", "1762"))
CLOSET_IMAGE_URL = os.getenv("CLOSET_IMAGE_URL", NFT_COLLECTION_LOGO)
CLOSET_NFT_FLAGS = int(os.getenv("CLOSET_NFT_FLAGS", "16"))  # mutable only (soulbound)
```

- [ ] **Step 2: `economy_store.py`** — in `_ECONOMY_SCHEMA`, rename the three tables and add two columns to `closet_tokens` (unused until Task 3):

```sql
CREATE TABLE IF NOT EXISTS closet_tokens (
    owner      TEXT PRIMARY KEY,
    nft_id     TEXT,
    uri_hex    TEXT,
    status     TEXT DEFAULT 'pending_accept',
    offer_id   TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

Rename `read_bucket_assets/read_bucket_bodies/set_bucket_contents/set_bucket_token/get_bucket_token` and their internal `bucket_assets/bucket_bodies/bucket_tokens` SQL to `closet_*`. Leave `set_closet_token`'s signature unchanged for now (status defaults via the column).

- [ ] **Step 3: `bucket_token.py` → `closet_token.py`** — `git mv lfg_core/bucket_token.py lfg_core/closet_token.py`; apply the rename map to its contents; make `parse_closet_metadata` dual-read:

```python
def parse_closet_metadata(meta: dict[str, Any]) -> tuple[list[Asset], list[int]]:
    """Inverse of build_closet_metadata. Reads the new `lfg_closet` block, falling
    back to the legacy `lfg_bucket` block so pre-rename tokens still parse."""
    block = meta.get("lfg_closet")
    if not isinstance(block, dict):
        block = meta.get("lfg_bucket")
    if not isinstance(block, dict):
        return [], []
    # ... existing asset/body parsing against `block` unchanged ...
```

`build_closet_metadata` emits the new key:

```python
        "lfg_closet": {
            "assets": [ ... ],
            "bodies": sorted(bodies),
        },
```

- [ ] **Step 4: remaining modules** — apply the rename map to `economy_flow.py`, `nft_listener.py`, all `scripts/*` listed, `lfg_service/app.py`, `webapp/economy_api.py`, `webapp/mock_economy.py`, both `surfaces/*/events.py`, and the frontend trio. In `nft_listener.apply_economy_tx`, the taxon check becomes dual:

```python
            if int(token.get("taxon") or -1) in (config.CLOSET_TAXON, config.LEGACY_BUCKET_TAXON):
                _apply_closet(conn, token, metadata)
```

- [ ] **Step 5: tests** — `git mv` the two token test files; apply the rename map across all listed test files (including `_Fakes`/`_deps` helpers that name `bucket_*`).

- [ ] **Step 6: rename DB migration helper** — add to `economy_store.py` a one-time copy so existing index DBs carry rows forward (called from `init_economy_schema` after `executescript`):

```python
def _migrate_bucket_tables(conn: sqlite3.Connection) -> None:
    """One-time copy of legacy bucket_* rows into closet_* (pre-rename DBs)."""
    have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for old, new, cols in (
        ("bucket_assets", "closet_assets", "owner, slot, value, count"),
        ("bucket_bodies", "closet_bodies", "owner, edition"),
        ("bucket_tokens", "closet_tokens", "owner, nft_id, uri_hex"),
    ):
        if old in have:
            conn.execute(f"INSERT OR IGNORE INTO {new} ({cols}) SELECT {cols} FROM {old}")
    conn.commit()
```

Call `_migrate_bucket_tables(conn)` at the end of `init_economy_schema`.

- [ ] **Step 7: run full suite, format, types**

Run: `.venv/bin/ruff format . && .venv/bin/ruff check lfg_core scripts tests webapp surfaces && .venv/bin/mypy . && .venv/bin/python -m pytest -q && node --check webapp/client/app.js`
Expected: all green (behavior unchanged).

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "refactor(economy): rename Bucket→Closet end-to-end (no behavior change)"
```

---

### Task 2: Closet status accessors + constants (economy_store)

**Files:**
- Modify: `lfg_core/closet_token.py` (add status constants)
- Modify: `lfg_core/economy_store.py`
- Test: `tests/test_economy_store_phase2.py`

**Interfaces:**
- Consumes: `closet_tokens(owner, nft_id, uri_hex, status, offer_id)` from Task 1.
- Produces:
  - `closet_token.PENDING_ACCEPT: str`, `closet_token.ACTIVE: str`
  - `economy_store.set_closet_token(conn, owner, nft_id, uri_hex, status=PENDING_ACCEPT, offer_id=None)` (extends existing)
  - `economy_store.set_closet_status(conn, owner, status) -> None`
  - `economy_store.get_closet_record(conn, owner) -> tuple[str, str, str, str | None] | None` (nft_id, uri_hex, status, offer_id)

- [ ] **Step 1: Add constants** to `lfg_core/closet_token.py` (top, after imports):

```python
PENDING_ACCEPT = "pending_accept"
ACTIVE = "active"
```

- [ ] **Step 2: Write failing test** in `tests/test_economy_store_phase2.py`:

```python
from lfg_core import closet_token as ct
from lfg_core import economy_store as es
import sqlite3

def _conn():
    c = sqlite3.connect(":memory:"); es.init_economy_schema(c); return c

def test_closet_record_roundtrip_and_status_update():
    c = _conn()
    assert es.get_closet_record(c, "rA") is None
    es.set_closet_token(c, "rA", "NFTC", "ABCD", status=ct.PENDING_ACCEPT, offer_id="OF1")
    assert es.get_closet_record(c, "rA") == ("NFTC", "ABCD", ct.PENDING_ACCEPT, "OF1")
    es.set_closet_status(c, "rA", ct.ACTIVE)
    assert es.get_closet_record(c, "rA") == ("NFTC", "ABCD", ct.ACTIVE, "OF1")
```

- [ ] **Step 3: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_economy_store_phase2.py::test_closet_record_roundtrip_and_status_update -v`
Expected: FAIL (`set_closet_status`/`get_closet_record` undefined; `set_closet_token` rejects kwargs).

- [ ] **Step 4: Implement** in `lfg_core/economy_store.py`:

```python
def set_closet_token(
    conn: sqlite3.Connection,
    owner: str,
    nft_id: str,
    uri_hex: str,
    status: str = "pending_accept",
    offer_id: str | None = None,
) -> None:
    """Record/update an owner's Closet NFToken id, URI, lifecycle status, and the
    outstanding accept offer id (kept so the UI can re-show the Xaman accept)."""
    conn.execute(
        """
        INSERT INTO closet_tokens (owner, nft_id, uri_hex, status, offer_id, updated_at)
        VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(owner) DO UPDATE SET
            nft_id=excluded.nft_id, uri_hex=excluded.uri_hex,
            status=excluded.status, offer_id=excluded.offer_id, updated_at=CURRENT_TIMESTAMP
        """,
        (owner, nft_id, uri_hex, status, offer_id),
    )
    conn.commit()


def set_closet_status(conn: sqlite3.Connection, owner: str, status: str) -> None:
    conn.execute(
        "UPDATE closet_tokens SET status=?, updated_at=CURRENT_TIMESTAMP WHERE owner=?",
        (status, owner),
    )
    conn.commit()


def get_closet_record(
    conn: sqlite3.Connection, owner: str
) -> tuple[str, str, str, str | None] | None:
    """(nft_id, uri_hex, status, offer_id) for an owner's Closet, or None."""
    cur = conn.execute(
        "SELECT nft_id, uri_hex, status, offer_id FROM closet_tokens WHERE owner=?", (owner,)
    )
    row = cur.fetchone()
    return None if row is None else (str(row[0]), str(row[1]), str(row[2]), row[3])
```

Keep `get_closet_token` (returns `(nft_id, uri_hex)`) — `sync_closet` still uses it.

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_economy_store_phase2.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add lfg_core/economy_store.py lfg_core/closet_token.py tests/test_economy_store_phase2.py
git commit -m "feat(economy): closet status + offer_id accessors"
```

---

### Task 3: `ensure_closet` records pending; `confirm_accept` promotes to active

**Files:**
- Modify: `lfg_core/closet_token.py`
- Test: `tests/test_closet_token.py`

**Interfaces:**
- Consumes: `economy_store.set_closet_token/get_closet_record/set_closet_status`, constants `PENDING_ACCEPT/ACTIVE`.
- Produces:
  - `ClosetRef(nft_id, uri_hex, status, accept_payload=None, minted=False)` — gains `status`.
  - `ensure_closet(conn, owner, *, upload_fn, mint_fn, offer_fn, accept_payload_fn, exists_fn=None) -> ClosetRef` — records `pending_accept` with `offer_id`; idempotent while pending (regenerates the accept payload from the stored offer_id); re-mints when `exists_fn` reports the recorded token gone.
  - `ExistsFn = Callable[[str], Awaitable[bool]]` (from #104), `OwnerFn = Callable[[str], Awaitable[str | None]]`
  - `confirm_accept(conn, owner, *, owner_fn: OwnerFn) -> str` — returns the resulting status; flips `pending_accept → active` when `await owner_fn(nft_id) == owner`.

- [ ] **Step 1: Write failing tests** in `tests/test_closet_token.py` (extend the existing fakes; add a `_run` if absent):

```python
import sqlite3
from lfg_core import closet_token as ct
from lfg_core import economy_store as es

class _F:
    def __init__(self, exists=True, owner=None):
        self.minted = 0; self.offers = 0; self.exists = exists; self.owner = owner
    async def up(self, meta): return "https://cdn/c.json"
    async def mint(self, url): self.minted += 1; return f"NFT{self.minted}"
    async def offer(self, nft_id, owner): self.offers += 1; return f"OF{self.offers}"
    async def accept(self, offer_id): return {"xumm_url": f"x/{offer_id}"}
    async def exists_fn(self, nft_id): return self.exists
    async def owner_fn(self, nft_id): return self.owner

def _run(coro):
    loop = __import__("asyncio").new_event_loop()
    try: return loop.run_until_complete(coro)
    finally: loop.close()

def _conn():
    c = sqlite3.connect(":memory:"); es.init_economy_schema(c); return c

def test_ensure_closet_first_use_records_pending():
    c, f = _conn(), _F()
    ref = _run(ct.ensure_closet(c, "rA", upload_fn=f.up, mint_fn=f.mint,
        offer_fn=f.offer, accept_payload_fn=f.accept))
    assert ref.status == ct.PENDING_ACCEPT and ref.minted and ref.accept_payload
    assert es.get_closet_record(c, "rA")[2] == ct.PENDING_ACCEPT
    assert f.minted == 1

def test_ensure_closet_pending_is_idempotent_and_reshows_accept():
    c, f = _conn(), _F()
    _run(ct.ensure_closet(c, "rA", upload_fn=f.up, mint_fn=f.mint,
        offer_fn=f.offer, accept_payload_fn=f.accept))
    ref = _run(ct.ensure_closet(c, "rA", upload_fn=f.up, mint_fn=f.mint,
        offer_fn=f.offer, accept_payload_fn=f.accept, exists_fn=f.exists_fn))
    assert f.minted == 1                       # did NOT re-mint
    assert ref.status == ct.PENDING_ACCEPT and ref.accept_payload  # re-showed accept

def test_ensure_closet_stale_record_remints():
    c, f = _conn(), _F(exists=False)
    _run(ct.ensure_closet(c, "rA", upload_fn=f.up, mint_fn=f.mint,
        offer_fn=f.offer, accept_payload_fn=f.accept))
    ref = _run(ct.ensure_closet(c, "rA", upload_fn=f.up, mint_fn=f.mint,
        offer_fn=f.offer, accept_payload_fn=f.accept, exists_fn=f.exists_fn))
    assert f.minted == 2 and ref.nft_id == "NFT2"

def test_confirm_accept_promotes_when_owner_matches():
    c, f = _conn(), _F(owner="rA")
    _run(ct.ensure_closet(c, "rA", upload_fn=f.up, mint_fn=f.mint,
        offer_fn=f.offer, accept_payload_fn=f.accept))
    assert _run(ct.confirm_accept(c, "rA", owner_fn=f.owner_fn)) == ct.ACTIVE
    assert es.get_closet_record(c, "rA")[2] == ct.ACTIVE

def test_confirm_accept_stays_pending_when_owner_mismatch():
    c, f = _conn(), _F(owner="rISSUER")
    _run(ct.ensure_closet(c, "rA", upload_fn=f.up, mint_fn=f.mint,
        offer_fn=f.offer, accept_payload_fn=f.accept))
    assert _run(ct.confirm_accept(c, "rA", owner_fn=f.owner_fn)) == ct.PENDING_ACCEPT
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_closet_token.py -k "ensure_closet or confirm_accept" -v`
Expected: FAIL (`status`/`confirm_accept` not present; offer_id not stored).

- [ ] **Step 3: Implement** in `lfg_core/closet_token.py`:

```python
from collections.abc import Awaitable, Callable

ExistsFn = Callable[[str], Awaitable[bool]]
OwnerFn = Callable[[str], Awaitable[str | None]]


@dataclass
class ClosetRef:
    nft_id: str
    uri_hex: str
    status: str = PENDING_ACCEPT
    accept_payload: dict[str, Any] | None = None
    minted: bool = False


async def ensure_closet(
    conn: Any,
    owner: str,
    *,
    upload_fn: UploadFn,
    mint_fn: MintFn,
    offer_fn: OfferFn,
    accept_payload_fn: AcceptFn,
    exists_fn: ExistsFn | None = None,
) -> ClosetRef:
    """Return the owner's Closet, minting on first use. A fresh Closet is minted
    empty, offered to the owner, and recorded `pending_accept` with its offer id.
    A recorded but on-ledger-absent Closet (verified via `exists_fn`) is treated
    as stale and re-minted. While pending, this is idempotent and regenerates the
    Xaman accept payload from the stored offer id so the UI can re-show it."""
    existing = economy_store.get_closet_record(conn, owner)
    if existing is not None:
        nft_id, uri_hex, status, offer_id = existing
        stale = exists_fn is not None and not await exists_fn(nft_id)
        if not stale:
            payload = None
            if status == PENDING_ACCEPT and offer_id:
                payload = await accept_payload_fn(offer_id)
            return ClosetRef(nft_id=nft_id, uri_hex=uri_hex, status=status, accept_payload=payload)

    url = await upload_fn(build_closet_metadata(owner, [], []))
    nft_id = await mint_fn(url)
    if not nft_id:
        raise ClosetError("failed to mint Closet NFToken")
    offer_id = await offer_fn(nft_id, owner)
    if not offer_id:
        raise ClosetError("failed to offer Closet NFToken to owner")
    payload = await accept_payload_fn(offer_id)  # None is non-fatal (accept later)
    economy_store.set_closet_token(
        conn, owner, nft_id, _hex(url), status=PENDING_ACCEPT, offer_id=offer_id
    )
    return ClosetRef(
        nft_id=nft_id, uri_hex=_hex(url), status=PENDING_ACCEPT,
        accept_payload=payload, minted=True,
    )


async def confirm_accept(conn: Any, owner: str, *, owner_fn: OwnerFn) -> str:
    """Promote `pending_accept → active` once the Closet is owned by `owner`
    (offer accepted on-ledger). Returns the resulting status; `none` if no Closet
    is recorded. Idempotent."""
    rec = economy_store.get_closet_record(conn, owner)
    if rec is None:
        return "none"
    nft_id, _uri, status, _offer = rec
    if status == ACTIVE:
        return ACTIVE
    if await owner_fn(nft_id) == owner:
        economy_store.set_closet_status(conn, owner, ACTIVE)
        return ACTIVE
    return status
```

Note: `sync_closet` keeps using `get_closet_token`; it raises `ClosetError` if no record (unchanged).

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_closet_token.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add lfg_core/closet_token.py tests/test_closet_token.py
git commit -m "feat(economy): ensure_closet pending/stale lifecycle + confirm_accept"
```

---

### Task 4: Economy flows gate on an ACTIVE Closet (remove buried issuance)

**Files:**
- Modify: `lfg_core/economy_flow.py`
- Test: `tests/test_economy_flow_harvest.py`, `tests/test_economy_flow_assemble.py`

**Interfaces:**
- Consumes: `closet_token.confirm_accept`, `economy_store.get_closet_record`, `ACTIVE`.
- Produces: `EconomyDeps` gains `closet_owner_fn: ct.OwnerFn | None = None`; `run_harvest`/`run_assemble` reject before any irreversible op unless the Closet is `active`. The inline `ensure_closet`+`session.bucket_accept` block is removed from `run_harvest`.

- [ ] **Step 1: Write failing tests.** In `tests/test_economy_flow_harvest.py`, extend `_Fakes` with `async def closet_owner(self, nft_id): return self.closet_owner_addr` (default `None`) and `_deps` to pass `closet_owner_fn=f.closet_owner`. Add:

```python
def test_harvest_rejected_without_active_closet(tmp_path):
    conn, f = _conn_with_genesis(), _Fakes()
    # no closet row at all → status none
    session = ef.HarvestSession(owner="rUser", character=_char(), burnable=True)
    _run(ef.run_harvest(session, _deps(conn, f, tmp_path)))
    assert session.state == ef.FAILED
    assert f.burns == []                      # never burned
    assert "closet" in (session.error or "").lower()

def test_harvest_succeeds_with_active_closet(tmp_path):
    conn, f = _conn_with_genesis(), _Fakes()
    es.set_closet_token(conn, "rUser", "NFTC", "AB", status=ct.ACTIVE, offer_id=None)
    session = ef.HarvestSession(owner="rUser", character=_char(), burnable=True)
    _run(ef.run_harvest(session, _deps(conn, f, tmp_path)))
    assert session.state == ef.DONE
    assert f.burns == [("NFT7", "rUser")]
```

Add `from lfg_core import closet_token as ct` and `economy_store as es` imports. Update existing happy-path harvest tests to seed an active closet first (so they still pass).

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_economy_flow_harvest.py -q`
Expected: FAIL (no precondition yet; happy path also fails after seeding requirement).

- [ ] **Step 3: Implement.** Add a helper and call it at the top of `run_harvest` and `run_assemble`, and delete the old ensure block:

```python
from lfg_core import closet_token as ct

async def _require_active_closet(deps: EconomyDeps, owner: str) -> str | None:
    """Returns an error string if the owner has no ACTIVE Closet, else None.
    Runs an on-demand accept confirmation first (promotes pending→active)."""
    if deps.closet_owner_fn is not None:
        await ct.confirm_accept(deps.conn, owner, owner_fn=deps.closet_owner_fn)
    rec = es.get_closet_record(deps.conn, owner)
    if rec is None or rec[2] != ct.ACTIVE:
        return "Create and claim your Closet first."
    return None
```

In `run_harvest`, after the `can_harvest` check and BEFORE snapshotting/burning, replace the `ensure_bucket`/`session.bucket_accept` block with:

```python
        err = await _require_active_closet(deps, owner)
        if err:
            session.fail(err)
            return
```

Remove `session.bucket_accept` assignment in the flow (keep the dataclass field for back-compat, or drop it and its `_record` usage — drop it; update `_record`). Do the same precondition in `run_assemble` (top, after `can_assemble`). Add `closet_owner_fn: ct.OwnerFn | None = None` to `EconomyDeps`.

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/test_economy_flow_harvest.py tests/test_economy_flow_assemble.py tests/test_economy_flow_equip.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add lfg_core/economy_flow.py tests/test_economy_flow_harvest.py tests/test_economy_flow_assemble.py
git commit -m "feat(economy): gate harvest/assemble on an ACTIVE Closet"
```

---

### Task 5: Listener marks closet pending on mint, active on accept

**Files:**
- Modify: `lfg_core/nft_listener.py`
- Test: `tests/test_economy_listener.py`

**Interfaces:**
- Consumes: `economy_store.set_closet_token`, `config.SWAP_ISSUER_ADDRESS`, `CLOSET_TAXON/LEGACY_BUCKET_TAXON`.
- Produces: `apply_economy_tx` also processes `"accept"`; `_apply_closet` writes `status = active` when the (post-transfer) owner is not the issuer, else `pending_accept`.

- [ ] **Step 1: Write failing test** in `tests/test_economy_listener.py` (follow its existing fake fetchers). Sketch:

```python
def test_closet_accept_marks_active():
    # build a tx classified "accept" for a CLOSET_TAXON token whose post-transfer
    # owner == a user; fetch_token returns owner=user, taxon=CLOSET_TAXON, empty meta
    ... apply_economy_tx(...) ...
    assert es.get_closet_record(conn, "rUser")[2] == ct.ACTIVE

def test_closet_mint_marks_pending():
    # same but owner == config.SWAP_ISSUER_ADDRESS and kind "mint"
    assert es.get_closet_record(conn, "rIssuer-or-user")[2] == ct.PENDING_ACCEPT
```

(Reuse the file's existing tx/fetch fixtures; assert via `get_closet_record`.)

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_economy_listener.py -k closet -v`
Expected: FAIL (accept not handled; status not written).

- [ ] **Step 3: Implement** in `lfg_core/nft_listener.py`:

```python
def _apply_closet(conn: sqlite3.Connection, token: dict[str, Any], metadata: Any) -> None:
    """Rebuild an owner's closet_* rows from the Closet NFToken metadata and set
    its lifecycle status: a token held by anyone other than the issuer has been
    accepted (active); one still in the issuer wallet is pending_accept."""
    owner = token.get("owner")
    if not owner:
        return
    assets, bodies = closet_token.parse_closet_metadata(metadata if isinstance(metadata, dict) else {})
    economy_store.set_closet_contents(conn, owner, assets, bodies)
    status = closet_token.ACTIVE if owner != config.SWAP_ISSUER_ADDRESS else closet_token.PENDING_ACCEPT
    economy_store.set_closet_token(
        conn, owner, token["nft_id"], token.get("uri_hex") or "", status=status
    )
```

In `apply_economy_tx`, widen the kind filter and keep the dual-taxon check from Task 1:

```python
    kind = classify_tx(tx)
    if kind not in ("mint", "modify", "accept"):
        return
    ...
            if int(token.get("taxon") or -1) in (config.CLOSET_TAXON, config.LEGACY_BUCKET_TAXON):
                _apply_closet(conn, token, metadata)
            elif kind == "mint":
                _apply_possible_growth(conn, token, metadata, genesis)
```

(Imports: `closet_token` already imported post-Task-1.)

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/test_economy_listener.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add lfg_core/nft_listener.py tests/test_economy_listener.py
git commit -m "feat(listener): closet mint→pending, accept→active"
```

---

### Task 6: Service — `POST /api/closet`, economy `closet` block, gated harvest/assemble, register issuance

**Files:**
- Modify: `lfg_service/app.py`, `webapp/economy_api.py`, `scripts/_economy_deps.py`
- Test: `webapp/test_economy_api.py`

**Interfaces:**
- Consumes: `closet_token.ensure_closet/confirm_accept`, `economy_store.get_closet_record`, `xrpl_ops.nft_info`.
- Produces:
  - `_economy_deps.build_economy_deps` wires `closet_owner_fn=lambda nft_id: _closet_owner(nft_id)` where `_closet_owner` returns the token's current owner via `nft_info`.
  - `economy_api.read_economy_state` includes `"closet": {"status", "nft_id", "accept"?}` (runs `confirm_accept` first).
  - `economy_api.start_closet(user_id, wallet) -> session-like dict` and `lfg_service` route `POST /api/closet`.
  - `POST /api/harvest` / `POST /api/assemble` return 400 `{"error": "Create and claim your Closet first."}` when not active.
  - `handle_register` triggers `ensure_closet` post-registration (non-blocking) and includes the accept link in the response.

- [ ] **Step 1: `_economy_deps.py`** — add the owner lookup and wire it:

```python
async def _closet_owner(nft_id: str) -> str | None:
    info = await xrpl_ops.nft_info(nft_id)
    return info.get("owner") if info else None
```

In `build_economy_deps(...)` add `closet_owner_fn=lambda nft_id: _closet_owner(nft_id)`. (Rename of `_bucket_exists`→`_closet_exists` already done in Task 1.)

- [ ] **Step 2: Write failing test** in `webapp/test_economy_api.py` (follow its existing harness; it likely uses `WEBAPP_DEV_MODE`/mock or a seeded conn). Add:

```python
def test_economy_state_reports_closet_status(...):
    # seed an active closet for the wallet
    state = read_economy_state(conn, "rWALLET")
    assert state["closet"]["status"] in ("none", "pending_accept", "active")
```

(If `read_economy_state` takes a conn, seed via `economy_store.set_closet_token`. If it routes through mock in dev mode, assert the mock's `closet` block — see Task 7.)

- [ ] **Step 3: Run to verify failure**

Run: `.venv/bin/python -m pytest webapp/test_economy_api.py -k closet -v`
Expected: FAIL (`closet` key absent).

- [ ] **Step 4: Implement.** In `webapp/economy_api.py`, in `read_economy_state(conn, wallet)`:

```python
    from lfg_core import closet_token as ct
    rec = economy_store.get_closet_record(conn, wallet)
    closet = {"status": "none", "nft_id": None}
    if rec is not None:
        closet = {"status": rec[2], "nft_id": rec[0]}
    state["closet"] = closet
```

Add a `start_closet` coroutine (mirrors how `start_harvest` builds deps + runs a flow, but here just `ensure_closet` then returns `{status, accept}`). In `lfg_service/app.py`, register `app.router.add_post("/api/closet", _closet_handler)` where the handler calls `start_closet`. In the harvest/assemble POST handlers (`_economy_post`), before starting the flow, fetch `get_closet_record` and return 400 if not `active`. In `handle_register`, after the existing success path, best-effort `ensure_closet` and attach the accept link to the JSON response (wrap in try/except; never fail registration on closet error).

- [ ] **Step 5: Run to verify pass**

Run: `.venv/bin/python -m pytest webapp/test_economy_api.py -q && .venv/bin/python -m pytest tests/test_service_firehose.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add lfg_service/app.py webapp/economy_api.py scripts/_economy_deps.py webapp/test_economy_api.py
git commit -m "feat(service): /api/closet, closet state, gated economy ops, register issuance"
```

---

### Task 7: Dev-mode mock parity (`mock_economy`)

**Files:**
- Modify: `webapp/mock_economy.py`
- Test: `webapp/test_mock_economy.py`

**Interfaces:**
- Produces: mock `read_state` returns a `closet: {status, nft_id, accept?}` block; a mock `create_closet(wallet)` transitions `none → pending_accept → active`; mock harvest/assemble raise unless closet active.

- [ ] **Step 1: Write failing test** in `webapp/test_mock_economy.py`:

```python
def test_mock_closet_lifecycle_gates_harvest():
    m = mock_economy.MockEconomy()
    assert m.read_state("rW")["closet"]["status"] == "none"
    with pytest.raises(Exception):
        m.start_harvest("rW", {"nft_id": "X"})       # blocked: no closet
    m.create_closet("rW")
    assert m.read_state("rW")["closet"]["status"] == "pending_accept"
    m.create_closet("rW")                            # second call = accept (mock)
    assert m.read_state("rW")["closet"]["status"] == "active"
```

(Adapt to the actual mock class/singleton `INSTANCE` API in the file.)

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest webapp/test_mock_economy.py -k closet -v`
Expected: FAIL.

- [ ] **Step 3: Implement** the `closet` block + `create_closet` transitions + harvest/assemble guard in `webapp/mock_economy.py`, mirroring real semantics (first `create_closet` → pending with a fake accept link; a follow-up confirm → active).

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest webapp/test_mock_economy.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add webapp/mock_economy.py webapp/test_mock_economy.py
git commit -m "feat(dev): mock Closet lifecycle + gating for WEBAPP_DEV_MODE"
```

---

### Task 8: Frontend — Closet states in the Dressing Room

**Files:**
- Modify: `webapp/client/app.js`, `webapp/client/index.html`, `webapp/client/style.css`
- Test: `tests/test_app_js_boot.py`

**Interfaces:**
- Consumes: `GET /api/economy` `closet` block; `POST /api/closet` `{status, accept}`.
- Produces: Dressing Room renders one of three Closet states and only enables Harvest/Assemble when `active`. The post-harvest "Claim your Closet" block (`app.js` ~887, already renamed in Task 1) is removed.

- [ ] **Step 1: Write failing test** in `tests/test_app_js_boot.py` (static-assertion style, matching the file):

```python
def test_app_js_has_closet_states():
    src = (ROOT / "webapp/client/app.js").read_text()
    assert "Create your Closet" in src
    assert "Finish claiming your Closet" in src
    assert "/api/closet" in src
    assert "closet.status" in src or "economyState.closet" in src
    # post-harvest claim block is gone:
    assert "Claim your Closet" not in src.replace("Finish claiming your Closet", "")
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_app_js_boot.py -k closet -v`
Expected: FAIL.

- [ ] **Step 3: Implement** in `app.js`:
  - In `openDressup()` after loading `economyState`, branch on `economyState.closet.status`:
    - `none` → show a `[ Create your Closet ]` button calling `await api('/api/closet', {method:'POST'})` then re-loading state; render the returned `accept` QR via the existing `showFlow(...)`.
    - `pending_accept` → show `[ Finish claiming your Closet ]` that re-`POST`s `/api/closet` and re-shows the accept QR (the endpoint regenerates it).
    - `active` → render roster/canvas/closet as today; enable Harvest/Assemble.
  - Disable/hide `dressup-harvest-btn` and the assemble `＋` tile unless `status === 'active'`.
  - Delete the `if (final.accept) { showFlow({ title: '👜 Claim your Closet', ... }) }` block in `harvestActive()`.

Add the button markup to `index.html` (a `closet-gate` container) and minimal styling to `style.css`.

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/test_app_js_boot.py -q && node --check webapp/client/app.js`
Expected: PASS + OK.

- [ ] **Step 5: Commit**

```bash
git add webapp/client/app.js webapp/client/index.html webapp/client/style.css tests/test_app_js_boot.py
git commit -m "feat(activity): Closet create/claim states gate the Dressing Room"
```

---

### Task 9: On-chain re-mint migration script

**Files:**
- Create: `scripts/migrate_bucket_to_closet.py`
- Test: `tests/test_economy_scripts_import.py` (import smoke) + `tests/test_migrate_bucket_to_closet.py` (logic with fakes)

**Interfaces:**
- Consumes: `closet_token.ensure_closet`/`build_closet_metadata`, `economy_store.read_closet_assets/read_closet_bodies/get_closet_record`, `_economy_deps`.
- Produces: `migrate_owner(conn, owner, deps) -> dict` — for an owner whose recorded Closet is under the legacy taxon (or whose `lfg_bucket` metadata predates the rename), mints a NEW Closet under `CLOSET_TAXON`, offers it, copies the owner's current asset/body contents into it (`sync_closet`), records the abandoned legacy token id, and leaves the new closet `pending_accept` for the user to accept. Idempotent (skips owners already on the new taxon).

- [ ] **Step 1: Write failing test** in `tests/test_migrate_bucket_to_closet.py` driving `migrate_owner` with injected fakes (reuse the harvest `_Fakes` shape): assert a new nft_id is minted, contents are synced into it, the old id is recorded as abandoned, status is `pending_accept`, and a second run is a no-op.

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_migrate_bucket_to_closet.py -v`
Expected: FAIL (module absent).

- [ ] **Step 3: Implement** `scripts/migrate_bucket_to_closet.py` with an `argparse` CLI (`--network`, `--owner` optional → all owners) mirroring `economy_harvest.py`'s structure, plus the testable `migrate_owner(conn, owner, deps)` function. Detect "needs migration" by the recorded token's on-ledger taxon (via `nft_info`) being the legacy taxon; skip when already `CLOSET_TAXON`. Print a summary per owner.

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/test_migrate_bucket_to_closet.py tests/test_economy_scripts_import.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/migrate_bucket_to_closet.py tests/test_migrate_bucket_to_closet.py
git commit -m "feat(migration): re-mint legacy Buckets as Closets under CLOSET_TAXON"
```

---

### Task 10: Docs + final gate

**Files:**
- Modify: `CLAUDE.md` (the Bucket/economy sections → Closet), `.env` example keys (`BUCKET_*`→`CLOSET_*` + `CLOSET_TAXON`).

- [ ] **Step 1:** Update `CLAUDE.md` economy/Bucket prose to "Closet", document `CLOSET_TAXON`/`LEGACY_BUCKET_TAXON` and the `none→pending_accept→active` lifecycle, and the migration script invocation.

- [ ] **Step 2: Full gate**

Run: `.venv/bin/ruff format . && .venv/bin/ruff check . && .venv/bin/mypy . && .venv/bin/python -m pytest -q && node --check webapp/client/app.js`
Expected: all green.

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md .env.example 2>/dev/null; git add -A
git commit -m "docs(closet): document Closet lifecycle, taxon, and migration"
```

---

## Self-Review

**Spec coverage:** standalone issuance (Tasks 3,6,8) · accepted-by-user gate (Tasks 3,4) · full rename (Task 1) · new CLOSET_TAXON + re-mint (Tasks 1,9) · dual-read parser + dual-taxon listener (Tasks 1,5) · register-path issuance (Task 6) · pending/active state machine (Tasks 2–5) · dev-mode parity (Task 7) · DB migration (Task 1 step 6) · merge #104 first (Global Constraints). All spec sections map to a task.

**Type consistency:** `ensure_closet`/`confirm_accept`/`ClosetRef` signatures match across Tasks 3,4,6,9. `get_closet_record` returns `(nft_id, uri_hex, status, offer_id)` consistently. `closet_owner_fn`/`OwnerFn` consistent across flow + deps. Status constants `PENDING_ACCEPT`/`ACTIVE` defined once (Task 2) and imported.

**Placeholder scan:** test sketches in Tasks 5–7 say "follow the file's existing fixtures/harness" because those fakes already exist in-repo and must be matched rather than reinvented — the asserted behavior and target symbols are concrete.
