# House Closet Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the app wallet's listed trait tokens into a house wallet's Closet and relist them there as Closet asks, so Closet bids fill against the project's stock.

**Architecture:** A new `lfg_core/house_closet.py` holds the house-wallet identity, the migration plan, a JSON plan file, the two phases (deposit, then list) and the one-time setup. Deposit reuses `economy_flow.run_deposit` with a new `credit_to` field, so the burn keeps Deposit's fail-closed checks and journaling. `scripts/house_closet.py` is a thin CLI over it (status, `--apply-setup`, `--migrate`, `--migrate --apply`).

**Tech Stack:** Python 3, sqlite3, xrpl-py (async client, `submit_and_wait`), pytest.

**Spec:** `docs/superpowers/specs/2026-09-18-house-closet-migration-design.md`

## Global Constraints

- Every transaction the house signs carries `SourceTag = config.SOURCE_TAG` (2606160021) and provenance memos from `memos.build_memo_models(memos.INITIATOR_BACKEND, memos.PLATFORM_BACKEND, <action>)`.
- Never print, log or put the house seed in an exception message.
- Env vars: `CLOSET_HOUSE_WALLET` (classic address), `CLOSET_HOUSE_SEED` (seed that derives that address). Empty = unset.
- The house wallet may not be `SIGNING_ACCOUNT`, `SWAP_ISSUER_ADDRESS`, `BRIX_ISSUER`, `TOKEN_ISSUER_ADDRESS`, `BRIX_DISTRIBUTOR_ADDRESS` or `BRIX_AMM_ACCOUNT`.
- Irreversible steps (`--apply-setup`, `--migrate --apply`) require typing the network name, like `scripts/closet_market_setup.py`.
- Tests never touch a store in the checkout: use `tmp_path` for the DB and the plan file (see CLAUDE.md "Test env isolation").
- Run tests with `.venv/bin/python -m pytest <files> -q -p no:cacheprovider`.

---

### Task 1: House wallet identity

**Files:**
- Modify: `lfg_core/config.py` (after `CLOSET_MARKET_ENC_KEY`)
- Create: `lfg_core/house_closet.py`
- Test: `tests/test_house_closet_identity.py`

**Interfaces:**
- Produces: `config.CLOSET_HOUSE_WALLET: str`, `config.CLOSET_HOUSE_SEED: str`; `house_closet.HouseConfigError`, `house_closet.house_address() -> str`, `house_closet.house_wallet() -> xrpl.wallet.Wallet`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_house_closet_identity.py
"""The house wallet (#548) is validated before anything signs for it."""

import pytest
from xrpl.wallet import Wallet

from lfg_core import config
from lfg_core import house_closet as hc


def test_house_address_requires_the_env(monkeypatch):
    monkeypatch.setattr(config, "CLOSET_HOUSE_WALLET", "")
    with pytest.raises(hc.HouseConfigError, match="CLOSET_HOUSE_WALLET is not set"):
        hc.house_address()


def test_house_address_rejects_a_malformed_address(monkeypatch):
    monkeypatch.setattr(config, "CLOSET_HOUSE_WALLET", "not-an-address")
    with pytest.raises(hc.HouseConfigError, match="not a classic address"):
        hc.house_address()


def test_house_address_rejects_system_wallets(monkeypatch):
    monkeypatch.setattr(config, "CLOSET_HOUSE_WALLET", config.SIGNING_ACCOUNT)
    with pytest.raises(hc.HouseConfigError, match="system wallet"):
        hc.house_address()
    distributor = Wallet.create().classic_address
    monkeypatch.setattr(config, "BRIX_DISTRIBUTOR_ADDRESS", distributor)
    monkeypatch.setattr(config, "CLOSET_HOUSE_WALLET", distributor)
    with pytest.raises(hc.HouseConfigError, match="system wallet"):
        hc.house_address()


def test_house_wallet_needs_a_seed_that_derives_the_address(monkeypatch):
    house = Wallet.create()
    monkeypatch.setattr(config, "CLOSET_HOUSE_WALLET", house.classic_address)
    monkeypatch.setattr(config, "CLOSET_HOUSE_SEED", "")
    with pytest.raises(hc.HouseConfigError, match="CLOSET_HOUSE_SEED is not set"):
        hc.house_wallet()
    monkeypatch.setattr(config, "CLOSET_HOUSE_SEED", Wallet.create().seed)
    with pytest.raises(hc.HouseConfigError, match="different account"):
        hc.house_wallet()
    monkeypatch.setattr(config, "CLOSET_HOUSE_SEED", house.seed)
    assert hc.house_wallet().classic_address == house.classic_address


def test_house_wallet_never_echoes_a_bad_seed(monkeypatch):
    monkeypatch.setattr(config, "CLOSET_HOUSE_WALLET", Wallet.create().classic_address)
    monkeypatch.setattr(config, "CLOSET_HOUSE_SEED", "sBADSEEDxyz")
    with pytest.raises(hc.HouseConfigError) as err:
        hc.house_wallet()
    assert "sBADSEEDxyz" not in str(err.value)
    assert err.value.__cause__ is None
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_house_closet_identity.py -q -p no:cacheprovider`
Expected: FAIL — `ModuleNotFoundError: No module named 'lfg_core.house_closet'`

- [ ] **Step 3: Add the config and the identity functions**

In `lfg_core/config.py`, directly after the `CLOSET_MARKET_ENC_KEY = ...` line:

```python
# House wallet (#548): the project wallet whose Closet holds the project's trait
# stock as Closet asks (the issuer can't own a Closet, #383). Its seed signs only
# the one-time setup (BRIX trust line + Closet accept) in scripts/house_closet.py.
CLOSET_HOUSE_WALLET = os.getenv("CLOSET_HOUSE_WALLET", "").strip()
CLOSET_HOUSE_SEED = os.getenv("CLOSET_HOUSE_SEED", "").strip()
```

Create `lfg_core/house_closet.py`:

```python
"""House Closet (#548): the project's trait stock sold as Closet asks.

The app wallet is the issuer and cannot own a Closet (#383), so the trait tokens
it has listed are burned into a separate house wallet's Closet and relisted there
as asks. scripts/house_closet.py drives it; the design is
docs/superpowers/specs/2026-09-18-house-closet-migration-design.md.
"""

from __future__ import annotations

from xrpl.core.addresscodec import is_valid_classic_address
from xrpl.wallet import Wallet

from lfg_core import config


class HouseConfigError(RuntimeError):
    """The house wallet config is missing or unsafe. Nothing was changed."""


def _system_wallets() -> set[str]:
    """Project wallets that must never double as the house: a Closet can't
    belong to the issuer (#383), and the others keep books of their own."""
    return {
        a
        for a in (
            config.SIGNING_ACCOUNT,
            config.SWAP_ISSUER_ADDRESS,
            config.BRIX_ISSUER,
            config.TOKEN_ISSUER_ADDRESS,
            config.BRIX_DISTRIBUTOR_ADDRESS,
            config.BRIX_AMM_ACCOUNT,
        )
        if a
    }


def house_address() -> str:
    """The configured house wallet, validated. Needs no seed."""
    address = config.CLOSET_HOUSE_WALLET
    if not address:
        raise HouseConfigError("CLOSET_HOUSE_WALLET is not set")
    if not is_valid_classic_address(address):
        raise HouseConfigError(f"CLOSET_HOUSE_WALLET is not a classic address: {address}")
    if address in _system_wallets():
        raise HouseConfigError(
            f"CLOSET_HOUSE_WALLET {address} is a system wallet (issuer, app, distributor "
            "or AMM); the house must be a wallet of its own"
        )
    return address


def house_wallet() -> Wallet:
    """The house wallet's signing key. The seed must derive CLOSET_HOUSE_WALLET:
    a regular key would sign for another account, so it is refused."""
    address = house_address()
    if not config.CLOSET_HOUSE_SEED:
        raise HouseConfigError("CLOSET_HOUSE_SEED is not set")
    try:
        wallet = Wallet.from_seed(config.CLOSET_HOUSE_SEED)
    except Exception:
        # Never echo the seed, not even inside the library's own message.
        raise HouseConfigError("CLOSET_HOUSE_SEED is not a valid seed") from None
    if wallet.classic_address != address:
        raise HouseConfigError(
            "CLOSET_HOUSE_SEED signs for a different account than CLOSET_HOUSE_WALLET "
            f"({wallet.classic_address} != {address}); a regular key is not supported"
        )
    return wallet
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_house_closet_identity.py -q -p no:cacheprovider`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add lfg_core/config.py lfg_core/house_closet.py tests/test_house_closet_identity.py
git commit -m "feat(house-closet): house wallet config and identity checks (#548)"
```

---

### Task 2: Deposit into another wallet's Closet

**Files:**
- Modify: `lfg_core/economy_flow.py` (`_serialize_by_owner`, `DepositSession`, `run_deposit`)
- Test: `tests/test_economy_deposit_credit_to.py`

**Interfaces:**
- Consumes: the fakes `_F`, `_deps`, `_run` from `tests/test_economy_flow_deposit.py`.
- Produces: `DepositSession(owner, nft_id, credit_to=None)`, `DepositSession.closet_owner -> str`; the journal record gains `"credit_to"`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_economy_deposit_credit_to.py
"""Deposit can credit a Closet other than the token holder's (#548: the app
wallet's stock moves into the house Closet)."""

import json
import sqlite3

from lfg_core import closet_token as ct
from lfg_core import economy_flow as ef
from lfg_core import economy_store as es
from tests.test_economy_flow_deposit import _F, _deps, _run

APP, HOUSE = "rApp", "rHouse"


class _HouseF(_F):
    async def closet_owner(self, nft_id: str) -> str | None:
        return {"C-house": HOUSE, "C-app": APP}.get(nft_id)


def _setup(conn):
    es.init_economy_schema(conn)
    es.set_closet_token(conn, HOUSE, "C-house", "AB", status=ct.ACTIVE, offer_id=None)
    es.set_closet_contents(conn, HOUSE, [], [])
    es.upsert_trait_token(conn, "TRAIT9", APP, "Hat", "Cap")


def test_deposit_credits_another_wallets_closet(tmp_path):
    conn = sqlite3.connect(":memory:")
    _setup(conn)
    f = _HouseF()
    f.owner_for["TRAIT9"] = APP
    session = ef.DepositSession(owner=APP, nft_id="TRAIT9", credit_to=HOUSE)
    _run(ef.run_deposit(session, _deps(conn, f, tmp_path)))

    assert session.state == ef.DONE
    assert f.burns == [("TRAIT9", APP)]  # the holder's token is burned
    assert {(o, s, v): n for o, s, v, n in es.read_closet_assets(conn)} == {
        (HOUSE, "Hat", "Cap"): 1
    }
    assert es.read_trait_tokens(conn) == []


def test_the_credited_closet_must_be_active(tmp_path):
    conn = sqlite3.connect(":memory:")
    es.init_economy_schema(conn)
    es.upsert_trait_token(conn, "TRAIT9", APP, "Hat", "Cap")
    # The holder has a Closet and the credit target doesn't: the target decides.
    es.set_closet_token(conn, APP, "C-app", "AB", status=ct.ACTIVE, offer_id=None)
    f = _HouseF()
    f.owner_for["TRAIT9"] = APP
    session = ef.DepositSession(owner=APP, nft_id="TRAIT9", credit_to=HOUSE)
    _run(ef.run_deposit(session, _deps(conn, f, tmp_path)))

    assert session.state == ef.FAILED
    assert f.burns == []


def test_the_holder_is_still_checked_on_ledger(tmp_path):
    conn = sqlite3.connect(":memory:")
    _setup(conn)
    f = _HouseF()
    f.owner_for["TRAIT9"] = "rSomeoneElse"
    session = ef.DepositSession(owner=APP, nft_id="TRAIT9", credit_to=HOUSE)
    _run(ef.run_deposit(session, _deps(conn, f, tmp_path)))

    assert session.state == ef.FAILED
    assert f.burns == []


def test_the_journal_names_the_credited_closet(tmp_path):
    conn = sqlite3.connect(":memory:")
    _setup(conn)
    f = _HouseF()
    f.owner_for["TRAIT9"] = APP
    session = ef.DepositSession(owner=APP, nft_id="TRAIT9", credit_to=HOUSE)
    _run(ef.run_deposit(session, _deps(conn, f, tmp_path)))

    record = json.loads((tmp_path / f"deposit-{session.id}.json").read_text())
    assert record["owner"] == APP and record["credit_to"] == HOUSE


def test_the_lock_is_the_credited_closet(tmp_path, monkeypatch):
    conn = sqlite3.connect(":memory:")
    _setup(conn)
    f = _HouseF()
    f.owner_for["TRAIT9"] = APP
    locked: list[str] = []
    real = ef.owner_lock.owner_lock

    def spy(owner):
        locked.append(owner)
        return real(owner)

    monkeypatch.setattr(ef.owner_lock, "owner_lock", spy)
    session = ef.DepositSession(owner=APP, nft_id="TRAIT9", credit_to=HOUSE)
    _run(ef.run_deposit(session, _deps(conn, f, tmp_path)))

    assert locked[0] == HOUSE
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_economy_deposit_credit_to.py -q -p no:cacheprovider`
Expected: FAIL — `TypeError: DepositSession.__init__() got an unexpected keyword argument 'credit_to'`

- [ ] **Step 3: Implement `credit_to`**

In `lfg_core/economy_flow.py`, `_serialize_by_owner`'s wrapper locks the Closet being rewritten:

```python
    @functools.wraps(runner)
    async def wrapper(session: _S, deps: EconomyDeps) -> None:
        # A Deposit can credit a Closet other than the token holder's (#548);
        # the lock guards the Closet being rewritten.
        owner = getattr(session, "closet_owner", None) or session.owner  # type: ignore[attr-defined]
        async with owner_lock.owner_lock(owner):
            await runner(session, deps)
```

`DepositSession` gains the field and property (after `pending_tx_hash`), and the record gains `credit_to`:

```python
    pending_tx_hash: str | None = None  # unknown-outcome tx on *_indeterminate (#493)
    # Credit this wallet's Closet instead of the holder's (#548: the app wallet's
    # stock moves into the house Closet). None = the holder's own Closet.
    credit_to: str | None = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex)

    @property
    def closet_owner(self) -> str:
        return self.credit_to or self.owner
```

```python
            "owner": self.owner,
            "credit_to": self.credit_to,
            "nft_id": self.nft_id,
```

In `run_deposit`, keep `owner` for the on-ledger holder check and the burn; use the Closet owner everywhere a Closet is read or written. The docstring's first line becomes "Deposit a standalone trait NFToken into a Closet: the owner's, or `credit_to`'s." Then:

```python
    conn, owner, nft_id = deps.conn, session.owner, session.nft_id
    closet_owner = session.closet_owner
    try:
        err = await _require_active_closet(deps, closet_owner)
        if err:
            session.fail(err)
            return
        stale = _mirror_pending_error(deps, closet_owner) or _mirror_behind_error(
            deps, closet_owner
        )
```

```python
        assets = _owner_contents(conn, closet_owner)
        assets[(session.slot, session.value)] = assets.get((session.slot, session.value), 0) + 1
        try:
            session.sync_tx_hash = await _sync_then_persist(deps, closet_owner, assets)
        except bt.ClosetMirrorError as e:
            ...
            es.set_mirror_pending(conn, closet_owner, True)
```

(The `info.get("owner") != owner` check and `deps.trait_burn_fn(nft_id, owner)` keep `owner`.)

- [ ] **Step 4: Run the new and the existing Deposit tests**

Run: `.venv/bin/python -m pytest tests/test_economy_deposit_credit_to.py tests/test_economy_flow_deposit.py tests/test_economy_char_indeterminate.py tests/test_economy_mirror_behind.py -q -p no:cacheprovider`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add lfg_core/economy_flow.py tests/test_economy_deposit_credit_to.py
git commit -m "feat(economy): Deposit can credit another wallet's Closet (#548)"
```

---

### Task 3: Migration plan, plan file and readiness

**Files:**
- Modify: `lfg_core/house_closet.py`
- Test: `tests/test_house_closet_migration.py`

**Interfaces:**
- Consumes: `house_closet.HouseConfigError` (Task 1).
- Produces: constants `LIST, HOLD, PLANNED, FAILED, NEEDS_ATTENTION, SKIPPED, HELD, DEPOSITED, LISTED`; `MigrationRefused`; `PlanItem(nft_id, slot, value, offer_index, price_brix, action)`; `build_plan(conn, app_wallet) -> list[PlanItem]`; `load_state(path) -> dict`; `save_state(path, state) -> None`; `merge_plan(state, items, house) -> int`; `house_busy(conn, house) -> bool`; `crossing_bids(conn, items, house) -> list[dict]`; `check_ready(conn, house, state, *, brix_line, limit) -> None`. State shape: `{"version": 1, "house": str | None, "started": int, "items": {nft_id: entry}}`, entry keys `slot, value, offer_index, price_brix, action, status, burn_hash, journal_id, order_id, fill_id, error`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_house_closet_migration.py
"""House Closet migration (#548): plan, plan file, phases."""

import sqlite3

import pytest
from cryptography.fernet import Fernet

from lfg_core import closet_market_store as cms
from lfg_core import closet_token as ct
from lfg_core import config
from lfg_core import economy_store as es
from lfg_core import house_closet as hc
from lfg_core import market_store
from lfg_core.market_store import MarketListing, upsert_listing
from lfg_core.nft_index import init_db as init_onchain_db

APP, HOUSE, USER, BIDDER = "rApp", "rHouse", "rUser", "rBidder"


def _db(tmp_path):
    conn = init_onchain_db(str(tmp_path / "onchain.db"))
    es.init_economy_schema(conn)
    market_store.init_db(conn)
    es.set_closet_token(conn, HOUSE, "C-house", "AB", status=ct.ACTIVE, offer_id=None)
    es.set_closet_contents(conn, HOUSE, [], [])
    conn.commit()
    return conn


def _listed(conn, nft_id, slot, value, price, *, seller=APP, destination=None):
    es.upsert_trait_token(conn, nft_id, seller, slot, value)
    upsert_listing(
        conn,
        MarketListing(
            offer_index=f"O{nft_id}".ljust(64, "0"),
            nft_id=nft_id,
            kind="trait",
            seller=seller,
            amount_brix=price,
            destination=destination,
            slot=slot,
            value=value,
            created_ledger=1,
            created_ts=1,
        ),
    )
    conn.commit()


def _stock(conn):
    _listed(conn, "TA", "Hat", "Cap", "12")
    _listed(conn, "TB", "Back", "None", "5")
    _listed(conn, "TC", "Hat", "Cap", "7", seller=USER)  # a user's listing
    _listed(conn, "TD", "Eyes", "Laser", "9", destination="rBroker")  # external
    es.upsert_trait_token(conn, "TE", APP, "Mouth", "Grin")  # app-held, unlisted
    conn.commit()


def test_build_plan_takes_the_app_wallets_listed_tokens(tmp_path):
    conn = _db(tmp_path)
    _stock(conn)
    assert hc.build_plan(conn, APP) == [
        hc.PlanItem("TB", "Back", "None", "OTB".ljust(64, "0"), "5", hc.HOLD),
        hc.PlanItem("TA", "Hat", "Cap", "OTA".ljust(64, "0"), "12", hc.LIST),
    ]


def test_merge_plan_resumes_and_pins_the_house(tmp_path):
    conn = _db(tmp_path)
    _stock(conn)
    state = hc.load_state(str(tmp_path / "plan.json"))
    assert hc.merge_plan(state, hc.build_plan(conn, APP), HOUSE) == 2
    state["items"]["TA"]["status"] = hc.DEPOSITED
    assert hc.merge_plan(state, hc.build_plan(conn, APP), HOUSE) == 0
    assert state["items"]["TA"]["status"] == hc.DEPOSITED  # a re-run resumes
    with pytest.raises(hc.MigrationRefused, match="house"):
        hc.merge_plan(state, [], "rOtherHouse")


def test_the_plan_file_round_trips(tmp_path):
    conn = _db(tmp_path)
    _stock(conn)
    path = str(tmp_path / "reports" / "plan.json")
    state = hc.load_state(path)
    hc.merge_plan(state, hc.build_plan(conn, APP), HOUSE)
    hc.save_state(path, state)
    assert hc.load_state(path) == state


def test_house_busy_sees_live_orders_and_unfinished_fills(tmp_path):
    conn = _db(tmp_path)
    assert not hc.house_busy(conn, HOUSE)
    es.set_closet_contents(conn, HOUSE, [("Hat", "Cap", 1)], [])
    cms.create_ask(conn, owner=HOUSE, slot="Hat", value="Cap", price_brix="3", platform=None)
    assert hc.house_busy(conn, HOUSE)


def test_crossing_bids_previews_fills_at_the_bid_price(tmp_path):
    conn = _db(tmp_path)
    _stock(conn)
    _open_bid(conn, "Hat", "Cap", "20")
    _open_bid(conn, "Hat", "Cap", "4", owner="rCheap")  # below the ask: no fill
    assert hc.crossing_bids(conn, hc.build_plan(conn, APP), HOUSE) == [
        {"slot": "Hat", "value": "Cap", "ask_brix": "12", "bid_brix": "20", "bidder": BIDDER}
    ]


def _open_bid(conn, slot, value, price, owner=BIDDER):
    es.set_closet_token(conn, owner, f"C-{owner}", "AB", status=ct.ACTIVE, offer_id=None)
    bid = cms.create_pending_bid(
        conn,
        owner=owner,
        slot=slot,
        value=value,
        price_brix=price,
        platform=None,
        condition="C",
        fulfillment_enc="S",
        cancel_after=9,
        payload_uuid=None,
        xumm_url=None,
        qr_url=None,
        push=None,
    )
    return cms.mark_bid_open(conn, bid["id"], escrow_tx_hash=f"E{bid['id']}", escrow_owner_seq=3)


def _ready_env(monkeypatch):
    monkeypatch.setattr(config, "ECONOMY_ENABLED", True)
    monkeypatch.setattr(config, "CLOSET_MARKET_ENABLED", True)
    monkeypatch.setattr(config, "CLOSET_MARKET_ENC_KEY", Fernet.generate_key().decode())


def test_check_ready_fails_closed(tmp_path, monkeypatch):
    conn = _db(tmp_path)
    state = hc.load_state(str(tmp_path / "plan.json"))
    line = {"limit": "1000000000"}
    _ready_env(monkeypatch)
    hc.check_ready(conn, HOUSE, state, brix_line=line, limit="1000000000")  # all good

    monkeypatch.setattr(config, "CLOSET_MARKET_ENABLED", False)
    with pytest.raises(hc.MigrationRefused, match="Closet market is off"):
        hc.check_ready(conn, HOUSE, state, brix_line=line, limit="1000000000")
    _ready_env(monkeypatch)
    with pytest.raises(hc.MigrationRefused, match="no active Closet"):
        hc.check_ready(conn, "rNoCloset", state, brix_line=line, limit="1000000000")
    with pytest.raises(hc.MigrationRefused, match="trust line"):
        hc.check_ready(conn, HOUSE, state, brix_line=None, limit="1000000000")
    with pytest.raises(hc.MigrationRefused, match="trust line"):
        hc.check_ready(conn, HOUSE, state, brix_line={"limit": "10"}, limit="1000000000")
    state["items"]["TX"] = {"status": hc.NEEDS_ATTENTION}
    with pytest.raises(hc.MigrationRefused, match="need attention"):
        hc.check_ready(conn, HOUSE, state, brix_line=line, limit="1000000000")
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_house_closet_migration.py -q -p no:cacheprovider`
Expected: FAIL — `AttributeError: module 'lfg_core.house_closet' has no attribute 'build_plan'` (and friends)

- [ ] **Step 3: Implement the plan, the plan file and the readiness checks**

Add to the imports of `lfg_core/house_closet.py`:

```python
import json
import os
import sqlite3
import tempfile
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from lfg_core import closet_market_store as cms
from lfg_core import market_ops
```

Then append:

```python
# Plan actions
LIST = "list"
HOLD = "hold"  # value "None": deposited, never listed (#516 D5)

# Item statuses (the spec's status table)
PLANNED = "planned"
FAILED = "failed"  # failed before the burn; nothing changed; retried
NEEDS_ATTENTION = "needs_attention"  # burned or unknown; never retried
SKIPPED = "skipped"
HELD = "held"
DEPOSITED = "deposited"
LISTED = "listed"


class MigrationRefused(RuntimeError):
    """A precondition failed. Nothing was changed."""


@dataclass(frozen=True)
class PlanItem:
    nft_id: str
    slot: str
    value: str
    offer_index: str
    price_brix: str
    action: str


def build_plan(conn: sqlite3.Connection, app_wallet: str) -> list[PlanItem]:
    """Every live in-app BRIX trait listing whose token the app wallet still
    holds. One item per token: its cheapest listing if it somehow has two."""
    rows = conn.execute(
        "SELECT m.nft_id, m.offer_index, t.slot, t.value, m.amount_brix "
        "FROM market_listings m JOIN trait_tokens t ON t.nft_id = m.nft_id "
        "WHERE m.kind = 'trait' AND m.is_live = 1 AND m.seller = ? AND t.owner = ? "
        "AND (m.destination IS NULL OR m.destination = '') AND m.amount_brix IS NOT NULL",
        (app_wallet, app_wallet),
    ).fetchall()
    best: dict[str, PlanItem] = {}
    for nft_id, offer_index, slot, value, amount_brix in rows:
        price = market_ops.validate_brix_value(str(amount_brix))
        item = PlanItem(nft_id, slot, value, offer_index, price, HOLD if value == "None" else LIST)
        kept = best.get(nft_id)
        if kept is None or Decimal(price) < Decimal(kept.price_brix):
            best[nft_id] = item
    return sorted(best.values(), key=lambda i: (i.slot, i.value, i.nft_id))


def load_state(path: str) -> dict[str, Any]:
    """The plan file, or a fresh one. `started` bounds which house asks a
    re-run may adopt as its own (see list_phase)."""
    try:
        with open(path) as fh:
            return dict(json.load(fh))
    except FileNotFoundError:
        return {"version": 1, "house": None, "started": int(time.time()), "items": {}}


def save_state(path: str, state: dict[str, Any]) -> None:
    """Atomic: a crash mid-write leaves the previous plan file intact."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".house_migration.")
    with os.fdopen(fd, "w") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)


def merge_plan(state: dict[str, Any], items: list[PlanItem], house: str) -> int:
    """Add items not in the plan yet as `planned`; existing entries keep their
    status, so a re-run resumes. Returns how many were added. A plan made for
    another house is refused: its deposits sit in that house's Closet."""
    if state["house"] not in (None, house):
        raise MigrationRefused(
            f"this plan file is for the house {state['house']}, not {house}; "
            "move it aside to start a new plan"
        )
    state["house"] = house
    added = 0
    for item in items:
        if item.nft_id in state["items"]:
            continue
        state["items"][item.nft_id] = {
            "slot": item.slot,
            "value": item.value,
            "offer_index": item.offer_index,
            "price_brix": item.price_brix,
            "action": item.action,
            "status": PLANNED,
            "burn_hash": None,
            "journal_id": None,
            "order_id": None,
            "fill_id": None,
            "error": None,
        }
        added += 1
    return added


def house_busy(conn: sqlite3.Connection, house: str) -> bool:
    """Whether the service may be rewriting the house Closet: a live order of
    the house's, or a fill with the house on either side that hasn't finished."""
    order = conn.execute(
        "SELECT 1 FROM closet_orders WHERE owner = ? "
        "AND state IN ('pending_escrow', 'open', 'matched', 'cancelling') LIMIT 1",
        (house,),
    ).fetchone()
    fill = conn.execute(
        "SELECT 1 FROM closet_fills WHERE (seller = ? OR buyer = ?) "
        "AND state NOT IN ('mirrored', 'refunded', 'failed') LIMIT 1",
        (house, house),
    ).fetchone()
    return order is not None or fill is not None


def crossing_bids(
    conn: sqlite3.Connection, items: list[PlanItem], house: str
) -> list[dict[str, str]]:
    """Open bids that a planned ask would fill the moment it is posted (bid >=
    the key's cheapest planned ask). Each fills at the BID's price: the resting
    order sets it (cms.cross_incoming)."""
    floors: dict[tuple[str, str], Decimal] = {}
    for item in items:
        if item.action == LIST:
            key = (item.slot, item.value)
            price = Decimal(item.price_brix)
            floors[key] = min(price, floors.get(key, price))
    out = []
    for (slot, value), floor in sorted(floors.items()):
        for bid in cms.book_levels(conn, slot, value)["bids"]:
            if bid["owner"] != house and Decimal(bid["price_brix"]) >= floor:
                out.append(
                    {
                        "slot": slot,
                        "value": value,
                        "ask_brix": market_ops.validate_brix_value(str(floor)),
                        "bid_brix": bid["price_brix"],
                        "bidder": bid["owner"],
                    }
                )
    return out


def check_ready(
    conn: sqlite3.Connection,
    house: str,
    state: dict[str, Any],
    *,
    brix_line: dict[str, Any] | None,
    limit: str,
) -> None:
    """Every precondition for --migrate --apply, checked before anything is
    written. Raises MigrationRefused naming the first one that fails."""
    if not config.closet_market_enabled():
        raise MigrationRefused(
            "the Closet market is off (CLOSET_MARKET_ENABLED + CLOSET_MARKET_ENC_KEY)"
        )
    if not cms.closet_active(conn, house):
        raise MigrationRefused(f"{house} has no active Closet; run --apply-setup first")
    if brix_line is None or Decimal(str(brix_line.get("limit", "0"))) < Decimal(limit):
        raise MigrationRefused(
            f"{house} needs a BRIX trust line with a limit of at least {limit}; "
            "run --apply-setup first"
        )
    stuck = [n for n, e in state["items"].items() if e["status"] == NEEDS_ATTENTION]
    if stuck:
        raise MigrationRefused(
            f"{len(stuck)} item(s) need attention ({', '.join(sorted(stuck))}); resolve "
            "them from their Deposit journals before the migration continues"
        )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_house_closet_migration.py -q -p no:cacheprovider`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add lfg_core/house_closet.py tests/test_house_closet_migration.py
git commit -m "feat(house-closet): migration plan, plan file and readiness checks (#548)"
```

---

### Task 4: Deposit and list phases

**Files:**
- Modify: `lfg_core/house_closet.py`
- Test: `tests/test_house_closet_migration.py` (append)

**Interfaces:**
- Consumes: Task 2's `DepositSession(..., credit_to=...)`; Task 3's plan, state, `house_busy`.
- Produces: `MigrationDeps(economy, app_wallet, house, fee_bps, plan_path)`; `async deposit_phase(state, deps) -> bool`; `list_phase(state, deps) -> bool`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_house_closet_migration.py`)

```python
from lfg_core import economy_flow as ef  # noqa: E402
from tests.test_economy_flow_deposit import _F, _deps, _run  # noqa: E402


class _Fakes(_F):
    """Deposit fakes with per-token metadata and the house Closet's owner."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.meta_for: dict[str, tuple[str, str]] = {}

    async def trait_meta(self, nft_id):
        slot, value = self.meta_for[nft_id]
        return {"lfg_trait": {"slot": slot, "value": value}}

    async def closet_owner(self, nft_id):
        return HOUSE if nft_id == "C-house" else None


def _migration(tmp_path, **fake_kw):
    conn = _db(tmp_path)
    _stock(conn)
    f = _Fakes(**fake_kw)
    for nft_id, key in {"TA": ("Hat", "Cap"), "TB": ("Back", "None")}.items():
        f.owner_for[nft_id] = APP
        f.meta_for[nft_id] = key
    deps = hc.MigrationDeps(
        economy=_deps(conn, f, tmp_path / "journal"),
        app_wallet=APP,
        house=HOUSE,
        fee_bps=700,
        plan_path=str(tmp_path / "plan.json"),
    )
    state = hc.load_state(deps.plan_path)
    hc.merge_plan(state, hc.build_plan(conn, APP), HOUSE)
    return conn, f, deps, state


def _live(conn, nft_id):
    return conn.execute(
        "SELECT is_live, closed_reason FROM market_listings WHERE nft_id = ?", (nft_id,)
    ).fetchone()


def test_deposit_phase_burns_into_the_house_and_closes_the_listings(tmp_path):
    conn, f, deps, state = _migration(tmp_path)
    assert _run(hc.deposit_phase(state, deps)) is True

    items = state["items"]
    assert items["TA"]["status"] == hc.DEPOSITED and items["TB"]["status"] == hc.HELD
    assert sorted(f.burns) == [("TA", APP), ("TB", APP)]
    assert {(o, s, v): n for o, s, v, n in es.read_closet_assets(conn)} == {
        (HOUSE, "Hat", "Cap"): 1,
        (HOUSE, "Back", "None"): 1,
    }
    assert tuple(_live(conn, "TA")) == (0, "cancelled")
    assert hc.load_state(deps.plan_path)["items"]["TA"]["status"] == hc.DEPOSITED


def test_deposit_phase_skips_a_token_the_app_wallet_no_longer_holds(tmp_path):
    conn, f, deps, state = _migration(tmp_path)
    f.owner_for["TA"] = "rBuyer"  # sold after the plan was made
    assert _run(hc.deposit_phase(state, deps)) is True
    assert state["items"]["TA"]["status"] == hc.SKIPPED
    assert f.burns == [("TB", APP)]


def test_a_failure_before_the_burn_stops_and_is_retried(tmp_path):
    conn, f, deps, state = _migration(tmp_path)
    f.info_none = True  # the on-ledger lookup fails
    assert _run(hc.deposit_phase(state, deps)) is False
    assert state["items"]["TB"]["status"] == hc.FAILED  # first in plan order
    assert state["items"]["TA"]["status"] == hc.PLANNED  # the run stopped
    assert f.burns == []
    f.info_none = False
    assert _run(hc.deposit_phase(state, deps)) is True
    assert state["items"]["TB"]["status"] == hc.HELD


def test_a_failure_after_the_burn_needs_attention_and_is_never_retried(tmp_path):
    conn, f, deps, state = _migration(tmp_path, fail_sync=True)
    assert _run(hc.deposit_phase(state, deps)) is False
    entry = state["items"]["TB"]
    assert entry["status"] == hc.NEEDS_ATTENTION and entry["journal_id"]
    assert f.burns == [("TB", APP)]
    f.fail_sync = False
    assert _run(hc.deposit_phase(state, deps)) is True  # TA only
    assert f.burns == [("TB", APP), ("TA", APP)]
    assert state["items"]["TB"]["status"] == hc.NEEDS_ATTENTION


def test_deposit_phase_refuses_while_the_house_is_busy(tmp_path):
    conn, f, deps, state = _migration(tmp_path)
    es.set_closet_contents(conn, HOUSE, [("Eyes", "Laser", 1)], [])
    cms.create_ask(conn, owner=HOUSE, slot="Eyes", value="Laser", price_brix="3", platform=None)
    with pytest.raises(hc.MigrationRefused, match="live order"):
        _run(hc.deposit_phase(state, deps))
    assert f.burns == []


def test_list_phase_relists_at_the_old_price_and_crosses_bids(tmp_path):
    conn, f, deps, state = _migration(tmp_path)
    _run(hc.deposit_phase(state, deps))
    bid = _open_bid(conn, "Hat", "Cap", "20")
    assert hc.list_phase(state, deps) is True

    entry = state["items"]["TA"]
    assert entry["status"] == hc.LISTED
    ask = cms.get_order(conn, entry["order_id"])
    assert (ask["owner"], ask["price_brix"]) == (HOUSE, "12")
    fill = cms.get_fill(conn, entry["fill_id"])
    assert fill["bid_order_id"] == bid["id"] and fill["price_brix"] == "20"
    assert state["items"]["TB"]["status"] == hc.HELD  # a None unit is never listed


def test_list_phase_waits_for_every_deposit(tmp_path):
    conn, f, deps, state = _migration(tmp_path)
    with pytest.raises(hc.MigrationRefused, match="not deposited"):
        hc.list_phase(state, deps)


def test_list_phase_adopts_an_ask_a_crash_left_unrecorded(tmp_path):
    conn, f, deps, state = _migration(tmp_path)
    _run(hc.deposit_phase(state, deps))
    # A crash between create_ask and the plan save: the ask exists, unrecorded.
    orphan = cms.create_ask(
        conn, owner=HOUSE, slot="Hat", value="Cap", price_brix="12", platform=None
    )
    assert hc.list_phase(state, deps) is True
    assert state["items"]["TA"]["order_id"] == orphan["id"]
    asks = conn.execute(
        "SELECT COUNT(*) FROM closet_orders WHERE owner = ? AND side = 'ask'", (HOUSE,)
    ).fetchone()[0]
    assert asks == 1
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_house_closet_migration.py -q -p no:cacheprovider`
Expected: FAIL — `AttributeError: module 'lfg_core.house_closet' has no attribute 'MigrationDeps'`

- [ ] **Step 3: Implement the phases** (append to `lfg_core/house_closet.py`; add `from lfg_core import economy_flow, market_store` to the imports)

```python
_RETRYABLE = (PLANNED, FAILED)


@dataclass
class MigrationDeps:
    economy: economy_flow.EconomyDeps
    app_wallet: str
    house: str
    fee_bps: int
    plan_path: str


async def deposit_phase(state: dict[str, Any], deps: MigrationDeps) -> bool:
    """Burn each planned token into the house Closet. True once nothing is left
    to deposit; False when it stopped on a failure (the item's error says why)."""
    items = state["items"]
    todo = sorted(n for n, e in items.items() if e["status"] in _RETRYABLE)
    if not todo:
        return True
    conn = deps.economy.conn
    if house_busy(conn, deps.house):
        raise MigrationRefused(
            "the house has a live order or an unfinished fill, so the service may be "
            "rewriting its Closet; deposits only run before the house lists anything"
        )
    for nft_id in todo:
        entry = items[nft_id]
        info = await deps.economy.trait_info_fn(nft_id)  # type: ignore[misc]
        if info is None:
            entry.update(status=FAILED, error="could not look up the token on-ledger")
            save_state(deps.plan_path, state)
            return False
        if info.get("is_burned") or info.get("owner") != deps.app_wallet:
            entry.update(
                status=SKIPPED,
                error=f"no longer held by the app wallet (owner {info.get('owner')}, "
                f"burned {bool(info.get('is_burned'))})",
            )
            save_state(deps.plan_path, state)
            continue
        session = economy_flow.DepositSession(
            owner=deps.app_wallet, nft_id=nft_id, credit_to=deps.house
        )
        await economy_flow.run_deposit(session, deps.economy)
        entry.update(journal_id=session.id, burn_hash=session.burn_hash)
        if session.state == economy_flow.DONE:
            # The burn deleted the sell offer on-ledger, but the listener does not
            # close listings on a burn; without this the row lingers until the
            # nightly market sweep.
            market_store.close_listing(conn, entry["offer_index"], "cancelled")
            entry.update(status=DEPOSITED if entry["action"] == LIST else HELD, error=None)
            save_state(deps.plan_path, state)
            continue
        burned = bool(session.burn_hash or session.pending_tx_hash)
        entry.update(status=NEEDS_ATTENTION if burned else FAILED, error=session.error)
        save_state(deps.plan_path, state)
        return False
    return True


def _unrecorded_ask(
    conn: sqlite3.Connection, house: str, key: tuple[str, str], state: dict[str, Any]
) -> dict[str, Any] | None:
    """A house ask for this key that no plan entry records, posted since this
    plan started: a crash between create_ask and the plan save. Adopting it keeps
    a re-run from listing the same unit twice."""
    recorded = {e.get("order_id") for e in state["items"].values()}
    rows = conn.execute(
        "SELECT id FROM closet_orders WHERE owner = ? AND side = 'ask' AND slot = ? "
        "AND value = ? AND state != 'cancelled' AND created_ts >= ? "
        "ORDER BY created_ts, id",
        (house, key[0], key[1], state["started"]),
    ).fetchall()
    for (order_id,) in rows:
        if order_id not in recorded:
            return cms.get_order(conn, order_id)
    return None


def list_phase(state: dict[str, Any], deps: MigrationDeps) -> bool:
    """Post an ask for each deposited unit at its old price, crossing any open
    bid at or above it (the service's sweep settles the fill). Runs only once
    every deposit is done; it never touches the Closet token, so it cannot race
    the service. True when every deposited unit is listed."""
    items = state["items"]
    waiting = [n for n, e in items.items() if e["status"] in (*_RETRYABLE, NEEDS_ATTENTION)]
    if waiting:
        raise MigrationRefused(
            f"{len(waiting)} item(s) not deposited yet; listing waits for every deposit"
        )
    conn = deps.economy.conn
    for nft_id in sorted(n for n, e in items.items() if e["status"] == DEPOSITED):
        entry = items[nft_id]
        key = (entry["slot"], entry["value"])
        order = _unrecorded_ask(conn, deps.house, key, state)
        if order is None:
            try:
                order = cms.create_ask(
                    conn,
                    owner=deps.house,
                    slot=key[0],
                    value=key[1],
                    price_brix=entry["price_brix"],
                    platform=None,
                )
            except cms.OrderError as exc:
                entry["error"] = str(exc)
                save_state(deps.plan_path, state)
                return False
        fill = (
            cms.cross_incoming(conn, order["id"], fee_bps=deps.fee_bps)
            if order["state"] == cms.OPEN
            else None
        )
        entry.update(
            status=LISTED, order_id=order["id"], fill_id=fill["id"] if fill else None, error=None
        )
        save_state(deps.plan_path, state)
    return True
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_house_closet_migration.py -q -p no:cacheprovider`
Expected: 14 passed

- [ ] **Step 5: Commit**

```bash
git add lfg_core/house_closet.py tests/test_house_closet_migration.py
git commit -m "feat(house-closet): deposit and list phases (#548)"
```

---

### Task 5: One-time house setup

**Files:**
- Modify: `lfg_core/house_closet.py`
- Test: `tests/test_house_closet_setup.py`

**Interfaces:**
- Consumes: `MigrationRefused` (Task 3); `closet_token.ensure_closet`, `closet_token.confirm_accept`, `economy_store.get_closet_record` / `set_closet_token`.
- Produces: `SetupDeps(economy, account_fn, brix_line_fn, submit_fn)`; `async setup_house(wallet, deps, *, limit) -> dict[str, str]`. `submit_fn(tx, wallet) -> str` returns the engine result.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_house_closet_setup.py
"""House setup (#548): BRIX trust line + Closet claim, signed by the house."""

import sqlite3

import pytest
from xrpl.models.transactions import NFTokenAcceptOffer, TrustSet
from xrpl.wallet import Wallet

from lfg_core import closet_token as ct
from lfg_core import config
from lfg_core import economy_store as es
from lfg_core import house_closet as hc
from tests.test_economy_flow_deposit import _F, _deps, _run

HOUSE_WALLET = Wallet.create()
HOUSE = HOUSE_WALLET.classic_address


class _Chain(_F):
    def __init__(self, *, funded=True, line=None, accept_result="tesSUCCESS"):
        super().__init__()
        self.funded, self.line, self.accept_result = funded, line, accept_result
        self.submitted: list = []
        self.accepted = False

    async def account(self, address):
        return {"Account": address} if self.funded else None

    async def brix_line(self, address):
        return self.line

    async def submit(self, tx, wallet):
        assert wallet is HOUSE_WALLET
        self.submitted.append(tx)
        if isinstance(tx, NFTokenAcceptOffer):
            self.accepted = self.accept_result == "tesSUCCESS"
            return self.accept_result
        return "tesSUCCESS"

    async def closet_owner(self, nft_id):
        return HOUSE if self.accepted else config.SWAP_ISSUER_ADDRESS


def _setup_deps(tmp_path, chain):
    conn = sqlite3.connect(":memory:")
    es.init_economy_schema(conn)
    return conn, hc.SetupDeps(
        economy=_deps(conn, chain, tmp_path),
        account_fn=chain.account,
        brix_line_fn=chain.brix_line,
        submit_fn=chain.submit,
    )


def _tagged(tx):
    return tx.source_tag == config.SOURCE_TAG and tx.memos


def test_setup_sets_the_trust_line_and_claims_the_closet(tmp_path):
    chain = _Chain()
    conn, deps = _setup_deps(tmp_path, chain)
    steps = _run(hc.setup_house(HOUSE_WALLET, deps, limit="1000000000"))

    trust, accept = chain.submitted
    assert isinstance(trust, TrustSet) and trust.account == HOUSE and _tagged(trust)
    assert trust.limit_amount.issuer == config.BRIX_ISSUER
    assert trust.limit_amount.value == "1000000000"
    assert isinstance(accept, NFTokenAcceptOffer) and accept.account == HOUSE and _tagged(accept)
    assert accept.nftoken_sell_offer == "OFFER"
    assert es.get_closet_record(conn, HOUSE)[2] == ct.ACTIVE
    assert steps == {"trust_line": "set", "closet": ct.ACTIVE}


def test_setup_is_idempotent(tmp_path):
    chain = _Chain(line={"limit": "1000000000"})
    conn, deps = _setup_deps(tmp_path, chain)
    es.set_closet_token(conn, HOUSE, "C-house", "AB", status=ct.ACTIVE, offer_id=None)
    assert _run(hc.setup_house(HOUSE_WALLET, deps, limit="1000000000")) == {
        "trust_line": "present",
        "closet": ct.ACTIVE,
    }
    assert chain.submitted == []


def test_setup_raises_a_low_limit(tmp_path):
    chain = _Chain(line={"limit": "10"})
    conn, deps = _setup_deps(tmp_path, chain)
    es.set_closet_token(conn, HOUSE, "C-house", "AB", status=ct.ACTIVE, offer_id=None)
    assert _run(hc.setup_house(HOUSE_WALLET, deps, limit="1000000000"))["trust_line"] == "set"


def test_setup_refuses_an_unfunded_wallet(tmp_path):
    chain = _Chain(funded=False)
    conn, deps = _setup_deps(tmp_path, chain)
    with pytest.raises(hc.MigrationRefused, match="not funded"):
        _run(hc.setup_house(HOUSE_WALLET, deps, limit="1000000000"))
    assert chain.submitted == []


def test_setup_accepts_a_pending_closets_offer_without_minting(tmp_path):
    chain = _Chain(line={"limit": "1000000000"})
    conn, deps = _setup_deps(tmp_path, chain)
    es.set_closet_token(conn, HOUSE, "C-house", "AB", status=ct.PENDING_ACCEPT, offer_id="OLD")
    _run(hc.setup_house(HOUSE_WALLET, deps, limit="1000000000"))
    (accept,) = chain.submitted
    assert accept.nftoken_sell_offer == "OLD"
    assert es.get_closet_record(conn, HOUSE)[0] == "C-house"


def test_a_failed_accept_clears_the_offer_for_the_next_run(tmp_path):
    chain = _Chain(line={"limit": "1000000000"}, accept_result="tecOBJECT_NOT_FOUND")
    conn, deps = _setup_deps(tmp_path, chain)
    es.set_closet_token(conn, HOUSE, "C-house", "AB", status=ct.PENDING_ACCEPT, offer_id="OLD")
    with pytest.raises(hc.MigrationRefused, match="tecOBJECT_NOT_FOUND"):
        _run(hc.setup_house(HOUSE_WALLET, deps, limit="1000000000"))
    assert es.get_closet_record(conn, HOUSE)[3] is None
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_house_closet_setup.py -q -p no:cacheprovider`
Expected: FAIL — `AttributeError: module 'lfg_core.house_closet' has no attribute 'SetupDeps'`

- [ ] **Step 3: Implement the setup** (append to `lfg_core/house_closet.py`; add the imports `from collections.abc import Awaitable, Callable`, `from xrpl.models.amounts import IssuedCurrencyAmount`, `from xrpl.models.transactions import NFTokenAcceptOffer, Transaction, TrustSet, TrustSetFlag`, `from lfg_core import closet_token as ct`, `from lfg_core import economy_store as es`, `from lfg_core import memos`)

```python
@dataclass
class SetupDeps:
    economy: economy_flow.EconomyDeps
    # account_info's account_data, or None when the account doesn't exist
    account_fn: Callable[[str], Awaitable[dict[str, Any] | None]]
    # the account's BRIX trust line (account_lines entry), or None
    brix_line_fn: Callable[[str], Awaitable[dict[str, Any] | None]]
    # sign with the wallet, submit, wait; returns the engine result
    submit_fn: Callable[[Transaction, Wallet], Awaitable[str]]


def _tag(action: str) -> dict[str, Any]:
    return {
        "source_tag": config.SOURCE_TAG,
        "memos": memos.build_memo_models(memos.INITIATOR_BACKEND, memos.PLATFORM_BACKEND, action),
    }


async def _no_payload(offer_id: str) -> None:
    """Setup accepts the Closet offer with the house seed: no Xaman payload."""
    return None


async def setup_house(wallet: Wallet, deps: SetupDeps, *, limit: str) -> dict[str, str]:
    """One-time and idempotent: the BRIX trust line (sale proceeds arrive in
    BRIX), then the house Closet, minted if needed and accepted with the house
    seed. Returns what each step did."""
    house = wallet.classic_address
    if await deps.account_fn(house) is None:
        raise MigrationRefused(
            f"{house} is not funded on this network; send it the XRP reserve first"
        )
    steps: dict[str, str] = {}
    line = await deps.brix_line_fn(house)
    if line is None or Decimal(str(line.get("limit", "0"))) < Decimal(limit):
        trust = TrustSet(
            account=house,
            flags=TrustSetFlag.TF_SET_NO_RIPPLE,
            limit_amount=IssuedCurrencyAmount(
                currency=config.BRIX_CURRENCY_HEX, issuer=config.BRIX_ISSUER, value=limit
            ),
            **_tag(memos.ACTION_TRUSTSET),
        )
        result = await deps.submit_fn(trust, wallet)
        if result != "tesSUCCESS":
            raise MigrationRefused(f"the BRIX TrustSet failed: {result}")
        steps["trust_line"] = "set"
    else:
        steps["trust_line"] = "present"

    econ, conn = deps.economy, deps.economy.conn
    rec = es.get_closet_record(conn, house)
    if rec is not None and rec[2] == ct.ACTIVE:
        steps["closet"] = ct.ACTIVE
        return steps
    if rec is None or not rec[3]:
        # No Closet yet (mint + offer), or a pending one whose offer id was lost
        # (a fresh offer). With no payload fn, ensure_closet only records the offer.
        await ct.ensure_closet(
            conn,
            house,
            upload_fn=econ.closet_upload_fn,
            mint_fn=econ.closet_mint_fn,
            offer_fn=econ.closet_offer_fn,
            accept_payload_fn=_no_payload,
            exists_fn=econ.closet_exists_fn,
        )
        rec = es.get_closet_record(conn, house)
    if rec is None or not rec[3]:
        raise MigrationRefused("the house Closet was not offered; re-run --apply-setup")
    nft_id, uri_hex, _status, offer_id = rec
    result = await deps.submit_fn(
        NFTokenAcceptOffer(account=house, nftoken_sell_offer=offer_id, **_tag(memos.ACTION_ACCEPT_OFFER)),
        wallet,
    )
    status = await ct.confirm_accept(conn, house, owner_fn=econ.closet_owner_fn)  # type: ignore[arg-type]
    if status != ct.ACTIVE and result != "tesSUCCESS":
        # The stored offer is gone or unusable: clear it so the next run makes
        # a fresh one for the same token.
        es.set_closet_token(conn, house, nft_id, uri_hex, status=status, offer_id=None)
        raise MigrationRefused(f"accepting the Closet offer failed: {result}; re-run --apply-setup")
    steps["closet"] = status
    return steps
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_house_closet_setup.py -q -p no:cacheprovider`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add lfg_core/house_closet.py tests/test_house_closet_setup.py
git commit -m "feat(house-closet): one-time house setup (#548)"
```

---

### Task 6: `scripts/house_closet.py`

**Files:**
- Create: `scripts/house_closet.py`
- Test: `tests/test_house_closet_script.py`

**Interfaces:**
- Consumes: everything in `lfg_core/house_closet.py`; `scripts/_economy_deps.open_index` / `build_economy_deps`; `xrpl_ops.async_rpc_client()`.
- Produces: the CLI; `main(argv: list[str] | None = None) -> int`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_house_closet_script.py
"""scripts/house_closet.py: argument rules and a dry run that writes nothing."""

import importlib
import json

import pytest

from lfg_core import config

script = importlib.import_module("scripts.house_closet")


def test_apply_needs_migrate():
    with pytest.raises(SystemExit):
        script.main(["--network", config.XRPL_NETWORK, "--apply"])


def test_dry_run_prints_the_plan_and_writes_nothing(tmp_path, monkeypatch, capsys):
    from tests.test_house_closet_migration import APP, _db, _stock

    conn = _db(tmp_path)
    _stock(conn)
    monkeypatch.setattr(config, "SIGNING_ACCOUNT", APP)
    monkeypatch.setattr(config, "CLOSET_HOUSE_WALLET", "")  # unset is fine for a dry run
    monkeypatch.setattr(script.deps, "open_index", lambda network: conn)
    plan_path = tmp_path / "reports" / "plan.json"
    monkeypatch.setattr(script, "plan_path", lambda network: str(plan_path))

    assert script.main(["--network", config.XRPL_NETWORK, "--migrate"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["plan"]["list"] == 1 and out["plan"]["hold"] == 1
    assert out["plan"]["list_brix_total"] == "12"
    assert not plan_path.exists()
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_house_closet_script.py -q -p no:cacheprovider`
Expected: FAIL — `ModuleNotFoundError: No module named 'scripts.house_closet'`

- [ ] **Step 3: Write the script**

```python
#!/usr/bin/env python3
"""House Closet (#548): move the project's trait stock into the Closet market.

  python scripts/house_closet.py --network mainnet                    # status (read-only)
  python scripts/house_closet.py --network mainnet --apply-setup      # one-time house setup
  python scripts/house_closet.py --network mainnet --migrate          # dry run: the plan
  python scripts/house_closet.py --network mainnet --migrate --apply  # burn -> house Closet -> relist

Needs CLOSET_HOUSE_WALLET (and, for --apply-setup, CLOSET_HOUSE_SEED) in .env.
The plan file is reports/house_migration_<network>.json. Runbook:
docs/ops/closet-market.md; design:
docs/superpowers/specs/2026-09-18-house-closet-migration-design.md.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from decimal import Decimal
from typing import Any

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.dirname(__file__))

from xrpl.asyncio.transaction import submit_and_wait  # noqa: E402
from xrpl.models.requests import AccountInfo, AccountLines  # noqa: E402
from xrpl.models.transactions import Transaction  # noqa: E402
from xrpl.wallet import Wallet  # noqa: E402

import _economy_deps as deps  # noqa: E402
from lfg_core import config, xrpl_ops  # noqa: E402
from lfg_core import house_closet as hc  # noqa: E402


def plan_path(network: str) -> str:
    return os.path.join(REPO_ROOT, "reports", f"house_migration_{network}.json")


async def _account(address: str) -> dict[str, Any] | None:
    client = xrpl_ops.async_rpc_client()
    result = (await client.request(AccountInfo(account=address, ledger_index="validated"))).result
    if result.get("error") == "actNotFound":
        return None
    if "account_data" not in result:
        raise SystemExit(f"account_info failed for {address}: {result}")
    return dict(result["account_data"])


async def _brix_line(address: str) -> dict[str, Any] | None:
    client = xrpl_ops.async_rpc_client()
    request = AccountLines(account=address, peer=config.BRIX_ISSUER, ledger_index="validated")
    for line in (await client.request(request)).result.get("lines", []):
        if str(line.get("currency", "")).upper() == str(config.BRIX_CURRENCY_HEX).upper():
            return dict(line)
    return None


async def _submit(tx: Transaction, wallet: Wallet) -> str:
    response = await submit_and_wait(tx, xrpl_ops.async_rpc_client(), wallet)
    return str(response.result.get("meta", {}).get("TransactionResult"))


def _confirm(network: str, what: str) -> None:
    typed = input(f"About to {what} on {network}. Type the network name to confirm: ").strip()
    if typed != network:
        raise SystemExit("aborted")


def _plan_summary(items: list[hc.PlanItem]) -> dict[str, Any]:
    listed = [i for i in items if i.action == hc.LIST]
    total = sum((Decimal(i.price_brix) for i in listed), Decimal(0))
    return {
        "list": len(listed),
        "hold": len(items) - len(listed),
        "list_brix_total": format(total, "f"),
    }


async def _status(conn: Any, network: str, show_items: bool) -> dict[str, Any]:
    out: dict[str, Any] = {"network": network, "app_wallet": config.SIGNING_ACCOUNT}
    house: str | None = None
    try:
        house = hc.house_address()
        out["house"] = house
    except hc.HouseConfigError as e:
        out["house_error"] = str(e)
    if house:
        out["house_funded"] = await _account(house) is not None
        line = await _brix_line(house) if out["house_funded"] else None
        out["house_brix_line"] = (
            {"limit": line.get("limit"), "balance": line.get("balance")} if line else None
        )
        out["house_closet_active"] = hc.cms.closet_active(conn, house)
    items = hc.build_plan(conn, config.SIGNING_ACCOUNT)
    out["plan"] = _plan_summary(items)
    out["crossing_bids"] = hc.crossing_bids(conn, items, house or "")
    if show_items:
        out["items"] = [
            {"nft_id": i.nft_id, "slot": i.slot, "value": i.value, "price_brix": i.price_brix, "action": i.action}
            for i in items
        ]
    state = hc.load_state(plan_path(network))
    if state["items"]:
        counts: dict[str, int] = {}
        for entry in state["items"].values():
            counts[entry["status"]] = counts.get(entry["status"], 0) + 1
        out["plan_file"] = {"house": state["house"], "statuses": counts}
    return out


async def _setup(conn: Any, network: str) -> dict[str, str]:
    wallet = hc.house_wallet()
    _confirm(network, f"set up {wallet.classic_address} as the house wallet (trust line + Closet)")
    setup_deps = hc.SetupDeps(
        economy=deps.build_economy_deps(conn),
        account_fn=_account,
        brix_line_fn=_brix_line,
        submit_fn=_submit,
    )
    return await hc.setup_house(wallet, setup_deps, limit=str(config.BRIX_TRUSTLINE_LIMIT))


async def _migrate(conn: Any, network: str) -> int:
    house = hc.house_address()
    path = plan_path(network)
    state = hc.load_state(path)
    items = hc.build_plan(conn, config.SIGNING_ACCOUNT)
    hc.check_ready(conn, house, state, brix_line=await _brix_line(house), limit=str(config.BRIX_TRUSTLINE_LIMIT))
    hc.merge_plan(state, items, house)
    todo = sum(1 for e in state["items"].values() if e["status"] in (hc.PLANNED, hc.FAILED))
    _confirm(network, f"burn {todo} project trait tokens into {house}'s Closet and relist them")
    hc.save_state(path, state)
    mdeps = hc.MigrationDeps(
        economy=deps.build_economy_deps(conn),
        app_wallet=config.SIGNING_ACCOUNT,
        house=house,
        fee_bps=config.CLOSET_MARKET_FEE_BPS,
        plan_path=path,
    )
    done = await hc.deposit_phase(state, mdeps) and hc.list_phase(state, mdeps)
    counts: dict[str, int] = {}
    for entry in state["items"].values():
        counts[entry["status"]] = counts.get(entry["status"], 0) + 1
    print(json.dumps({"plan_file": path, "statuses": counts, "complete": done}, indent=2))
    return 0 if done else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--network", required=True, choices=["mainnet", "testnet"])
    parser.add_argument("--apply-setup", action="store_true")
    parser.add_argument("--migrate", action="store_true")
    parser.add_argument("--apply", action="store_true", help="with --migrate: run it")
    args = parser.parse_args(argv)
    if args.apply and not args.migrate:
        parser.error("--apply only goes with --migrate")
    conn = deps.open_index(args.network)
    try:
        if args.apply_setup:
            print(json.dumps(asyncio.run(_setup(conn, args.network)), indent=2))
            return 0
        if args.migrate and args.apply:
            return asyncio.run(_migrate(conn, args.network))
        print(json.dumps(asyncio.run(_status(conn, args.network, args.migrate)), indent=2))
        return 0
    except (hc.HouseConfigError, hc.MigrationRefused) as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
```

The dry-run test sets `CLOSET_HOUSE_WALLET` empty, so `_status` never touches the network (no house → no account lookups).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_house_closet_script.py -q -p no:cacheprovider`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add scripts/house_closet.py tests/test_house_closet_script.py
git commit -m "feat(house-closet): scripts/house_closet.py status, setup and migration (#548)"
```

---

### Task 7: Keep the house out of leaderboards and metrics

**Files:**
- Modify: `lfg_service/app.py` (`_lb_system_accounts`)
- Modify: `scripts/sourcetag_metrics.py` (`excluded_wallets`)
- Test: `tests/test_house_closet_exclusions.py`

**Interfaces:**
- Consumes: `config.CLOSET_HOUSE_WALLET` (Task 1).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_house_closet_exclusions.py
"""The house wallet (#548) is a project wallet: its BRIX proceeds must not top
the leaderboards, and its setup transactions must not count as a user."""

import importlib

from lfg_core import config
from lfg_service import app as server

stm = importlib.import_module("scripts.sourcetag_metrics")
HOUSE = "rHouseWa11etXXXXXXXXXXXXXXXXXXXXXX"


def test_leaderboards_exclude_the_house(monkeypatch):
    monkeypatch.setattr(config, "CLOSET_HOUSE_WALLET", HOUSE)
    assert HOUSE in server._lb_system_accounts()


def test_sourcetag_metrics_exclude_the_house(monkeypatch):
    monkeypatch.setattr(config, "CLOSET_HOUSE_WALLET", HOUSE)
    assert HOUSE in stm.excluded_wallets()


def test_an_unset_house_adds_nothing(monkeypatch):
    monkeypatch.setattr(config, "CLOSET_HOUSE_WALLET", "")
    assert "" not in server._lb_system_accounts()
    assert "" not in stm.excluded_wallets()
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_house_closet_exclusions.py -q -p no:cacheprovider`
Expected: FAIL on the first two tests (`assert HOUSE in ...`)

- [ ] **Step 3: Add the house to both lists**

`lfg_service/app.py`, `_lb_system_accounts()`:

```python
                config.BRIX_DISTRIBUTOR_ADDRESS,
                config.BRIX_AMM_ACCOUNT,
                # #548: sale proceeds pile up here; it is a project wallet.
                config.CLOSET_HOUSE_WALLET,
```

`scripts/sourcetag_metrics.py`, `excluded_wallets()`:

```python
    # The house wallet (#548) signs its own setup (trust line + Closet accept).
    configured = {
        config.SIGNING_ACCOUNT,
        config.BRIX_DISTRIBUTOR_ADDRESS,
        config.CLOSET_HOUSE_WALLET,
    }
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_house_closet_exclusions.py tests/test_sourcetag_metrics.py -q -p no:cacheprovider`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add lfg_service/app.py scripts/sourcetag_metrics.py tests/test_house_closet_exclusions.py
git commit -m "feat(house-closet): keep the house wallet out of leaderboards and metrics (#548)"
```

---

### Task 8: Docs

**Files:**
- Modify: `CLAUDE.md` (env block; Closet Market section)
- Modify: `docs/ops/closet-market.md` (new section 7)

- [ ] **Step 1: CLAUDE.md env block** — after the `CLOSET_MARKET_LEDGER_MARGIN` line:

```
CLOSET_HOUSE_WALLET=<xrpl-address>                          # optional (#548); house wallet: owns the project's trait stock as Closet asks — never the issuer/app wallet
CLOSET_HOUSE_SEED=<seed>                                    # optional (#548); signs ONLY the house's one-time setup (BRIX trust line + Closet accept) in scripts/house_closet.py; must derive CLOSET_HOUSE_WALLET
```

- [ ] **Step 2: CLAUDE.md Closet Market section** — append a bullet:

```markdown
- **House wallet (#548).** The app wallet is the issuer and can't own a Closet
  (#383), so its trait stock moves into a house wallet's Closet:
  `scripts/house_closet.py --network <net>` (status) → `--apply-setup` (trust
  line + Closet, signed with `CLOSET_HOUSE_SEED`) → `--migrate` (dry run) →
  `--migrate --apply`. Deposit burns each listed token with
  `DepositSession(credit_to=house)`, then asks are posted at the old prices (a
  standing bid at or above one fills at the bid's price). Deposits refuse while
  the house has a live order or fill; the plan file
  `reports/house_migration_<net>.json` makes re-runs resume, and a post-burn
  failure (`needs_attention`) is never retried automatically.
```

- [ ] **Step 3: `docs/ops/closet-market.md`** — append:

````markdown
## 7. House wallet (#548)

The project's trait stock (the app wallet's NFT trait listings) sells through
the Closet market from a house wallet. The app wallet is the issuer and can't
own a Closet (#383).

1. Fill `CLOSET_HOUSE_WALLET` and `CLOSET_HOUSE_SEED` in `.env` (placeholders
   are already there). Fund the wallet with the XRP reserve.
2. Status, then the one-time setup (BRIX trust line + Closet claim, both signed
   with the house seed):
   ```bash
   .venv/bin/python scripts/house_closet.py --network mainnet
   .venv/bin/python scripts/house_closet.py --network mainnet --apply-setup
   ```
3. Review the plan: counts, total BRIX, the `None` units that are held, and
   `crossing_bids` (open bids that fill the moment their ask posts, at the
   bid's price):
   ```bash
   .venv/bin/python scripts/house_closet.py --network mainnet --migrate
   ```
4. Run it. Phase 1 burns each listed token into the house Closet and closes its
   listing; phase 2 posts the asks. The service's 2-minute sweep settles any
   fills.
   ```bash
   .venv/bin/python scripts/house_closet.py --network mainnet --migrate --apply
   ```
5. Check Browse and `scripts/audit_trait_economy.py` (the census must not move).

Re-running is safe: `reports/house_migration_<net>.json` records every item.
`failed` items (nothing changed) are retried. `needs_attention` items (burned,
or burn outcome unknown) are not: find the Deposit journal named in the plan
file under `ECONOMY_RECORDS_DIR` and resolve it first. Deposits refuse while
the house has a live order or an unfinished fill.
````

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md docs/ops/closet-market.md
git commit -m "docs(house-closet): env vars, CLAUDE.md and runbook (#548)"
```

---

### Task 9: Full gate and PR

- [ ] **Step 1:** `.venv/bin/ruff check . && .venv/bin/ruff format --check . && scripts/venv-python -m mypy .`
- [ ] **Step 2:** push through the pre-push gate: `PYTEST_ADDOPTS="--basetemp=<scratchpad>/pytest-tmp -p no:cacheprovider" git push -u origin feat/548-house-closet`
- [ ] **Step 3:** open the PR (ready, no AI attribution), link #548, wait for Greptile, close out findings on their threads.
