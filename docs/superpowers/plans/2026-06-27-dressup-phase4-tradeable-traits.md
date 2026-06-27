# Dress-up Phase 4 — Tradeable Trait NFTokens (Extract / Deposit) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make loose Closet traits independently tradeable via Extract (Closet trait → standalone NFToken) and Deposit (trait NFToken → burned back into a Closet), bringing the dormant `trait_tokens` table live while preserving supply conservation.

**Architecture:** Two new async state-machine flows (Extract = Assemble-shaped reversible mint; Deposit = Harvest-shaped irreversible burn) added to `lfg_core/economy_flow.py` alongside harvest/assemble/equip — reusing its private helpers (`_require_active_closet`, `_owner_contents`, `_sync_then_persist`, `_write_record`). A new pure `lfg_core/trait_token.py` holds trait metadata; the listener brings `trait_tokens` live (mint/transfer/burn); service endpoints + Activity UI + CLI expose the ops. Extract/Deposit are supply-neutral (a trait moves between the Closet tally and the `trait_tokens` tally, both already counted by `asset_census`), so **no `supply_changes` rows**.

**Tech Stack:** Python 3 (aiohttp service, sqlite3, xrpl-py), vanilla JS (Discord Activity), pytest (repo-native sync style: `asyncio.new_event_loop()` + direct call, NOT pytest-asyncio).

**Spec:** `docs/superpowers/specs/2026-06-27-dressup-phase4-tradeable-traits-design.md`

## Global Constraints

- **Decomposition deviation from spec:** the spec named `extract_flow.py`/`deposit_flow.py`; instead the flows live in `lfg_core/economy_flow.py` with harvest/assemble/equip (they reuse that module's private helpers and match the existing flows pattern). `lfg_core/trait_token.py` holds only the pure trait metadata helpers.
- **Supply-neutral:** Extract/Deposit write NO `supply_changes` rows. Conservation holds because `trait_economy.asset_census` already tallies `trait_tokens`. Verify with a round-trip auditor test.
- **Trait token:** `TRAIT_TAXON` (new, distinct from `SWAP_TAXON=1760`/`CLOSET_TAXON=1762`/`LEGACY_BUCKET_TAXON=1761`); `TRAIT_NFT_FLAGS = 9` (burnable+transferable, NOT mutable). `xrpl_ops.mint_nft` auto-applies `NFT_TRANSFER_FEE` (7000) to any transferable token — so the 70% royalty is inherited; NO new fee constant.
- **Active-Closet gate:** Extract and Deposit both require an active Closet — reuse `economy_flow._require_active_closet` (surface 400 `"Create and claim your Closet first."` + flow precondition).
- **Deposit is fail-closed:** verify on-ledger owner == depositor AND taxon==TRAIT_TAXON AND issuer==SWAP_ISSUER_ADDRESS before the irreversible burn; any mismatch/uncertainty refuses (no asset loss).
- **SourceTag `2606160021`** on every XRPL tx/payload — stamped by `xrpl_ops` builders; never hand-build a tx dict.
- **DB authoritative for accounting; token metadata/ownership is the on-chain truth** the listener rebuilds from. Flows write the token before the DB.
- **mypy:** `lfg_core` full `--strict`; run `.venv/bin/mypy .` (full, not per-file). `tests/*` is `ignore_errors=true`. Repo-native sync tests; parametrize generics (`dict[str, Any]`).
- **Free ops** (no XRP/BRIX cost).
- **Run the full suite green** after each task: `.venv/bin/python -m pytest -q`.
- **Status constants** (reuse from `economy_flow.py`): `RUNNING`/`DONE`/`FAILED`.

---

### Task 1: Trait token config + pure metadata (`trait_token.py`)

**Files:**
- Modify: `lfg_core/config.py`
- Create: `lfg_core/trait_token.py`
- Test: `tests/test_trait_token.py`

**Interfaces:**
- Produces:
  - `config.TRAIT_TAXON: int`, `config.TRAIT_NFT_FLAGS: int`, `config.TRAIT_CDN_SUBDIR: str`
  - `trait_token.build_trait_metadata(slot: str, value: str, image_url: str) -> dict[str, Any]`
  - `trait_token.parse_trait_metadata(meta: dict[str, Any]) -> tuple[str, str] | None`

- [ ] **Step 1: config constants.** In `lfg_core/config.py`, after the Closet constants block:

```python
# Standalone tradeable trait NFTokens (Phase 4). Burnable + transferable (NOT
# soulbound, NOT mutable); xrpl_ops.mint_nft applies NFT_TRANSFER_FEE to any
# transferable token, so the trait royalty is inherited (no separate constant).
TRAIT_TAXON = int(os.getenv("TRAIT_TAXON", "1763"))
TRAIT_NFT_FLAGS = int(os.getenv("TRAIT_NFT_FLAGS", "9"))  # burnable(1)+transferable(8)
TRAIT_CDN_SUBDIR = os.getenv("TRAIT_CDN_SUBDIR", "traits")
```

- [ ] **Step 2: Write failing test** `tests/test_trait_token.py`:

```python
from lfg_core import trait_token as tt


def test_build_and_parse_roundtrip():
    meta = tt.build_trait_metadata("Hat", "Red Cap", "https://cdn/x.png")
    assert meta["lfg_trait"] == {"slot": "Hat", "value": "Red Cap"}
    assert meta["image"] == "https://cdn/x.png"
    assert "Hat" in meta["name"] and "Red Cap" in meta["name"]
    assert tt.parse_trait_metadata(meta) == ("Hat", "Red Cap")


def test_parse_tolerates_garbage():
    assert tt.parse_trait_metadata({}) is None
    assert tt.parse_trait_metadata({"lfg_trait": {"slot": "Hat"}}) is None  # missing value
    assert tt.parse_trait_metadata({"lfg_trait": "nope"}) is None
```

- [ ] **Step 3: Run to verify fail** — `.venv/bin/python -m pytest tests/test_trait_token.py -v` → FAIL (module absent).

- [ ] **Step 4: Implement** `lfg_core/trait_token.py`:

```python
# lfg_core/trait_token.py
# Pure metadata for the standalone tradeable trait NFToken (Phase 4). The
# `lfg_trait` block is the on-chain record of which (slot, value) the token
# represents; the listener rebuilds the trait_tokens table from it.

from __future__ import annotations

from typing import Any

from lfg_core import config


def build_trait_metadata(slot: str, value: str, image_url: str) -> dict[str, Any]:
    return {
        "schema": config.NFT_SCHEMA_URL,
        "name": f"LFG Trait — {slot}: {value}",
        "description": f"A tradeable {slot} trait ({value}) extracted from an LFG Closet.",
        "image": image_url,
        "external_link": config.EXTERNAL_WEBSITE_URL,
        "collection": {"name": "LFG Traits", "family": config.NFT_COLLECTION_NAME},
        "lfg_trait": {"slot": slot, "value": value},
    }


def parse_trait_metadata(meta: dict[str, Any]) -> tuple[str, str] | None:
    """Read (slot, value) back out of a trait NFToken's metadata. Tolerant of
    missing/garbage fields (the listener consumes untrusted on-chain metadata)."""
    block = meta.get("lfg_trait")
    if not isinstance(block, dict):
        return None
    slot, value = block.get("slot"), block.get("value")
    if isinstance(slot, str) and isinstance(value, str):
        return (slot, value)
    return None
```

- [ ] **Step 5: Run to verify pass** — `.venv/bin/python -m pytest tests/test_trait_token.py -q` → PASS.

- [ ] **Step 6: Commit**

```bash
git add lfg_core/config.py lfg_core/trait_token.py tests/test_trait_token.py
git commit -m "feat(traits): TRAIT_TAXON config + pure trait metadata helpers"
```

---

### Task 2: `trait_tokens` store accessors

**Files:**
- Modify: `lfg_core/economy_store.py`
- Test: `tests/test_economy_store_phase2.py`

**Interfaces:**
- Consumes: existing `trait_tokens(nft_id PK, owner, slot, value)` table + `read_trait_tokens`.
- Produces:
  - `economy_store.upsert_trait_token(conn, nft_id: str, owner: str, slot: str, value: str) -> None`
  - `economy_store.delete_trait_token(conn, nft_id: str) -> None`

- [ ] **Step 1: Write failing test** in `tests/test_economy_store_phase2.py`:

```python
def test_trait_token_upsert_and_delete():
    import sqlite3
    from lfg_core import economy_store as es
    c = sqlite3.connect(":memory:"); es.init_economy_schema(c)
    es.upsert_trait_token(c, "NFT1", "rA", "Hat", "Cap")
    assert ("NFT1", "rA", "Hat", "Cap") in es.read_trait_tokens(c)
    es.upsert_trait_token(c, "NFT1", "rB", "Hat", "Cap")  # ownership transfer
    rows = es.read_trait_tokens(c)
    assert ("NFT1", "rB", "Hat", "Cap") in rows and len(rows) == 1
    es.delete_trait_token(c, "NFT1")
    assert es.read_trait_tokens(c) == []
```

- [ ] **Step 2: Run to verify fail** — `.venv/bin/python -m pytest tests/test_economy_store_phase2.py::test_trait_token_upsert_and_delete -v` → FAIL.

- [ ] **Step 3: Implement** in `lfg_core/economy_store.py` (near `read_trait_tokens`):

```python
def upsert_trait_token(conn: sqlite3.Connection, nft_id: str, owner: str, slot: str, value: str) -> None:
    """Insert/replace a standalone trait NFToken row (PK nft_id). Used by the
    listener (mint/transfer rebuild) and the extract flow (optimistic write)."""
    conn.execute(
        """
        INSERT INTO trait_tokens (nft_id, owner, slot, value) VALUES (?, ?, ?, ?)
        ON CONFLICT(nft_id) DO UPDATE SET owner=excluded.owner, slot=excluded.slot, value=excluded.value
        """,
        (nft_id, owner, slot, value),
    )
    conn.commit()


def delete_trait_token(conn: sqlite3.Connection, nft_id: str) -> None:
    conn.execute("DELETE FROM trait_tokens WHERE nft_id = ?", (nft_id,))
    conn.commit()
```

- [ ] **Step 4: Run to verify pass** — `.venv/bin/python -m pytest tests/test_economy_store_phase2.py -q` → PASS.

- [ ] **Step 5: Commit**

```bash
git add lfg_core/economy_store.py tests/test_economy_store_phase2.py
git commit -m "feat(traits): trait_tokens upsert/delete accessors"
```

---

### Task 3: Extract flow (`run_extract` in `economy_flow.py`)

**Files:**
- Modify: `lfg_core/economy_flow.py`
- Test: `tests/test_economy_flow_extract.py`

**Interfaces:**
- Consumes: `_require_active_closet`, `_owner_contents`, `_sync_then_persist`, `_write_record`, `RUNNING/DONE/FAILED`, `economy_store.upsert_trait_token`, `trait_token.build_trait_metadata`.
- Produces:
  - New `EconomyDeps` fields (all Optional, defaults `None`): `trait_compose_fn: TraitComposeFn | None`, `trait_upload_fn: bt.UploadFn | None`, `trait_mint_fn: bt.MintFn | None`, `trait_burn_fn: BurnFn | None`. (Offer/accept reuse `closet_offer_fn`/`closet_accept_fn`.)
  - `TraitComposeFn = Callable[[str, str], Awaitable[str]]` (slot, value) -> image_url.
  - `ExtractSession(owner: str, slot: str, value: str)` dataclass with `state, error, nft_id, accept, id`.
  - `run_extract(session: ExtractSession, deps: EconomyDeps) -> None`.

- [ ] **Step 1: Write failing tests** `tests/test_economy_flow_extract.py` (mirror `tests/test_economy_flow_harvest.py`'s `_Fakes`/`_deps`/`_conn_with_genesis`; add trait fakes). Key tests:

```python
import asyncio, sqlite3
from lfg_core import economy_flow as ef
from lfg_core import economy_store as es
from lfg_core import closet_token as ct


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _F:
    def __init__(self, *, fail_sync=False):
        self.minted, self.burns, self.uploads = [], [], 0
        self.modifies = 0
        self.fail_sync = fail_sync
    async def trait_compose(self, slot, value): return f"https://cdn/trait/{slot}-{value}.png"
    async def trait_upload(self, meta): self.uploads += 1; return f"https://cdn/t/{self.uploads}.json"
    async def trait_mint(self, url): nid = f"TRAIT{len(self.minted)}"; self.minted.append(nid); return nid
    async def trait_burn(self, nft_id, owner): self.burns.append((nft_id, owner)); return "BURN"
    async def closet_upload(self, meta): return "https://cdn/c.json"
    async def closet_modify(self, nft_id, owner, url):
        if self.fail_sync: return None
        self.modifies += 1; return "MOD"
    async def closet_offer(self, nft_id, owner): return "OFFER"
    async def closet_accept(self, offer_id): return {"xumm_url": "x"}
    async def closet_owner(self, nft_id): return "rUser"


def _deps(conn, f, tmp):
    return ef.EconomyDeps(
        conn=conn,
        closet_upload_fn=f.closet_upload, closet_mint_fn=f.trait_mint,
        closet_offer_fn=f.closet_offer, closet_accept_fn=f.closet_accept,
        closet_modify_fn=f.closet_modify,
        char_compose_fn=None, char_mint_fn=None, char_modify_fn=None,
        char_burn_fn=None, char_offer_fn=f.closet_offer, char_accept_fn=f.closet_accept,
        closet_owner_fn=f.closet_owner,
        trait_compose_fn=f.trait_compose, trait_upload_fn=f.trait_upload,
        trait_mint_fn=f.trait_mint, trait_burn_fn=f.trait_burn,
        records_dir=str(tmp),
    )


def _active_closet_with_trait(conn, owner="rUser"):
    es.init_economy_schema(conn)
    es.set_closet_token(conn, owner, "CLOSET", "AB", status=ct.ACTIVE, offer_id=None)
    es.set_closet_contents(conn, owner, [("Hat", "Cap", 2)], [])


def test_extract_happy_path(tmp_path):
    conn = sqlite3.connect(":memory:"); _active_closet_with_trait(conn)
    f = _F(); s = ef.ExtractSession(owner="rUser", slot="Hat", value="Cap")
    _run(ef.run_extract(s, _deps(conn, f, tmp_path)))
    assert s.state == ef.DONE and s.nft_id == "TRAIT0"
    # Closet decremented to 1, trait_tokens has the new token
    assets = {(sl, v): n for o, sl, v, n in es.read_closet_assets(conn) if o == "rUser"}
    assert assets[("Hat", "Cap")] == 1
    assert ("TRAIT0", "rUser", "Hat", "Cap") in es.read_trait_tokens(conn)


def test_extract_rejected_without_active_closet(tmp_path):
    conn = sqlite3.connect(":memory:"); es.init_economy_schema(conn)
    f = _F(); s = ef.ExtractSession(owner="rUser", slot="Hat", value="Cap")
    _run(ef.run_extract(s, _deps(conn, f, tmp_path)))
    assert s.state == ef.FAILED and f.minted == []


def test_extract_rejected_when_trait_absent(tmp_path):
    conn = sqlite3.connect(":memory:"); _active_closet_with_trait(conn)
    f = _F(); s = ef.ExtractSession(owner="rUser", slot="Hat", value="Top Hat")  # not in closet
    _run(ef.run_extract(s, _deps(conn, f, tmp_path)))
    assert s.state == ef.FAILED and f.minted == []


def test_extract_burns_back_on_closet_sync_failure(tmp_path):
    conn = sqlite3.connect(":memory:"); _active_closet_with_trait(conn)
    f = _F(fail_sync=True); s = ef.ExtractSession(owner="rUser", slot="Hat", value="Cap")
    _run(ef.run_extract(s, _deps(conn, f, tmp_path)))
    assert s.state == ef.FAILED
    assert f.burns == [("TRAIT0", "")]          # compensating issuer burn
    assert es.read_trait_tokens(conn) == []      # no token row left
    assets = {(sl, v): n for o, sl, v, n in es.read_closet_assets(conn) if o == "rUser"}
    assert assets[("Hat", "Cap")] == 2           # closet untouched
```

- [ ] **Step 2: Run to verify fail** — `.venv/bin/python -m pytest tests/test_economy_flow_extract.py -v` → FAIL (`ExtractSession`/`run_extract` undefined).

- [ ] **Step 3: Implement** in `lfg_core/economy_flow.py`. Add the type alias near `ComposeFn`:

```python
TraitComposeFn = Callable[[str, str], Awaitable[str]]  # (slot, value) -> image_url
```

Add the new Optional fields to `EconomyDeps` (after `closet_owner_fn`, before `records_dir`):

```python
    trait_compose_fn: TraitComposeFn | None = None
    trait_upload_fn: bt.UploadFn | None = None
    trait_mint_fn: bt.MintFn | None = None
    trait_burn_fn: BurnFn | None = None
```

Add (import `trait_token as tt` at top; `from lfg_core import trait_token as tt`):

```python
@dataclass
class ExtractSession:
    owner: str
    slot: str
    value: str
    state: str = RUNNING
    error: str | None = None
    nft_id: str | None = None
    accept: dict[str, Any] | None = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def _record(self, status: str) -> dict[str, Any]:
        return {"op": "extract", "id": self.id, "owner": self.owner, "slot": self.slot,
                "value": self.value, "nft_id": self.nft_id, "status": status, "error": self.error}

    def fail(self, msg: str) -> None:
        self.state = FAILED
        self.error = msg


async def run_extract(session: ExtractSession, deps: EconomyDeps) -> None:
    """Extract a loose Closet trait into a standalone tradeable NFToken. Order:
    precheck (active Closet + trait present) -> compose+mint (reversible) ->
    decrement Closet + record trait_token -> burn-back on Closet failure ->
    offer+accept. Supply-neutral (no supply_changes)."""
    conn, owner, slot, value = deps.conn, session.owner, session.slot, session.value
    try:
        err = await _require_active_closet(deps, owner)
        if err:
            session.fail(err)
            return
        assets, bodies = _owner_contents(conn, owner)
        if assets.get((slot, value), 0) < 1:
            session.fail(f"no loose '{value}' {slot} in your Closet to extract")
            return

        image_url = await deps.trait_compose_fn(slot, value)  # type: ignore[misc]
        meta_url = await deps.trait_upload_fn(tt.build_trait_metadata(slot, value, image_url))  # type: ignore[misc]
        _write_record(deps.records_dir, "extract", session.id, session._record("minting"))

        nft_id = await deps.trait_mint_fn(meta_url)  # type: ignore[misc]
        if not nft_id:
            session.fail(f"failed to mint trait token for {value} {slot}; your Closet is untouched")
            _write_record(deps.records_dir, "extract", session.id, session._record("failed_mint"))
            return
        session.nft_id = nft_id
        _write_record(deps.records_dir, "extract", session.id, session._record("minted"))

        assets[(slot, value)] = assets.get((slot, value), 0) - 1
        try:
            await _sync_then_persist(deps, owner, assets, bodies)
            es.upsert_trait_token(conn, nft_id, owner, slot, value)
        except Exception as e:
            revert = await deps.trait_burn_fn(nft_id, "")  # type: ignore[misc]
            if revert:
                session.nft_id = None
                session.fail(f"extract failed updating the Closet ({e}); your Closet is untouched")
                _write_record(deps.records_dir, "extract", session.id, session._record("reverted_mint"))
            else:
                session.fail(
                    f"extract failed updating the Closet ({e}) and the compensating burn of "
                    f"{nft_id} failed — admin must burn it (journal {session.id})")
                _write_record(deps.records_dir, "extract", session.id, session._record("failed_revert_mint"))
            return

        offer_id = await deps.closet_offer_fn(nft_id, owner)
        session.accept = await deps.closet_accept_fn(offer_id) if offer_id else None
        session.state = DONE
        _write_record(deps.records_dir, "extract", session.id, session._record("complete"))
    except Exception as e:
        logging.error(f"Extract {session.id} failed: {traceback.format_exc()}")
        session.fail(str(e))
```

- [ ] **Step 4: Run to verify pass** — `.venv/bin/python -m pytest tests/test_economy_flow_extract.py tests/test_economy_flow_harvest.py -q` → PASS (existing harvest tests must still pass — the new Optional deps fields don't break them).

- [ ] **Step 5: Commit**

```bash
git add lfg_core/economy_flow.py tests/test_economy_flow_extract.py
git commit -m "feat(traits): extract flow (Closet trait -> tradeable NFToken)"
```

---

### Task 4: Deposit flow (`run_deposit` in `economy_flow.py`)

**Files:**
- Modify: `lfg_core/economy_flow.py`
- Test: `tests/test_economy_flow_deposit.py`

**Interfaces:**
- Consumes: `_require_active_closet`, `_owner_contents`, `_sync_then_persist`, `_write_record`, `economy_store.delete_trait_token`, `trait_token.parse_trait_metadata`, `deps.closet_owner_fn` (on-ledger owner), `deps.trait_burn_fn`. Plus two new Optional `EconomyDeps` fields: `trait_info_fn: TraitInfoFn | None` (nft_id -> {taxon, issuer, owner} | None) and `trait_meta_fn: TraitMetaFn | None` (nft_id -> metadata dict | None).
- Produces:
  - `TraitInfoFn = Callable[[str], Awaitable[dict[str, Any] | None]]`, `TraitMetaFn = Callable[[str], Awaitable[dict[str, Any] | None]]`.
  - `DepositSession(owner: str, nft_id: str)` with `state, error, slot, value, burn_hash, id`.
  - `run_deposit(session: DepositSession, deps: EconomyDeps) -> None`.

- [ ] **Step 1: Write failing tests** `tests/test_economy_flow_deposit.py` (reuse the extract test's `_F`/`_deps` shape; extend with `trait_info`/`trait_meta`):

```python
# additions to the fake:
#   self.owner_for = {nft_id: addr}     # on-ledger owner
#   async def trait_info(self, nft_id): return {"taxon": config.TRAIT_TAXON,
#       "issuer": config.SWAP_ISSUER_ADDRESS, "owner": self.owner_for.get(nft_id)}
#   async def trait_meta(self, nft_id): return {"lfg_trait": {"slot": "Hat", "value": "Cap"}}
#   wire trait_info_fn=f.trait_info, trait_meta_fn=f.trait_meta into _deps

def test_deposit_happy_path(tmp_path):
    # active closet (empty contents ok), depositor owns TRAIT9
    # run_deposit -> state DONE; trait burned; closet credited +1 (Hat, Cap);
    # trait_tokens row deleted
    ...

def test_deposit_rejected_without_active_closet(tmp_path): ...   # FAILED, no burn

def test_deposit_rejects_foreign_token(tmp_path):
    # trait_info returns taxon != TRAIT_TAXON  -> FAILED, no burn
    ...

def test_deposit_fail_closed_when_owner_mismatch(tmp_path):
    # trait_info owner != depositor -> FAILED, f.burns == []
    ...

def test_deposit_burn_then_credit_fails_journals(tmp_path):
    # fail_sync=True -> burn happened, FAILED, journal status deposited_pending_closet,
    # trait NOT credited to closet, trait_tokens row still removed-or-journaled (assert journal)
    ...
```

Write each test body concretely following `test_economy_flow_harvest.py`'s journal-assertion style (read `tmp_path / f"deposit-{session.id}.json"`).

- [ ] **Step 2: Run to verify fail** → FAIL (`DepositSession`/`run_deposit` undefined).

- [ ] **Step 3: Implement** in `lfg_core/economy_flow.py`:

```python
TraitInfoFn = Callable[[str], Awaitable[dict[str, Any] | None]]
TraitMetaFn = Callable[[str], Awaitable[dict[str, Any] | None]]
```

Add Optional fields to `EconomyDeps`: `trait_info_fn: TraitInfoFn | None = None`, `trait_meta_fn: TraitMetaFn | None = None`.

```python
@dataclass
class DepositSession:
    owner: str
    nft_id: str
    state: str = RUNNING
    error: str | None = None
    slot: str | None = None
    value: str | None = None
    burn_hash: str | None = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def _record(self, status: str) -> dict[str, Any]:
        return {"op": "deposit", "id": self.id, "owner": self.owner, "nft_id": self.nft_id,
                "slot": self.slot, "value": self.value, "burn_hash": self.burn_hash,
                "status": status, "error": self.error}

    def fail(self, msg: str) -> None:
        self.state = FAILED
        self.error = msg


async def run_deposit(session: DepositSession, deps: EconomyDeps) -> None:
    """Deposit a standalone trait NFToken back into the owner's Closet. Order:
    precheck (active Closet + token is ours + on-ledger owner == depositor) ->
    issuer BURN (irreversible) -> credit Closet + delete trait_token row ->
    journal on credit failure. Supply-neutral. Fail-closed on any ownership
    uncertainty."""
    conn, owner, nft_id = deps.conn, session.owner, session.nft_id
    try:
        err = await _require_active_closet(deps, owner)
        if err:
            session.fail(err)
            return
        info = await deps.trait_info_fn(nft_id)  # type: ignore[misc]
        if not info:
            session.fail("could not verify the trait token on-ledger; nothing was changed")
            return
        if int(info.get("taxon") or -1) != config.TRAIT_TAXON or info.get("issuer") != config.SWAP_ISSUER_ADDRESS:
            session.fail("that NFToken is not an LFG trait token")
            return
        if info.get("owner") != owner:
            session.fail("you do not own that trait token on-ledger; nothing was changed")
            return
        meta = await deps.trait_meta_fn(nft_id)  # type: ignore[misc]
        parsed = tt.parse_trait_metadata(meta or {})
        if parsed is None:
            session.fail("that trait token has unreadable metadata; nothing was changed")
            return
        session.slot, session.value = parsed
        _write_record(deps.records_dir, "deposit", session.id, session._record("depositing"))

        burn_hash = await deps.trait_burn_fn(nft_id, owner)  # type: ignore[misc]
        if not burn_hash:
            session.fail(f"failed to burn trait token {nft_id}; nothing was lost")
            _write_record(deps.records_dir, "deposit", session.id, session._record("failed_burn"))
            return
        session.burn_hash = burn_hash
        es.delete_trait_token(conn, nft_id)
        _write_record(deps.records_dir, "deposit", session.id, session._record("burned"))

        assets, bodies = _owner_contents(conn, owner)
        assets[(session.slot, session.value)] = assets.get((session.slot, session.value), 0) + 1
        try:
            await _sync_then_persist(deps, owner, assets, bodies)
        except Exception as e:
            session.fail(
                f"trait burned but Closet credit failed ({e}); recorded in the journal "
                f"({session.id}) for recovery")
            _write_record(deps.records_dir, "deposit", session.id, session._record("deposited_pending_closet"))
            return

        session.state = DONE
        _write_record(deps.records_dir, "deposit", session.id, session._record("complete"))
    except Exception as e:
        logging.error(f"Deposit {session.id} failed: {traceback.format_exc()}")
        session.fail(str(e))
```

- [ ] **Step 4: Run to verify pass** — `.venv/bin/python -m pytest tests/test_economy_flow_deposit.py tests/test_economy_flow_extract.py -q` → PASS.

- [ ] **Step 5: Commit**

```bash
git add lfg_core/economy_flow.py tests/test_economy_flow_deposit.py
git commit -m "feat(traits): deposit flow (trait NFToken -> burned back into Closet)"
```

---

### Task 5: Conservation round-trip auditor test

**Files:**
- Test: `tests/test_trait_economy_phase2.py` (or `tests/test_trait_economy.py` — pick the one already exercising `asset_census`/`verify_conservation`)

**Interfaces:** Consumes `trait_economy.asset_census`, `trait_economy.verify_conservation`, `economy_store.read_*`.

- [ ] **Step 1: Write the test** asserting supply-neutrality across extract+deposit at the census level (no flow needed — operate the store directly to model the moves):

```python
def test_extract_then_deposit_conserves_census():
    import sqlite3
    from lfg_core import economy_store as es, trait_economy as te
    c = sqlite3.connect(":memory:"); es.init_economy_schema(c)
    genesis = te.Genesis(trait_counts={("Hat", "Cap"): 1}, edition_bodies={})
    es.freeze_genesis(c, genesis, {})
    # trait starts loose in a Closet
    es.set_closet_contents(c, "rA", [("Hat", "Cap", 1)], [])
    base = te.asset_census([], es.read_closet_assets(c), es.read_trait_tokens(c))
    # EXTRACT: Closet -1, trait_tokens +1
    es.set_closet_contents(c, "rA", [("Hat", "Cap", 0)], [])
    es.upsert_trait_token(c, "T1", "rA", "Hat", "Cap")
    after_extract = te.asset_census([], es.read_closet_assets(c), es.read_trait_tokens(c))
    assert after_extract == base                      # census unchanged
    # DEPOSIT: trait_tokens -1, Closet +1
    es.delete_trait_token(c, "T1")
    es.set_closet_contents(c, "rA", [("Hat", "Cap", 1)], [])
    after_deposit = te.asset_census([], es.read_closet_assets(c), es.read_trait_tokens(c))
    assert after_deposit == base
    assert te.verify_conservation(te.effective_genesis(genesis, []), after_deposit, []).ok
```

(Adjust `asset_census`/`verify_conservation` call signatures to the actual ones — read `lfg_core/trait_economy.py` first; the test must use the real signatures.)

- [ ] **Step 2: Run** — `.venv/bin/python -m pytest tests/test_trait_economy_phase2.py -k conserves -q`. If it fails because the census already balances trivially, that confirms supply-neutrality; make the assertions reflect the real census shape. Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/test_trait_economy_phase2.py
git commit -m "test(traits): extract+deposit conserve the asset census"
```

---

### Task 6: Listener brings `trait_tokens` live

**Files:**
- Modify: `lfg_core/nft_listener.py`, `scripts/onchain_listener.py`
- Test: `tests/test_economy_listener.py`, `tests/test_onchain_listener.py`

**Interfaces:**
- Consumes: `economy_store.upsert_trait_token/delete_trait_token`, `trait_token.parse_trait_metadata`, `config.TRAIT_TAXON`.
- Produces: `apply_economy_tx` processes a `TRAIT_TAXON` token on mint/accept (upsert with current owner) and on burn (delete); `onchain_listener` dispatch filter includes `burn`.

- [ ] **Step 1: Write failing tests** in `tests/test_economy_listener.py` (mirror the closet listener tests; drive `apply_economy_tx`):

```python
def test_trait_mint_inserts_row():
    # mint of a TRAIT_TAXON token owned by rUser, meta lfg_trait{Hat,Cap}
    # -> read_trait_tokens contains (nft_id, rUser, Hat, Cap)
    ...

def test_trait_transfer_updates_owner():
    # seed a trait_tokens row; an accept-kind tx whose post-transfer owner is rNew
    # -> row owner becomes rNew
    ...

def test_trait_burn_deletes_row():
    # seed a trait_tokens row; a burn-kind tx for that nft_id (taxon TRAIT_TAXON)
    # -> row removed
    ...
```

And in `tests/test_onchain_listener.py` (mirror the existing `process_stream_tx` accept test added in #105):

```python
def test_listen_path_burn_deletes_trait_token():
    # drive a TRAIT_TAXON NFTokenBurn through process_stream_tx -> row deleted
    ...
```

- [ ] **Step 2: Run to verify fail** → FAIL.

- [ ] **Step 3: Implement.** In `lfg_core/nft_listener.py` add (import `trait_token`):

```python
def _apply_trait_token(conn: sqlite3.Connection, kind: str, token: dict[str, Any], metadata: Any) -> None:
    """Maintain the trait_tokens table from a standalone trait NFToken's chain
    events: mint/accept upsert (current owner), burn deletes."""
    nft_id = token["nft_id"]
    if kind == "burn" or token.get("is_burned"):
        economy_store.delete_trait_token(conn, nft_id)
        return
    owner = token.get("owner")
    parsed = trait_token.parse_trait_metadata(metadata if isinstance(metadata, dict) else {})
    if owner and parsed:
        economy_store.upsert_trait_token(conn, nft_id, owner, parsed[0], parsed[1])
```

In `apply_economy_tx`, widen the kind filter to include `"burn"` and add the trait branch before the growth branch:

```python
    if kind not in ("mint", "modify", "accept", "burn"):
        return
    ...
            taxon = int(token.get("taxon") or -1)
            if taxon in (config.CLOSET_TAXON, config.LEGACY_BUCKET_TAXON):
                _apply_closet(conn, token, metadata)
            elif taxon == config.TRAIT_TAXON:
                _apply_trait_token(conn, kind, token, metadata)
            elif kind == "mint":
                _apply_possible_growth(conn, token, metadata, genesis)
```

(Note: a burn returns a token whose `fetch_token_fn` may yield `is_burned=True` or minimal data — `_apply_trait_token` keys off `kind == "burn"` so it deletes regardless. Confirm `affected_nft_ids`/`classify_tx` already classify `NFTokenBurn` as `"burn"`.)

In `scripts/onchain_listener.py`, add `"burn"` to the economy dispatch filter (the one that currently lists `("mint", "modify", "accept")` after #105):

```python
    if nft_listener.classify_tx(tx) in ("mint", "modify", "accept", "burn") and economy_store.genesis_exists(conn):
```

- [ ] **Step 4: Run to verify pass** — `.venv/bin/python -m pytest tests/test_economy_listener.py tests/test_onchain_listener.py -q` → PASS.

- [ ] **Step 5: Commit**

```bash
git add lfg_core/nft_listener.py scripts/onchain_listener.py tests/test_economy_listener.py tests/test_onchain_listener.py
git commit -m "feat(listener): trait_tokens live (mint/transfer/burn)"
```

---

### Task 7: Real deps wiring (`_economy_deps.py`)

**Files:**
- Modify: `scripts/_economy_deps.py`
- Test: `tests/test_economy_scripts_import.py` (import-smoke covering the new callables) + a focused unit test for `_compose_trait`

**Interfaces:**
- Consumes: `xrpl_ops.mint_nft/burn_nft/nft_info`, `layer_store`, `cdn`, `config.TRAIT_TAXON/TRAIT_NFT_FLAGS/TRAIT_CDN_SUBDIR`.
- Produces: `build_economy_deps` wires `trait_compose_fn`, `trait_upload_fn`, `trait_mint_fn`, `trait_burn_fn`, `trait_info_fn`, `trait_meta_fn`.

- [ ] **Step 1: Write failing test** (in a new `tests/test_economy_deps_trait.py`) that builds deps and asserts the trait callables are present + that `_compose_trait` resolves the first body that has the layer (inject a fake `layer_store`):

```python
def test_build_economy_deps_wires_trait_callables():
    import sqlite3, scripts._economy_deps as deps
    d = deps.build_economy_deps(sqlite3.connect(":memory:"))
    for attr in ("trait_compose_fn", "trait_upload_fn", "trait_mint_fn",
                 "trait_burn_fn", "trait_info_fn", "trait_meta_fn"):
        assert getattr(d, attr) is not None
```

(Match the harness env-stub pattern from `tests/test_economy_scripts_import.py`.)

- [ ] **Step 2: Run to verify fail** → FAIL.

- [ ] **Step 3: Implement** in `scripts/_economy_deps.py`:

```python
async def _compose_trait(slot: str, value: str) -> str:
    """Resolve the bare trait layer for (slot, value) from the first body that
    has it and upload it as a transparent trait image."""
    store = layer_store.get_layer_store()
    for body in await store.list_bodies():
        path = await store.resolve(body, slot, value)
        if path:
            with open(path, "rb") as fh:
                data = fh.read()
            ext = os.path.splitext(path)[1].lstrip(".") or "png"
            return await _upload(
                f"{config.TRAIT_CDN_SUBDIR}/{uuid.uuid4().hex}.{ext}", data, f"image/{ext}"
            )
    raise RuntimeError(f"no layer found for {slot}={value}")


async def _trait_info(nft_id: str) -> dict[str, Any] | None:
    return await xrpl_ops.nft_info(nft_id)


async def _trait_meta(nft_id: str) -> dict[str, Any] | None:
    info = await xrpl_ops.nft_info(nft_id)
    uri_hex = (info or {}).get("uri_hex") or ""
    return await cdn.fetch_json_by_uri_hex(uri_hex) if uri_hex else None
```

(For `_trait_meta`, use whatever helper the listener uses to fetch metadata from a uri_hex — read `lfg_core/nft_listener.py`'s `fetch_meta_fn` wiring / `scripts/onchain_listener.py` to find the exact function name and reuse it; do NOT invent `cdn.fetch_json_by_uri_hex` if a different one exists.)

In `build_economy_deps(...)` add:

```python
        trait_compose_fn=lambda slot, value: _compose_trait(slot, value),
        trait_upload_fn=_upload_bucket,  # reuse the JSON metadata uploader
        trait_mint_fn=lambda url: xrpl_ops.mint_nft(
            url, config.TRAIT_TAXON, config.SWAP_ISSUER_ADDRESS, flags=config.TRAIT_NFT_FLAGS
        ),
        trait_burn_fn=lambda nft_id, owner: xrpl_ops.burn_nft(nft_id, owner or None),
        trait_info_fn=lambda nft_id: _trait_info(nft_id),
        trait_meta_fn=lambda nft_id: _trait_meta(nft_id),
```

(`_upload_bucket` uploads a metadata dict to a unique JSON path — reuse it; the name is historical.)

- [ ] **Step 4: Run to verify pass** — `.venv/bin/python -m pytest tests/test_economy_deps_trait.py tests/test_economy_scripts_import.py -q` → PASS; `.venv/bin/mypy .` clean.

- [ ] **Step 5: Commit**

```bash
git add scripts/_economy_deps.py tests/test_economy_deps_trait.py
git commit -m "feat(traits): wire real extract/deposit deps (mint/burn/compose/info/meta)"
```

---

### Task 8: Service endpoints + economy state

**Files:**
- Modify: `webapp/economy_api.py`, `lfg_service/app.py`
- Test: `webapp/test_economy_api.py`

**Interfaces:**
- Consumes: `economy_flow.ExtractSession/run_extract/DepositSession/run_deposit`, `economy_store.read_trait_tokens/get_closet_record`, `_economy_deps.build_economy_deps`, `closet_token.ACTIVE`.
- Produces: `economy_api.start_extract(user_id, wallet, body)` and `start_deposit(...)`; `read_economy_state` gains `trait_tokens`; routes `POST /api/extract`, `POST /api/deposit`; `economy_session_dict` handles the `extract`/`deposit` kinds.

- [ ] **Step 1: Write failing test** in `webapp/test_economy_api.py` (follow its harness): `read_economy_state` includes `trait_tokens` (list filtered to the wallet); `start_extract`/`start_deposit` raise `EconomyError("Create and claim your Closet first.")` when the wallet's closet is not active. Write concretely against the file's existing seed/mocked patterns.

- [ ] **Step 2: Run to verify fail** → FAIL (`trait_tokens` key absent / `start_extract` undefined).

- [ ] **Step 3: Implement.** In `webapp/economy_api.py`:
  - In `read_economy_state(conn, wallet)`: add
    ```python
    state["trait_tokens"] = [
        {"nft_id": nid, "slot": s, "value": v}
        for nid, o, s, v in economy_store.read_trait_tokens(conn) if o == wallet
    ]
    ```
  - Add `start_extract(discord_id, owner, body)` and `start_deposit(discord_id, owner, body)` coroutines mirroring `start_harvest`: open conn, gate on `get_closet_record(...)[2] == ct.ACTIVE` (raise `EconomyError("Create and claim your Closet first.")` otherwise), build deps, construct `ExtractSession(owner, body["slot"], body["value"])` / `DepositSession(owner, body["nft_id"])`, run the flow as a tracked session, return it.
  - In `economy_session_dict(kind, s)` add `extract` (`base["accept"] = (s.accept or {}).get("xumm_url")`, `base["nft_id"] = s.nft_id`) and `deposit` (`base["slot"] = s.slot`, `base["value"] = s.value`) branches.

  In `lfg_service/app.py`: register `app.router.add_post("/api/extract", ...)` and `/api/deposit` via the existing `_economy_post(kind, start_coro, mock_call)` factory (same gating/session machinery as harvest); dev-mode routes to the mock methods added in Task 9.

- [ ] **Step 4: Run to verify pass** — `.venv/bin/python -m pytest webapp/test_economy_api.py tests/test_service_firehose.py -q` → PASS.

- [ ] **Step 5: Commit**

```bash
git add webapp/economy_api.py lfg_service/app.py webapp/test_economy_api.py
git commit -m "feat(service): /api/extract + /api/deposit, trait_tokens in economy state"
```

---

### Task 9: Dev-mode mock parity

**Files:**
- Modify: `webapp/mock_economy.py`
- Test: `webapp/test_mock_economy.py`

**Interfaces:** Produces mock `read_state` with `trait_tokens`; mock `extract(wallet, slot, value)` and `deposit(wallet, nft_id)` that gate on an active mock closet and move a trait between the closet and a per-wallet trait-token list.

- [ ] **Step 1: Write failing test** in `webapp/test_mock_economy.py`: with an active closet holding a `(Hat, Cap)`, `extract` removes it from the closet and adds a `trait_tokens` entry; `deposit` of that nft_id reverses it; both raise the mock error when the closet is not active.

- [ ] **Step 2: Run to verify fail** → FAIL.

- [ ] **Step 3: Implement** the mock `trait_tokens` list + `extract`/`deposit` methods + `read_state` `trait_tokens` block, raising `MockEconomyError` unless `_closet_active(owner)`. Wire `handle_extract`/`handle_deposit` dev branches in `lfg_service/app.py` to these.

- [ ] **Step 4: Run to verify pass** — `.venv/bin/python -m pytest webapp/test_mock_economy.py webapp/test_economy_api.py -q` → PASS.

- [ ] **Step 5: Commit**

```bash
git add webapp/mock_economy.py lfg_service/app.py webapp/test_mock_economy.py
git commit -m "feat(dev): mock extract/deposit + trait_tokens for WEBAPP_DEV_MODE"
```

---

### Task 10: Activity UI — Extract / Deposit in the Dressing Room

**Files:**
- Modify: `webapp/client/app.js`, `webapp/client/index.html`, `webapp/client/style.css`
- Test: `tests/test_app_js_boot.py`

**Interfaces:** Consumes `GET /api/economy` `trait_tokens` + `closet.token.status`; `POST /api/extract`, `POST /api/deposit`.

- [ ] **Step 1: Write failing test** in `tests/test_app_js_boot.py` (static-assertion style):

```python
def test_app_js_has_extract_deposit():
    src = (ROOT / "webapp/client/app.js").read_text()
    assert "/api/extract" in src and "/api/deposit" in src
    assert "trait_tokens" in src or "economyState.trait_tokens" in src
    assert "Extract" in src and "Deposit" in src
```

- [ ] **Step 2: Run to verify fail** → FAIL.

- [ ] **Step 3: Implement** in `app.js`:
  - In `renderCloset()` (the closet items grid — currently each loose asset tile), add an **Extract** button per loose trait tile that calls `await api('/api/extract', {method:'POST', body: JSON.stringify({slot: asset.slot, value: asset.value})})`, polls the session via `pollEconomyOp('extract', res)`, surfaces the accept QR (`final.accept`) via `showFlow`, then reloads `economyState` and re-renders.
  - Add a **"Your tradeable traits"** strip (new container) populated from `economyState.trait_tokens`, each entry with a **Deposit** button calling `POST /api/deposit {nft_id}` → `pollEconomyOp('deposit', res)` → reload + re-render. Show the trait image via `layerSrc`-style or a generic chip (the trait token has `slot`/`value`).
  - Both actions only wired when `closetStatus() === 'active'` (reuse the gate).
  Add the strip markup to `index.html` and minimal `.trait-strip`/`.trait-chip` styles to `style.css`.

- [ ] **Step 4: Run to verify pass** — `.venv/bin/python -m pytest tests/test_app_js_boot.py -q && node --check webapp/client/app.js` → PASS + OK.

- [ ] **Step 5: Commit**

```bash
git add webapp/client/app.js webapp/client/index.html webapp/client/style.css tests/test_app_js_boot.py
git commit -m "feat(activity): Extract/Deposit trait tokens in the Dressing Room"
```

---

### Task 11: CLI scripts + docs + final gate

**Files:**
- Create: `scripts/economy_extract.py`, `scripts/economy_deposit.py`
- Modify: `tests/test_economy_scripts_import.py`, `CLAUDE.md`, env block
- Test: `tests/test_economy_scripts_import.py`

**Interfaces:** Consumes `_economy_deps`, `economy_flow.run_extract/run_deposit`.

- [ ] **Step 1: Write failing import-smoke** — add `economy_extract` and `economy_deposit` to the script list in `tests/test_economy_scripts_import.py`. Run → FAIL (modules absent).

- [ ] **Step 2: Implement** `scripts/economy_extract.py` (argparse `--network --owner --slot --value`) and `scripts/economy_deposit.py` (`--network --owner --nft-id`), each mirroring `scripts/economy_harvest.py`'s structure: `deps.open_index(network)`, `deps.build_economy_deps(conn)`, construct the session, run the flow, print `state`/`error`/`accept`. For extract, load nothing from the index (it operates on the Closet); for deposit, the nft_id is the arg.

- [ ] **Step 3: Run to verify pass** — `.venv/bin/python -m pytest tests/test_economy_scripts_import.py -q` → PASS.

- [ ] **Step 4: Docs.** In `CLAUDE.md`, under the dress-up economy section, add a Phase 4 subsection documenting Extract/Deposit, the `TRAIT_TAXON`/`TRAIT_NFT_FLAGS=9`/inherited-70%-fee model, `trait_tokens` going live, the supply-neutral property, and the two CLI invocations. Add `TRAIT_TAXON=1763` to the env block.

- [ ] **Step 5: Final full gate**

Run: `.venv/bin/ruff format . && .venv/bin/ruff check . && .venv/bin/mypy . && .venv/bin/python -m pytest -q && node --check webapp/client/app.js`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add scripts/economy_extract.py scripts/economy_deposit.py tests/test_economy_scripts_import.py CLAUDE.md
git commit -m "feat(traits): extract/deposit CLI + Phase 4 docs"
```

---

## Self-Review

**Spec coverage:** supply-neutral (Tasks 3,4,5) · trait token taxon/flags/fee/metadata/image (Tasks 1,7) · Extract flow (Task 3) · Deposit flow incl. fail-closed + reject-foreign + journal (Task 4) · listener trait_tokens mint/transfer/burn + onchain dispatch (Task 6) · service endpoints + economy state + gating (Task 8) · mock parity (Task 9) · Activity UI (Task 10) · CLI (Task 11) · conservation auditor (Task 5) · docs (Task 11). All spec sections map to a task.

**Placeholder scan:** Task 4's test bodies are sketched as comments because they reuse Task 3's concrete `_F`/`_deps` harness (defined in full in Task 3) — the asserted behaviors and target symbols (`run_deposit`, journal statuses, `f.burns`) are concrete. Task 7's `_trait_meta` flags an explicit "use the real metadata-fetch helper, don't invent one" instruction (the exact name must be read from the listener wiring). No "TBD/handle edge cases" placeholders remain.

**Type consistency:** `ExtractSession`/`run_extract`/`DepositSession`/`run_deposit` signatures match across Tasks 3,4,8,11. `EconomyDeps` trait fields (`trait_compose_fn`/`trait_upload_fn`/`trait_mint_fn`/`trait_burn_fn`/`trait_info_fn`/`trait_meta_fn`) defined in Tasks 3–4, wired in Task 7, all Optional. `upsert_trait_token`/`delete_trait_token` consistent across Tasks 2,4,6. `TraitComposeFn`/`TraitInfoFn`/`TraitMetaFn` aliases defined where first used. Status journal keys (`reverted_mint`/`failed_revert_mint`/`deposited_pending_closet`) consistent with the harvest/assemble precedent.
