# Build Panel Batched Save Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Clicking a Closet tile in the Build panel stages a change locally; a Save button commits every staged change in one `NFTokenModify` plus one Closet sync.

**Architecture:** Generalize the existing `EquipSession` / `run_equip` to carry a *list* of `(slot, incoming_value)` changes, keeping the same endpoint, session kind, journal statuses, and fail-safe ordering. The client keeps a `pendingEquips` map, renders the canvas and Closet counts from it via pure functions in `build_pure.js`, and POSTs the net batch on Save.

**Tech Stack:** Python 3 / aiohttp (`lfg_service`, `webapp`), sqlite3, xrpl-py, pytest; vanilla ES-module JS (no build step) for the Activity client, with pure logic unit-tested from Python via Node.

**Spec:** `docs/superpowers/specs/2026-07-21-build-batched-save-design.md`

## Global Constraints

- **One Python shape.** `EquipSession(changes=[(slot, value), ...])` and `economy_api.start_equip(..., changes)` are the only in-repo signatures. Legacy `{nft_id, slot, value}` compatibility exists **at the HTTP wire level only**, normalized in `lfg_service/app.py`. Do not add dual-signature shims.
- **Never reorder `run_equip`.** precheck → compose → **one** `NFTokenModify` → **one** `_sync_then_persist`. All existing failure branches (`ClosetMirrorError` → `complete_pending_mirror`, `ClosetIndeterminateError` → `equip_sync_indeterminate`, ledger-failed → single modify-back, falsy revert hash → `failed_revert`) keep their exact semantics and journal status strings.
- **Journal statuses are an operator contract.** The status strings documented in the `lfg_core/economy_flow.py` module docstring must not change. Only the record's payload changes: the three scalars `slot` / `incoming` / `displaced` become `"changes": [{"slot", "incoming", "displaced"}, ...]`.
- **No native `window.confirm` in client code** — it is a silent no-op inside Discord's sandboxed iframe. Use the existing `confirmDialog({title, text, confirmLabel})` in `app.js`.
- **Cache busters.** Any commit touching `webapp/client/app.js` must bump `app.js?v=` in `webapp/client/index.html`; any commit touching `webapp/client/build_pure.js` must ALSO bump the `./build_pure.js?v=` import inside `app.js`. ES-module import URLs are cache keys.
- **Pre-push gate is blocking**: ruff (`--fix`), ruff-format, mypy, gitleaks, pytest, validate-trait-config. Never bypass with `--no-verify`.
- Run everything through the project venv: `.venv/bin/python`, `.venv/bin/pytest`.

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `lfg_core/economy_flow.py` | `EquipSession` dataclass + `run_equip` batch loop | 1 |
| `tests/test_economy_flow_equip.py` | flow-level tests, migrated to `changes=` + new batch cases | 1 |
| `scripts/economy_equip.py` | CLI, repeatable `--set SLOT=VALUE` | 1 |
| `webapp/economy_api.py` | `start_equip(changes)` precheck loop, `economy_session_dict` displaced list | 2 |
| `lfg_service/app.py` | wire normalization + validation for `POST /api/equip` | 2 |
| `webapp/mock_economy.py` | dev-mode mock applies a change list | 2 |
| `webapp/test_economy_api.py`, `webapp/test_smoke.py`, `webapp/test_mock_economy.py` | service-seam tests | 2 |
| `webapp/client/build_pure.js` | `applyPending` / `effectiveAssets` / `netChanges` | 3 |
| `tests/test_build_pure_js.py` | Node-executed tests for the above | 3 |
| `webapp/client/app.js` | pending state, staging click, Save bar, dirty guards | 4 |
| `webapp/client/index.html`, `webapp/client/style.css` | Save bar markup + styles, cache busters | 4 |

---

### Task 1: Batch the equip flow (`EquipSession`, `run_equip`, CLI)

**Files:**
- Modify: `lfg_core/economy_flow.py:622-764`
- Modify: `scripts/economy_equip.py`
- Test: `tests/test_economy_flow_equip.py`

**Interfaces:**
- Consumes: `trait_economy.can_equip(rec, slot, value, owner_assets, mutable)`, `trait_economy.slot_value(rec, slot)`, `_owner_contents(conn, owner)`, `_sync_then_persist(deps, owner, assets, bodies)` — all unchanged.
- Produces:
  - `economy_flow.EquipSession(owner: str, character: OnchainNft, changes: list[tuple[str, str]])` with fields `state`, `error`, `displaced: dict[str, str]` (slot → displaced value), `modify_hash`, `sync_tx_hash`, `mirror_pending`, `id`.
  - `economy_flow.run_equip(session, deps) -> None` — unchanged signature.

- [ ] **Step 1: Migrate the existing tests to the `changes=` shape**

In `tests/test_economy_flow_equip.py`, replace every `EquipSession(...)` construction and the one `displaced_value` assertion. There are six constructions; all use `slot="Head", incoming_value="Crown"` except `test_equip_rejects_missing_asset` which uses `"Tiara"`.

Change each `slot="Head", incoming_value="Crown"` to `changes=[("Head", "Crown")]`, and `slot="Head", incoming_value="Tiara"` to `changes=[("Head", "Tiara")]`. For example, `test_equip_happy_path` becomes:

```python
def test_equip_happy_path(tmp_path):
    conn, f = _conn_with_bucket(), _Fakes()
    s = ef.EquipSession(owner="rUser", character=_char(), changes=[("Head", "Crown")])
    _run(ef.run_equip(s, _deps(conn, f, tmp_path)))

    assert s.state == ef.DONE
    assert s.displaced == {"Head": "None"}
    assert f.char_modifies == [("NFT7", "rUser", "https://cdn/new.json")]
    assets = {(slot, v): n for o, slot, v, n in es.read_closet_assets(conn)}
    assert ("Head", "Crown") not in assets  # incoming consumed
    assert assets[("Head", "None")] == 1  # displaced returned
```

Leave every other assertion in the file exactly as it is — those failure-path assertions are the regression net for the fail-safe ordering.

- [ ] **Step 2: Add the new batch tests**

Append to `tests/test_economy_flow_equip.py`. Note `_conn_with_bucket()` seeds only `("Head", "Crown"): 1`, so these helpers seed their own richer Closets.

```python
def _conn_with_assets(pairs) -> sqlite3.Connection:
    """A Closet seeded with explicit (slot, value, count) rows."""
    c = sqlite3.connect(":memory:")
    es.init_economy_schema(c)
    es.freeze_genesis(
        c, te.Genesis(trait_counts={}, edition_bodies={7: ("Straight Blue", "male")}), {}
    )
    es.set_closet_token(c, "rUser", "CLOSET", "00")
    es.set_closet_contents(c, "rUser", list(pairs), [])
    return c


def test_equip_batch_is_one_modify_and_one_sync(tmp_path):
    """Two slots in one batch: exactly one compose, one character modify, one
    Closet sync carrying BOTH deltas."""
    conn = _conn_with_assets([("Head", "Crown", 1), ("Eyes", "Laser", 1)])
    f = _Fakes()
    composed = []
    orig_compose = f.char_compose

    async def spy_compose(attrs, body, edition, rev):
        composed.append(attrs)
        return await orig_compose(attrs, body, edition, rev)

    f.char_compose = spy_compose
    s = ef.EquipSession(
        owner="rUser", character=_char(), changes=[("Head", "Crown"), ("Eyes", "Laser")]
    )
    _run(ef.run_equip(s, _deps(conn, f, tmp_path)))

    assert s.state == ef.DONE
    assert len(composed) == 1  # one compose for the whole batch
    assert f.char_modifies == [("NFT7", "rUser", "https://cdn/new.json")]  # one modify
    by_type = {a["trait_type"]: a["value"] for a in composed[0]}
    assert by_type["Head"] == "Crown" and by_type["Eyes"] == "Laser"
    assets = {(slot, v): n for o, slot, v, n in es.read_closet_assets(conn)}
    assert ("Head", "Crown") not in assets and ("Eyes", "Laser") not in assets
    assert assets[("Head", "None")] == 1 and assets[("Eyes", "None")] == 1
    assert s.displaced == {"Head": "None", "Eyes": "None"}


def test_equip_batch_aborts_whole_batch_on_a_bad_change(tmp_path):
    """The second change is not in the Closet: the batch is all-or-nothing, so
    the character is never modified and the Closet is untouched."""
    conn = _conn_with_assets([("Head", "Crown", 1)])
    f = _Fakes()
    s = ef.EquipSession(
        owner="rUser", character=_char(), changes=[("Head", "Crown"), ("Eyes", "Laser")]
    )
    _run(ef.run_equip(s, _deps(conn, f, tmp_path)))

    assert s.state == ef.FAILED
    assert f.char_modifies == []  # never touched the character
    assets = {(slot, v): n for o, slot, v, n in es.read_closet_assets(conn)}
    assert assets == {("Head", "Crown"): 1}  # Closet untouched


def test_equip_batch_displaces_a_worn_trait_back_per_slot(tmp_path):
    """Closet assets are keyed (slot, value): a Crown displaced off Head returns
    as ('Head', 'Crown'), independent of any Eyes change in the same batch."""
    rec = _char()
    next(a for a in rec.attributes if a["trait_type"] == "Head")["value"] = "Crown"
    conn = _conn_with_assets([("Head", "Tiara", 1), ("Eyes", "Laser", 1)])
    f = _Fakes()
    s = ef.EquipSession(
        owner="rUser", character=rec, changes=[("Head", "Tiara"), ("Eyes", "Laser")]
    )
    _run(ef.run_equip(s, _deps(conn, f, tmp_path)))

    assert s.state == ef.DONE
    assert s.displaced == {"Head": "Crown", "Eyes": "None"}
    assets = {(slot, v): n for o, slot, v, n in es.read_closet_assets(conn)}
    assert assets[("Head", "Crown")] == 1  # displaced back into its own slot key
    assert assets[("Eyes", "None")] == 1
    assert ("Head", "Tiara") not in assets and ("Eyes", "Laser") not in assets


def test_equip_rejects_duplicate_slot_in_one_batch(tmp_path):
    conn = _conn_with_assets([("Head", "Crown", 1), ("Head", "Tiara", 1)])
    f = _Fakes()
    s = ef.EquipSession(
        owner="rUser", character=_char(), changes=[("Head", "Crown"), ("Head", "Tiara")]
    )
    _run(ef.run_equip(s, _deps(conn, f, tmp_path)))

    assert s.state == ef.FAILED
    assert "duplicate slot" in (s.error or "")
    assert f.char_modifies == []


def test_equip_rejects_empty_batch(tmp_path):
    conn, f = _conn_with_bucket(), _Fakes()
    s = ef.EquipSession(owner="rUser", character=_char(), changes=[])
    _run(ef.run_equip(s, _deps(conn, f, tmp_path)))

    assert s.state == ef.FAILED
    assert f.char_modifies == []


def test_equip_batch_journal_records_every_change(tmp_path):
    conn = _conn_with_assets([("Head", "Crown", 1), ("Eyes", "Laser", 1)])
    f = _Fakes()
    s = ef.EquipSession(
        owner="rUser", character=_char(), changes=[("Head", "Crown"), ("Eyes", "Laser")]
    )
    _run(ef.run_equip(s, _deps(conn, f, tmp_path)))

    record = json.loads((tmp_path / f"equip-{s.id}.json").read_text())
    assert record["status"] == "complete"
    assert record["op"] == "equip"
    assert record["changes"] == [
        {"slot": "Head", "incoming": "Crown", "displaced": "None"},
        {"slot": "Eyes", "incoming": "Laser", "displaced": "None"},
    ]
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_economy_flow_equip.py -x -q`
Expected: FAIL — `TypeError: EquipSession.__init__() got an unexpected keyword argument 'changes'`.

- [ ] **Step 4: Rewrite the `EquipSession` dataclass**

In `lfg_core/economy_flow.py`, replace the dataclass at line 622 with:

```python
@dataclass
class EquipSession:
    owner: str
    character: OnchainNft
    changes: list[tuple[str, str]]  # ordered (slot, incoming_value) pairs
    state: str = RUNNING
    error: str | None = None
    displaced: dict[str, str] = field(default_factory=dict)  # slot -> value pushed out
    modify_hash: str | None = None
    sync_tx_hash: str | None = None
    mirror_pending: bool = False
    id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def _record(self, status: str) -> dict[str, Any]:
        return {
            "op": "equip",
            "id": self.id,
            "owner": self.owner,
            "nft_id": self.character.nft_id,
            "changes": [
                {"slot": slot, "incoming": value, "displaced": self.displaced.get(slot, "")}
                for slot, value in self.changes
            ],
            "modify_hash": self.modify_hash,
            "sync_tx_hash": self.sync_tx_hash,
            "mirror_pending": self.mirror_pending,
            "status": status,
            "error": self.error,
        }

    def fail(self, msg: str) -> None:
        self.state = FAILED
        self.error = msg
```

- [ ] **Step 5: Rewrite the `run_equip` precheck and attribute build**

In the same file, replace the docstring and the body from `conn, owner, rec = ...` down to (and including) the two `assets[...] = assets.get(...)` delta lines that precede the `try:` around `_sync_then_persist`. The deltas move **into** the precheck loop; everything from `try: session.sync_tx_hash = ...` onward is untouched.

```python
@_serialize_by_owner
async def run_equip(session: EquipSession, deps: EconomyDeps) -> None:
    """Drive a batch equip to a terminal state. Order: precheck every change ->
    compose+upload ONCE -> ONE MODIFY of the character in place (reversible:
    modify back to the old URI) -> ONE Closet swap carrying every
    (-incoming, +displaced) delta. If the closet swap fails after the modify,
    the character is reverted (a whole-URI revert restores all slots at once)
    and the Closet untouched."""
    conn, owner, rec = deps.conn, session.owner, session.character
    try:
        stale = _mirror_pending_error(deps, owner)
        if stale:
            session.fail(stale)
            return
        if not session.changes:
            session.fail("cannot equip: no changes to apply")
            return
        assets, _bodies = _owner_contents(conn, owner)
        # Precheck every change, accumulating each (-incoming, +displaced) delta
        # into ONE asset dict for the single Closet sync below. Assets are keyed
        # (slot, value) and a slot may appear at most once, so the changes are
        # independent; any failing precheck aborts the batch before the ledger
        # is touched.
        seen: set[str] = set()
        for slot, incoming in session.changes:
            if slot in seen:
                session.fail(f"cannot equip: duplicate slot in one batch ({slot})")
                return
            seen.add(slot)
            chk = te.can_equip(rec, slot, incoming, assets, mutable=bool(rec.mutable))
            if not chk.ok:
                session.fail(f"cannot equip: {chk.reason}")
                return
            displaced = te.slot_value(rec, slot)
            session.displaced[slot] = displaced
            assets[(slot, incoming)] = assets.get((slot, incoming), 0) - 1
            assets[(slot, displaced)] = assets.get((slot, displaced), 0) + 1

        incoming_by_slot = dict(session.changes)
        new_attrs = [
            {
                "trait_type": a["trait_type"],
                "value": incoming_by_slot.get(a["trait_type"], a["value"]),
            }
            for a in rec.attributes
        ]
        _image_url, _video_url, meta_url = await deps.char_compose_fn(
            new_attrs, rec.body, rec.nft_number or 0, 0
        )
        _write_record(deps.records_dir, "equip", session.id, session._record("equipping"))

        # Reversible: NFTokenModify keeps the nft_id; we can modify back.
        modify_hash = await deps.char_modify_fn(rec.nft_id, owner, meta_url)
        if not modify_hash:
            session.fail(f"failed to update character {rec.nft_id}; your character is unchanged")
            _write_record(deps.records_dir, "equip", session.id, session._record("failed_modify"))
            return
        session.modify_hash = modify_hash

        # Swap the closet with every delta at once. Token first, then DB.
        try:
            session.sync_tx_hash = await _sync_then_persist(deps, owner, assets, _bodies)
```

Everything from the `except bt.ClosetMirrorError as e:` line onward stays byte-identical.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_economy_flow_equip.py -q`
Expected: PASS, 13 passed (7 migrated + 6 new).

- [ ] **Step 7: Update the CLI to accept repeatable `--set`**

Replace `scripts/economy_equip.py`'s `_amain` and `main` with:

```python
async def _amain(args: argparse.Namespace) -> int:
    changes: list[tuple[str, str]] = []
    for pair in args.set:
        slot, sep, value = pair.partition("=")
        if not sep or not slot or not value:
            print(f"--set expects SLOT=VALUE, got {pair!r}")
            return 2
        changes.append((slot, value))
    if args.slot and args.value:
        changes.append((args.slot, args.value))
    if not changes:
        print("nothing to do: pass --set SLOT=VALUE (repeatable) or --slot/--value")
        return 2

    conn = deps.open_index(args.network)
    rec = deps.load_index_character(conn, args.nft_id)
    if rec is None:
        print(f"NFT {args.nft_id} not found in the {args.network} index.")
        return 2
    session = economy_flow.EquipSession(owner=args.owner, character=rec, changes=changes)
    await economy_flow.run_equip(session, deps.build_economy_deps(conn))
    print(f"State: {session.state}")
    if session.error:
        print(f"Error: {session.error}")
    if session.state == economy_flow.DONE:
        for slot, value in changes:
            print(f"Equipped {slot}={value}; {session.displaced[slot]} returned to the Closet.")
    return 0 if session.state == economy_flow.DONE else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Equip Closet assets onto a character.")
    parser.add_argument("--network", choices=["mainnet", "testnet"], default=config.ECONOMY_NETWORK)
    parser.add_argument("--owner", required=True, help="owner's XRPL address")
    parser.add_argument("--nft-id", required=True, help="character NFTokenID to modify")
    parser.add_argument(
        "--set",
        action="append",
        default=[],
        metavar="SLOT=VALUE",
        help="repeatable; all changes commit in ONE NFTokenModify",
    )
    parser.add_argument("--slot", help="single-change form; non-body slot to change")
    parser.add_argument("--value", help="single-change form; incoming asset value")
    return asyncio.run(_amain(parser.parse_args()))
```

Also update the module docstring's usage example to show the batch form:

```python
"""Equip loose Closet assets onto a live character; each displaced asset returns
to the Closet. All changes commit in ONE in-place NFTokenModify.

  python scripts/economy_equip.py --network testnet --owner rUSER \\
      --nft-id 00... --set Head=Crown --set Eyes=Laser

Operations are free. All txns carry SourceTag.
"""
```

- [ ] **Step 8: Verify the CLI still imports and parses**

Run: `.venv/bin/python scripts/economy_equip.py --help`
Expected: usage text listing `--set`, `--slot`, `--value`; exit 0.

Run: `.venv/bin/pytest tests/test_economy_scripts_import.py -q`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add lfg_core/economy_flow.py scripts/economy_equip.py tests/test_economy_flow_equip.py
git commit -m "feat(economy): batch equip — N slot changes in one NFTokenModify"
```

---

### Task 2: Service seam — `start_equip`, wire normalization, mock

**Files:**
- Modify: `webapp/economy_api.py` (`economy_session_dict` ~line 103, `start_equip` ~line 277)
- Modify: `lfg_service/app.py:5143-5149` (`handle_equip_start`)
- Modify: `webapp/mock_economy.py:135-150` (`equip`)
- Test: `webapp/test_economy_api.py`, `webapp/test_smoke.py`, `webapp/test_mock_economy.py`

**Interfaces:**
- Consumes: `economy_flow.EquipSession(owner, character, changes)` and `run_equip` from Task 1.
- Produces:
  - `economy_api.normalize_equip_changes(body: dict) -> list[tuple[str, str]]` — raises `EconomyError` on an empty / duplicated / oversized / malformed change list. Accepts both `{"changes": [{"slot", "value"}, ...]}` and legacy `{"slot", "value"}`.
  - `economy_api.start_equip(discord_id, owner, nft_id, changes: list[tuple[str, str]], user_token=None) -> EconomyWebSession`
  - Status dict key `displaced` is now `[{"slot": str, "value": str}, ...]`.
  - `mock_economy.MockEconomy.equip(owner, nft_id, changes: list[tuple[str, str]]) -> dict`

- [ ] **Step 1: Write the failing tests**

In `webapp/test_economy_api.py`, replace `test_equip_session_dict`, `test_web_session_delegates`, `test_start_equip_precheck_rejects_unowned`, and `test_start_equip_happy_returns_session` with the `changes=` shape, and add normalization + multi-change cases:

```python
def test_equip_session_dict():
    s = economy_flow.EquipSession(
        owner="rOwner", character=_char(), changes=[("Head", "Halo")]
    )
    s.state = economy_flow.DONE
    s.displaced = {"Head": "Crown"}
    d = economy_api.economy_session_dict("equip", s)
    assert d["state"] == "done" and d["error"] is None
    assert d["displaced"] == [{"slot": "Head", "value": "Crown"}]


def test_web_session_delegates():
    s = economy_flow.EquipSession(
        owner="rOwner", character=_char(), changes=[("Head", "Halo")]
    )
    ws = economy_api.EconomyWebSession(discord_id="123", kind="equip", inner=s)
    assert ws.state == economy_flow.RUNNING
    assert ws.id == s.id
    assert ws.to_dict()["state"] == economy_flow.RUNNING
    assert isinstance(ws.created_at, float)


def test_normalize_equip_changes_accepts_legacy_shape():
    body = {"nft_id": "A", "slot": "Head", "value": "Halo"}
    assert economy_api.normalize_equip_changes(body) == [("Head", "Halo")]


def test_normalize_equip_changes_accepts_list():
    body = {"nft_id": "A", "changes": [
        {"slot": "Head", "value": "Halo"}, {"slot": "Eyes", "value": "Laser"}]}
    assert economy_api.normalize_equip_changes(body) == [("Head", "Halo"), ("Eyes", "Laser")]


@pytest.mark.parametrize("body", [
    {"nft_id": "A"},                                          # neither shape
    {"nft_id": "A", "changes": []},                           # empty
    {"nft_id": "A", "changes": [{"slot": "Head", "value": "X"},
                                {"slot": "Head", "value": "Y"}]},   # duplicate slot
    {"nft_id": "A", "changes": [{"slot": "Head"}]},           # missing value
    {"nft_id": "A", "changes": "Head=Halo"},                  # not a list
])
def test_normalize_equip_changes_rejects_bad_input(body):
    with pytest.raises(economy_api.EconomyError):
        economy_api.normalize_equip_changes(body)


def test_normalize_equip_changes_rejects_oversized_batch():
    from lfg_core import trait_economy as te
    changes = [{"slot": f"S{i}", "value": "X"} for i in range(len(te.NON_BODY_SLOTS) + 1)]
    with pytest.raises(economy_api.EconomyError):
        economy_api.normalize_equip_changes({"nft_id": "A", "changes": changes})


def test_start_equip_precheck_rejects_unowned(monkeypatch):
    conn = _seed_conn()  # owner rOwner holds edition 3537 (nft_id "A"), Bucket has Head=Halo
    monkeypatch.setattr(economy_api, "open_conn", lambda: conn)

    async def go():
        with pytest.raises(economy_api.EconomyError):
            # nft_id "A" is owned by rOwner, not rNobody -> precheck fails
            await economy_api.start_equip("123", "rNobody", "A", [("Head", "Halo")])

    asyncio.get_event_loop().run_until_complete(go())


def test_start_equip_happy_returns_session(monkeypatch):
    conn = _seed_conn()
    monkeypatch.setattr(economy_api, "open_conn", lambda: conn)
    _stub_permissive_layer_store(monkeypatch)

    captured = {}

    async def fake_run_equip(session, deps):
        captured["ran"] = True
        captured["changes"] = list(session.changes)
        session.state = economy_flow.DONE

    monkeypatch.setattr(economy_flow, "run_equip", fake_run_equip)
    # Stub the real deps builder so no XRPL/CDN is touched.
    from scripts import _economy_deps

    monkeypatch.setattr(_economy_deps, "build_economy_deps", lambda c, user_token=None: object())

    async def go():
        ws = await economy_api.start_equip("123", "rOwner", "A", [("Head", "Halo")])
        # give the scheduled task a tick to run
        await asyncio.sleep(0)
        return ws

    ws = asyncio.get_event_loop().run_until_complete(go())
    assert ws.kind == "equip" and ws.discord_id == "123"
    assert captured.get("ran") is True
    assert captured["changes"] == [("Head", "Halo")]


def test_start_equip_batch_rejects_a_bad_change(monkeypatch):
    """Every change is prechecked up front: the seeded Closet holds only
    Head=Halo, so a batch whose second change is Eyes=Laser is rejected before
    any session is scheduled."""
    conn = _seed_conn()
    monkeypatch.setattr(economy_api, "open_conn", lambda: conn)
    _stub_permissive_layer_store(monkeypatch)

    async def go():
        with pytest.raises(economy_api.EconomyError):
            await economy_api.start_equip(
                "123", "rOwner", "A", [("Head", "Halo"), ("Eyes", "Laser")]
            )

    asyncio.get_event_loop().run_until_complete(go())
```

Also update `test_start_equip_closes_conn_after_task` in the same file: change its `start_equip(...)` call's trailing `"Head", "Halo"` arguments to `[("Head", "Halo")]`.

In `webapp/test_mock_economy.py`, replace `test_equip_swaps_and_returns_displaced`:

```python
def test_equip_swaps_and_returns_displaced():
    m = mock_economy.MockEconomy()
    owner = mock_economy.DEV_OWNER
    char = m.read_state(owner)["characters"][0]
    asset = m.read_state(owner)["closet"]["assets"][0]
    old = next(a["value"] for a in char["attributes"] if a["trait_type"] == asset["slot"])
    res = m.equip(owner, char["nft_id"], [(asset["slot"], asset["value"])])
    assert res["state"] == "done"
    assert res["displaced"] == [{"slot": asset["slot"], "value": old}]
    # incoming now on the character; displaced now in the bucket
    char2 = m.read_state(owner)["characters"][0]
    assert any(
        a["trait_type"] == asset["slot"] and a["value"] == asset["value"]
        for a in char2["attributes"]
    )
```

In `webapp/test_smoke.py`, update the comment inside `test_equip_missing_body_field_returns_400` (the empty-body path must still 400) and add a batch-shape test right after it:

```python
@pytest.mark.filterwarnings("ignore::aiohttp.web_exceptions.NotAppKeyWarning")
def test_equip_accepts_batch_and_legacy_shapes(monkeypatch):
    """Both the new {changes:[...]} body and the legacy {slot, value} body reach
    start_equip as a normalized list of (slot, value) pairs."""
    from aiohttp.test_utils import make_mocked_request

    monkeypatch.setattr(server.config, "WEBAPP_DEV_MODE", False)
    seen = []

    async def fake_start_equip(uid, wallet, nft_id, changes, user_token=None):
        seen.append(changes)
        raise server.economy_api.EconomyError("stop here")

    monkeypatch.setattr(server.economy_api, "start_equip", fake_start_equip)

    def call(body):
        req = make_mocked_request("POST", "/api/equip")
        req["user"] = {"id": "u1", "name": "test"}
        req["wallet"] = "rOwner"

        async def _json():
            return body

        req.json = _json  # type: ignore[method-assign]
        return asyncio.get_event_loop().run_until_complete(server.handle_equip_start(req))

    assert call({"nft_id": "N", "slot": "Head", "value": "Crown"}).status == 400
    assert call({"nft_id": "N", "changes": [
        {"slot": "Head", "value": "Crown"}, {"slot": "Eyes", "value": "Laser"}]}).status == 400
    assert seen == [[("Head", "Crown")], [("Head", "Crown"), ("Eyes", "Laser")]]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest webapp/test_economy_api.py webapp/test_mock_economy.py webapp/test_smoke.py -q -k equip`
Expected: FAIL — `AttributeError: module 'webapp.economy_api' has no attribute 'normalize_equip_changes'` and `TypeError` on the `changes=` kwarg.

- [ ] **Step 3: Implement `normalize_equip_changes` and the new `start_equip`**

In `webapp/economy_api.py`, add above `start_equip`:

```python
def normalize_equip_changes(body: dict[str, Any]) -> list[tuple[str, str]]:
    """Wire-level compatibility seam: accept either the batch shape
    {"changes": [{"slot", "value"}, ...]} or the legacy single
    {"slot", "value"}, and return the canonical list of (slot, value) pairs.
    Raises EconomyError (-> HTTP 400) on anything malformed."""
    raw = body.get("changes")
    if raw is None:
        slot, value = body.get("slot"), body.get("value")
        if not slot or not value:
            raise EconomyError("no changes to apply")
        raw = [{"slot": slot, "value": value}]
    if not isinstance(raw, list) or not raw:
        raise EconomyError("no changes to apply")
    if len(raw) > len(trait_economy.NON_BODY_SLOTS):
        raise EconomyError("too many changes in one batch")
    changes: list[tuple[str, str]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise EconomyError("each change must be {slot, value}")
        slot, value = item.get("slot"), item.get("value")
        if not isinstance(slot, str) or not isinstance(value, str) or not slot or not value:
            raise EconomyError("each change must be {slot, value}")
        if slot in seen:
            raise EconomyError(f"duplicate slot in one batch ({slot})")
        seen.add(slot)
        changes.append((slot, value))
    return changes
```

Replace `start_equip` with:

```python
async def start_equip(
    discord_id: str,
    owner: str,
    nft_id: str,
    changes: list[tuple[str, str]],
    user_token: str | None = None,
) -> EconomyWebSession:
    conn = open_conn()
    try:
        rec = _load_owned_character(conn, owner, nft_id)
        assets = {
            (s, v): c for (o, s, v, c) in economy_store.read_closet_assets(conn) if o == owner
        }
        # Mirror run_equip's running working copy so an over-spending batch is
        # rejected up front, with the same message the flow would produce.
        for slot, value in changes:
            chk = trait_economy.can_equip(rec, slot, value, assets, mutable=bool(rec.mutable))
            if not chk.ok:
                raise EconomyError(f"cannot equip: {chk.reason}")
            await _require_body_affinity(rec.body, slot, value)
            displaced = trait_economy.slot_value(rec, slot)
            assets[(slot, value)] = assets.get((slot, value), 0) - 1
            assets[(slot, displaced)] = assets.get((slot, displaced), 0) + 1
    except Exception:
        conn.close()
        raise
    session = economy_flow.EquipSession(owner=owner, character=rec, changes=changes)
    return _schedule("equip", discord_id, session, conn, economy_flow.run_equip, user_token)
```

In the same file, change the `equip` branch of `economy_session_dict` (line ~103) to:

```python
    if kind == "equip":
        base["displaced"] = [{"slot": k, "value": v} for k, v in s.displaced.items()]
```

- [ ] **Step 4: Wire the handler and the mock**

In `lfg_service/app.py`, replace the `handle_equip_start` definition (line ~5143):

```python
handle_equip_start = _economy_post(
    "equip",
    lambda uid, w, b, tok: economy_api.start_equip(
        uid, w, b["nft_id"], economy_api.normalize_equip_changes(b), user_token=tok
    ),
    lambda w, b: mock_economy.INSTANCE.equip(
        w, b["nft_id"], economy_api.normalize_equip_changes(b)
    ),
)
```

`_economy_post` already maps `EconomyError` → 400 and `KeyError`/`ValueError` → 400, so a body missing `nft_id` still returns 400 and a malformed change list now does too. In `WEBAPP_DEV_MODE` the mock lambda's `EconomyError` is caught by that path's `except Exception` → 400.

In `webapp/mock_economy.py`, replace `equip` (line 135):

```python
    def equip(self, owner: str, nft_id: str, changes: list[tuple[str, str]]) -> dict[str, Any]:
        char = self._char(nft_id)
        # Validate the whole batch against a working copy before mutating, so a
        # partial apply is impossible (mirrors run_equip's precheck).
        working = dict(self.assets)
        displaced: list[dict[str, str]] = []
        for slot, value in changes:
            if working.get((slot, value), 0) <= 0:
                return {
                    "id": "mock",
                    "state": "failed",
                    "error": "asset not in closet",
                    "displaced": [],
                }
            was = next(a["value"] for a in char["attributes"] if a["trait_type"] == slot)
            displaced.append({"slot": slot, "value": was})
            working[(slot, value)] = working.get((slot, value), 0) - 1
            if was != "None":
                working[(slot, was)] = working.get((slot, was), 0) + 1
        for slot, value in changes:
            next(a for a in char["attributes"] if a["trait_type"] == slot)["value"] = value
        self.assets = working
        return {"id": "mock", "state": "done", "error": None, "displaced": displaced}
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/pytest webapp/test_economy_api.py webapp/test_mock_economy.py webapp/test_smoke.py tests/test_economy_feature_flag.py -q`
Expected: PASS, no failures.

- [ ] **Step 6: Commit**

```bash
git add webapp/economy_api.py webapp/mock_economy.py lfg_service/app.py \
        webapp/test_economy_api.py webapp/test_mock_economy.py webapp/test_smoke.py
git commit -m "feat(api): /api/equip accepts a batch of changes (legacy shape still normalized)"
```

---

### Task 3: Pure client logic in `build_pure.js`

**Files:**
- Modify: `webapp/client/build_pure.js`
- Test: `tests/test_build_pure_js.py`

**Interfaces:**
- Consumes: nothing from earlier tasks (pure functions, no imports).
- Produces, all exported from `webapp/client/build_pure.js`:
  - `applyPending(attributes, pending) -> [{trait_type, value}, ...]`
  - `effectiveAssets(assets, character, pending) -> [{slot, value, count}, ...]`
  - `netChanges(character, pending) -> [{slot, value}, ...]`

  where `attributes` is `character.attributes`, `assets` is `economyState.closet.assets` (`[{slot, value, count}, ...]`), and `pending` is `{slot: incomingValue}`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_build_pure_js.py`:

```python
# ---------------------------------------------------------------------------
# applyPending(attributes, pending) -> attributes with staged values applied
# ---------------------------------------------------------------------------

ATTRS = (
    "[{trait_type: 'Body', value: 'Straight Blue'},"
    " {trait_type: 'Head', value: 'Crown'},"
    " {trait_type: 'Eyes', value: 'None'}]"
)
CHAR = f"{{nft_id: 'A', body: 'male', attributes: {ATTRS}}}"


def test_apply_pending_overrides_only_staged_slots():
    out = run_js(f"M.applyPending({ATTRS}, {{Head: 'Tiara'}})")
    assert out == [
        {"trait_type": "Body", "value": "Straight Blue"},
        {"trait_type": "Head", "value": "Tiara"},
        {"trait_type": "Eyes", "value": "None"},
    ]


def test_apply_pending_empty_is_identity():
    out = run_js(f"M.applyPending({ATTRS}, {{}})")
    assert out[1] == {"trait_type": "Head", "value": "Crown"}


def test_apply_pending_ignores_slots_the_character_lacks():
    out = run_js(f"M.applyPending({ATTRS}, {{Wings: 'Angel'}})")
    assert len(out) == 3 and all(a["trait_type"] != "Wings" for a in out)


# ---------------------------------------------------------------------------
# effectiveAssets(assets, character, pending) -> optimistic Closet counts
# ---------------------------------------------------------------------------


def test_effective_assets_decrements_the_staged_incoming():
    assets = "[{slot: 'Head', value: 'Tiara', count: 2}]"
    out = run_js(f"M.effectiveAssets({assets}, {CHAR}, {{Head: 'Tiara'}})")
    # Tiara -1; Crown (displaced off the character) appears
    assert {"slot": "Head", "value": "Tiara", "count": 1} in out
    assert {"slot": "Head", "value": "Crown", "count": 1} in out


def test_effective_assets_drops_entries_reaching_zero():
    assets = "[{slot: 'Head', value: 'Tiara', count: 1}]"
    out = run_js(f"M.effectiveAssets({assets}, {CHAR}, {{Head: 'Tiara'}})")
    assert all(a["value"] != "Tiara" for a in out)
    assert out == [{"slot": "Head", "value": "Crown", "count": 1}]


def test_effective_assets_never_materializes_none():
    # Eyes currently holds 'None'; staging Laser must not create an Eyes/None tile
    assets = "[{slot: 'Eyes', value: 'Laser', count: 1}]"
    out = run_js(f"M.effectiveAssets({assets}, {CHAR}, {{Eyes: 'Laser'}})")
    assert out == []


def test_effective_assets_merges_displaced_into_an_existing_stack():
    assets = "[{slot: 'Head', value: 'Tiara', count: 1}, {slot: 'Head', value: 'Crown', count: 2}]"
    out = run_js(f"M.effectiveAssets({assets}, {CHAR}, {{Head: 'Tiara'}})")
    assert {"slot": "Head", "value": "Crown", "count": 3} in out


def test_effective_assets_no_pending_is_identity():
    assets = "[{slot: 'Head', value: 'Tiara', count: 2}]"
    out = run_js(f"M.effectiveAssets({assets}, {CHAR}, {{}})")
    assert out == [{"slot": "Head", "value": "Tiara", "count": 2}]


def test_effective_assets_without_a_character_is_identity():
    assets = "[{slot: 'Head', value: 'Tiara', count: 2}]"
    out = run_js(f"M.effectiveAssets({assets}, null, {{Head: 'Tiara'}})")
    assert out == [{"slot": "Head", "value": "Tiara", "count": 2}]


# ---------------------------------------------------------------------------
# netChanges(character, pending) -> the POST payload
# ---------------------------------------------------------------------------


def test_net_changes_lists_staged_slots():
    out = run_js(f"M.netChanges({CHAR}, {{Head: 'Tiara', Eyes: 'Laser'}})")
    assert sorted(out, key=lambda c: c["slot"]) == [
        {"slot": "Eyes", "value": "Laser"},
        {"slot": "Head", "value": "Tiara"},
    ]


def test_net_changes_drops_a_slot_staged_back_to_its_current_value():
    # Re-clicking the character's own Crown undoes the stage -> empty batch
    out = run_js(f"M.netChanges({CHAR}, {{Head: 'Crown'}})")
    assert out == []


def test_net_changes_empty_pending_is_empty():
    assert run_js(f"M.netChanges({CHAR}, {{}})") == []


def test_net_changes_without_a_character_is_empty():
    assert run_js("M.netChanges(null, {Head: 'Tiara'})") == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_build_pure_js.py -q`
Expected: FAIL — `node script failed` / `M.applyPending is not a function`.

- [ ] **Step 3: Implement the three functions**

Append to `webapp/client/build_pure.js`:

```javascript
// --- Pending (unsaved) Build changes -----------------------------------
// The Build panel stages tile clicks in a `{slot: incomingValue}` map and only
// commits them on Save, as ONE NFTokenModify. These three functions are the
// whole model: what the canvas draws, what the Closet grid shows, and what the
// POST body is.

// The character's attributes with every staged change applied. Slots the
// character does not have are ignored (never invented).
export function applyPending(attributes, pending) {
  const staged = pending || {};
  return (attributes || []).map((a) => (
    Object.prototype.hasOwnProperty.call(staged, a.trait_type)
      ? { ...a, value: staged[a.trait_type] }
      : a
  ));
}

// Current value held in `slot` by `character`; 'None' when the slot is empty or
// the character has no such attribute — the same convention the server's
// trait_economy.slot_value uses.
function currentValue(character, slot) {
  if (!character) return 'None';
  const a = (character.attributes || []).find((x) => x.trait_type === slot);
  return (a && a.value) || 'None';
}

// Closet counts with the staged changes applied: each staged incoming asset is
// -1, each displaced value is +1. Entries reaching 0 are dropped; a displaced
// value the Closet did not already hold is synthesized so it can be clicked
// back on. 'None' is never materialized as a tile (it is the file-less
// stand-in for an empty slot, not a real asset).
export function effectiveAssets(assets, character, pending) {
  const staged = pending || {};
  const out = (assets || []).map((a) => ({ ...a }));
  if (!character) return out;
  const find = (slot, value) => out.find((a) => a.slot === slot && a.value === value);
  for (const slot of Object.keys(staged)) {
    const incoming = staged[slot];
    const displaced = currentValue(character, slot);
    if (incoming === displaced) continue;       // staged back to current: no-op
    const inEntry = find(slot, incoming);
    if (inEntry) inEntry.count -= 1;
    if (displaced !== 'None') {
      const outEntry = find(slot, displaced);
      if (outEntry) outEntry.count += 1;
      else out.push({ slot, value: displaced, count: 1 });
    }
  }
  return out.filter((a) => a.count > 0);
}

// The `changes` array for POST /api/equip: one {slot, value} per staged slot
// whose value actually differs from what the character wears on-chain. A slot
// staged back to its current value nets out — that is how undo works.
export function netChanges(character, pending) {
  const staged = pending || {};
  if (!character) return [];
  return Object.keys(staged)
    .filter((slot) => staged[slot] !== currentValue(character, slot))
    .map((slot) => ({ slot, value: staged[slot] }));
}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_build_pure_js.py -q`
Expected: PASS, 21 passed.

- [ ] **Step 5: Commit**

```bash
git add webapp/client/build_pure.js tests/test_build_pure_js.py
git commit -m "feat(build): pure staging logic — applyPending/effectiveAssets/netChanges"
```

---

### Task 4: Client wiring — staging, Save bar, dirty guards

**Files:**
- Modify: `webapp/client/app.js` (import line 18; `renderCloset` ~2155; `equipTrait` ~2313; `selectCharacter` ~2024; `openDressup` ~2037; `harvestActive` ~2371; back button ~3318; `renderGoPicker`'s assemble tile ~1997)
- Modify: `webapp/client/index.html` (dressup stage markup ~line 128, `app.js?v=` line 345)
- Modify: `webapp/client/style.css`

**Interfaces:**
- Consumes: `buildPure.applyPending`, `buildPure.effectiveAssets`, `buildPure.netChanges` (Task 3); `POST /api/equip {nft_id, changes}` (Task 2).
- Produces: no exported surface — this is the top-level UI.

There is no DOM test harness in this repo (`app.js` is verified by the pure modules plus manual smoke), so this task's verification is the full suite plus the dev-mode manual check in Step 8.

- [ ] **Step 1: Add the Save bar markup**

In `webapp/client/index.html`, inside `<div class="dressup-stage">`, after the `dressup-harvest-btn` button, add:

```html
          <div id="build-save-bar" class="build-save-bar" hidden>
            <button id="build-save-btn" class="primary">💾 Save changes</button>
            <button id="build-discard-btn" class="secondary">Discard</button>
          </div>
```

- [ ] **Step 2: Style it**

Append to `webapp/client/style.css`:

```css
.build-save-bar {
  display: flex;
  gap: .5rem;
  justify-content: center;
  margin-top: .5rem;
}
.build-save-bar button { flex: 0 1 auto; }
.closet-item.staged { outline: 2px solid var(--blue); }
```

- [ ] **Step 3: Add the pending state and replace `equipTrait` with staging**

In `webapp/client/app.js`, next to `let equipBusy = false;` (line 2135), replace that line with:

```javascript
let saveBusy = false;
let pendingEquips = {};   // {slot: incomingValue} — staged, uncommitted
let pendingFor = null;    // nft_id the staged batch belongs to
```

Replace the whole `equipTrait` function (lines 2313-2341) with:

```javascript
// Staged (unsaved) Build helpers. A tile click no longer transacts: it records
// the change, repaints from the pending model, and surfaces the Save bar. The
// whole batch commits in ONE NFTokenModify when the user clicks Save.

function pending() {
  return pendingFor === activeNftId ? pendingEquips : {};
}

function isDirty() {
  const char = activeChar();
  return Boolean(char) && buildPure.netChanges(char, pending()).length > 0;
}

function clearPending() {
  pendingEquips = {};
  pendingFor = null;
}

function stagePendingEquip(slot, value) {
  const char = activeChar();
  if (!char || saveBusy) return;
  if (pendingFor !== activeNftId) { pendingEquips = {}; pendingFor = activeNftId; }
  pendingEquips[slot] = value;
  renderCanvas(char);
  renderCloset();
}

function renderSaveBar() {
  const bar = el('build-save-bar');
  if (!bar) return;   // tolerate a stale cached index.html
  const char = activeChar();
  const n = char ? buildPure.netChanges(char, pending()).length : 0;
  bar.hidden = n === 0;
  el('build-save-btn').textContent = `💾 Save changes (${n})`;
  el('build-save-btn').disabled = saveBusy;
  el('build-discard-btn').disabled = saveBusy;
}

function discardPending() {
  clearPending();
  const char = activeChar();
  if (char) renderCanvas(char);
  renderCloset();
}

// Every exit from the current character routes through here. Returns true when
// it is safe to proceed (nothing staged, or the user chose to discard).
// Native window.confirm is a silent no-op in Discord's sandboxed iframe.
async function confirmDiscardIfDirty() {
  if (!isDirty()) return true;
  const char = activeChar();
  const ok = await confirmDialog({
    title: 'Discard unsaved changes?',
    text: `You have unsaved changes to #${char.edition}. They have not been saved to the ledger.`,
    confirmLabel: 'Discard',
  });
  if (ok) discardPending();
  return ok;
}

async function saveBuild() {
  const char = activeChar();
  if (!char || saveBusy) return;
  const changes = buildPure.netChanges(char, pending());
  if (!changes.length) return;
  saveBusy = true;
  renderSaveBar();
  status('Saving your build…');
  try {
    const res = await api('/api/equip', {
      method: 'POST',
      body: JSON.stringify({ nft_id: activeNftId, changes }),
    });
    const final = await pollEconomyOp('equip', res);
    if (final.state === 'failed') throw new Error(final.error || 'save failed');
    status('');
  } catch (e) {
    showError(e.message);
    status('');
  } finally {
    // Always resync from authoritative state and drop the staged batch — the
    // indeterminate / mirror-pending branches can leave the character genuinely
    // changed, so silently re-offering the same batch could double-apply it.
    saveBusy = false;
    clearPending();
    try {
      economyState = await api('/api/economy');
    } catch (e) {
      showError(e.message);
    }
    selectCharacter(activeNftId);
  }
}
```

- [ ] **Step 4: Render the canvas and Closet from the pending model**

In `renderCloset` (line ~2155), replace the loop header

```javascript
  for (const asset of economyState.closet.assets) {
```

with

```javascript
  const staged = pending();
  for (const asset of buildPure.effectiveAssets(economyState.closet.assets, char, staged)) {
```

and replace the equip wiring line

```javascript
    if (compatible) item.onclick = () => equipTrait(asset.slot, asset.value, item);
```

with

```javascript
    if (staged[asset.slot] === asset.value) item.classList.add('staged');
    // Staging only — nothing goes on-ledger until Save.
    if (compatible) item.onclick = () => stagePendingEquip(asset.slot, asset.value);
```

Then, at the end of `renderCloset`, replace the final `renderTraitStrip();` with:

```javascript
  renderSaveBar();
  renderTraitStrip();
```

In `renderCanvas` (line ~1920), replace the `byType` line

```javascript
  const byType = Object.fromEntries(char.attributes.map((a) => [a.trait_type, a.value]));
```

with

```javascript
  // Draw staged (unsaved) changes, not just what is on-ledger.
  const shown = buildPure.applyPending(char.attributes, pending());
  const byType = Object.fromEntries(shown.map((a) => [a.trait_type, a.value]));
```

- [ ] **Step 5: Disable Extract/Deposit while dirty, and gate every exit**

In `renderCloset`, right after `extractBtn.textContent = '↑';`, add:

```javascript
    // Extract mutates the very Closet counts the staged batch is computed
    // against — block it until the batch is saved or discarded.
    if (isDirty()) {
      extractBtn.disabled = true;
      extractBtn.title = 'Save or discard your changes first';
    }
```

In `renderTraitStrip`, right after `depositBtn.textContent = 'Deposit';`, add:

```javascript
    if (isDirty()) {
      depositBtn.disabled = true;
      depositBtn.title = 'Save or discard your changes first';
    }
```

In `selectCharacter` (line ~2024), clear any batch belonging to a different character:

```javascript
function selectCharacter(nftId) {
  if (nftId !== pendingFor) clearPending();
  activeNftId = nftId;
  const char = economyState.characters.find((c) => c.nft_id === nftId);
  if (char) renderCanvas(char);
  renderCloset();
}
```

In `renderGoPicker`, replace the character tile's handler

```javascript
      tile.onclick = () => { closeGoPicker(); selectCharacter(char.nft_id); };
```

with

```javascript
      tile.onclick = async () => {
        if (!(await confirmDiscardIfDirty())) return;
        closeGoPicker();
        selectCharacter(char.nft_id);
      };
```

and the assemble tile's handler

```javascript
  if (goAssembleEnabled) add.onclick = () => { closeGoPicker(); openAssemble(); };
```

with

```javascript
  if (goAssembleEnabled) add.onclick = async () => {
    if (!(await confirmDiscardIfDirty())) return;
    closeGoPicker();
    openAssemble();
  };
```

In `harvestActive` (line ~2371), add the guard as the first statement after the `if (!char) return;`:

```javascript
  if (!(await confirmDiscardIfDirty())) return;
```

At line ~3318, replace the back-button wiring:

```javascript
  el('dressup-back-btn').onclick = async () => {
    if (!(await confirmDiscardIfDirty())) return;
    showMintHome();
  };
```

And in the same wiring block, add the Save bar handlers:

```javascript
  el('build-save-btn').onclick = () => saveBuild();
  el('build-discard-btn').onclick = () => discardPending();
```

Finally, in `openDressup` (line ~2037), add `clearPending();` immediately after `showPanel('dressup-panel');` so re-entering the panel never resurrects a stale batch.

- [ ] **Step 6: Bump the cache busters**

In `webapp/client/app.js` line 18, bump the pure-module import (it changed in Task 3):

```javascript
import * as buildPure from './build_pure.js?v=24';
```

In `webapp/client/index.html` line 345:

```html
  <script type="module" src="app.js?v=26"></script>
```

and line 27 (the Save bar styles changed):

```html
  <link rel="stylesheet" href="style.css?v=19">
```

- [ ] **Step 7: Verify no stale references remain**

Run: `grep -n "equipTrait\|equipBusy\|displaced_value" webapp/client/app.js`
Expected: no output (the old per-click path is fully gone).

Run: `grep -n "build_pure.js?v=" webapp/client/app.js && grep -n "app.js?v=\|style.css?v=" webapp/client/index.html`
Expected: `?v=24`, `?v=26`, `?v=19` respectively — all bumped.

- [ ] **Step 8: Manual smoke in dev mode**

Run: `WEBAPP_DEV_MODE=1 .venv/bin/python -m webapp.server` (or the project's usual dev entry), open the Activity, go to Build.

Verify, in order:
1. Clicking a compatible Closet tile changes the canvas and shows `💾 Save changes (1)` — and **no** network request to `/api/equip` fires (check the browser network tab).
2. The clicked tile's `×count` drops by 1; the trait it displaced appears in the grid.
3. Clicking that displaced trait back on returns the counter to `(0)` and hides the Save bar.
4. Staging two different slots shows `(2)`; Save fires exactly **one** `POST /api/equip` whose body carries both changes.
5. With changes staged, Back / Switch GO / Harvest each raise the in-app discard dialog; Cancel keeps the staged state.
6. With changes staged, the Extract `↑` and Deposit buttons are disabled.

- [ ] **Step 9: Run the full suite**

Run: `.venv/bin/pytest -q`
Expected: PASS — no failures. (Full-suite-only failures are not automatically flakes; if one appears, verify it on clean `main` before dismissing it.)

- [ ] **Step 10: Commit**

```bash
git add webapp/client/app.js webapp/client/index.html webapp/client/style.css
git commit -m "feat(build): stage trait changes and commit them with one Save"
```

---

## Wrap-up

- [ ] **Update the spec's status line**

In `docs/superpowers/specs/2026-07-21-build-batched-save-design.md`, change the header line to `**Status:** Implemented`.

- [ ] **Push and open a PR**

This touches application source, so it takes the normal reviewed-PR path (not a direct push to `main`). The pre-push hook runs the full gate; never bypass it with `--no-verify`.

```bash
git push -u origin claude/nft-building-save-workflow-07b8d2
gh pr create --repo Team-Hamsa/LFG --title "Build panel: batch trait changes behind one Save" \
  --body "$(cat <<'EOF'
Clicking a Closet tile in Build no longer sends an NFTokenModify. Changes stage
locally, a Save button surfaces, and the whole batch commits in ONE
NFTokenModify plus ONE Closet sync.

- `EquipSession` / `run_equip` take a list of `(slot, incoming)` changes;
  ordering and every fail-safe branch are unchanged.
- `POST /api/equip` accepts `{nft_id, changes:[...]}`; the legacy
  `{nft_id, slot, value}` body is normalized to a one-element list.
- Client stages into `pendingEquips`, renders canvas + optimistic Closet counts
  from `build_pure.js`, and guards every exit with the in-app discard dialog.

Spec: docs/superpowers/specs/2026-07-21-build-batched-save-design.md
EOF
)"
```

Per repo convention, wait for Greptile and CodeRabbit and resolve or explicitly address every actionable finding before merging.
