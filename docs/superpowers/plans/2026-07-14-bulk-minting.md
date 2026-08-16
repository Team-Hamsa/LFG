# Bulk Minting Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a user pay once for N mints and have the backend mint-and-offer all N as a durable, restart-safe batch job, reusing the existing single-mint pipeline.

**Architecture:** A bulk mint is a durable batch job (`lfg_core/bulk_mint_flow.py`). After one N× payment, a background task loops the existing per-unit mint pipeline N times, persisting progress after each unit so a restart resumes the remainder. Offer acceptance is fully decoupled (offers never expire; a separate follow-up surfaces them). A supply cap clamps N to collection headroom; an entitlement seam leaves room for a future burn-to-mint source.

**Tech Stack:** Python 3, asyncio, aiohttp (`lfg_service`), xrpl-py, SQLite, pytest. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-07-14-bulk-minting-design.md`

## Global Constraints

- **SourceTag = 2606160021** on every `NFTokenMint` and `NFTokenCreateOffer` (already stamped by `xrpl_ops` builders — nothing new to set, but asserted by tests).
- **Provenance memos** (`mint` / `create-offer`) via `memos.platform_for_surface(job.platform)` — inherited from the reused builders.
- **Offers carry no `Expiration`** — `create_nft_offer` already omits it; do not add one.
- **No forced burns** — burn-to-mint is a stub only in this plan; write no burn logic.
- **`MAX_COLLECTION_SIZE` default 10000**, **`BULK_MINT_MAX` default 10** — both config-overridable.
- **Never take payment for undeliverable mints** — clamp K to headroom *before* building the payment payload.
- **Pre-push gate is blocking** (ruff, ruff-format, mypy from `.venv`, gitleaks, pytest). Every commit must pass it; never `--no-verify`.
- Run tests with `.venv/bin/python -m pytest`.

## File Structure

- **Create `lfg_core/supply.py`** — collection-size census + headroom (reads `onchain_<net>.db`).
- **Create `lfg_core/mint_credits.py`** — `mint_credits` table helpers (last-resort tail).
- **Create `lfg_core/entitlement.py`** — `PaymentEntitlement` / `BurnEntitlement` seam.
- **Create `lfg_core/bulk_mint_flow.py`** — `BulkMintJob`, `Unit`, clamping, persistence, fulfillment loop, resume, cancel.
- **Modify `lfg_core/mint_flow.py`** — extract the reusable per-unit pipeline `mint_one_unit(...)` from `run_mint_session` so both single and bulk mint share it.
- **Modify `lfg_core/config.py`** — `MAX_COLLECTION_SIZE`, `BULK_MINT_MAX`.
- **Modify `lfg_service/app.py`** — `POST /api/mint/bulk`, `GET /api/mint/bulk/{id}`, startup resume sweep, `/api/mint/active` integration.
- **Create tests**: `tests/test_supply.py`, `tests/test_mint_credits.py`, `tests/test_entitlement.py`, `tests/test_mint_one_unit.py`, `tests/test_bulk_mint_flow.py`, `tests/test_bulk_mint_durability.py`, `tests/test_bulk_mint_supply_cap.py`, `tests/test_bulk_mint_service.py`; extend `webapp/test_smoke.py` and the SourceTag invariant test.

**Test env-guard:** every new test file importing `lfg_core` at module top MUST begin with the standard env-guard preamble (set `BUNNY_PULL_ZONE`, `LAYER_SOURCE` before importing `lfg_core`), copied from an existing test file such as `tests/test_mint_flow.py` — otherwise it strands frozen config constants and breaks full-suite ordering.

---

### Task 1: Config knobs

**Files:**
- Modify: `lfg_core/config.py`
- Test: `tests/test_bulk_mint_flow.py`

**Interfaces:**
- Produces: `config.MAX_COLLECTION_SIZE: int` (default 10000), `config.BULK_MINT_MAX: int` (default 10).

- [ ] **Step 1: Write the failing test**

Create `tests/test_bulk_mint_flow.py` with the env-guard preamble (copy from `tests/test_mint_flow.py`), then:

```python
def test_config_defaults():
    from lfg_core import config
    assert config.MAX_COLLECTION_SIZE == 10000
    assert config.BULK_MINT_MAX == 10
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_bulk_mint_flow.py::test_config_defaults -v`
Expected: FAIL (`AttributeError: MAX_COLLECTION_SIZE`).

- [ ] **Step 3: Add the config lines**

In `lfg_core/config.py`, near the other `int(os.getenv(...))` mint settings (around line 111):

```python
# Bulk minting (#215). MAX_COLLECTION_SIZE caps total live editions; a bulk
# request is clamped to the remaining headroom before payment. BULK_MINT_MAX
# caps how many a single bulk job may request.
MAX_COLLECTION_SIZE = int(os.getenv("MAX_COLLECTION_SIZE", "10000"))
BULK_MINT_MAX = int(os.getenv("BULK_MINT_MAX", "10"))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_bulk_mint_flow.py::test_config_defaults -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add lfg_core/config.py tests/test_bulk_mint_flow.py
git commit -m "feat(config): MAX_COLLECTION_SIZE + BULK_MINT_MAX for bulk minting (#215)"
```

---

### Task 2: Supply census & headroom

**Files:**
- Create: `lfg_core/supply.py`
- Test: `tests/test_supply.py`

**Interfaces:**
- Consumes: `nft_index.index_db_path(network)`, `config.MAX_COLLECTION_SIZE`.
- Produces:
  - `current_supply(network: str) -> int` — count of live editions.
  - `remaining_headroom(network: str) -> int` — `max(0, MAX_COLLECTION_SIZE - current_supply(network))`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_supply.py` (env-guard preamble first):

```python
import sqlite3
from lfg_core import supply, config


def _seed(path, n_live, n_burned=0):
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE onchain_nfts (nft_id TEXT PRIMARY KEY, nft_number INTEGER, "
        "is_burned INTEGER DEFAULT 0)"
    )
    for i in range(n_live):
        conn.execute("INSERT INTO onchain_nfts VALUES (?,?,0)", (f"live{i}", i))
    for i in range(n_burned):
        conn.execute("INSERT INTO onchain_nfts VALUES (?,?,1)", (f"burn{i}", 10000 + i))
    conn.commit()
    conn.close()


def test_current_supply_counts_only_live(tmp_path, monkeypatch):
    db = tmp_path / "onchain_testnet.db"
    _seed(str(db), n_live=42, n_burned=7)
    monkeypatch.setattr(supply.nft_index, "index_db_path", lambda net: str(db))
    assert supply.current_supply("testnet") == 42


def test_remaining_headroom(tmp_path, monkeypatch):
    db = tmp_path / "onchain_testnet.db"
    _seed(str(db), n_live=9995)
    monkeypatch.setattr(supply.nft_index, "index_db_path", lambda net: str(db))
    monkeypatch.setattr(config, "MAX_COLLECTION_SIZE", 10000)
    assert supply.remaining_headroom("testnet") == 5


def test_headroom_never_negative(tmp_path, monkeypatch):
    db = tmp_path / "onchain_testnet.db"
    _seed(str(db), n_live=10005)
    monkeypatch.setattr(supply.nft_index, "index_db_path", lambda net: str(db))
    monkeypatch.setattr(config, "MAX_COLLECTION_SIZE", 10000)
    assert supply.remaining_headroom("testnet") == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_supply.py -v`
Expected: FAIL (`ModuleNotFoundError: lfg_core.supply`).

- [ ] **Step 3: Implement `lfg_core/supply.py`**

```python
# lfg_core/supply.py
# Collection-size census + headroom for bulk minting (#215). The authoritative
# live-edition count is the on-chain index (onchain_<net>.db, is_burned=0) —
# the same store the economy conservation audit reads.
import sqlite3

from lfg_core import config, nft_index


def current_supply(network: str) -> int:
    """Number of live (un-burned) editions currently on-chain for `network`."""
    path = nft_index.index_db_path(network)
    conn = sqlite3.connect(path)
    try:
        row = conn.execute("SELECT COUNT(*) FROM onchain_nfts WHERE is_burned=0").fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


def remaining_headroom(network: str) -> int:
    """How many more mints fit under MAX_COLLECTION_SIZE. Never negative."""
    return max(0, config.MAX_COLLECTION_SIZE - current_supply(network))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_supply.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add lfg_core/supply.py tests/test_supply.py
git commit -m "feat(supply): live-edition census + collection headroom (#215)"
```

---

### Task 3: Mint-credits store

**Files:**
- Create: `lfg_core/mint_credits.py`
- Test: `tests/test_mint_credits.py`

**Interfaces:**
- Produces (all take an explicit `db_path: str` so tests can point at a temp file; production callers pass `db_path.app_db_path(network)`):
  - `ensure_table(db_path: str) -> None`
  - `add_credit(db_path: str, discord_id: str, network: str, n: int = 1) -> int` — returns new balance.
  - `get_credits(db_path: str, discord_id: str, network: str) -> int`

- [ ] **Step 1: Write the failing test**

Create `tests/test_mint_credits.py` (env-guard preamble first):

```python
from lfg_core import mint_credits


def test_add_and_get(tmp_path):
    db = str(tmp_path / "app.db")
    mint_credits.ensure_table(db)
    assert mint_credits.get_credits(db, "u1", "testnet") == 0
    assert mint_credits.add_credit(db, "u1", "testnet", 2) == 2
    assert mint_credits.add_credit(db, "u1", "testnet") == 3
    assert mint_credits.get_credits(db, "u1", "testnet") == 3


def test_credits_are_per_network_and_user(tmp_path):
    db = str(tmp_path / "app.db")
    mint_credits.ensure_table(db)
    mint_credits.add_credit(db, "u1", "testnet", 5)
    assert mint_credits.get_credits(db, "u1", "mainnet") == 0
    assert mint_credits.get_credits(db, "u2", "testnet") == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_mint_credits.py -v`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Implement `lfg_core/mint_credits.py`**

```python
# lfg_core/mint_credits.py
# Last-resort tail for bulk minting (#215): a unit that is permanently
# undeliverable (cap-hit race, exhausted retries) becomes a durable credit
# the user can redeem later with no re-payment.
import sqlite3


def ensure_table(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS mint_credits ("
            "discord_id TEXT NOT NULL, network TEXT NOT NULL, "
            "credits INTEGER NOT NULL DEFAULT 0, "
            "updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, "
            "PRIMARY KEY (discord_id, network))"
        )
        conn.commit()
    finally:
        conn.close()


def add_credit(db_path: str, discord_id: str, network: str, n: int = 1) -> int:
    ensure_table(db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO mint_credits (discord_id, network, credits) VALUES (?,?,?) "
            "ON CONFLICT(discord_id, network) DO UPDATE SET "
            "credits = credits + excluded.credits, updated_at = CURRENT_TIMESTAMP",
            (discord_id, network, n),
        )
        conn.commit()
        row = conn.execute(
            "SELECT credits FROM mint_credits WHERE discord_id=? AND network=?",
            (discord_id, network),
        ).fetchone()
        return int(row[0])
    finally:
        conn.close()


def get_credits(db_path: str, discord_id: str, network: str) -> int:
    ensure_table(db_path)
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT credits FROM mint_credits WHERE discord_id=? AND network=?",
            (discord_id, network),
        ).fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_mint_credits.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add lfg_core/mint_credits.py tests/test_mint_credits.py
git commit -m "feat(mint-credits): durable per-user mint-credit tail (#215)"
```

---

### Task 4: Entitlement seam

**Files:**
- Create: `lfg_core/entitlement.py`
- Test: `tests/test_entitlement.py`

**Interfaces:**
- Produces:
  - `@dataclass PaymentEntitlement: quantity: int` with `source = "payment"` and `cap_exempt = False`.
  - `@dataclass BurnEntitlement: quantity: int; burn_nft_ids: list[str]` with `source = "burn"` and `cap_exempt = True`.
  - `from_dict(d: dict) -> PaymentEntitlement | BurnEntitlement` and each has `.to_dict()`.
  - `build_burn_entitlement(*args, **kwargs) -> BurnEntitlement` — raises `NotImplementedError` (stub for #220).

- [ ] **Step 1: Write the failing test**

Create `tests/test_entitlement.py` (env-guard preamble first):

```python
import pytest
from lfg_core import entitlement


def test_payment_entitlement_roundtrip():
    e = entitlement.PaymentEntitlement(quantity=5)
    assert e.source == "payment"
    assert e.cap_exempt is False
    assert entitlement.from_dict(e.to_dict()) == e


def test_burn_entitlement_is_cap_exempt_and_roundtrips():
    e = entitlement.BurnEntitlement(quantity=3, burn_nft_ids=["a", "b", "c"])
    assert e.source == "burn"
    assert e.cap_exempt is True
    assert entitlement.from_dict(e.to_dict()) == e


def test_build_burn_entitlement_is_stub():
    with pytest.raises(NotImplementedError):
        entitlement.build_burn_entitlement(quantity=1, burn_nft_ids=["a"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_entitlement.py -v`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Implement `lfg_core/entitlement.py`**

```python
# lfg_core/entitlement.py
# Entitlement seam for bulk minting (#215): the fulfillment loop reads how many
# mints a user is owed (`quantity`) without caring WHY. `payment` is built now;
# `burn` (#220, "infinite" minting past the cap) is a documented stub.
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class PaymentEntitlement:
    quantity: int
    source: str = field(default="payment", init=False)
    cap_exempt: bool = field(default=False, init=False)

    def to_dict(self) -> dict[str, Any]:
        return {"source": "payment", "quantity": self.quantity}


@dataclass
class BurnEntitlement:
    quantity: int
    burn_nft_ids: list[str]
    # Burning M live NFTs to mint M fresh ones is supply-neutral, so it is
    # exempt from MAX_COLLECTION_SIZE.
    source: str = field(default="burn", init=False)
    cap_exempt: bool = field(default=True, init=False)

    def to_dict(self) -> dict[str, Any]:
        return {"source": "burn", "quantity": self.quantity, "burn_nft_ids": self.burn_nft_ids}


def from_dict(d: dict[str, Any]) -> PaymentEntitlement | BurnEntitlement:
    if d["source"] == "payment":
        return PaymentEntitlement(quantity=d["quantity"])
    if d["source"] == "burn":
        return BurnEntitlement(quantity=d["quantity"], burn_nft_ids=d["burn_nft_ids"])
    raise ValueError(f"unknown entitlement source: {d['source']!r}")


def build_burn_entitlement(quantity: int, burn_nft_ids: list[str]) -> BurnEntitlement:
    """Stub for #220 (burn-to-mint). The seam exists; the logic does not yet."""
    raise NotImplementedError("burn-to-mint is not implemented yet (#220)")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_entitlement.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add lfg_core/entitlement.py tests/test_entitlement.py
git commit -m "feat(entitlement): payment/burn seam for bulk minting (#215, stub #220)"
```

---

### Task 5: Extract reusable per-unit mint pipeline

Extract the compose→upload→mint→record→offer body of `run_mint_session` into a standalone `mint_one_unit(...)` in `mint_flow.py` so single and bulk mint share one code path. `run_mint_session` is refactored to call it; its external behavior is unchanged (guarded by the existing mint tests).

**Files:**
- Modify: `lfg_core/mint_flow.py` (extract from `run_mint_session`, lines ~302-442)
- Test: `tests/test_mint_one_unit.py`, plus the existing `tests/test_mint_flow.py` must still pass.

**Interfaces:**
- Produces:
  ```python
  @dataclass
  class UnitResult:
      nft_number: int | None
      nft_id: str | None
      image_url: str | None
      offer_id: str | None
      accept: dict | None   # xumm accept payload dict, or None
      error: str | None

  async def mint_one_unit(
      *, discord_id: str, wallet_address: str, platform: str,
      push_user_token: str | None, return_url: dict | None,
      nft_number: int,           # caller pre-allocates via _allocate_nft_number
      session_tag: str,          # unique tag for image-archive staging (e.g. job_id:index)
  ) -> UnitResult: ...
  ```
  On any failure it sets `error` and returns partial fields (e.g. `nft_id` set but `offer_id` None = minted-but-offer-failed). It performs the same image-archive promote/discard and `_save_recovery_record` handling as today.

- [ ] **Step 1: Characterize current behavior**

Confirm the existing mint tests pass before refactoring:
Run: `.venv/bin/python -m pytest tests/test_mint_flow.py -v`
Expected: PASS (record the count).

- [ ] **Step 2: Write the failing test for the extracted unit**

Create `tests/test_mint_one_unit.py` (env-guard preamble first). Mock the network/CDN layer the way `tests/test_mint_flow.py` already does (reuse its fixtures/monkeypatches as a reference):

```python
import pytest
from lfg_core import mint_flow


@pytest.mark.asyncio
async def test_mint_one_unit_happy_path(monkeypatch, _mint_mocks):
    # _mint_mocks: patches traits.select_random_attributes, swap_compose.*,
    # cdn upload, xrpl_ops.mint_nft -> "NFTID1", create_nft_offer -> "OFFER1",
    # xumm_ops.create_accept_offer_payload -> {"qr_url":"q","xumm_url":"x","uuid":"u"},
    # record_nft_mint -> True. (Model on the existing mint_flow test mocks.)
    res = await mint_flow.mint_one_unit(
        discord_id="u1", wallet_address="rUSER", platform="discord",
        push_user_token=None, return_url=None, nft_number=4000, session_tag="job1:0",
    )
    assert res.nft_id == "NFTID1"
    assert res.offer_id == "OFFER1"
    assert res.accept["uuid"] == "u"
    assert res.error is None


@pytest.mark.asyncio
async def test_mint_one_unit_offer_fail_reports_nft_id(monkeypatch, _mint_mocks):
    monkeypatch.setattr(mint_flow.xrpl_ops, "create_nft_offer",
                        _async_return(None))
    res = await mint_flow.mint_one_unit(
        discord_id="u1", wallet_address="rUSER", platform="discord",
        push_user_token=None, return_url=None, nft_number=4001, session_tag="job1:1",
    )
    assert res.nft_id == "NFTID1"      # minted
    assert res.offer_id is None        # offer failed
    assert res.error is not None
```

(Define `_mint_mocks` and `_async_return` helpers by copying the mocking approach already used in `tests/test_mint_flow.py`.)

- [ ] **Step 3: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_mint_one_unit.py -v`
Expected: FAIL (`AttributeError: mint_one_unit`).

- [ ] **Step 4: Extract the function**

In `lfg_core/mint_flow.py`, add the `UnitResult` dataclass and `mint_one_unit(...)` containing the current steps 2–5 of `run_mint_session` (compose → upload → mint → record + rarity → offer → accept payload), returning a `UnitResult` instead of mutating a `MintSession`. Keep the image-archive promote/discard, `_release_unused_number` equivalent (release the number if `nft_id` is None), and `_save_recovery_record` exactly as they are.

Then rewrite `run_mint_session`'s post-payment body to:
```python
        session.nft_number = await _allocate_nft_number()
        res = await mint_one_unit(
            discord_id=session.discord_id, wallet_address=session.wallet_address,
            platform=session.platform, push_user_token=session.push_user_token,
            return_url=session.return_url, nft_number=session.nft_number,
            session_tag=session.id,
        )
        session.nft_id = res.nft_id
        session.image_url = res.image_url
        if res.error or not res.offer_id or not res.accept:
            session.state = FAILED
            session.error = res.error or "mint failed"
            return
        session.accept_qr_url = res.accept["qr_url"]
        session.accept_deeplink = res.accept["xumm_url"]
        session.accept_uuid = res.accept.get("uuid")
        session.state = OFFER_READY
```
(Keep the `GENERATING`/`MINTING`/`CREATING_OFFER` state assignments for single mint by setting them around the call, or accept a small state-granularity loss for single mint — preserve them by having `mint_one_unit` take an optional `on_state(state: str)` callback the single-mint path passes to keep its existing UI states. Wire the callback so `test_mint_flow.py` still passes.)

- [ ] **Step 5: Run tests to verify both pass**

Run: `.venv/bin/python -m pytest tests/test_mint_one_unit.py tests/test_mint_flow.py -v`
Expected: PASS (new tests + the pre-existing count from Step 1 unchanged).

- [ ] **Step 6: Commit**

```bash
git add lfg_core/mint_flow.py tests/test_mint_one_unit.py
git commit -m "refactor(mint): extract reusable mint_one_unit from run_mint_session (#215)"
```

---

### Task 6: BulkMintJob model + clamping + payment prep

**Files:**
- Create: `lfg_core/bulk_mint_flow.py`
- Test: `tests/test_bulk_mint_flow.py`

**Interfaces:**
- Consumes: `supply.remaining_headroom`, `config.BULK_MINT_MAX`, `config.MINT_PRICE_LFGO/XRP`, `xrpl_ops.get_trustline_balance`, `xumm_ops.create_payment_payload`, `entitlement.PaymentEntitlement`.
- Produces:
  - States: `AWAITING_PAYMENT, PAID, FULFILLING, DONE, FAILED, PAYMENT_TIMEOUT, CANCELLED`; `TERMINAL_STATES = {DONE, FAILED, PAYMENT_TIMEOUT, CANCELLED}` (note: `FULFILLING` is **non-terminal**).
  - Unit states: `PENDING, MINTED, OFFERED, UNIT_FAILED`.
  - `class Unit` (dataclass): `index, state, nft_number, nft_id, image_url, offer_id, error`.
  - `class BulkMintJob`: constructor `(discord_id, wallet_address, requested_qty, platform="discord", push_user_token=None, return_url=None)`; attributes incl. `id, quantity, unit_price, pay_with, pay_amount, entitlement, units, state, error`; methods `clamp_to_headroom()`, `prepare_payment()`, `cancel()`, `to_dict()`.
  - `CollectionFull(Exception)` raised by `clamp_to_headroom()` when headroom is 0.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_bulk_mint_flow.py`:

```python
import pytest
from lfg_core import bulk_mint_flow, config


def _job(qty):
    return bulk_mint_flow.BulkMintJob(
        discord_id="u1", wallet_address="rUSER", requested_qty=qty, platform="discord"
    )


def test_clamp_within_headroom_keeps_quantity(monkeypatch):
    monkeypatch.setattr(bulk_mint_flow.supply, "remaining_headroom", lambda net: 100)
    monkeypatch.setattr(config, "BULK_MINT_MAX", 10)
    j = _job(5)
    j.clamp_to_headroom()
    assert j.quantity == 5
    assert len(j.units) == 5


def test_clamp_respects_bulk_max(monkeypatch):
    monkeypatch.setattr(bulk_mint_flow.supply, "remaining_headroom", lambda net: 100)
    monkeypatch.setattr(config, "BULK_MINT_MAX", 10)
    j = _job(50)
    j.clamp_to_headroom()
    assert j.quantity == 10


def test_clamp_to_headroom_when_low(monkeypatch):
    monkeypatch.setattr(bulk_mint_flow.supply, "remaining_headroom", lambda net: 3)
    monkeypatch.setattr(config, "BULK_MINT_MAX", 10)
    j = _job(8)
    j.clamp_to_headroom()
    assert j.quantity == 3


def test_clamp_collection_full_raises(monkeypatch):
    monkeypatch.setattr(bulk_mint_flow.supply, "remaining_headroom", lambda net: 0)
    j = _job(5)
    with pytest.raises(bulk_mint_flow.CollectionFull):
        j.clamp_to_headroom()


@pytest.mark.asyncio
async def test_prepare_payment_multiplies_price_xrp(monkeypatch):
    monkeypatch.setattr(bulk_mint_flow.supply, "remaining_headroom", lambda net: 100)
    monkeypatch.setattr(config, "BULK_MINT_MAX", 10)
    monkeypatch.setattr(config, "MINT_PRICE_XRP", "10")
    monkeypatch.setattr(bulk_mint_flow.xrpl_ops, "get_trustline_balance",
                        _async_return(None))  # no LFGO -> XRP path
    monkeypatch.setattr(bulk_mint_flow.xumm_ops, "create_payment_payload",
                        _async_return({"xumm_url": "x", "uuid": "u"}))
    j = _job(4)
    j.clamp_to_headroom()
    await j.prepare_payment()
    assert j.pay_with == "XRP"
    assert j.pay_amount == "40"     # 4 x 10
```

(Reuse the `_async_return` helper defined for Task 5, or define it locally.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_bulk_mint_flow.py -v -k "clamp or prepare"`
Expected: FAIL (`ModuleNotFoundError: lfg_core.bulk_mint_flow`).

- [ ] **Step 3: Implement the model, clamping, and payment prep**

```python
# lfg_core/bulk_mint_flow.py
# Bulk mint (#215): a durable batch job. After one K x payment, a background
# task loops mint_flow.mint_one_unit K times, persisting after each unit so a
# restart resumes the remainder. Offers never expire, so acceptance is fully
# decoupled (Phase B / #218).
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Any

from lfg_core import config, entitlement, memos, supply, xrpl_ops, xumm_ops

AWAITING_PAYMENT = "awaiting_payment"
PAID = "paid"
FULFILLING = "fulfilling"
DONE = "done"
FAILED = "failed"
PAYMENT_TIMEOUT = "payment_timeout"
CANCELLED = "cancelled"
# FULFILLING is deliberately NOT terminal: the job must stay live in
# /api/mint/active so the client can re-attach, and so the restart sweep
# resumes it.
TERMINAL_STATES = {DONE, FAILED, PAYMENT_TIMEOUT, CANCELLED}

PENDING = "pending"
MINTED = "minted"
OFFERED = "offered"
UNIT_FAILED = "failed"


class CollectionFull(Exception):
    """No headroom under MAX_COLLECTION_SIZE."""


@dataclass
class Unit:
    index: int
    state: str = PENDING
    nft_number: int | None = None
    nft_id: str | None = None
    image_url: str | None = None
    offer_id: str | None = None
    error: str | None = None


class BulkMintJob:
    def __init__(
        self,
        discord_id: str,
        wallet_address: str,
        requested_qty: int,
        platform: str = "discord",
        push_user_token: str | None = None,
        return_url: dict[str, str] | None = None,
    ) -> None:
        self.id = uuid.uuid4().hex
        self.discord_id = discord_id
        self.wallet_address = wallet_address
        self.platform = platform
        self.push_user_token = push_user_token
        self.return_url = return_url
        self.requested_qty = requested_qty
        self.quantity = requested_qty
        self.network = config.XRPL_NETWORK
        self.created_at = time.time()
        self.paid_at: float | None = None
        self.state = AWAITING_PAYMENT
        self.error: str | None = None
        self.pay_with: str | None = None
        self.pay_amount: str | None = None
        self.unit_price: str | None = None
        self.payment_link: str | None = None
        self.payment_uuid: str | None = None
        self.entitlement: Any = None
        self.units: list[Unit] = []
        self.task: asyncio.Task[None] | None = None
        self._published = False

    def clamp_to_headroom(self) -> None:
        """Clamp quantity to min(requested, BULK_MINT_MAX, headroom). Raise
        CollectionFull if no headroom. Cap-exempt entitlements (burn) skip the
        headroom clamp (#220). Must run BEFORE prepare_payment so we never take
        payment for undeliverable mints."""
        cap_exempt = self.entitlement is not None and getattr(
            self.entitlement, "cap_exempt", False
        )
        q = min(self.requested_qty, config.BULK_MINT_MAX)
        if not cap_exempt:
            headroom = supply.remaining_headroom(self.network)
            if headroom <= 0:
                raise CollectionFull()
            q = min(q, headroom)
        self.quantity = q
        self.units = [Unit(index=i) for i in range(q)]
        if self.entitlement is None:
            self.entitlement = entitlement.PaymentEntitlement(quantity=q)

    def _payment_params(self) -> dict[str, Any]:
        if self.pay_with == "XRP":
            return {"destination": xrpl_ops.bot_wallet_address(), "value": self.pay_amount,
                    "currency": "XRP", "issuer": None}
        return {"destination": config.TOKEN_ISSUER_ADDRESS, "value": self.pay_amount,
                "currency": config.TOKEN_CURRENCY_HEX, "issuer": config.TOKEN_ISSUER_ADDRESS}

    async def prepare_payment(self) -> None:
        """Detect LFGO vs XRP path (same rule as single mint) at K x price and
        build the XUMM payment payload."""
        balance = await xrpl_ops.get_trustline_balance(
            self.wallet_address, config.TOKEN_CURRENCY_HEX, config.TOKEN_ISSUER_ADDRESS
        )
        total_lfgo = Decimal(config.MINT_PRICE_LFGO) * self.quantity
        if balance is not None and balance >= total_lfgo:
            self.pay_with, self.unit_price = "LFGO", config.MINT_PRICE_LFGO
            self.pay_amount = str(total_lfgo)
        else:
            self.pay_with, self.unit_price = "XRP", config.MINT_PRICE_XRP
            self.pay_amount = str(Decimal(config.MINT_PRICE_XRP) * self.quantity)
        p = self._payment_params()
        payload = await xumm_ops.create_payment_payload(
            p["destination"], value=p["value"], currency=p["currency"], issuer=p["issuer"],
            return_url=self.return_url, user_token=self.push_user_token,
            platform=memos.platform_for_surface(self.platform),
        )
        if payload:
            self.payment_link = payload["xumm_url"]
            self.payment_uuid = payload.get("uuid")

    def cancel(self) -> bool:
        """Legal only while awaiting payment (once paid, fulfillment must
        complete). Synchronous state guard, same discipline as MintSession."""
        if self.state != AWAITING_PAYMENT:
            return False
        self.state = CANCELLED
        if self.task is not None:
            self.task.cancel()
        return True

    def mark_published(self) -> None:
        self._published = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "platform": self.platform, "state": self.state,
            "error": self.error, "requested_qty": self.requested_qty,
            "quantity": self.quantity, "pay_with": self.pay_with,
            "pay_amount": self.pay_amount, "payment_link": self.payment_link,
            "network": self.network,
            "units": [asdict(u) for u in self.units],
            "minted": sum(1 for u in self.units if u.state in (MINTED, OFFERED)),
            "offered": sum(1 for u in self.units if u.state == OFFERED),
        }
```

Note: `str(Decimal("10") * 4)` is `"40"`; if `MINT_PRICE_XRP` has decimals the string is exact. Keep amounts as Decimal-derived strings to match `wait_for_payment`'s string compare.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_bulk_mint_flow.py -v -k "clamp or prepare or config"`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add lfg_core/bulk_mint_flow.py tests/test_bulk_mint_flow.py
git commit -m "feat(bulk-mint): job model, headroom clamping, K-price payment prep (#215)"
```

---

### Task 7: Durable persistence (save/load)

**Files:**
- Modify: `lfg_core/bulk_mint_flow.py`
- Test: `tests/test_bulk_mint_durability.py`

**Interfaces:**
- Produces:
  - `JOBS_DIR` — default `bulk_mint_jobs/`, overridable via `BULK_MINT_JOBS_DIR` env.
  - `persist(job: BulkMintJob) -> None` — atomic write of the job's full serialized form (incl. reconstruction fields not in `to_dict`: `discord_id, wallet_address, push_user_token, return_url, unit_price, entitlement, paid_at, created_at`) to `JOBS_DIR/<id>.json`.
  - `load_all_resumable() -> list[BulkMintJob]` — rebuild every job whose state is `PAID` or `FULFILLING`.
  - `delete_record(job_id: str) -> None`.
  - `BulkMintJob.serialize() -> dict` / `BulkMintJob.from_serialized(d) -> BulkMintJob`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_bulk_mint_durability.py` (env-guard preamble first):

```python
from lfg_core import bulk_mint_flow


def _paid_job(tmp_path, monkeypatch, state):
    monkeypatch.setattr(bulk_mint_flow, "JOBS_DIR", str(tmp_path))
    j = bulk_mint_flow.BulkMintJob("u1", "rUSER", 3, platform="discord")
    j.entitlement = bulk_mint_flow.entitlement.PaymentEntitlement(quantity=3)
    j.quantity = 3
    j.units = [bulk_mint_flow.Unit(index=i) for i in range(3)]
    j.pay_with, j.pay_amount, j.unit_price = "XRP", "30", "10"
    j.state = state
    return j


def test_persist_and_reload_roundtrip(tmp_path, monkeypatch):
    j = _paid_job(tmp_path, monkeypatch, bulk_mint_flow.FULFILLING)
    j.units[0].state = bulk_mint_flow.OFFERED
    j.units[0].nft_id = "N0"
    bulk_mint_flow.persist(j)
    reloaded = bulk_mint_flow.load_all_resumable()
    assert len(reloaded) == 1
    r = reloaded[0]
    assert r.id == j.id
    assert r.wallet_address == "rUSER"
    assert r.units[0].state == bulk_mint_flow.OFFERED
    assert r.units[0].nft_id == "N0"
    assert r.entitlement.quantity == 3


def test_terminal_jobs_not_resumable(tmp_path, monkeypatch):
    j = _paid_job(tmp_path, monkeypatch, bulk_mint_flow.DONE)
    bulk_mint_flow.persist(j)
    assert bulk_mint_flow.load_all_resumable() == []


def test_delete_record(tmp_path, monkeypatch):
    j = _paid_job(tmp_path, monkeypatch, bulk_mint_flow.FULFILLING)
    bulk_mint_flow.persist(j)
    bulk_mint_flow.delete_record(j.id)
    assert bulk_mint_flow.load_all_resumable() == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_bulk_mint_durability.py -v`
Expected: FAIL (`AttributeError: JOBS_DIR` / `persist`).

- [ ] **Step 3: Implement persistence**

Add to `lfg_core/bulk_mint_flow.py`:

```python
import json
import logging
import os
import tempfile

JOBS_DIR = os.getenv("BULK_MINT_JOBS_DIR", "bulk_mint_jobs")


def _record_path(job_id: str) -> str:
    return os.path.join(JOBS_DIR, f"{job_id}.json")


def persist(job: "BulkMintJob") -> None:
    """Atomically write the job's full reconstruction record."""
    os.makedirs(JOBS_DIR, exist_ok=True)
    data = job.serialize()
    fd, tmp = tempfile.mkstemp(dir=JOBS_DIR, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, _record_path(job.id))
    except Exception:
        logging.error("failed to persist bulk job %s", job.id)
        if os.path.exists(tmp):
            os.remove(tmp)


def delete_record(job_id: str) -> None:
    try:
        os.remove(_record_path(job_id))
    except FileNotFoundError:
        pass


def load_all_resumable() -> list["BulkMintJob"]:
    out: list[BulkMintJob] = []
    if not os.path.isdir(JOBS_DIR):
        return out
    for name in os.listdir(JOBS_DIR):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(JOBS_DIR, name)) as f:
                data = json.load(f)
            if data.get("state") in (PAID, FULFILLING):
                out.append(BulkMintJob.from_serialized(data))
        except Exception:
            logging.error("skipping unreadable bulk job record %s", name)
    return out
```

And add the (de)serialization methods to `BulkMintJob`:

```python
    def serialize(self) -> dict[str, Any]:
        return {
            "id": self.id, "discord_id": self.discord_id,
            "wallet_address": self.wallet_address, "platform": self.platform,
            "push_user_token": self.push_user_token, "return_url": self.return_url,
            "requested_qty": self.requested_qty, "quantity": self.quantity,
            "network": self.network, "created_at": self.created_at,
            "paid_at": self.paid_at, "state": self.state, "error": self.error,
            "pay_with": self.pay_with, "pay_amount": self.pay_amount,
            "unit_price": self.unit_price, "payment_uuid": self.payment_uuid,
            "payment_link": self.payment_link,
            "entitlement": self.entitlement.to_dict() if self.entitlement else None,
            "units": [asdict(u) for u in self.units],
        }

    @classmethod
    def from_serialized(cls, d: dict[str, Any]) -> "BulkMintJob":
        j = cls(d["discord_id"], d["wallet_address"], d["requested_qty"],
                platform=d["platform"], push_user_token=d.get("push_user_token"),
                return_url=d.get("return_url"))
        j.id = d["id"]
        j.quantity = d["quantity"]
        j.network = d["network"]
        j.created_at = d["created_at"]
        j.paid_at = d.get("paid_at")
        j.state = d["state"]
        j.error = d.get("error")
        j.pay_with = d.get("pay_with")
        j.pay_amount = d.get("pay_amount")
        j.unit_price = d.get("unit_price")
        j.payment_uuid = d.get("payment_uuid")
        j.payment_link = d.get("payment_link")
        j.entitlement = entitlement.from_dict(d["entitlement"]) if d.get("entitlement") else None
        j.units = [Unit(**u) for u in d["units"]]
        return j
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_bulk_mint_durability.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add lfg_core/bulk_mint_flow.py tests/test_bulk_mint_durability.py
git commit -m "feat(bulk-mint): durable job records + resumable load (#215)"
```

---

### Task 8: Fulfillment loop with per-unit cap re-check & credit tail

**Files:**
- Modify: `lfg_core/bulk_mint_flow.py`
- Test: `tests/test_bulk_mint_flow.py`, `tests/test_bulk_mint_supply_cap.py`

**Interfaces:**
- Consumes: `mint_flow.mint_one_unit`, `mint_flow._allocate_nft_number`, `supply.current_supply`, `config.MAX_COLLECTION_SIZE`, `mint_credits.add_credit`, `db_path.app_db_path`, `xrpl_ops.wait_for_payment`.
- Produces:
  - `async def run_bulk_mint_job(job: BulkMintJob) -> None` — the background task. Waits for payment (unless already `PAID`/`FULFILLING` on resume), then fulfills each `PENDING` unit, persisting after each transition; sets terminal state.
  - `async def _fulfill_unit(job, unit) -> None` — one unit: cap re-check → allocate → `mint_one_unit` → set unit state / credit tail.
  - `_UNIT_MAX_ATTEMPTS = 3`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_bulk_mint_flow.py`:

```python
@pytest.mark.asyncio
async def test_fulfillment_all_units_offered(monkeypatch, tmp_path):
    monkeypatch.setattr(bulk_mint_flow, "JOBS_DIR", str(tmp_path))
    monkeypatch.setattr(bulk_mint_flow.supply, "current_supply", lambda net: 0)
    monkeypatch.setattr(bulk_mint_flow.mint_flow, "_allocate_nft_number",
                        _async_counter(start=4000))
    monkeypatch.setattr(bulk_mint_flow.mint_flow, "mint_one_unit",
                        _fake_mint_ok())   # returns UnitResult with nft_id/offer_id set
    j = _job(3)
    monkeypatch.setattr(bulk_mint_flow.supply, "remaining_headroom", lambda net: 100)
    j.clamp_to_headroom()
    j.state = bulk_mint_flow.PAID
    await bulk_mint_flow.run_bulk_mint_job(j)
    assert j.state == bulk_mint_flow.DONE
    assert all(u.state == bulk_mint_flow.OFFERED for u in j.units)


@pytest.mark.asyncio
async def test_offer_fail_marks_unit_failed_but_job_completes(monkeypatch, tmp_path):
    monkeypatch.setattr(bulk_mint_flow, "JOBS_DIR", str(tmp_path))
    monkeypatch.setattr(bulk_mint_flow.supply, "current_supply", lambda net: 0)
    monkeypatch.setattr(bulk_mint_flow.mint_flow, "_allocate_nft_number",
                        _async_counter(start=4000))
    # mint ok but offer None -> minted-but-offer-failed
    monkeypatch.setattr(bulk_mint_flow.mint_flow, "mint_one_unit",
                        _fake_mint_offer_fail())
    j = _job(2)
    monkeypatch.setattr(bulk_mint_flow.supply, "remaining_headroom", lambda net: 100)
    j.clamp_to_headroom()
    j.state = bulk_mint_flow.PAID
    await bulk_mint_flow.run_bulk_mint_job(j)
    assert j.state == bulk_mint_flow.DONE   # job still reaches DONE
    assert all(u.nft_id is not None for u in j.units)
```

Add to `tests/test_bulk_mint_supply_cap.py` (env-guard preamble first):

```python
import pytest
from lfg_core import bulk_mint_flow, config, mint_credits


@pytest.mark.asyncio
async def test_cap_hit_mid_fulfillment_becomes_credit(monkeypatch, tmp_path):
    monkeypatch.setattr(bulk_mint_flow, "JOBS_DIR", str(tmp_path))
    monkeypatch.setattr(config, "MAX_COLLECTION_SIZE", 10000)
    monkeypatch.setattr(bulk_mint_flow.db_path, "app_db_path",
                        lambda net: str(tmp_path / "app.db"))
    # headroom exists at request time (clamp) ...
    monkeypatch.setattr(bulk_mint_flow.supply, "remaining_headroom", lambda net: 100)
    # ... but the cap is fully consumed by the time fulfillment runs:
    monkeypatch.setattr(bulk_mint_flow.supply, "current_supply", lambda net: 10000)
    j = bulk_mint_flow.BulkMintJob("u1", "rUSER", 2, platform="discord")
    j.clamp_to_headroom()
    j.state = bulk_mint_flow.PAID
    await bulk_mint_flow.run_bulk_mint_job(j)
    # no unit could mint; both converted to credit, none lost
    assert mint_credits.get_credits(str(tmp_path / "app.db"), "u1", j.network) == 2
    assert all(u.state == bulk_mint_flow.UNIT_FAILED for u in j.units)
```

(Define `_async_counter`, `_fake_mint_ok`, `_fake_mint_offer_fail` helpers returning `mint_flow.UnitResult` instances.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_bulk_mint_flow.py tests/test_bulk_mint_supply_cap.py -v -k "fulfill or offer_fail or cap_hit"`
Expected: FAIL (`AttributeError: run_bulk_mint_job`).

- [ ] **Step 3: Implement the loop**

Add to `lfg_core/bulk_mint_flow.py` (add imports `from lfg_core import db_path, mint_credits, mint_flow`):

```python
_UNIT_MAX_ATTEMPTS = 3


async def _fulfill_unit(job: "BulkMintJob", unit: Unit) -> None:
    """Mint+offer one unit. Cap re-check first (a concurrent job may have
    consumed the tail); a cap-hit or exhausted unit becomes a mint credit
    rather than a loss. Cap-exempt (burn) entitlements skip the re-check."""
    cap_exempt = job.entitlement is not None and getattr(job.entitlement, "cap_exempt", False)
    for _ in range(_UNIT_MAX_ATTEMPTS):
        if not cap_exempt and supply.current_supply(job.network) >= config.MAX_COLLECTION_SIZE:
            break  # cap hit -> credit below
        nft_number = await mint_flow._allocate_nft_number()
        res = await mint_flow.mint_one_unit(
            discord_id=job.discord_id, wallet_address=job.wallet_address,
            platform=job.platform, push_user_token=job.push_user_token,
            return_url=job.return_url, nft_number=nft_number,
            session_tag=f"{job.id}:{unit.index}",
        )
        if res.nft_id:
            unit.nft_id = res.nft_id
            unit.nft_number = res.nft_number
            unit.image_url = res.image_url
            if res.offer_id:
                unit.offer_id = res.offer_id
                unit.state = OFFERED
            else:
                # minted but offer failed: NFT exists, do NOT re-mint. Mark
                # failed (delivered-pending-offer); admin/backfill re-offers.
                unit.state = UNIT_FAILED
                unit.error = res.error or "offer creation failed"
            return
        unit.error = res.error  # transient mint failure: retry
    # Never minted after retries (or cap-hit): durable credit, no money lost.
    unit.state = UNIT_FAILED
    mint_credits.add_credit(db_path.app_db_path(job.network), job.discord_id, job.network, 1)


async def run_bulk_mint_job(job: "BulkMintJob") -> None:
    """Drive a bulk job to terminal state. Background task / resume entrypoint."""
    try:
        if job.state == AWAITING_PAYMENT:
            p = job._payment_params()
            paid = await xrpl_ops.wait_for_payment(
                destination=p["destination"], expected_sender=job.wallet_address,
                expected_amount=job.pay_amount, not_before=job.created_at - 10,
                currency=p["currency"], issuer=p["issuer"],
            )
            if not paid:
                job.state = PAYMENT_TIMEOUT
                persist(job)
                return
            job.paid_at = time.time()
            job.state = PAID
            persist(job)

        job.state = FULFILLING
        persist(job)
        for unit in job.units:
            if unit.state in (OFFERED, UNIT_FAILED):
                continue  # resume: skip already-processed units
            await _fulfill_unit(job, unit)
            persist(job)

        job.state = DONE
        persist(job)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logging.error("bulk job %s failed: %s", job.id, e)
        job.state = FAILED
        job.error = str(e)
        persist(job)
```

Note the resume-safety: a unit already `OFFERED`/`UNIT_FAILED` is skipped, and `wait_for_payment` runs only from `AWAITING_PAYMENT`, so a resumed `FULFILLING` job never re-waits for payment and never re-mints a done unit.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_bulk_mint_flow.py tests/test_bulk_mint_supply_cap.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add lfg_core/bulk_mint_flow.py tests/test_bulk_mint_flow.py tests/test_bulk_mint_supply_cap.py
git commit -m "feat(bulk-mint): fulfillment loop, per-unit cap re-check, credit tail (#215)"
```

---

### Task 9: Restart-resume double-mint safety test

**Files:**
- Test: `tests/test_bulk_mint_durability.py`

**Interfaces:** (no new production code — this task proves Task 8's resume behavior against reloaded records.)

- [ ] **Step 1: Write the failing/regression test**

Add to `tests/test_bulk_mint_durability.py`:

```python
import pytest


@pytest.mark.asyncio
async def test_resume_skips_done_units_no_double_mint(tmp_path, monkeypatch):
    monkeypatch.setattr(bulk_mint_flow, "JOBS_DIR", str(tmp_path))
    monkeypatch.setattr(bulk_mint_flow.supply, "current_supply", lambda net: 0)
    monkeypatch.setattr(bulk_mint_flow.supply, "remaining_headroom", lambda net: 100)

    calls = {"mint": 0, "wait": 0}

    async def _count_mint(**kw):
        calls["mint"] += 1
        from lfg_core.mint_flow import UnitResult
        return UnitResult(nft_number=kw["nft_number"], nft_id=f"N{kw['nft_number']}",
                          image_url="i", offer_id="O", accept={"qr_url": "q",
                          "xumm_url": "x", "uuid": "u"}, error=None)

    async def _count_wait(**kw):
        calls["wait"] += 1
        return True

    monkeypatch.setattr(bulk_mint_flow.mint_flow, "mint_one_unit", _count_mint)
    monkeypatch.setattr(bulk_mint_flow.mint_flow, "_allocate_nft_number",
                        _async_counter(start=5000))
    monkeypatch.setattr(bulk_mint_flow.xrpl_ops, "wait_for_payment", _count_wait)

    # A job already fulfilling with 2 of 3 done, persisted to disk.
    j = bulk_mint_flow.BulkMintJob("u1", "rUSER", 3, platform="discord")
    j.clamp_to_headroom()
    j.pay_amount = "30"
    j.state = bulk_mint_flow.FULFILLING
    j.units[0].state = bulk_mint_flow.OFFERED
    j.units[1].state = bulk_mint_flow.OFFERED
    bulk_mint_flow.persist(j)

    resumed = bulk_mint_flow.load_all_resumable()[0]
    await bulk_mint_flow.run_bulk_mint_job(resumed)

    assert resumed.state == bulk_mint_flow.DONE
    assert calls["mint"] == 1   # only the 1 remaining pending unit minted
    assert calls["wait"] == 0   # payment never re-waited on resume
```

- [ ] **Step 2: Run test to verify it fails or passes**

Run: `.venv/bin/python -m pytest tests/test_bulk_mint_durability.py::test_resume_skips_done_units_no_double_mint -v`
Expected: PASS (Task 8 already implements this; if it FAILS, fix `run_bulk_mint_job`'s skip/resume guards until it passes).

- [ ] **Step 3: Commit**

```bash
git add tests/test_bulk_mint_durability.py
git commit -m "test(bulk-mint): resume skips done units, no double-mint/charge (#215)"
```

---

### Task 10: Service endpoints + active-session integration + startup sweep

**Files:**
- Modify: `lfg_service/app.py`
- Modify: `webapp/test_smoke.py`
- Test: `tests/test_bulk_mint_service.py`

**Interfaces:**
- Consumes: `bulk_mint_flow`, existing `_active_session`, `_prune_sessions`, `require_registration`, `_platform`, `_publish_event`/terminal-publish pattern used by mint.
- Produces:
  - Module global `bulk_sessions: dict[str, BulkMintJob]`.
  - `POST /api/mint/bulk` → `handle_bulk_mint_start` (body `{"quantity": int}`).
  - `GET /api/mint/bulk/{session_id}` → `handle_bulk_mint_status`.
  - `resume_bulk_jobs()` — startup coroutine that loads resumable jobs, re-registers them in `bulk_sessions`, and re-spawns `run_bulk_mint_job`.
  - `/api/mint/active` also reports a live bulk job for the caller.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_bulk_mint_service.py` (env-guard preamble first). Follow the structure of `tests/test_mint_active_resume.py` (from PR #216) for building the app + authed request:

```python
import pytest
from lfg_service import app as svc_app


def test_bulk_routes_registered():
    app = svc_app.make_app()
    paths = {r.resource.canonical for r in app.router.routes()}
    assert "/api/mint/bulk" in paths
    assert "/api/mint/bulk/{session_id}" in paths


def test_bulk_route_registered_before_mint_session_wildcard():
    app = svc_app.make_app()
    ordered = [r.resource.canonical for r in app.router.routes()
               if r.resource.canonical.startswith("/api/mint/")]
    # /api/mint/bulk must precede /api/mint/{session_id} or the wildcard
    # swallows "bulk" as a session id (aiohttp dispatches in registration order)
    assert ordered.index("/api/mint/bulk") < ordered.index("/api/mint/{session_id}")
```

Add a clamp/collection-full behavior test using the authed-request helper from `test_mint_active_resume.py`:

```python
@pytest.mark.asyncio
async def test_bulk_start_rejects_when_collection_full(monkeypatch, authed_client):
    monkeypatch.setattr(svc_app.bulk_mint_flow.supply, "remaining_headroom", lambda net: 0)
    resp = await authed_client.post("/api/mint/bulk", json={"quantity": 5})
    assert resp.status == 409
    body = await resp.json()
    assert body["error"] == "collection_full"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_bulk_mint_service.py -v`
Expected: FAIL (routes not registered).

- [ ] **Step 3: Implement handlers and wiring**

In `lfg_service/app.py`, near the mint handlers:

```python
from lfg_core import bulk_mint_flow

bulk_sessions: dict[str, Any] = {}


@require_registration
async def handle_bulk_mint_start(request):
    _prune_sessions(bulk_sessions, bulk_mint_flow.TERMINAL_STATES)
    user = request["user"]
    platform = _platform(user)
    active = _active_session(bulk_sessions, bulk_mint_flow.TERMINAL_STATES, user["id"], platform)
    if active:
        return web.json_response(
            {"error": "bulk mint already in progress", "session": active.to_dict()}, status=409
        )
    body = await request.json()
    try:
        qty = int(body.get("quantity", 0))
    except (TypeError, ValueError):
        qty = 0
    if qty < 1:
        return web.json_response({"error": "invalid_quantity"}, status=400)

    push = await _push_token(platform, user["id"])
    job = bulk_mint_flow.BulkMintJob(
        discord_id=user["id"], wallet_address=request["wallet"], requested_qty=qty,
        platform=platform, push_user_token=push,
    )
    try:
        job.clamp_to_headroom()
    except bulk_mint_flow.CollectionFull:
        return web.json_response({"error": "collection_full"}, status=409)
    await job.prepare_payment()
    bulk_sessions[job.id] = job
    job.task = asyncio.create_task(bulk_mint_flow.run_bulk_mint_job(job))
    return web.json_response(job.to_dict())


@require_auth
async def handle_bulk_mint_status(request):
    job = bulk_sessions.get(request.match_info["session_id"])
    if not job:
        return web.json_response({"error": "not found"}, status=404)
    return web.json_response(job.to_dict())


async def resume_bulk_jobs() -> None:
    """On startup, re-attach and resume any paid/fulfilling bulk jobs."""
    for job in bulk_mint_flow.load_all_resumable():
        bulk_sessions[job.id] = job
        job.task = asyncio.create_task(bulk_mint_flow.run_bulk_mint_job(job))
```

Register the routes in `make_app()` **before** `/api/mint/{session_id}`:

```python
    app.router.add_post("/api/mint/bulk", handle_bulk_mint_start)
    app.router.add_get("/api/mint/bulk/{session_id}", handle_bulk_mint_status)
```

Extend the existing `handle_mint_active` (from #216) so it also returns a live bulk job: after the single-mint `_active_session` lookup returns null, check `bulk_sessions` the same way and return it (shape `{"session": job.to_dict()}` with a `"kind": "bulk"` marker so the client can branch). Add `"kind": "bulk"` to `BulkMintJob.to_dict()` output in `app.py` wrapping if you prefer not to touch the core dataclass.

Wire `resume_bulk_jobs()` into the app startup (the aiohttp `on_startup` list where other background tasks are launched):

```python
    app.on_startup.append(lambda _app: resume_bulk_jobs())
```

If `_push_token` is the existing helper name in `app.py`, reuse it; otherwise resolve the push token via the same call the single-mint start handler uses.

- [ ] **Step 4: Add smoke-test route assertion**

In `webapp/test_smoke.py` `test_routes_registered`, add `/api/mint/bulk` between `/api/mint` (POST) and `/api/mint/{session_id}` to reflect registration order.

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_bulk_mint_service.py webapp/test_smoke.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add lfg_service/app.py webapp/test_smoke.py tests/test_bulk_mint_service.py
git commit -m "feat(service): bulk mint endpoints, active-session + startup resume (#215)"
```

---

### Task 11: SourceTag & memo invariant coverage for bulk

**Files:**
- Modify: the existing SourceTag invariant test (find with `grep -rl "SourceTag" tests/`; likely `tests/test_source_tag_invariant.py` or similar).
- Test: same file.

**Interfaces:** (no production change — bulk reuses `mint_one_unit`, which uses the already-stamped builders. This proves it.)

- [ ] **Step 1: Locate the invariant test**

Run: `grep -rln "SOURCE_TAG\|SourceTag" tests/`
Pick the invariant test that asserts mint/offer txns carry the SourceTag.

- [ ] **Step 2: Write the failing/confirming test**

Add a test asserting a bulk-driven unit stamps SourceTag + `mint`/`create-offer` memos. Since bulk calls `mint_flow.mint_one_unit`, assert at that boundary that `xrpl_ops.mint_nft` and `xrpl_ops.create_nft_offer` are invoked with `platform` set (memo) and that the builders apply `config.SOURCE_TAG` — mirror however the existing invariant test inspects the built tx dict. If the existing test already covers `mint_nft`/`create_nft_offer` generically, add an explicit assertion that the bulk path routes through them (e.g. spy that `mint_one_unit` calls `create_nft_offer` with no `Expiration` kwarg).

```python
@pytest.mark.asyncio
async def test_bulk_unit_offer_has_no_expiration(monkeypatch, _mint_mocks):
    seen = {}

    async def _spy_offer(nft_id, destination, **kw):
        seen.update(kw)
        seen["nft_id"] = nft_id
        return "OFFER1"

    monkeypatch.setattr(mint_flow.xrpl_ops, "create_nft_offer", _spy_offer)
    await mint_flow.mint_one_unit(
        discord_id="u1", wallet_address="rUSER", platform="discord",
        push_user_token=None, return_url=None, nft_number=4200, session_tag="j:0",
    )
    assert "expiration" not in seen and "Expiration" not in seen
```

- [ ] **Step 3: Run test**

Run: `.venv/bin/python -m pytest <invariant_test_file> -v`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add <invariant_test_file>
git commit -m "test(bulk-mint): assert SourceTag/memos + no-Expiration on bulk offers (#215)"
```

---

### Task 12: Full-suite gate + docs

**Files:**
- Modify: `CLAUDE.md` (document the bulk-mint job store + config), `.gitignore` (ignore `bulk_mint_jobs/`).

- [ ] **Step 1: Ignore the job store**

Add to `.gitignore`:
```
bulk_mint_jobs/
```

- [ ] **Step 2: Document in CLAUDE.md**

Add a short subsection under the minting notes describing: `POST /api/mint/bulk` (quantity, clamped to `MAX_COLLECTION_SIZE` headroom and `BULK_MINT_MAX`), the durable `bulk_mint_jobs/` store + startup resume, `mint_credits` tail, and the `entitlement` seam (payment now, burn stub #220). Note new env vars `MAX_COLLECTION_SIZE`, `BULK_MINT_MAX`, `BULK_MINT_JOBS_DIR`.

- [ ] **Step 3: Run the full suite**

Run: `.venv/bin/python -m pytest`
Expected: PASS (all prior + new tests). Fix any full-suite-ordering env-guard issues per the note at the top.

- [ ] **Step 4: Run the pre-push gate dry**

Run: `.venv/bin/python -m pytest && .venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy lfg_core lfg_service`
Expected: clean.

- [ ] **Step 5: Commit**

```bash
git add CLAUDE.md .gitignore
git commit -m "docs(bulk-mint): document job store, config, entitlement seam (#215)"
```

---

## Self-Review

**Spec coverage:**
- Two-phase decoupling → Tasks 6–8 (fulfillment) + follow-up #218 (acceptance, out of scope). ✓
- Single N× payment, LFGO/XRP detection → Task 6 `prepare_payment`. ✓
- Quantity cap 10 → Task 1 `BULK_MINT_MAX` + Task 6 clamp. ✓
- Supply cap 10000, request-time clamp + per-unit re-check → Tasks 2, 6 (clamp), 8 (`_fulfill_unit` re-check). ✓
- Entitlement seam + burn stub cap-exempt → Task 4 + Task 6/8 `cap_exempt` handling. ✓
- Per-unit fail-safe ordering, failure taxonomy → Task 5 (`mint_one_unit` preserves promote/discard/recovery) + Task 8. ✓
- Durable job + restart resume, no double-charge/mint → Tasks 7, 8, 9. ✓
- Mint-credit tail → Task 3 + Task 8. ✓
- Cancellation legal only while awaiting payment → Task 6 `cancel()`. ✓
- Service endpoints + `/api/mint/active` integration (relates #216) → Task 10. ✓
- SourceTag/memos + no-Expiration → Task 11. ✓

**Placeholder scan:** No TBD/TODO; the only intentionally-abstract steps (reusing existing test mocks, locating the invariant test file) instruct the engineer to copy a named existing file's approach rather than inventing behavior. ✓

**Type consistency:** `mint_one_unit` signature and `UnitResult` fields defined in Task 5 are used identically in Task 8. `BulkMintJob`/`Unit`/state constants defined in Task 6 are consumed unchanged in Tasks 7–10. `clamp_to_headroom`/`prepare_payment`/`cancel`/`serialize`/`from_serialized` names are stable across tasks. ✓
