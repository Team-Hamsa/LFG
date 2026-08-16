# Blank-Harvest Economy (modify-in-place) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Harvest strips a character to a blank via `NFTokenModify` (no burn); Assemble dresses one of the user's blank NFTs in place (no mint/offer/accept); a full Activity builder UI lets the user pick the blank, the body, and every trait.

**Architecture:** Rework `run_harvest`/`run_assemble` in `lfg_core/economy_flow.py` onto the modify+revert pattern `run_equip` already uses. Bodies become ordinary Closet assets (`slot="Body"`, keyed by value, not edition) — `closet_bodies` is retired via a one-off migration. Blanks (all attributes `None`, shared silhouette art) are derived state, no new tables. Legacy flag-24 characters get a one-time burn+remint-as-blank upgrade. Phase B adds `GET /api/assemble/options` and a builder overlay in the vanilla-JS Activity client.

**Tech Stack:** Python 3 / aiohttp / sqlite3 / xrpl-py (existing), vanilla JS no-build client, pytest.

## Global Constraints

- Every XRPL tx keeps `SourceTag = 2606160021` and provenance memos (existing builders — no memo/SourceTag code changes allowed or needed).
- Slot universe is `swap_meta.TRAIT_ORDER` (9 slots incl. `Body`); non-body slots are `trait_economy.NON_BODY_SLOTS` (8). Never hardcode slot lists.
- `"None"` is a real, conserved asset value.
- Chain-first ordering + phase-aware `ClosetError`/`ClosetMirrorError`/`ClosetIndeterminateError` taxonomy must be preserved in every flow.
- Pre-push gate (ruff, ruff-format, mypy, gitleaks, pytest, validate-trait-config) must pass; never `--no-verify` on push.
- New test files importing `lfg_core` at module top MUST copy the env-guard preamble (`BUNNY_PULL_ZONE`/`LAYER_SOURCE` setdefaults) used by existing tests (see `tests/test_economy_flow.py` top).
- Cache-buster: any client asset change bumps its `?v=` in `index.html`/`app.js` in the same PR (ES-module imports too).
- Two PRs: Phase A = Tasks 1–9 (backend), Phase B = Tasks 10–13 (UI). Both target `main`, normal Greptile+CodeRabbit review. Phase A rebases over the fire-and-forget-harvest branch if it lands first (overlap: `economy_flow.py`, `lfg_service/app.py`).

---

## Phase A — backend model

### Task 1: Blankness + body-map helpers in `trait_economy`

**Files:**
- Modify: `lfg_core/trait_economy.py`
- Test: `tests/test_trait_economy_blank.py` (create)

**Interfaces:**
- Produces: `is_blank(rec: OnchainNft) -> bool`; `blank_attributes() -> list[dict[str, str]]`; `body_class_map(genesis: Genesis) -> dict[str, str]` (body value → layer-dir class, e.g. `"Milady" -> "milady"`).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_trait_economy_blank.py
import os

os.environ.setdefault("BUNNY_PULL_ZONE", "test.b-cdn.net")
os.environ.setdefault("LAYER_SOURCE", "local")

from lfg_core import trait_economy as te
from lfg_core.nft_index import OnchainNft
from lfg_core.swap_meta import TRAIT_ORDER


def _rec(attrs):
    return OnchainNft(
        nft_id="A" * 64, nft_number=7, owner="rOwner", uri_hex="",
        name="LFG #7", image="", body="milady", attributes=attrs,
        is_burned=0, mutable=1, ledger_index=1,
    )


def test_blank_attributes_covers_every_slot_with_none():
    attrs = te.blank_attributes()
    assert [a["trait_type"] for a in attrs] == TRAIT_ORDER
    assert all(a["value"] == "None" for a in attrs)


def test_is_blank_true_for_blank_attrs():
    assert te.is_blank(_rec(te.blank_attributes()))


def test_is_blank_false_when_any_slot_set():
    attrs = te.blank_attributes()
    attrs[2] = {"trait_type": "Body", "value": "Milady"}
    assert not te.is_blank(_rec(attrs))


def test_is_blank_true_for_missing_attrs():
    # Absent slots read as "None" (slot_value semantics).
    assert te.is_blank(_rec([]))


def test_body_class_map_from_genesis():
    g = te.Genesis(
        trait_counts={},
        edition_bodies={1: ("Milady", "milady"), 2: ("Skeleton", "skeleton"),
                        3: ("Milady", "milady")},
    )
    assert te.body_class_map(g) == {"Milady": "milady", "Skeleton": "skeleton"}
```

Adjust the `OnchainNft(...)` kwargs to the dataclass's actual fields (read `lfg_core/nft_index.py`) — construct however existing tests do.

- [ ] **Step 2: Run to verify failure** — `.venv/bin/pytest tests/test_trait_economy_blank.py -q` → FAIL (`AttributeError: blank_attributes`).

- [ ] **Step 3: Implement in `lfg_core/trait_economy.py`** (below `slot_value`):

```python
def blank_attributes() -> list[dict[str, str]]:
    """The canonical attribute list of a BLANK character: every TRAIT_ORDER
    slot (including Body) explicitly "None"."""
    return [{"trait_type": s, "value": "None"} for s in swap_meta.TRAIT_ORDER]


def is_blank(rec: OnchainNft) -> bool:
    """A character is blank iff every slot (including Body) reads "None"."""
    return all(
        (swap_meta.get_attr(rec.attributes, s) or "None") == "None"
        for s in swap_meta.TRAIT_ORDER
    )


def body_class_map(genesis: Genesis) -> dict[str, str]:
    """body value -> layer-dir class, derived from the frozen genesis."""
    return {value: cls for (value, cls) in genesis.edition_bodies.values()}
```

- [ ] **Step 4: Run** — `.venv/bin/pytest tests/test_trait_economy_blank.py -q` → PASS.
- [ ] **Step 5: Commit** — `git add -A && git commit -m "feat(economy): blank-character helpers (is_blank, blank_attributes, body_class_map)"`

### Task 2: Bodies-by-value in the Closet store

**Files:**
- Modify: `lfg_core/economy_store.py`, `lfg_core/closet_token.py`
- Test: `tests/test_closet_bodies_by_value.py` (create)

**Interfaces:**
- Consumes: existing `set_closet_contents(conn, owner, assets, bodies)` / `read_closet_assets` / `read_closet_bodies` / `build_closet_metadata` / `parse_closet_metadata`.
- Produces: `slot="Body"` rows flow through `closet_assets` like any asset. `build_closet_metadata` writes schema v2 (`"bodies": []`, Body rows inside `"assets"`). `parse_closet_metadata(meta, genesis=None)` gains an optional genesis to convert legacy integer `bodies` into `("Body", value, 1)` asset rows; returns `(assets, legacy_editions)` where `legacy_editions` is non-empty only for unconverted legacy metadata (no genesis passed).

- [ ] **Step 1: Failing tests** — `closet_assets` round-trips a `("Body", "Milady", 2)` row via `set_closet_contents`/`read_closet_assets`; `build_closet_metadata(owner, [("Body", "Milady", 1)], [])` puts the Body row in `assets` and `bodies == []`; `parse_closet_metadata` on a legacy dict (`"bodies": [3]`, genesis mapping `3 -> ("Milady", "milady")`) returns assets containing `("Body", "Milady", 1)`.
- [ ] **Step 2: Run — FAIL.**
- [ ] **Step 3: Implement.** `economy_store` likely needs no schema change (`closet_assets` is `(owner, slot, value, count)` — verify no CHECK constraint rejects `Body`). In `closet_token.py`:

```python
def parse_closet_metadata(
    meta: dict[str, Any], genesis: Any | None = None
) -> tuple[list[Asset], list[int]]:
    block = meta.get("lfg_closet") or meta.get("lfg_bucket") or {}
    assets = [tuple(a) for a in block.get("assets", [])]
    editions = [int(e) for e in block.get("bodies", [])]
    if genesis is not None and editions:
        from collections import Counter
        body_counts: Counter[str] = Counter()
        for e in editions:
            pair = genesis.edition_bodies.get(e)
            if pair:
                body_counts[pair[0]] += 1
        assets += [("Body", v, n) for v, n in sorted(body_counts.items())]
        editions = []
    return assets, editions
```

(Adapt to the real current body — keep validation of untrusted metadata exactly as-is.) `build_closet_metadata` keeps its `bodies: list[int]` parameter for signature stability but always writes `"bodies": []` — every caller passes `[]` after Task 3.
- [ ] **Step 4: Run — PASS**, plus the existing suites: `.venv/bin/pytest tests/ -k "closet or economy_store" -q`.
- [ ] **Step 5: Commit** — `feat(economy): closet bodies stored by value as Body assets (schema v2)`.

### Task 3: Precheck rework — `can_harvest` / `can_assemble` v2

**Files:**
- Modify: `lfg_core/trait_economy.py`
- Test: extend `tests/test_trait_economy_blank.py` (and update existing precheck tests in `tests/test_trait_economy*.py`)

**Interfaces:**
- Produces:
  - `can_harvest(rec: OnchainNft, *, mutable: bool, burnable: bool) -> Precheck` — ok iff live, not blank, and (`mutable` or `burnable`); genesis/edition checks removed (body value is read from on-chain attributes).
  - `can_assemble(rec: OnchainNft, body_value: str, chosen: dict[str, str], owner_assets: dict[tuple[str, str], int], *, mutable: bool) -> Precheck` — ok iff `rec` is live, `mutable`, `is_blank(rec)`, `chosen` covers exactly `NON_BODY_SLOTS`, and `owner_assets` holds `("Body", body_value)` plus every chosen `(slot, value)` at the needed multiplicity.

- [ ] **Step 1: Failing tests** — matrix: harvest refuses burned / blank / flag-24-non-burnable-non-mutable; allows mutable non-burnable (the old "equip-only" refusal is GONE); assemble refuses non-blank target, non-mutable target, missing Body asset, short multiplicity (two slots choosing the same value with count 1), unknown slot key, missing slot; accepts a full valid set.
- [ ] **Step 2: Run — FAIL.**
- [ ] **Step 3: Implement:**

```python
def can_harvest(rec: OnchainNft, *, mutable: bool, burnable: bool) -> Precheck:
    """A character can be harvested iff it is live, not already blank, and
    either mutable (modify-in-place strip) or burnable (legacy one-time
    burn+remint-as-blank upgrade)."""
    if rec.is_burned:
        return Precheck(False, "character is already burned")
    if is_blank(rec):
        return Precheck(False, "character is already blank")
    if not (mutable or burnable):
        return Precheck(False, "character is neither mutable nor burnable")
    return _OK


def can_assemble(
    rec: OnchainNft,
    body_value: str,
    chosen: dict[str, str],
    owner_assets: dict[tuple[str, str], int],
    *,
    mutable: bool,
) -> Precheck:
    """A blank the caller owns can be dressed iff it is live+mutable+blank and
    the Closet covers the body plus a full valid non-body set."""
    if rec.is_burned:
        return Precheck(False, "character is burned")
    if not mutable:
        return Precheck(False, "character is not mutable")
    if not is_blank(rec):
        return Precheck(False, "character is not blank — harvest it first")
    missing = [s for s in NON_BODY_SLOTS if s not in chosen]
    if missing:
        return Precheck(False, f"incomplete set, missing slots: {', '.join(missing)}")
    extra = [s for s in chosen if s not in NON_BODY_SLOTS]
    if extra:
        return Precheck(False, f"unknown slots in set: {', '.join(extra)}")
    need = Counter((s, chosen[s]) for s in NON_BODY_SLOTS)
    need[("Body", body_value)] += 1
    for (slot, value), qty in need.items():
        if owner_assets.get((slot, value), 0) < qty:
            return Precheck(False, f"Closet lacks asset {slot}={value}")
    return _OK
```

Update every existing caller/test of the old signatures in the same commit (grep `can_harvest\|can_assemble` across `lfg_core/`, `webapp/`, `scripts/`, `tests/`).
- [ ] **Step 4: Run — PASS**: `.venv/bin/pytest tests/ webapp/ -q` (full, callers updated).
- [ ] **Step 5: Commit** — `feat(economy): blank-model prechecks for harvest/assemble`.

### Task 4: Census/conservation v2 + auditor

**Files:**
- Modify: `lfg_core/trait_economy.py` (`asset_census`, conservation compare), `scripts/audit_trait_economy.py`
- Test: extend existing census tests (`tests/test_trait_economy*.py`)

**Interfaces:**
- Produces: `asset_census(characters, closet_assets, trait_tokens)` — `closet_bodies` parameter removed; Body is tallied in `trait_counts` like any slot (dressed characters contribute their on-chain Body value; blanks contribute `("Body", "None")`… **no** — blanks contribute nothing for Body, see below). `genesis_trait_counts_with_bodies(genesis) -> dict` folds per-body-value counts derived from `edition_bodies` into `trait_counts` so the conservation compare covers Body.

Conservation semantics: at genesis every edition wore a body, so baseline Body counts = `Counter(v for (v, _cls) in genesis.edition_bodies.values())`. Post-harvest that body sits in a Closet (`("Body", v)` asset); the blank character contributes **no** Body count (its slot is `None`, and `("Body", "None")` was never a genesis asset — exclude the Body slot from the "None is conserved" rule). Non-body slots keep today's exact rule ("None" conserved).

- [ ] **Step 1: Failing tests** — a dressed char + empty closet reproduces genesis counts; harvesting it (char → blank, its 8 non-body values + `("Body", v)` → closet assets) leaves census equal to genesis (invariance); `body_presence` is gone.
- [ ] **Step 2: Run — FAIL.**
- [ ] **Step 3: Implement** — in `asset_census`, skip the Body slot for blank characters, count `("Body", slot_value(rec, "Body"))` for dressed ones, drop the `closet_bodies` param and `body_presence`; add `genesis_trait_counts_with_bodies`; update `Census`/`ConservationReport` and the compare function; update `scripts/audit_trait_economy.py` call sites (it reads `read_closet_bodies` — after migration that returns `[]`, keep passing nothing). Update `apply_economy_tx`/backfill callers if they build a census.
- [ ] **Step 4: Run — PASS** full suite.
- [ ] **Step 5: Commit** — `feat(economy): supply-neutral census with Body as a first-class asset`.

### Task 5: `run_harvest` v2 — modify-to-blank (+ legacy burn+remint branch)

**Files:**
- Modify: `lfg_core/economy_flow.py` (`EconomyDeps`, `HarvestSession`, `run_harvest`, `_owner_contents`, `_sync_then_persist` signature)
- Test: `tests/test_economy_flow.py` (rework harvest cases; keep the fake-deps style already there)

**Interfaces:**
- Consumes: Task 1 `blank_attributes`, Task 3 `can_harvest`, Task 2 bodies-as-assets.
- Produces:
  - `EconomyDeps.blank_meta_fn: Callable[[int], Awaitable[str | None]] | None = None` — uploads blank metadata (silhouette image URL, `blank_attributes()`, edition-numbered name) and returns the metadata URL. Wired in Task 8.
  - `_owner_contents(conn, owner) -> dict[tuple[str, str], int]` (bodies set removed; callers updated).
  - `_sync_then_persist(deps, owner, assets)` (bodies param removed; passes `[]` to `sync_closet`/`set_closet_contents`).
  - `HarvestSession` gains `modify_hash`, `legacy_upgrade: bool`, `new_nft_id`, `accept: str | None`; `results` for the legacy accept payload.

- [ ] **Step 1: Failing tests** (fake deps, existing style):
  - mutable path: `char_modify_fn` called with a blank meta URL from `blank_meta_fn`; **no** `char_burn_fn` call; closet credited with 8 non-body values + `("Body", value)`; state DONE; no `supply_changes` rows.
  - mutable path, closet ledger-fail after modify: character is modified **back to its original URI** (`char_modify_fn(nft_id, owner, old_uri)`), journal `reverted_modify`, closet untouched.
  - mutable path, `ClosetMirrorError`: DONE `complete_pending_mirror`, mirror-pending flag set, no revert.
  - legacy path (mutable=0, burnable=1): burn → `blank_meta_fn` → `char_mint_fn` → offer → accept payload surfaced; closet credited; `supply_changes` has the `-1`/`+1` pair (`kind="burn"` then `kind="mint"` on the same edition/traits — net zero).
  - legacy path, remint fails after burn: journal `burned_no_remint`, session FAILED with admin-recovery message, closet untouched (assets ride in the journal).
- [ ] **Step 2: Run — FAIL.**
- [ ] **Step 3: Implement.** Body of the new `run_harvest` (keeping the existing decorator, journaling, and exception frame):

```python
@_serialize_by_owner
async def run_harvest(session: HarvestSession, deps: EconomyDeps) -> None:
    """Strip a character to a BLANK. Mutable path: NFTokenModify to blank
    metadata (reversible: modify back), then credit all assets + the body to
    the Closet. Legacy (non-mutable, burnable) path: one-time upgrade — burn,
    remint the same edition as a mutable blank, offer it back (one accept),
    then credit the Closet. Supply-neutral; the legacy pair is journaled as
    -1/+1 for audit clarity."""
    conn, rec, owner = deps.conn, session.character, session.owner
    try:
        stale = _mirror_pending_error(deps, owner)
        if stale:
            session.fail(stale)
            return
        chk = te.can_harvest(rec, mutable=bool(rec.mutable), burnable=session.burnable)
        if not chk.ok:
            session.fail(f"cannot harvest: {chk.reason}")
            return
        err = await _require_active_closet(deps, owner)
        if err:
            session.fail(err)
            return

        body_value = te.slot_value(rec, "Body")
        session.moved_assets = [(s, te.slot_value(rec, s)) for s in te.NON_BODY_SLOTS]
        session.moved_assets.append(("Body", body_value))
        _write_record(deps.records_dir, "harvest", session.id, session._record("harvesting"))

        blank_meta_url = await deps.blank_meta_fn(session.edition) if deps.blank_meta_fn else None
        if not blank_meta_url:
            session.fail("failed to prepare blank metadata; nothing was changed")
            return

        if rec.mutable:
            modify_hash = await deps.char_modify_fn(rec.nft_id, owner, blank_meta_url)
            if not modify_hash:
                session.fail(f"failed to blank character {rec.nft_id}; nothing was changed")
                _write_record(deps.records_dir, "harvest", session.id,
                              session._record("failed_modify"))
                return
            session.modify_hash = modify_hash
        else:
            session.legacy_upgrade = True
            burn_hash = await deps.char_burn_fn(rec.nft_id, owner)
            if not burn_hash:
                session.fail(f"failed to burn character {rec.nft_id}; nothing was lost")
                _write_record(deps.records_dir, "harvest", session.id,
                              session._record("failed_burn"))
                return
            session.burn_hash = burn_hash
            es.record_supply_change(conn, kind="burn", edition=session.edition,
                                    trait_deltas=_legacy_deltas(rec, sign=-1))
            _write_record(deps.records_dir, "harvest", session.id, session._record("burned"))
            new_id = await deps.char_mint_fn(blank_meta_url)
            if not new_id:
                session.fail(
                    f"character burned but the blank remint failed — admin must remint "
                    f"edition {session.edition} (journal {session.id})")
                _write_record(deps.records_dir, "harvest", session.id,
                              session._record("burned_no_remint"))
                return
            session.new_nft_id = new_id
            es.record_supply_change(conn, kind="mint", edition=session.edition,
                                    trait_deltas=_legacy_deltas(rec, sign=+1))
            offer_id = await deps.char_offer_fn(new_id, owner)
            session.accept = await deps.char_accept_fn(offer_id) if offer_id else None
            _write_record(deps.records_dir, "harvest", session.id, session._record("reminted"))

        assets = _owner_contents(conn, owner)
        for slot, value in session.moved_assets:
            assets[(slot, value)] = assets.get((slot, value), 0) + 1
        try:
            session.sync_tx_hash = await _sync_then_persist(deps, owner, assets)
        except bt.ClosetMirrorError as e:
            session.sync_tx_hash = e.tx_hash
            session.mirror_pending = True
            es.set_mirror_pending(conn, owner, True)
            session.state = DONE
            _write_record(deps.records_dir, "harvest", session.id,
                          session._record("complete_pending_mirror"))
            return
        except bt.ClosetIndeterminateError as e:
            session.fail(f"character blanked but the Closet deposit outcome is unknown ({e}); "
                         f"reconcile from chain (journal {session.id})")
            _write_record(deps.records_dir, "harvest", session.id,
                          session._record("harvest_sync_indeterminate"))
            return
        except Exception as e:
            if rec.mutable and not session.legacy_upgrade:
                old_uri = _raw_uri(rec.uri_hex)
                revert = await deps.char_modify_fn(rec.nft_id, owner, old_uri) if old_uri else None
                if revert:
                    session.fail(f"harvest failed depositing to the Closet ({e}); "
                                 f"your character was restored")
                    _write_record(deps.records_dir, "harvest", session.id,
                                  session._record("reverted_modify"))
                    return
            session.fail(f"harvest failed depositing to the Closet ({e}); assets are in "
                         f"the journal ({session.id}) for recovery")
            _write_record(deps.records_dir, "harvest", session.id,
                          session._record("harvested_pending_closet"))
            return

        session.state = DONE
        _write_record(deps.records_dir, "harvest", session.id, session._record("complete"))
    except Exception as e:
        logging.error(f"Harvest {session.id} failed: {traceback.format_exc()}")
        session.fail(str(e))
        try:
            _write_record(deps.records_dir, "harvest", session.id, session._record("failed"))
        except Exception:
            logging.error(f"Harvest {session.id} terminal record write failed: "
                          f"{traceback.format_exc()}")
```

`_legacy_deltas(rec, sign)` builds `{f"{slot}|{value}": sign}` over all 9 slots — match the exact `trait_deltas` key format `effective_genesis` parses (`slot|value`), and match `record_supply_change`'s real signature (read it first; adapt kwargs). Session `_record` gains the new fields.
- [ ] **Step 4: Run — PASS**: `.venv/bin/pytest tests/test_economy_flow.py -q`, then full suite.
- [ ] **Step 5: Commit** — `feat(economy): harvest strips to a blank via NFTokenModify (legacy burn+remint upgrade)`.

### Task 6: `run_assemble` v2 — dress a blank in place

**Files:**
- Modify: `lfg_core/economy_flow.py` (`AssembleSession`, `run_assemble`)
- Test: `tests/test_economy_flow.py`

**Interfaces:**
- Consumes: Task 3 `can_assemble`, Task 1 helpers.
- Produces: `AssembleSession(owner, character: OnchainNft, body_value, body_class, chosen)` (edition/`live_editions` removed — `edition` becomes a property off `character.nft_number`); `run_assemble` composes → `char_modify_fn` → closet debit; NO mint/offer/accept. `results` keeps `{nft_id, image_url, video_url, metadata_url, accept: None}` shape so status handlers stay compatible (accept is always `None` now).

- [ ] **Step 1: Failing tests:**
  - happy path: `char_modify_fn(rec.nft_id, owner, meta_url)` called; closet debited by the 8 chosen values + `("Body", body_value)`; no `char_mint_fn`/`char_offer_fn` calls; no `supply_changes` rows; DONE.
  - precheck rejects a non-blank target and a missing Body asset (messages from Task 3 surface as `cannot assemble: …`).
  - closet ledger-fail after modify: character modified **back to the blank URI** (the pre-modify `_raw_uri(rec.uri_hex)`), journal `reverted_modify`, closet untouched.
  - `ClosetMirrorError`: DONE `complete_pending_mirror`, no revert.
  - `ClosetIndeterminateError`: FAILED `assemble_sync_indeterminate`, no revert.
- [ ] **Step 2: Run — FAIL.**
- [ ] **Step 3: Implement** — mirror `run_equip`'s structure exactly (compose → modify → debit → phase-aware handlers → revert-on-ledger-fail using `_raw_uri(rec.uri_hex)`), with the debit:

```python
assets = _owner_contents(conn, owner)
chk = te.can_assemble(rec, session.body_value, session.chosen, assets,
                      mutable=bool(rec.mutable))
...
attrs = _character_attributes(session.body_value, session.chosen)
image_url, video_url, meta_url = await deps.char_compose_fn(
    attrs, session.body_class, session.edition, 0)
...
modify_hash = await deps.char_modify_fn(rec.nft_id, owner, meta_url)
...
assets[("Body", session.body_value)] = assets.get(("Body", session.body_value), 0) - 1
for slot in te.NON_BODY_SLOTS:
    key = (slot, session.chosen[slot])
    assets[key] = assets.get(key, 0) - 1
session.sync_tx_hash = await _sync_then_persist(deps, owner, assets)
```

Journal statuses: `assembling` → `modified` → `complete` / `complete_pending_mirror` / `reverted_modify` / `failed_revert` / `assemble_sync_indeterminate` (document in the module docstring status table).
- [ ] **Step 4: Run — PASS** full suite.
- [ ] **Step 5: Commit** — `feat(economy): assemble dresses a blank in place via NFTokenModify`.

### Task 7: Migration script — closet bodies → Body assets

**Files:**
- Create: `scripts/migrate_closet_bodies_to_values.py`
- Test: `tests/test_migrate_closet_bodies.py` (create)

**Interfaces:**
- Consumes: Task 2 store shape, `economy_store.read_closet_bodies` / `read_closet_assets` / `set_closet_contents`, `closet_token.sync_closet`, frozen genesis.

- [ ] **Step 1: Failing test** — build an in-memory economy schema with a genesis (`3 -> ("Milady", "milady")`), a closet holding body edition 3 and asset `("Head", "Cap", 1)`; run `migrate_owner(conn, owner, sync_fn)` (the script's core, injectable sync); assert closet_assets now has `("Body", "Milady", 1)` + the Cap, `read_closet_bodies` returns `[]`, `sync_fn` was called once with the merged asset list and `bodies=[]`, and a second run is a no-op (idempotent).
- [ ] **Step 2: Run — FAIL.**
- [ ] **Step 3: Implement** — per owner with legacy body rows: convert editions → body values via `effective_genesis(...).edition_bodies`, merge into assets, chain-first (`sync_closet` with the new contents) then `set_closet_contents`; `--network testnet|mainnet` + optional `--owner rXXX`; unknown editions logged and left in place (never silently dropped); CLI main mirrors `scripts/migrate_bucket_to_closet.py`'s structure.
- [ ] **Step 4: Run — PASS.**
- [ ] **Step 5: Commit** — `feat(economy): closet bodies→values migration script`.

### Task 8: Service wiring — deps, API signatures, blank art

**Files:**
- Modify: `webapp/economy_api.py` (`start_harvest`/`start_assemble`/economy state; delete `assemble_prefill`), `lfg_service/app.py` (deps builder `build_economy_deps`, `handle_assemble_start`, remove prefill route/handler), `webapp/mock_economy.py`, `lfg_core/config.py` (`BLANK_IMAGE_URL` env, default `f"https://{BUNNY_PULL_ZONE}/blank/silhouette.png"`)
- Create: `scripts/upload_blank_art.py` (one-off: upload a provided 1080×1080 silhouette PNG to the CDN path above; idempotent)
- Test: `webapp/test_economy_api.py` (rework assemble tests, delete prefill tests), `webapp/test_smoke.py` route updates

**Interfaces:**
- Produces:
  - `blank_meta_fn(edition)` implemented next to the existing compose/upload plumbing: builds `{name: f"... #{edition}", image: config.BLANK_IMAGE_URL, attributes: te.blank_attributes()}` (copy the metadata envelope the normal compose path uploads — same collection fields), uploads via the existing metadata-upload helper, returns the URL.
  - `start_assemble(discord_id, owner, nft_id, body, chosen, user_token=None)` — loads the caller's character by `nft_id` (reuse `_load_owned_character`), resolves `body_class` via `te.body_class_map(effective_genesis)[body]` (unknown body → `EconomyError`), builds the new `AssembleSession`.
  - `POST /api/assemble` body: `{"nft_id": str, "body": str, "chosen": {slot: value}}`.
  - `GET /api/economy` characters gain `"blank": te.is_blank(rec)`; closet payload keeps `assets` (now including Body rows) and drops `bodies`.
  - Mock economy mirrors all of it (`mock_economy.INSTANCE.assemble(wallet, nft_id, body, chosen)`, blank flags, no prefill).
- [ ] **Step 1: Failing tests** — API-level: assemble start with a valid blank succeeds (mocked deps), with a dressed target 4xx `cannot assemble: character is not blank — harvest it first`; economy state carries `blank` flags and Body assets; prefill route returns 404 (removed from smoke's route table).
- [ ] **Step 2: Run — FAIL.**
- [ ] **Step 3: Implement** (incl. deleting `assemble_prefill` + its handler/route, updating the Telegram/Discord announce lines produced in `lfg_service/app.py`'s economy status handler: harvest → "stripped a character down to a blank", assemble → "dressed a blank into #N").
- [ ] **Step 4: Run — PASS**: `.venv/bin/pytest webapp/ tests/ -q`.
- [ ] **Step 5: Commit** — `feat(service): blank-model harvest/assemble API + blank art plumbing`.

### Task 9: Listener + auditor sweep, Phase A close-out

**Files:**
- Modify: `lfg_core/nft_listener.py` (`apply_economy_tx`: closet rebuild passes genesis into `parse_closet_metadata` so legacy metadata converts on rebuild; character Modify needs no new economy writes — verify with a test), `scripts/audit_trait_economy.py` (final call-site pass), `CLAUDE.md` (economy section: blank model summary)
- Test: `tests/test_nft_listener*.py` additions

- [ ] **Step 1: Failing test** — listener closet-rebuild on a legacy-schema Closet token (integer `bodies`) lands Body-value asset rows in the mirror; a character modify-to-blank tx leaves closet/trait tables untouched.
- [ ] **Step 2: Run — FAIL.**
- [ ] **Step 3: Implement**, then run the auditor against a synthetic mixed history (pre-model burns/mints + post-model neutral ops) in a test to prove `ok`.
- [ ] **Step 4: Full gate:** `.venv/bin/pytest -q` + `ruff check . && ruff format --check .` → all green.
- [ ] **Step 5: Commit + open Phase A PR** — `feat(economy): blank-harvest modify-in-place model (Phase A)`; PR body links the spec; note the fire-and-forget-harvest branch interplay.

---

## Phase B — Activity builder UI

### Task 10: `GET /api/assemble/options`

**Files:**
- Modify: `webapp/economy_api.py`, `lfg_service/app.py` (route + handler), `webapp/mock_economy.py`
- Test: `webapp/test_economy_api.py`

**Interfaces:**
- Produces:

```python
async def assemble_options(conn, owner) -> dict:
    # {"blanks":  [{"nft_id": ..., "edition": ...}],
    #  "bodies":  ["Milady", ...],            # closet Body assets, count > 0
    #  "slots":   te.NON_BODY_SLOTS,
    #  "options": {"milady": {"Head": ["Cap", "None"], ...}, ...}}
```

Gates on active Closet (same as old prefill). `blanks` = caller-owned, live, mutable, `is_blank` characters from the index. `options` computed only for body classes of held bodies; per slot, closet assets (count > 0) passing `await swap_compose.resolve_layer(store, cfg, body_class, slot, value)` (value `"None"` always passes) — same filter the old prefill used.
- [ ] **Step 1: Failing tests** — affinity filtering (a female-only value absent under `skeleton`, present under `female`); blanks exclude dressed and non-mutable characters; no-closet → `EconomyError`; empty closet → empty bodies/options (not an error — the UI explains).
- [ ] **Step 2: Run — FAIL.**
- [ ] **Step 3: Implement** (+ route `GET /api/assemble/options` behind `require_wallet`, + mock parity for `WEBAPP_DEV_MODE=1`).
- [ ] **Step 4: Run — PASS.**
- [ ] **Step 5: Commit** — `feat(service): assemble options endpoint for the builder UI`.

### Task 11: Pure client helpers

**Files:**
- Modify: `webapp/client/build_pure.js`
- Test: wherever `build_pure` tests live today (grep `build_pure` under `webapp/` — extend that file in the same style)

**Interfaces:**
- Produces:

```js
// First legal value per slot from an options map — mirrors the server's old
// first-match prefill so one-tap assemble still works.
export function defaultChosen(slots, slotOptions) {
  const chosen = {};
  for (const s of slots) {
    const vals = slotOptions[s] || [];
    if (vals.length) chosen[s] = vals[0];
  }
  return chosen;
}

// Slots with no legal closet asset for this body (blocks the commit button).
export function missingSlots(slots, slotOptions) {
  return slots.filter((s) => !(slotOptions[s] || []).length);
}
```

- [ ] **Step 1: Failing tests** — defaultChosen picks first values and skips empty slots; missingSlots lists exactly the empty ones.
- [ ] **Step 2: Run — FAIL** (run the existing JS/pure test command used by the repo — grep how `build_pure` is tested; if via pytest smoke, follow that pattern).
- [ ] **Step 3–4: Implement, PASS.**
- [ ] **Step 5: Commit** — `feat(client): pure helpers for the assemble builder`.

### Task 12: Builder overlay + harvest copy + roster silhouettes

**Files:**
- Modify: `webapp/client/app.js` (`openAssemble` rework, roster/canvas blank rendering, harvest confirm copy), `webapp/client/index.html` (builder overlay markup), `webapp/client/style.css`
- Test: `webapp/test_smoke.py` (served-asset checks if present), manual dev-mode pass

**Interfaces:**
- Consumes: Task 10 endpoint, Task 11 helpers, existing `layerSrc`/`layerMediaEl`/`confirmDialog`/`showFlow`/`commitAssemble` plumbing.

- [ ] **Step 1: Rework `openAssemble()`:**

```js
async function openAssemble() {
  let opts;
  try { opts = await api('/api/assemble/options'); }
  catch (e) { showError(e.message); return; }
  if (!opts.blanks.length) {
    showError('No blank characters to assemble — harvest a character first.');
    return;
  }
  if (!opts.bodies.length) {
    showError('Your Closet has no bodies — harvest a character first.');
    return;
  }
  openBuilder(opts);
}
```

`openBuilder(opts)` drives a three-step overlay (`#builder-overlay` in `index.html`): blank tiles (silhouette + `#edition`, auto-select when one) → body tiles → per-slot `<select>`s (options from `opts.options[bodyClass]`, defaults via `buildPure.defaultChosen`) beside a live stacked preview (body layer + each chosen slot via `layerMediaEl(layerSrc(bodyClass, slot, value))`). `missingSlots(...)` renders an inline warning and disables Assemble. The Assemble button calls the existing `commitAssemble` reworked to `POST {nft_id, body, chosen}`; success `showFlow` drops the QR/accept branch (nothing to sign) and shows the new art with `celebrate: true`.

The body value → layer-dir class mapping the preview needs: have Task 10 return `options` keyed by body **value** with a parallel `"body_class": {"Milady": "milady"}` map (adjust Task 10's payload accordingly — one source of truth, the client never guesses).

- [ ] **Step 2: Blank rendering** — wherever the roster/canvas draws a character (`renderCanvas`, roster tiles), if `c.blank` render the shared silhouette (`/assets/` copy of the blank art or the CDN `image` URL already in its metadata — use the metadata image, zero new client config) and a "Blank — build me!" caption; `pickDefaultCharacter` prefers dressed characters.
- [ ] **Step 3: Harvest copy** — in `harvestActive()`: mutable char → title "Strip this character down?", text `This strips #N to a blank. Its parts go to your Closet; the NFT stays in your wallet.`, confirm "🧺 Harvest"; legacy (non-mutable) → keep the burn warning, text `#N predates Dynamic NFTs: harvesting burns and re-mints it as a blank (one Xaman accept), then its parts go to your Closet.` (the `burnable` flag is already on the character payload — verify; expose if not).
- [ ] **Step 4: Cache-busters** — bump `?v=` for `app.js`, `build_pure.js`, `style.css` in `index.html` AND any ES-module import of `build_pure.js` inside `app.js`.
- [ ] **Step 5: Verify** — `WEBAPP_DEV_MODE=1` local run: full builder flow against the mock (blank pick → body pick → trait picks → assemble → celebrate screen); `.venv/bin/pytest webapp/ -q`.
- [ ] **Step 6: Commit** — `feat(activity): assemble builder overlay + blank rendering + harvest copy`.

### Task 13: Phase B close-out

- [ ] **Step 1:** Full gate: `.venv/bin/pytest -q`, `ruff check .`, `ruff format --check .` → green.
- [ ] **Step 2:** Open Phase B PR — `feat(activity): assemble builder UI (Phase B)`, body links spec + Phase A PR.
- [ ] **Step 3:** Ops notes in the PR body: run `scripts/upload_blank_art.py` (needs the silhouette PNG from the user — 1080×1080, flag it explicitly).

  **Closet-bodies migration ordering — run it BEFORE the new code serves traffic, NOT after.** The Phase A flows wipe `closet_bodies` (both the DB rows and the on-chain Closet token's `bodies` list) on a user's first economy op, so once the new code is live any un-migrated legacy body edition is lost. The correct deploy sequence per network is:
  1. Stop economy traffic (take the economy stack down, or use a deploy window where no harvest/assemble/equip/extract/deposit can run).
  2. Run the migration on each network: `scripts/migrate_closet_bodies_to_values.py --network testnet` then `--network mainnet`.
  3. Verify with `scripts/audit_trait_economy.py --network testnet|mainnet` (conservation OK).
  4. Only then restart with the new Phase A code.

---

## Self-review notes

- Spec coverage: harvest modify path (T5), legacy upgrade (T5), assemble in place (T6), bodies-by-value + migration (T2/T7), census/audit (T4), listener (T9), options endpoint (T10), builder UI (T12), announce copy (T8), blank art (T8), prefill removal (T8). Marketplace/equip/extract/deposit untouched per spec.
- The `record_supply_change` / `OnchainNft` signatures are used descriptively — implementers MUST read the real signatures first and adapt kwargs (flagged inline).
- Rebase note: if `claude/harvesting-mechanism-perf-a484ee` merges first, re-apply T5 onto its `run_harvest` and keep its `_economy_post` policy intact.
