# Dress-Up Phase 3 — Dressing Room UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the Dressing Room game screen in the Discord Activity webapp — a unified canvas + Bucket palette + roster strip that wires the Phase 2 economy ops (equip / harvest / assemble) to new HTTP endpoints, with a `WEBAPP_DEV_MODE` mock harness + live-reload for fast local iteration.

**Architecture:** Vanilla-JS client (no build) talks to new aiohttp endpoints in `webapp/server.py`, which delegate to a new `webapp/economy_api.py` (real backend, reusing the Phase 2 `scripts/_economy_deps` wiring and the on-chain index DB) or `webapp/mock_economy.py` (in-memory fixture) when dev mode is on. The figure is composited client-side by stacking same-origin layer PNGs in `swap_meta.TRAIT_ORDER`; the server runs the real `makeNft` at commit.

**Tech Stack:** Python 3.10, aiohttp, sqlite3, xrpl-py; vanilla JS/HTML/CSS (no framework, no bundler); pytest.

## Global Constraints

- **No build step / no framework.** All client work is vanilla JS in `webapp/client/{index.html,app.js,style.css}`. No React/Tailwind/npm. Match the existing design system (`.card`, `.sticker`, `.primary`, Fredoka/Inter).
- **Same-origin only.** The Activity CSP blocks cross-origin `<img>`/fetch. Every layer/image the client loads goes through a same-origin route (`/api/layer`, existing `/api/img`).
- **Canonical z-order** is `swap_meta.TRAIT_ORDER = ["Background","Back","Body","Clothing","Mouth","Eyebrows","Eyes","Head","Accessory"]`; non-body slots are `trait_economy.NON_BODY_SLOTS` (TRAIT_ORDER minus `"Body"`). Never hardcode a different order.
- **`WEBAPP_DEV_MODE`** defaults to off (`""`/`"0"`) and MUST never be set in the pm2 prod env. When on it bypasses Discord OAuth and routes economy ops to the in-memory mock.
- **`ECONOMY_NETWORK`** defaults to `"testnet"` (Phase 2 economy is testnet-only for MVP). The economy DB is the per-network index (`onchain_<network>.db`) opened via `scripts._economy_deps.open_index`.
- **SourceTag** (`2606160021`) is already set by the Phase 2 ops in `xrpl_ops`; this phase adds no new transaction-building code (it only drives the existing flows), so no SourceTag work is needed here.
- **Untrusted data:** NFT/economy metadata is untrusted — build DOM with `textContent`/element nodes, never `innerHTML` (follow the existing swap card pattern).
- **Server-side re-verification:** never trust client-supplied ownership; re-load the character from the index and re-check ownership (`owner == wallet`) before every mutating op.

---

### Task 1: Config — `ECONOMY_NETWORK` and `WEBAPP_DEV_MODE`

**Files:**
- Modify: `lfg_core/config.py` (add two settings near `WEBAPP_PORT`, ~line 94)
- Test: `webapp/test_smoke.py`

**Interfaces:**
- Produces: `config.ECONOMY_NETWORK: str` (default `"testnet"`), `config.WEBAPP_DEV_MODE: bool` (default `False`).

- [ ] **Step 1: Write the failing test**

Add to `webapp/test_smoke.py`:

```python
def test_economy_config_defaults():
    from lfg_core import config
    assert config.ECONOMY_NETWORK in ("testnet", "mainnet")
    assert isinstance(config.WEBAPP_DEV_MODE, bool)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest webapp/test_smoke.py::test_economy_config_defaults -v`
Expected: FAIL with `AttributeError: module 'lfg_core.config' has no attribute 'ECONOMY_NETWORK'`

- [ ] **Step 3: Add the config**

In `lfg_core/config.py`, after the `WEBAPP_PORT` line:

```python
ECONOMY_NETWORK = os.getenv("ECONOMY_NETWORK", "testnet")  # economy DB network
WEBAPP_DEV_MODE = os.getenv("WEBAPP_DEV_MODE", "") not in ("", "0", "false", "False")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest webapp/test_smoke.py::test_economy_config_defaults -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add lfg_core/config.py webapp/test_smoke.py
git commit -m "feat(webapp): add ECONOMY_NETWORK and WEBAPP_DEV_MODE config"
```

---

### Task 2: Index read helper — `owner_live_nfts`

**Files:**
- Modify: `lfg_core/nft_index.py` (add after `live_nfts`, ~line 178)
- Test: `tests/test_nft_index_owner.py` (create)

**Interfaces:**
- Consumes: `nft_index.init_db`, `nft_index.upsert`, `OnchainNft`, `_row_to_nft`.
- Produces: `nft_index.owner_live_nfts(conn: sqlite3.Connection, owner: str) -> list[OnchainNft]` — non-burned tokens whose `owner` matches, ordered by `nft_number, nft_id`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_nft_index_owner.py`:

```python
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lfg_core import nft_index
from lfg_core.nft_index import OnchainNft


def _nft(nft_id, num, owner, burned=False):
    return OnchainNft(
        nft_id=nft_id, nft_number=num, owner=owner, is_burned=burned,
        mutable=True, uri_hex="", body="male", attributes=[], image="", ledger_index=1,
    )


def test_owner_live_nfts_filters_owner_and_burned():
    conn = nft_index.init_db(":memory:")
    nft_index.upsert(conn, _nft("A", 1, "rOwner"))
    nft_index.upsert(conn, _nft("B", 2, "rOther"))
    nft_index.upsert(conn, _nft("C", 3, "rOwner", burned=True))
    got = nft_index.owner_live_nfts(conn, "rOwner")
    assert [n.nft_id for n in got] == ["A"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_nft_index_owner.py -v`
Expected: FAIL with `AttributeError: module 'lfg_core.nft_index' has no attribute 'owner_live_nfts'`

- [ ] **Step 3: Implement**

In `lfg_core/nft_index.py`, after `live_nfts`:

```python
def owner_live_nfts(conn: sqlite3.Connection, owner: str) -> list[OnchainNft]:
    """Non-burned tokens currently owned by `owner`, in edition order."""
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        "SELECT * FROM onchain_nfts WHERE is_burned=0 AND owner=? "
        "ORDER BY nft_number, nft_id",
        (owner,),
    )
    return [_row_to_nft(row) for row in cur.fetchall()]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_nft_index_owner.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add lfg_core/nft_index.py tests/test_nft_index_owner.py
git commit -m "feat(index): owner_live_nfts query helper"
```

---

### Task 3: Economy read model — `webapp/economy_api.read_economy_state`

**Files:**
- Create: `webapp/economy_api.py`
- Test: `webapp/test_economy_api.py` (create)

**Interfaces:**
- Consumes: `nft_index.owner_live_nfts`, `economy_store.read_bucket_assets`, `economy_store.read_bucket_bodies`, `swap_meta.TRAIT_ORDER`, `trait_economy.NON_BODY_SLOTS`, `economy_flow.{DONE,FAILED}`.
- Produces:
  - `economy_api.TERMINAL_STATES: set[str]`
  - `economy_api.read_economy_state(conn, owner: str) -> dict` with shape:
    ```python
    {
      "characters": [{"nft_id": str, "edition": int|None, "body": str,
                       "mutable": bool, "image_url": str,
                       "attributes": list[{"trait_type": str, "value": str}]}],
      "bucket": {"assets": [{"slot": str, "value": str, "count": int}],
                  "bodies": [int]},
      "trait_order": list[str],
      "slots": list[str],
    }
    ```

- [ ] **Step 1: Write the failing test**

Create `webapp/test_economy_api.py`:

```python
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("XUMM_API_KEY", "test")
os.environ.setdefault("XUMM_API_SECRET", "test")
os.environ.setdefault("SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2")
os.environ.setdefault("TOKEN_ISSUER_ADDRESS", "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
os.environ.setdefault("TOKEN_CURRENCY_HEX", "4C46474F00000000000000000000000000000000")
os.environ.setdefault("BUNNY_CDN_ACCESS_KEY", "test")
os.environ.setdefault("BUNNY_CDN_STORAGE_ZONE", "test")
os.environ.setdefault("LAYER_SOURCE", "local")
os.environ.setdefault("BUNNY_PULL_ZONE", "nft.pullzone.example")

from lfg_core import nft_index, economy_store
from lfg_core.nft_index import OnchainNft
from webapp import economy_api


def _seed_conn():
    conn = nft_index.init_db(":memory:")
    economy_store.init_economy_schema(conn)
    nft_index.upsert(conn, OnchainNft(
        nft_id="A", nft_number=3537, owner="rOwner", is_burned=False, mutable=True,
        uri_hex="", body="male",
        attributes=[{"trait_type": "Head", "value": "Crown"}],
        image="https://cdn.example/3537.png", ledger_index=1))
    economy_store.set_bucket_contents(conn, "rOwner", [("Head", "Halo", 2)], [42])
    return conn


def test_read_economy_state_shape():
    conn = _seed_conn()
    state = economy_api.read_economy_state(conn, "rOwner")
    assert state["characters"][0]["edition"] == 3537
    assert state["characters"][0]["attributes"][0]["value"] == "Crown"
    assert state["bucket"]["assets"][0] == {"slot": "Head", "value": "Halo", "count": 2}
    assert state["bucket"]["bodies"] == [42]
    assert state["trait_order"][0] == "Background"
    assert "Body" not in state["slots"]


def test_read_economy_state_excludes_other_owners():
    conn = _seed_conn()
    state = economy_api.read_economy_state(conn, "rNobody")
    assert state["characters"] == []
    assert state["bucket"]["assets"] == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest webapp/test_economy_api.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'webapp.economy_api'`

- [ ] **Step 3: Implement**

Create `webapp/economy_api.py`:

```python
# webapp/economy_api.py
# HTTP-facing economy read model + session plumbing for the Dressing Room.
# Wraps the Phase 2 economy_flow ops (driven via scripts._economy_deps) and the
# per-network on-chain index DB. Kept separate from server.py so the economy
# HTTP concern stays focused.
from __future__ import annotations

import sqlite3
from typing import Any

from lfg_core import economy_flow, economy_store, nft_index, swap_meta, trait_economy

TERMINAL_STATES: set[str] = {economy_flow.DONE, economy_flow.FAILED}


def _char_dict(r: nft_index.OnchainNft) -> dict[str, Any]:
    return {
        "nft_id": r.nft_id,
        "edition": r.nft_number,
        "body": r.body,
        "mutable": bool(r.mutable),
        "image_url": r.image,
        "attributes": r.attributes,
    }


def read_economy_state(conn: sqlite3.Connection, owner: str) -> dict[str, Any]:
    """The Dressing Room's full view for one owner: live characters + Bucket."""
    chars = [_char_dict(r) for r in nft_index.owner_live_nfts(conn, owner)]
    assets = [
        {"slot": s, "value": v, "count": c}
        for (o, s, v, c) in economy_store.read_bucket_assets(conn)
        if o == owner
    ]
    bodies = [ed for (o, ed) in economy_store.read_bucket_bodies(conn) if o == owner]
    return {
        "characters": chars,
        "bucket": {"assets": assets, "bodies": bodies},
        "trait_order": swap_meta.TRAIT_ORDER,
        "slots": trait_economy.NON_BODY_SLOTS,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest webapp/test_economy_api.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add webapp/economy_api.py webapp/test_economy_api.py
git commit -m "feat(webapp): economy read model (read_economy_state)"
```

---

### Task 4: Session wrapper + serializer — `EconomyWebSession`

**Files:**
- Modify: `webapp/economy_api.py`
- Test: `webapp/test_economy_api.py`

**Interfaces:**
- Consumes: the Phase 2 session dataclasses (`economy_flow.{EquipSession,HarvestSession,AssembleSession}`).
- Produces:
  - `economy_api.economy_session_dict(kind: str, s: Any) -> dict` — JSON-safe per-op status.
  - `economy_api.EconomyWebSession` dataclass: fields `discord_id: str`, `kind: str`, `inner: Any`, `created_at: float`; properties `.id`, `.state`; method `.to_dict()`. (Shape matches what `make_status_handler`/`_prune_sessions`/`_active_session` require: `.discord_id`, `.state`, `.created_at`, `.to_dict()`.)

- [ ] **Step 1: Write the failing test**

Add to `webapp/test_economy_api.py`:

```python
from lfg_core import economy_flow
from lfg_core.nft_index import OnchainNft as _ON


def _char():
    return _ON(nft_id="A", nft_number=1, owner="rOwner", is_burned=False, mutable=True,
               uri_hex="", body="male", attributes=[], image="", ledger_index=1)


def test_equip_session_dict():
    s = economy_flow.EquipSession(owner="rOwner", character=_char(), slot="Head",
                                  incoming_value="Halo")
    s.state = economy_flow.DONE
    s.displaced_value = "Crown"
    d = economy_api.economy_session_dict("equip", s)
    assert d["state"] == "done" and d["displaced"] == "Crown" and d["error"] is None


def test_assemble_session_dict_surfaces_accept_link():
    s = economy_flow.AssembleSession(owner="rOwner", edition=42, chosen={},
                                     body_value="male", body_class="male")
    s.results = [{"nft_id": "N", "image_url": "img", "metadata_url": "m",
                  "accept": {"xumm_url": "https://xaman/abc"}}]
    d = economy_api.economy_session_dict("assemble", s)
    assert d["accept"] == "https://xaman/abc" and d["nft_id"] == "N"


def test_web_session_delegates():
    s = economy_flow.EquipSession(owner="rOwner", character=_char(), slot="Head",
                                  incoming_value="Halo")
    ws = economy_api.EconomyWebSession(discord_id="123", kind="equip", inner=s)
    assert ws.state == economy_flow.RUNNING
    assert ws.id == s.id
    assert ws.to_dict()["state"] == economy_flow.RUNNING
    assert isinstance(ws.created_at, float)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest webapp/test_economy_api.py -k "session_dict or web_session" -v`
Expected: FAIL with `AttributeError: module 'webapp.economy_api' has no attribute 'economy_session_dict'`

- [ ] **Step 3: Implement**

Add to `webapp/economy_api.py` (imports `time`, `dataclasses`):

```python
import time
from dataclasses import dataclass, field


def economy_session_dict(kind: str, s: Any) -> dict[str, Any]:
    """JSON-safe per-op session status for the client poller."""
    base: dict[str, Any] = {"id": s.id, "state": s.state, "error": s.error}
    if kind == "equip":
        base["displaced"] = s.displaced_value
    elif kind == "harvest":
        base["accept"] = (s.bucket_accept or {}).get("xumm_url")
        base["moved_assets"] = s.moved_assets
    elif kind == "assemble":
        r = s.results[0] if s.results else None
        base["accept"] = ((r["accept"] or {}).get("xumm_url")) if r else None
        base["image_url"] = r["image_url"] if r else None
        base["nft_id"] = r["nft_id"] if r else None
    return base


@dataclass
class EconomyWebSession:
    """Adapts a Phase 2 economy session to what server.py's session helpers
    expect (discord_id, state, created_at, to_dict)."""

    discord_id: str
    kind: str  # "equip" | "harvest" | "assemble"
    inner: Any
    created_at: float = field(default_factory=time.time)

    @property
    def id(self) -> str:
        return self.inner.id  # type: ignore[no-any-return]

    @property
    def state(self) -> str:
        return self.inner.state  # type: ignore[no-any-return]

    def to_dict(self) -> dict[str, Any]:
        return economy_session_dict(self.kind, self.inner)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest webapp/test_economy_api.py -v`
Expected: PASS (all)

- [ ] **Step 5: Commit**

```bash
git add webapp/economy_api.py webapp/test_economy_api.py
git commit -m "feat(webapp): EconomyWebSession wrapper + per-op serializer"
```

---

### Task 5: Op starters — `start_equip` / `start_harvest` / `start_assemble`

**Files:**
- Modify: `webapp/economy_api.py`
- Verify: `scripts/__init__.py` exists (so `scripts._economy_deps` imports). If missing, `touch scripts/__init__.py` and include it in the commit.
- Test: `webapp/test_economy_api.py`

**Interfaces:**
- Consumes: `scripts._economy_deps` (`open_index`, `load_index_character`, `fetch_burnable`, `build_economy_deps`), `config.ECONOMY_NETWORK`, `trait_economy.{effective_genesis,can_equip,can_harvest,can_assemble}`, `economy_store.{read_genesis,read_supply_changes}`, `nft_index.live_nfts`.
- Produces three async coroutines that re-verify server-side, build the Phase 2 session, schedule `run_*` as a background task, and return an `EconomyWebSession` (or raise `EconomyError(msg)`):
  - `economy_api.start_equip(discord_id, owner, nft_id, slot, value) -> EconomyWebSession`
  - `economy_api.start_harvest(discord_id, owner, nft_id) -> EconomyWebSession`
  - `economy_api.start_assemble(discord_id, owner, edition, chosen) -> EconomyWebSession`
  - `economy_api.EconomyError(Exception)` (carries a user-safe message)
  - `economy_api.open_conn() -> sqlite3.Connection` (opens the configured economy index)

> Implementation note: `run_*` use `conn` synchronously inside async code, so open the conn on the event-loop thread (do NOT wrap `open_conn` in `asyncio.to_thread`). One conn per op is fine (sqlite open is cheap); the conn is captured by the scheduled task and the deps.

- [ ] **Step 1: Write the failing test** (uses monkeypatched deps; no network)

Add to `webapp/test_economy_api.py`:

```python
import asyncio
import pytest


def test_start_equip_precheck_rejects_unowned(monkeypatch):
    conn = _seed_conn()  # owner rOwner holds edition 3537 (nft_id "A"), Bucket has Head=Halo
    monkeypatch.setattr(economy_api, "open_conn", lambda: conn)

    async def go():
        with pytest.raises(economy_api.EconomyError):
            # nft_id "A" is owned by rOwner, not rNobody -> precheck fails
            await economy_api.start_equip("123", "rNobody", "A", "Head", "Halo")

    asyncio.get_event_loop().run_until_complete(go())


def test_start_equip_happy_returns_session(monkeypatch):
    conn = _seed_conn()
    monkeypatch.setattr(economy_api, "open_conn", lambda: conn)

    captured = {}

    async def fake_run_equip(session, deps):
        captured["ran"] = True
        session.state = economy_flow.DONE
        session.displaced_value = "Crown"

    monkeypatch.setattr(economy_flow, "run_equip", fake_run_equip)
    # Stub the real deps builder so no XRPL/CDN is touched.
    from scripts import _economy_deps
    monkeypatch.setattr(_economy_deps, "build_economy_deps", lambda c: object())

    async def go():
        ws = await economy_api.start_equip("123", "rOwner", "A", "Head", "Halo")
        # give the scheduled task a tick to run
        await asyncio.sleep(0)
        return ws

    ws = asyncio.get_event_loop().run_until_complete(go())
    assert ws.kind == "equip" and ws.discord_id == "123"
    assert captured.get("ran") is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest webapp/test_economy_api.py -k start_equip -v`
Expected: FAIL with `AttributeError: module 'webapp.economy_api' has no attribute 'open_conn'`

- [ ] **Step 3: Implement**

Add to `webapp/economy_api.py`:

```python
import asyncio

from lfg_core import config
from scripts import _economy_deps


class EconomyError(Exception):
    """A user-safe economy precondition/validation failure."""


def open_conn() -> sqlite3.Connection:
    """Open the configured per-network economy index (event-loop thread only)."""
    return _economy_deps.open_index(config.ECONOMY_NETWORK)


def _load_owned_character(conn: sqlite3.Connection, owner: str, nft_id: str):
    rec = _economy_deps.load_index_character(conn, nft_id)
    if rec is None:
        raise EconomyError("character not found in the index")
    if rec.owner != owner:
        raise EconomyError("that character is not in your wallet")
    return rec


def _schedule(kind: str, discord_id: str, session: Any, conn: sqlite3.Connection,
              runner: Any) -> EconomyWebSession:
    deps = _economy_deps.build_economy_deps(conn)
    asyncio.get_event_loop().create_task(runner(session, deps))
    return EconomyWebSession(discord_id=discord_id, kind=kind, inner=session)


async def start_equip(discord_id: str, owner: str, nft_id: str, slot: str,
                      value: str) -> EconomyWebSession:
    conn = open_conn()
    rec = _load_owned_character(conn, owner, nft_id)
    assets = {(s, v): c for (o, s, v, c) in economy_store.read_bucket_assets(conn)
              if o == owner}
    chk = trait_economy.can_equip(rec, slot, value, assets, mutable=bool(rec.mutable))
    if not chk.ok:
        raise EconomyError(f"cannot equip: {chk.reason}")
    session = economy_flow.EquipSession(owner=owner, character=rec, slot=slot,
                                        incoming_value=value)
    return _schedule("equip", discord_id, session, conn, economy_flow.run_equip)


async def start_harvest(discord_id: str, owner: str, nft_id: str) -> EconomyWebSession:
    conn = open_conn()
    rec = _load_owned_character(conn, owner, nft_id)
    genesis = trait_economy.effective_genesis(
        economy_store.read_genesis(conn), economy_store.read_supply_changes(conn))
    burnable = await _economy_deps.fetch_burnable(owner, nft_id)
    chk = trait_economy.can_harvest(rec, genesis, burnable)
    if not chk.ok:
        raise EconomyError(f"cannot harvest: {chk.reason}")
    session = economy_flow.HarvestSession(owner=owner, character=rec, burnable=burnable)
    return _schedule("harvest", discord_id, session, conn, economy_flow.run_harvest)


async def start_assemble(discord_id: str, owner: str, edition: int,
                         chosen: dict[str, str]) -> EconomyWebSession:
    conn = open_conn()
    genesis = trait_economy.effective_genesis(
        economy_store.read_genesis(conn), economy_store.read_supply_changes(conn))
    body = genesis.edition_bodies.get(edition)
    if body is None:
        raise EconomyError(f"edition {edition} has no known body")
    assets = {(s, v): c for (o, s, v, c) in economy_store.read_bucket_assets(conn)
              if o == owner}
    bodies = {ed for (o, ed) in economy_store.read_bucket_bodies(conn) if o == owner}
    live_editions = {r.nft_number for r in nft_index.live_nfts(conn)
                     if r.nft_number is not None}
    chk = trait_economy.can_assemble(edition, chosen, bodies, assets, live_editions,
                                     genesis)
    if not chk.ok:
        raise EconomyError(f"cannot assemble: {chk.reason}")
    session = economy_flow.AssembleSession(
        owner=owner, edition=edition, chosen=chosen,
        body_value=body[0], body_class=body[1], live_editions=live_editions)
    return _schedule("assemble", discord_id, session, conn, economy_flow.run_assemble)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest webapp/test_economy_api.py -v`
Expected: PASS (all)

- [ ] **Step 5: Commit**

```bash
git add webapp/economy_api.py webapp/test_economy_api.py scripts/__init__.py
git commit -m "feat(webapp): economy op starters with server-side preconditions"
```

---

### Task 6: Layer route — `GET /api/layer`

**Files:**
- Modify: `webapp/server.py` (new handler + route)
- Test: `webapp/test_smoke.py`

**Interfaces:**
- Consumes: `lfg_core.layer_store.get_layer_store()`, `layer_store.LAYER_EXTENSIONS`.
- Produces: `GET /api/layer?body=<b>&trait=<t>&value=<v>` → the layer file bytes with the right `Content-Type` (same-origin, CSP-safe), 404 if unresolved, 400 on missing/oversized params.

- [ ] **Step 1: Write the failing test**

Add to `webapp/test_smoke.py`:

```python
def test_layer_route_registered():
    app = server.create_app()
    paths = {getattr(r.resource, "canonical", "") for r in app.router.routes()}
    assert "/api/layer" in paths


def test_layer_handler_bad_params(monkeypatch):
    from aiohttp.test_utils import make_mocked_request
    req = make_mocked_request("GET", "/api/layer")  # no query
    resp = asyncio.get_event_loop().run_until_complete(server.handle_layer(req))
    assert resp.status == 400
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest webapp/test_smoke.py -k layer -v`
Expected: FAIL (`handle_layer` undefined / route missing)

- [ ] **Step 3: Implement**

In `webapp/server.py`, add `import mimetypes` and `from lfg_core import ... layer_store` to the existing import, then:

```python
async def handle_layer(request):
    """Same-origin layer file for client-side compositing (CSP-safe).
    Resolves (body, trait, value) through the configured layer_store, which
    serves from local disk or the CDN download cache."""
    body = request.query.get("body", "")
    trait = request.query.get("trait", "")
    value = request.query.get("value", "")
    if not body or not trait or not value or any(
        len(x) > 128 or "/" in x or ".." in x for x in (body, trait, value)
    ):
        return web.json_response({"error": "bad layer params"}, status=400)
    store = layer_store.get_layer_store()
    path = await store.resolve(body, trait, value)
    if not path or not os.path.exists(path):
        return web.json_response({"error": "layer not found"}, status=404)
    ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
    return web.FileResponse(
        path, headers={"Content-Type": ctype, "Cache-Control": "public, max-age=86400"}
    )
```

Register in `create_app()` (before `add_static`):

```python
    app.router.add_get("/api/layer", handle_layer)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest webapp/test_smoke.py -k layer -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add webapp/server.py webapp/test_smoke.py
git commit -m "feat(webapp): /api/layer same-origin layer route"
```

---

### Task 7: Mock economy fixture — `webapp/mock_economy.py`

**Files:**
- Create: `webapp/mock_economy.py`
- Test: `webapp/test_mock_economy.py` (create)

**Interfaces:**
- Produces `mock_economy.MockEconomy` with an in-memory state implementing the same surface the handlers use:
  - `read_state(owner: str) -> dict` (same shape as `read_economy_state`)
  - `equip(owner, nft_id, slot, value) -> dict` — mutates mock state, returns a terminal session dict `{"id","state":"done","error":None,"displaced":<old>}`
  - `harvest(owner, nft_id) -> dict` — moves the character's non-body attrs into the Bucket, returns `{"id","state":"done","error":None,"accept":None,"moved_assets":[...]}`
  - `assemble(owner, edition, chosen) -> dict` — returns `{"id","state":"done","error":None,"accept":"https://xaman/MOCK","nft_id":"MOCK","image_url":...}`
  - `mock_economy.DEV_OWNER: str` and a module-level `INSTANCE = MockEconomy()` seeded with one character + a few Bucket assets so the UI renders immediately.

> Keep it deterministic (no randomness). It is BOTH the dev-mode data source AND the endpoint-test fixture.

- [ ] **Step 1: Write the failing test**

Create `webapp/test_mock_economy.py`:

```python
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from webapp import mock_economy


def test_seeded_state_renders():
    m = mock_economy.MockEconomy()
    st = m.read_state(mock_economy.DEV_OWNER)
    assert st["characters"], "seed at least one character"
    assert st["bucket"]["assets"], "seed at least one bucket asset"
    assert st["trait_order"][0] == "Background"


def test_equip_swaps_and_returns_displaced():
    m = mock_economy.MockEconomy()
    owner = mock_economy.DEV_OWNER
    char = m.read_state(owner)["characters"][0]
    asset = m.read_state(owner)["bucket"]["assets"][0]
    old = next(a["value"] for a in char["attributes"] if a["trait_type"] == asset["slot"])
    res = m.equip(owner, char["nft_id"], asset["slot"], asset["value"])
    assert res["state"] == "done" and res["displaced"] == old
    # incoming now on the character; displaced now in the bucket
    char2 = m.read_state(owner)["characters"][0]
    assert any(a["trait_type"] == asset["slot"] and a["value"] == asset["value"]
               for a in char2["attributes"])


def test_harvest_moves_parts_to_bucket():
    m = mock_economy.MockEconomy()
    owner = mock_economy.DEV_OWNER
    char = m.read_state(owner)["characters"][0]
    res = m.harvest(owner, char["nft_id"])
    assert res["state"] == "done"
    assert not any(c["nft_id"] == char["nft_id"]
                   for c in m.read_state(owner)["characters"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest webapp/test_mock_economy.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'webapp.mock_economy'`

- [ ] **Step 3: Implement**

Create `webapp/mock_economy.py`:

```python
# webapp/mock_economy.py
# In-memory economy stand-in for WEBAPP_DEV_MODE and endpoint tests. No network,
# no XRPL/XUMM, deterministic. Mirrors economy_api's read/op surface.
from __future__ import annotations

import copy
from typing import Any

from lfg_core import swap_meta, trait_economy

DEV_OWNER = "rDevOwnerLFG000000000000000000000"


def _attrs(**slots: str) -> list[dict[str, str]]:
    return [{"trait_type": s, "value": slots.get(s, "None")} for s in swap_meta.TRAIT_ORDER]


class MockEconomy:
    def __init__(self) -> None:
        self.characters: list[dict[str, Any]] = [
            {
                "nft_id": "MOCK-3537", "edition": 3537, "body": "male", "mutable": True,
                "image_url": "", "attributes": _attrs(
                    Body="male", Background="Blue", Clothing="Hoodie", Eyes="Laser",
                    Head="Crown", Mouth="Grin", Eyebrows="Raised"),
            },
            {
                "nft_id": "MOCK-3540", "edition": 3540, "body": "female", "mutable": True,
                "image_url": "", "attributes": _attrs(
                    Body="female", Background="Pink", Clothing="Dress", Eyes="Wink",
                    Head="Bow", Mouth="Smile", Eyebrows="Flat"),
            },
        ]
        # Bucket assets keyed (slot, value) -> count; only male-compatible for demo.
        self.assets: dict[tuple[str, str], int] = {
            ("Head", "Halo"): 2, ("Head", "Tophat"): 1,
            ("Eyes", "Shades"): 1, ("Clothing", "Suit"): 1,
        }
        self.bodies: list[int] = [42]

    # --- reads ---
    def read_state(self, owner: str) -> dict[str, Any]:
        chars = copy.deepcopy(self.characters) if owner == DEV_OWNER else []
        assets = [{"slot": s, "value": v, "count": c}
                  for (s, v), c in self.assets.items() if c > 0] if owner == DEV_OWNER else []
        bodies = list(self.bodies) if owner == DEV_OWNER else []
        return {"characters": chars, "bucket": {"assets": assets, "bodies": bodies},
                "trait_order": swap_meta.TRAIT_ORDER, "slots": trait_economy.NON_BODY_SLOTS}

    def _char(self, nft_id: str) -> dict[str, Any]:
        for c in self.characters:
            if c["nft_id"] == nft_id:
                return c
        raise KeyError(nft_id)

    # --- ops ---
    def equip(self, owner: str, nft_id: str, slot: str, value: str) -> dict[str, Any]:
        char = self._char(nft_id)
        attr = next(a for a in char["attributes"] if a["trait_type"] == slot)
        displaced = attr["value"]
        if self.assets.get((slot, value), 0) <= 0:
            return {"id": "mock", "state": "failed", "error": "asset not in bucket"}
        attr["value"] = value
        self.assets[(slot, value)] -= 1
        if displaced != "None":
            self.assets[(slot, displaced)] = self.assets.get((slot, displaced), 0) + 1
        return {"id": "mock", "state": "done", "error": None, "displaced": displaced}

    def harvest(self, owner: str, nft_id: str) -> dict[str, Any]:
        char = self._char(nft_id)
        moved = []
        for a in char["attributes"]:
            if a["trait_type"] in trait_economy.NON_BODY_SLOTS and a["value"] != "None":
                self.assets[(a["trait_type"], a["value"])] = \
                    self.assets.get((a["trait_type"], a["value"]), 0) + 1
                moved.append((a["trait_type"], a["value"]))
        self.bodies.append(char["edition"])
        self.characters = [c for c in self.characters if c["nft_id"] != nft_id]
        return {"id": "mock", "state": "done", "error": None, "accept": None,
                "moved_assets": moved}

    def assemble(self, owner: str, edition: int, chosen: dict[str, str]) -> dict[str, Any]:
        for slot, value in chosen.items():
            self.assets[(slot, value)] = self.assets.get((slot, value), 0) - 1
        if edition in self.bodies:
            self.bodies.remove(edition)
        self.characters.append({
            "nft_id": f"MOCK-{edition}", "edition": edition, "body": "male",
            "mutable": True, "image_url": "",
            "attributes": [{"trait_type": s, "value": chosen.get(s, "None")}
                           for s in swap_meta.TRAIT_ORDER],
        })
        return {"id": "mock", "state": "done", "error": None,
                "accept": "https://xaman/MOCK", "nft_id": f"MOCK-{edition}",
                "image_url": ""}


INSTANCE = MockEconomy()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest webapp/test_mock_economy.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add webapp/mock_economy.py webapp/test_mock_economy.py
git commit -m "feat(webapp): MockEconomy in-memory fixture (dev + tests)"
```

---

### Task 8: Economy endpoints — `/api/economy`, `/api/equip`, `/api/harvest`, `/api/assemble` (+ status)

**Files:**
- Modify: `webapp/server.py`
- Test: `webapp/test_smoke.py`

**Interfaces:**
- Consumes: `economy_api`, `mock_economy`, `config.WEBAPP_DEV_MODE`, the existing `require_wallet`, `make_status_handler`, `_prune_sessions`, `_active_session`.
- Produces routes:
  - `GET /api/economy` → `read_economy_state` (or mock) for the caller's wallet.
  - `POST /api/equip` `{nft_id,slot,value}` → starts op; `GET /api/equip/{session_id}` polls.
  - `POST /api/harvest` `{nft_id}` → starts op; `GET /api/harvest/{session_id}` polls.
  - `POST /api/assemble` `{edition,chosen}` → starts op; `GET /api/assemble/{session_id}` polls.
- Adds `economy_sessions: dict[str, Any] = {}` to server.py module state.

> Dev mode: when `config.WEBAPP_DEV_MODE`, mutating endpoints return the mock's terminal dict directly (state already `done`), and `GET /api/economy` returns `mock_economy.INSTANCE.read_state(...)`. The status GET routes still resolve real sessions (mock ops are synchronous, so the POST response is already terminal — the client treats a terminal POST as no-poll-needed).

- [ ] **Step 1: Write the failing test**

Add to `webapp/test_smoke.py`:

```python
def test_economy_routes_registered():
    app = server.create_app()
    paths = {getattr(r.resource, "canonical", "") for r in app.router.routes()}
    for expected in ["/api/economy", "/api/equip", "/api/equip/{session_id}",
                     "/api/harvest", "/api/harvest/{session_id}",
                     "/api/assemble", "/api/assemble/{session_id}"]:
        assert expected in paths, f"missing route {expected}"


def test_economy_dev_mode_read(monkeypatch):
    from aiohttp.test_utils import make_mocked_request
    from webapp import mock_economy
    monkeypatch.setattr(server.config, "WEBAPP_DEV_MODE", True)
    # require_wallet is bypassed in dev mode; handler reads the dev owner.
    req = make_mocked_request("GET", "/api/economy")
    req["user"] = {"id": "dev", "name": "dev"}
    req["wallet"] = mock_economy.DEV_OWNER
    resp = asyncio.get_event_loop().run_until_complete(server.handle_economy(req))
    assert resp.status == 200
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest webapp/test_smoke.py -k economy -v`
Expected: FAIL (routes/handlers missing)

- [ ] **Step 3: Implement**

In `webapp/server.py` add `from webapp import economy_api, mock_economy` and module state `economy_sessions: dict[str, Any] = {}`, then:

```python
@require_wallet
async def handle_economy(request):
    if config.WEBAPP_DEV_MODE:
        return web.json_response(mock_economy.INSTANCE.read_state(request["wallet"]))
    conn = economy_api.open_conn()
    try:
        return web.json_response(economy_api.read_economy_state(conn, request["wallet"]))
    finally:
        conn.close()


def _economy_post(kind, start_coro, mock_call):
    @require_wallet
    async def handler(request):
        user = request["user"]
        body = await request.json()
        if config.WEBAPP_DEV_MODE:
            try:
                return web.json_response(mock_call(request["wallet"], body))
            except Exception as e:
                return web.json_response({"error": str(e)}, status=400)
        _prune_sessions(economy_sessions, economy_api.TERMINAL_STATES)
        if _active_session(economy_sessions, economy_api.TERMINAL_STATES, user["id"]):
            return web.json_response({"error": "an economy action is already in progress"},
                                     status=409)
        try:
            ws = await start_coro(user["id"], request["wallet"], body)
        except economy_api.EconomyError as e:
            return web.json_response({"error": str(e)}, status=400)
        except Exception as e:
            logging.error(f"{kind} failed to start: {e}")
            return web.json_response({"error": "could not start the action"}, status=502)
        economy_sessions[ws.id] = ws
        return web.json_response(ws.to_dict())

    return handler


handle_equip_start = _economy_post(
    "equip",
    lambda uid, w, b: economy_api.start_equip(uid, w, b["nft_id"], b["slot"], b["value"]),
    lambda w, b: mock_economy.INSTANCE.equip(w, b["nft_id"], b["slot"], b["value"]),
)
handle_harvest_start = _economy_post(
    "harvest",
    lambda uid, w, b: economy_api.start_harvest(uid, w, b["nft_id"]),
    lambda w, b: mock_economy.INSTANCE.harvest(w, b["nft_id"]),
)
handle_assemble_start = _economy_post(
    "assemble",
    lambda uid, w, b: economy_api.start_assemble(uid, w, int(b["edition"]), b["chosen"]),
    lambda w, b: mock_economy.INSTANCE.assemble(w, int(b["edition"]), b["chosen"]),
)

handle_equip_status = make_status_handler(economy_sessions)
handle_harvest_status = make_status_handler(economy_sessions)
handle_assemble_status = make_status_handler(economy_sessions)
```

Register in `create_app()` (before `add_static`):

```python
    app.router.add_get("/api/economy", handle_economy)
    app.router.add_post("/api/equip", handle_equip_start)
    app.router.add_get("/api/equip/{session_id}", handle_equip_status)
    app.router.add_post("/api/harvest", handle_harvest_start)
    app.router.add_get("/api/harvest/{session_id}", handle_harvest_status)
    app.router.add_post("/api/assemble", handle_assemble_start)
    app.router.add_get("/api/assemble/{session_id}", handle_assemble_status)
```

> Note: `require_wallet` must resolve a wallet in dev mode too. Task 9 makes `require_auth`/`require_wallet` honor dev mode; until then this test sets `request["wallet"]` directly, so it passes now.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest webapp/test_smoke.py -k economy -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add webapp/server.py webapp/test_smoke.py
git commit -m "feat(webapp): economy endpoints (economy/equip/harvest/assemble + status)"
```

---

### Task 9: Dev-mode auth bypass + `dev_mode` in `/api/config`

**Files:**
- Modify: `webapp/server.py`
- Test: `webapp/test_smoke.py`

**Interfaces:**
- Consumes: `config.WEBAPP_DEV_MODE`, `mock_economy.DEV_OWNER`.
- Produces:
  - `require_auth`/`require_wallet`: in dev mode inject a synthetic `request["user"] = {"id":"dev","name":"dev"}` and `request["wallet"] = mock_economy.DEV_OWNER` without a token.
  - `handle_config` returns `{"client_id": ..., "dev_mode": config.WEBAPP_DEV_MODE}`.

- [ ] **Step 1: Write the failing test**

Add to `webapp/test_smoke.py`:

```python
def test_require_auth_dev_bypass(monkeypatch):
    from aiohttp.test_utils import make_mocked_request
    from webapp import mock_economy
    monkeypatch.setattr(server.config, "WEBAPP_DEV_MODE", True)

    @server.require_wallet
    async def probe(request):
        return server.web.json_response({"wallet": request["wallet"]})

    req = make_mocked_request("GET", "/x")  # no Authorization header
    resp = asyncio.get_event_loop().run_until_complete(probe(req))
    assert resp.status == 200


def test_config_reports_dev_mode(monkeypatch):
    from aiohttp.test_utils import make_mocked_request
    monkeypatch.setattr(server.config, "WEBAPP_DEV_MODE", True)
    req = make_mocked_request("GET", "/api/config")
    resp = asyncio.get_event_loop().run_until_complete(server.handle_config(req))
    import json
    assert json.loads(resp.body)["dev_mode"] is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest webapp/test_smoke.py -k "dev_bypass or reports_dev" -v`
Expected: FAIL (no bypass; `dev_mode` absent)

- [ ] **Step 3: Implement**

In `webapp/server.py`, update `require_auth` to short-circuit in dev mode (add at the top of its `wrapper`, before the header check):

```python
        if config.WEBAPP_DEV_MODE:
            request["user"] = {"id": "dev", "name": "dev"}
            return await handler(request)
```

Update `require_wallet`'s inner wrapper to short-circuit before the DB lookup:

```python
        if config.WEBAPP_DEV_MODE:
            request["wallet"] = mock_economy.DEV_OWNER
            return await handler(request)
```

Update `handle_config`:

```python
async def handle_config(request):
    """Public config the frontend needs before auth (client_id, dev flag)."""
    return web.json_response(
        {"client_id": config.DISCORD_CLIENT_ID, "dev_mode": config.WEBAPP_DEV_MODE}
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest webapp/test_smoke.py -k "dev_bypass or reports_dev" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add webapp/server.py webapp/test_smoke.py
git commit -m "feat(webapp): dev-mode auth bypass + dev_mode in /api/config"
```

---

### Task 10: Live-reload — `/__dev/reload` SSE + mtime watcher

**Files:**
- Modify: `webapp/server.py`
- Test: `webapp/test_smoke.py`

**Interfaces:**
- Consumes: `config.WEBAPP_DEV_MODE`, `CLIENT_DIR`.
- Produces:
  - `GET /__dev/reload` — when dev mode, a `text/event-stream` that emits `data: reload\n\n` whenever a file under `client/` changes; 404 when dev mode is off.
  - `server._client_dir_mtime() -> float` — max mtime across `client/` files (the change signal).
  - A background task started in `main()` (dev mode only) that polls mtimes; not under test.

- [ ] **Step 1: Write the failing test**

Add to `webapp/test_smoke.py`:

```python
def test_dev_reload_route_404_when_off(monkeypatch):
    from aiohttp.test_utils import make_mocked_request
    monkeypatch.setattr(server.config, "WEBAPP_DEV_MODE", False)
    req = make_mocked_request("GET", "/__dev/reload")
    resp = asyncio.get_event_loop().run_until_complete(server.handle_dev_reload(req))
    assert resp.status == 404


def test_client_dir_mtime_is_float():
    assert isinstance(server._client_dir_mtime(), float)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest webapp/test_smoke.py -k "dev_reload or client_dir_mtime" -v`
Expected: FAIL (`handle_dev_reload` / `_client_dir_mtime` undefined)

- [ ] **Step 3: Implement**

In `webapp/server.py`:

```python
def _client_dir_mtime() -> float:
    latest = 0.0
    for root, _dirs, files in os.walk(CLIENT_DIR):
        for f in files:
            try:
                latest = max(latest, os.path.getmtime(os.path.join(root, f)))
            except OSError:
                continue
    return latest


async def handle_dev_reload(request):
    if not config.WEBAPP_DEV_MODE:
        return web.json_response({"error": "not found"}, status=404)
    resp = web.StreamResponse(headers={"Content-Type": "text/event-stream",
                                       "Cache-Control": "no-store"})
    await resp.prepare(request)
    last = _client_dir_mtime()
    try:
        while True:
            await asyncio.sleep(0.5)
            now = _client_dir_mtime()
            if now > last:
                last = now
                await resp.write(b"data: reload\n\n")
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    return resp
```

Register in `create_app()`:

```python
    app.router.add_get("/__dev/reload", handle_dev_reload)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest webapp/test_smoke.py -k "dev_reload or client_dir_mtime" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add webapp/server.py webapp/test_smoke.py
git commit -m "feat(webapp): dev-mode live-reload SSE endpoint"
```

---

### Task 11: Dressing Room markup + styles (static shell)

**Files:**
- Modify: `webapp/client/index.html` (add a `dressup-panel` section; bump `style.css?v=` cache-buster)
- Modify: `webapp/client/style.css` (dressing-room layout)
- Test: manual (`WEBAPP_DEV_MODE` harness) — no unit test for static markup.

**Interfaces:**
- Produces DOM ids the JS in Task 12+ binds to: `dressup-panel`, `dressup-canvas` (stack container), `dressup-id` (caption), `dressup-harvest-btn`, `bucket-grid`, `bucket-filter`, `roster-strip`.

- [ ] **Step 1: Add the panel to `index.html`**

After the existing `swap-result-panel` section, before `flow-panel`:

```html
    <section id="dressup-panel" class="card" hidden>
      <div class="dressup">
        <div class="dressup-stage">
          <div id="dressup-canvas" class="dressup-canvas" role="img"
               aria-label="Character preview"></div>
          <p id="dressup-id" class="cap"></p>
          <button id="dressup-harvest-btn" class="secondary">🔥 Harvest</button>
        </div>
        <div class="dressup-bucket">
          <div class="bucket-head">
            <h3>Bucket</h3>
            <select id="bucket-filter" aria-label="Filter traits by slot"></select>
          </div>
          <div id="bucket-grid" class="bucket-grid"></div>
        </div>
      </div>
      <div id="roster-strip" class="roster-strip"></div>
    </section>
```

Bump the stylesheet cache-buster in `<head>` (e.g. `style.css?v=7` → `style.css?v=8`).

- [ ] **Step 2: Add styles to `style.css`**

Append:

```css
/* --- Dressing Room --- */
.dressup { display: flex; gap: 18px; flex-wrap: wrap; }
.dressup-stage { flex: 1 1 240px; text-align: center; }
.dressup-canvas {
  position: relative; width: 240px; height: 240px; margin: 0 auto;
  background: var(--surface-2); border: 3px solid var(--ink);
  border-radius: 16px; box-shadow: var(--sticker-sm); overflow: hidden;
}
.dressup-canvas img, .dressup-canvas video {
  position: absolute; inset: 0; width: 100%; height: 100%; object-fit: contain;
}
.dressup-bucket { flex: 1 1 240px; }
.bucket-head { display: flex; justify-content: space-between; align-items: center; }
.bucket-grid {
  display: grid; grid-template-columns: repeat(auto-fill, minmax(64px, 1fr));
  gap: 8px; margin-top: 10px;
}
.bucket-item {
  position: relative; aspect-ratio: 1; border: 2px solid var(--ink);
  border-radius: 10px; overflow: hidden; background: var(--surface-2);
  cursor: pointer; padding: 0;
}
.bucket-item[disabled] { opacity: .35; cursor: not-allowed; }
.bucket-item img { width: 100%; height: 100%; object-fit: contain; }
.bucket-item .count {
  position: absolute; bottom: 2px; right: 4px; font-size: .7rem; font-weight: 700;
}
.bucket-item.busy { opacity: .5; }
.roster-strip {
  display: flex; gap: 8px; overflow-x: auto; margin-top: 16px; padding-bottom: 4px;
}
.roster-tile {
  flex: 0 0 auto; width: 64px; height: 64px; border: 2px solid var(--ink);
  border-radius: 10px; overflow: hidden; background: var(--surface-2); cursor: pointer;
}
.roster-tile.active { box-shadow: 0 0 0 4px var(--blue); }
.roster-tile img { width: 100%; height: 100%; object-fit: contain; }
.roster-tile.assemble { display: grid; place-items: center; font-size: 1.6rem; }
```

- [ ] **Step 3: Verify it renders (manual)**

Run: `WEBAPP_DEV_MODE=1 LAYER_SOURCE=local .venv/bin/python -m webapp.server`
Open `http://localhost:8176/` and temporarily un-hide the panel in devtools (`document.getElementById('dressup-panel').hidden=false`). Expected: empty canvas + bucket + roster containers laid out side by side. (Wiring comes next.)

- [ ] **Step 4: Commit**

```bash
git add webapp/client/index.html webapp/client/style.css
git commit -m "feat(webapp): Dressing Room markup + styles"
```

---

### Task 12: Client — load economy + render canvas (layer stack) and roster

**Files:**
- Modify: `webapp/client/app.js`
- Test: manual (dev harness)

**Interfaces:**
- Consumes: `api()`, `imgUrl()`, `showPanel()`, `showError()`, the `/api/economy` payload, `/api/layer`.
- Produces JS state + functions: `economyState`, `activeNftId`, `openDressup()`, `renderCanvas(char)`, `renderRoster()`, `layerSrc(body,trait,value)`. Adds `'dressup-panel'` to `ALL_PANELS`.

- [ ] **Step 1: Implement the loader + renderers**

Add to `app.js`:

```javascript
// --- Dressing Room ---
let economyState = null;
let activeNftId = null;

function layerSrc(body, trait, value) {
  return `/api/layer?body=${encodeURIComponent(body)}` +
         `&trait=${encodeURIComponent(trait)}&value=${encodeURIComponent(value)}`;
}

function renderCanvas(char) {
  const canvas = el('dressup-canvas');
  canvas.replaceChildren();
  const order = economyState.trait_order;
  const byType = Object.fromEntries(char.attributes.map((a) => [a.trait_type, a.value]));
  for (const slot of order) {
    const value = byType[slot];
    if (!value || value === 'None') continue;
    const img = document.createElement('img');
    img.src = layerSrc(char.body, slot, value);
    img.alt = '';
    canvas.appendChild(img);
  }
  el('dressup-id').textContent = `#${char.edition} · ${char.body} · live`;
}

function renderRoster() {
  const strip = el('roster-strip');
  strip.replaceChildren();
  for (const char of economyState.characters) {
    const tile = document.createElement('button');
    tile.className = 'roster-tile' + (char.nft_id === activeNftId ? ' active' : '');
    const img = document.createElement('img');
    img.src = imgUrl(char.image_url) || layerSrc(char.body, 'Body',
      (char.attributes.find((a) => a.trait_type === 'Body') || {}).value || 'None');
    img.alt = `#${char.edition}`;
    tile.appendChild(img);
    tile.onclick = () => selectCharacter(char.nft_id);
    strip.appendChild(tile);
  }
  const add = document.createElement('button');
  add.className = 'roster-tile assemble';
  add.textContent = '＋';
  add.title = 'Assemble new';
  add.onclick = () => openAssemble();
  strip.appendChild(add);
}

function selectCharacter(nftId) {
  activeNftId = nftId;
  const char = economyState.characters.find((c) => c.nft_id === nftId);
  if (char) renderCanvas(char);
  renderRoster();
  renderBucket();
}

async function openDressup() {
  showPanel('dressup-panel');
  status('Loading your wardrobe…');
  try {
    economyState = await api('/api/economy');
    status('');
    activeNftId = economyState.characters[0] ? economyState.characters[0].nft_id : null;
    if (activeNftId) selectCharacter(activeNftId);
    else { renderRoster(); el('dressup-canvas').replaceChildren(); }
  } catch (e) {
    showError(e.message);
  }
}
```

Add `'dressup-panel'` to the `ALL_PANELS` array. (`renderBucket`/`openAssemble` arrive in Tasks 13/15 — they are referenced but the file won't be exercised until then; if running before Task 13, stub `function renderBucket(){}` and `function openAssemble(){}` and remove the stubs as those tasks land.)

- [ ] **Step 2: Verify (manual)**

Run the dev server (Task 11 command). In devtools console: `openDressup()`. Expected: canvas shows the stacked male character; roster shows two tiles + a `＋`.

- [ ] **Step 3: Commit**

```bash
git add webapp/client/app.js
git commit -m "feat(webapp): Dressing Room canvas stack + roster rendering"
```

---

### Task 13: Client — Bucket palette + immediate per-swap equip (with in-flight lock)

**Files:**
- Modify: `webapp/client/app.js`
- Test: manual (dev harness)

**Interfaces:**
- Consumes: `economyState`, `activeNftId`, `api()`, `layerSrc()`, `renderCanvas()`, `pollEconomyOp()` (Task 14). Replaces the Task 12 `renderBucket` stub.
- Produces: `renderBucket()`, `equipTrait(slot, value, tileEl)`, `bucketFilter` state.

- [ ] **Step 1: Implement palette + equip**

```javascript
let bucketFilter = 'All';
let equipBusy = false;

function activeChar() {
  return economyState.characters.find((c) => c.nft_id === activeNftId) || null;
}

function renderBucketFilter() {
  const sel = el('bucket-filter');
  const slots = ['All', ...economyState.slots];
  sel.replaceChildren();
  for (const s of slots) {
    const o = document.createElement('option');
    o.value = s; o.textContent = s; sel.appendChild(o);
  }
  sel.value = bucketFilter;
  sel.onchange = () => { bucketFilter = sel.value; renderBucket(); };
}

function renderBucket() {
  renderBucketFilter();
  const grid = el('bucket-grid');
  grid.replaceChildren();
  const char = activeChar();
  for (const asset of economyState.bucket.assets) {
    if (bucketFilter !== 'All' && asset.slot !== bucketFilter) continue;
    const item = document.createElement('button');
    item.className = 'bucket-item';
    // Compatibility: only enable when this asset can go on the active character.
    // Client mirrors the server precheck (server re-verifies on commit).
    const compatible = char && economyState.slots.includes(asset.slot);
    if (!compatible) item.disabled = true;
    const img = document.createElement('img');
    img.src = char ? layerSrc(char.body, asset.slot, asset.value) : '';
    img.alt = `${asset.slot}: ${asset.value}`;
    const count = document.createElement('span');
    count.className = 'count';
    count.textContent = `×${asset.count}`;
    item.replaceChildren(img, count);
    item.onclick = () => equipTrait(asset.slot, asset.value, item);
    grid.appendChild(item);
  }
}

async function equipTrait(slot, value, tileEl) {
  if (equipBusy || !activeChar()) return;       // in-flight lock
  equipBusy = true;
  tileEl.classList.add('busy');
  // Optimistic client stack: update the active character's attribute now.
  const char = activeChar();
  const attr = char.attributes.find((a) => a.trait_type === slot);
  const previous = attr ? attr.value : 'None';
  if (attr) attr.value = value;
  renderCanvas(char);
  try {
    const res = await api('/api/equip', {
      method: 'POST',
      body: JSON.stringify({ nft_id: activeNftId, slot, value }),
    });
    const final = await pollEconomyOp('equip', res);
    if (final.state === 'failed') throw new Error(final.error || 'equip failed');
    // Reconcile the Bucket from authoritative state.
    economyState = await api('/api/economy');
    selectCharacter(activeNftId);
  } catch (e) {
    if (attr) attr.value = previous;             // revert optimistic stack
    renderCanvas(char);
    showError(e.message);
  } finally {
    equipBusy = false;
    tileEl.classList.remove('busy');
  }
}
```

- [ ] **Step 2: Verify (manual)**

Dev server running; `openDressup()`; click a Bucket trait. Expected: canvas updates instantly; the bucket re-renders with the displaced trait now present (mock equip is synchronous → terminal immediately).

- [ ] **Step 3: Commit**

```bash
git add webapp/client/app.js
git commit -m "feat(webapp): Bucket palette + immediate per-swap equip"
```

---

### Task 14: Client — economy op poller

**Files:**
- Modify: `webapp/client/app.js`
- Test: manual (dev harness)

**Interfaces:**
- Consumes: `api()`. Produces `pollEconomyOp(kind, startResp) -> Promise<finalSessionDict>`.
- Contract: if `startResp.state` is already terminal (`done`/`failed`) — the dev/mock path — resolve immediately. Otherwise poll `GET /api/<kind>/<id>` every 3s until terminal.

- [ ] **Step 1: Implement**

```javascript
function isTerminal(s) { return s === 'done' || s === 'failed'; }

function pollEconomyOp(kind, startResp) {
  if (isTerminal(startResp.state)) return Promise.resolve(startResp);
  const id = startResp.id;
  return new Promise((resolve) => {
    const tick = async () => {
      let s;
      try {
        s = await api(`/api/${kind}/${id}`);
      } catch (e) {
        setTimeout(tick, 3000); // transient; keep polling
        return;
      }
      if (isTerminal(s.state)) resolve(s);
      else setTimeout(tick, 3000);
    };
    setTimeout(tick, 3000);
  });
}
```

- [ ] **Step 2: Verify (manual)**

With the dev harness, equip still works (poller resolves immediately on the terminal mock response).

- [ ] **Step 3: Commit**

```bash
git add webapp/client/app.js
git commit -m "feat(webapp): economy op poller"
```

---

### Task 15: Client — harvest (guarded) and assemble flows

**Files:**
- Modify: `webapp/client/app.js`
- Test: manual (dev harness)

**Interfaces:**
- Consumes: `api()`, `pollEconomyOp()`, `economyState`, `activeNftId`, `showFlow()` (existing flow-panel renderer for QR + result), `qrUrl()`, `imgUrl()`. Replaces the Task 12 `openAssemble` stub.
- Produces: `harvestActive()`, `openAssemble()`, `commitAssemble(edition, chosen)`.

- [ ] **Step 1: Wire the Harvest button**

In `openDressup()` (or once at init) bind the harvest button:

```javascript
el('dressup-harvest-btn').onclick = () => harvestActive();

async function harvestActive() {
  const char = activeChar();
  if (!char) return;
  if (!window.confirm(
    `This permanently burns #${char.edition}. Its parts go to your Bucket. Continue?`)) {
    return;
  }
  status('Harvesting…');
  try {
    const res = await api('/api/harvest', {
      method: 'POST', body: JSON.stringify({ nft_id: char.nft_id }),
    });
    const final = await pollEconomyOp('harvest', res);
    status('');
    if (final.state === 'failed') throw new Error(final.error || 'harvest failed');
    if (final.accept) {
      // First-ever Bucket: user must accept the soulbound token in Xaman.
      showFlow({ title: '👜 Claim your Bucket',
        text: 'Scan to accept your trait Bucket in Xaman.',
        qrData: final.accept, link: final.accept, done: true });
    }
    economyState = await api('/api/economy');
    activeNftId = economyState.characters[0] ? economyState.characters[0].nft_id : null;
    showPanel('dressup-panel');
    if (activeNftId) selectCharacter(activeNftId);
    else { renderRoster(); renderBucket(); el('dressup-canvas').replaceChildren(); }
  } catch (e) {
    showError(e.message);
  }
}
```

- [ ] **Step 2: Implement assemble**

```javascript
function openAssemble() {
  const bodies = economyState.bucket.bodies;
  if (!bodies.length) { showError('No bodies in your Bucket to assemble.'); return; }
  // MVP: assemble the first available body edition, auto-filling each slot with the
  // first compatible Bucket asset; the user reviews the preview before committing.
  const edition = bodies[0];
  const chosen = {};
  for (const slot of economyState.slots) {
    const asset = economyState.bucket.assets.find((a) => a.slot === slot && a.count > 0);
    if (asset) chosen[slot] = asset.value;
  }
  const missing = economyState.slots.filter((s) => !(s in chosen));
  if (missing.length) {
    showError(`Bucket is missing assets for: ${missing.join(', ')}`);
    return;
  }
  if (!window.confirm(`Assemble a new character for edition #${edition}?`)) return;
  commitAssemble(edition, chosen);
}

async function commitAssemble(edition, chosen) {
  status('Assembling…');
  try {
    const res = await api('/api/assemble', {
      method: 'POST', body: JSON.stringify({ edition, chosen }),
    });
    const final = await pollEconomyOp('assemble', res);
    status('');
    if (final.state === 'failed') throw new Error(final.error || 'assemble failed');
    showFlow({ title: `🎉 #${edition} assembled!`,
      text: final.accept ? 'Scan to accept your new character in Xaman.'
                         : 'Your new character is on its way.',
      qrData: final.accept || null, link: final.accept || null,
      image: imgUrl(final.image_url), done: true, celebrate: true });
    economyState = await api('/api/economy');
  } catch (e) {
    showError(e.message);
  }
}
```

- [ ] **Step 3: Verify (manual)**

Dev harness: harvest the active character → it disappears from the roster, its parts appear in the Bucket. Click `＋` → assemble runs (mock) → a new character appears after `economyState` reload.

- [ ] **Step 4: Commit**

```bash
git add webapp/client/app.js
git commit -m "feat(webapp): harvest (guarded) + assemble client flows"
```

---

### Task 16: Client — entry point (replace Trait Swapper) + dev live-reload hookup

**Files:**
- Modify: `webapp/client/index.html` (relabel the swap button), `webapp/client/app.js`
- Test: manual + full test suite

**Interfaces:**
- Consumes: `handle_config`'s `dev_mode` flag, `openDressup()`.
- Produces: the mint panel's secondary button opens the Dressing Room; in dev mode an `EventSource('/__dev/reload')` reloads the tab on file change.

- [ ] **Step 1: Repoint the entry button**

In `index.html`, change the existing swap button (id `swap-btn`) label to `👗 Dress Up` (keep the id), or add a new button `<button id="dressup-btn" class="secondary">👗 Dress Up</button>` next to it. In `app.js`, bind it:

```javascript
el('swap-btn').textContent = '👗 Dress Up';
el('swap-btn').onclick = () => openDressup();
```

(The legacy swap endpoints/panels remain for now; this only repoints the entry. Removing the old swap UI is a follow-up.)

- [ ] **Step 2: Hook up live reload (dev only)**

Where the client first fetches `/api/config` (near startup), after reading the config:

```javascript
  // cfg = await api('/api/config') earlier in startup
  if (cfg.dev_mode && 'EventSource' in window) {
    const es = new EventSource('/__dev/reload');
    es.onmessage = () => location.reload();
  }
```

If startup doesn't already fetch `/api/config`, add a guarded fetch at the end of init:

```javascript
try {
  const cfg = await api('/api/config');
  if (cfg.dev_mode && 'EventSource' in window) {
    new EventSource('/__dev/reload').onmessage = () => location.reload();
  }
} catch (_) { /* non-dev: ignore */ }
```

- [ ] **Step 3: Run the full backend test suite**

Run: `.venv/bin/python -m pytest webapp/ tests/ -q`
Expected: PASS (all, including the new economy/layer/dev tests).

- [ ] **Step 4: Manual end-to-end (dev harness)**

Run: `WEBAPP_DEV_MODE=1 LAYER_SOURCE=local .venv/bin/python -m webapp.server`
Open `http://localhost:8176/`, click **Dress Up**, verify: canvas stack, equip a trait, harvest, assemble. Edit `style.css`, save → tab auto-reloads.

- [ ] **Step 5: Commit**

```bash
git add webapp/client/index.html webapp/client/app.js
git commit -m "feat(webapp): Dress Up entry point + dev live-reload hookup"
```

---

### Task 17: Lint, type-check, and final verification

**Files:** none (verification only)

- [ ] **Step 1: Run the pre-commit gates**

Run: `.venv/bin/ruff check . && .venv/bin/mypy lfg_core webapp scripts && .venv/bin/python -m pytest webapp/ tests/ -q`
Expected: ruff clean, mypy clean, all tests pass. Fix any findings inline (add type annotations where mypy flags the new modules; `economy_api.py`/`mock_economy.py` should be strict-clean).

- [ ] **Step 2: Commit any fixes**

```bash
git add -A
git commit -m "chore(webapp): lint/type fixes for Dressing Room"
```

---

## Self-Review

**Spec coverage:**
- §3 unified Dressing Room (canvas + Bucket + roster) → Tasks 11–13, 15.
- §3 body-compatibility gating (mirror `can_equip`) → Task 13 (client dim) + Task 5 (`start_equip` re-runs `can_equip`).
- §4 immediate per-swap + in-flight lock → Task 13 (`equipBusy`).
- §5 hybrid preview: instant client stack ✅ Task 12/13; authoritative at commit ✅ (server `makeNft` in Phase 2 flow). **Debounced server preview (`/api/economy/preview`) deferred** — see deviation note; the instant + authoritative legs are present, debounced fidelity is an additive follow-up and not required for a working screen.
- §6 flows + signing: equip/harvest/assemble → Tasks 5, 8, 13, 15; assemble + first-Bucket XUMM accept surfaced via `economy_session_dict` `accept` → Task 4/15.
- §7 endpoints → Tasks 6, 8 (manifest folded into `/api/economy` `trait_order`/`slots` — deviation note).
- §8 dev harness (`WEBAPP_DEV_MODE` mock + live reload + shared `MockEconomy`) → Tasks 7, 9, 10, 16.
- §9 error handling (toasts, journaled partial-failure messages) → Task 8 (`error` surfaced) + Task 13/15 (`showError`).
- §10 testing (endpoint pytest + `MockEconomy` fixture) → Tasks 1–10, 17.

**Deviations from spec (intentional, captured here):**
1. **`/api/layers/manifest` dropped.** The client stacks using the character's `attributes` ordered by `swap_meta.TRAIT_ORDER`, which is returned inline in `/api/economy` (`trait_order`, `slots`). No separate manifest endpoint is needed.
2. **Assemble payload is `{edition, chosen}`** (not `{body, assets}`), matching the Phase 2 `AssembleSession` (body is derived server-side from `genesis.edition_bodies[edition]`).
3. **`/api/economy/preview` (debounced server composite) is deferred** to a follow-up. Instant client stacking + authoritative commit image cover the core UX; the debounced fidelity leg can be added without touching the rest.

**Placeholder scan:** No TBD/TODO. The one stubbed-forward reference (`renderBucket`/`openAssemble` referenced in Task 12 before Tasks 13/15) is called out with an explicit interim stub instruction.

**Type consistency:** `economy_session_dict(kind, s)` keys (`displaced`, `accept`, `moved_assets`, `image_url`, `nft_id`) match what the client poller/flows read (Tasks 13/15). `EconomyWebSession` exposes `.discord_id/.state/.created_at/.to_dict()/.id` exactly as `make_status_handler`/`_prune_sessions`/`_active_session` require. `read_economy_state` shape matches `MockEconomy.read_state` and the client's `economyState` usage (`characters[].{nft_id,edition,body,attributes,image_url}`, `bucket.{assets,bodies}`, `trait_order`, `slots`).
