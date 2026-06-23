# Dress-Up Trait Economy Foundation (Phase 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the pure accounting model, index-DB schema, genesis-freeze tool, and conservation/completeness auditor for the NFT dress-up trait economy — no on-ledger writes, no UI.

**Architecture:** A pure, I/O-free core (`lfg_core/trait_economy.py`) defines the asset model and the two invariants (completeness, conservation). A thin DB layer (`lfg_core/economy_store.py`) persists a frozen genesis baseline and the (initially empty) live-state tables in the existing per-network `onchain_{network}.db`. Two CLI scripts wrap it: `freeze_genesis.py` (reconcile today's chain → freeze genesis + report) and `audit_trait_economy.py` (verify invariants + report). This mirrors the existing `nft_index.py` / `audit_collection_integrity.py` split.

**Tech Stack:** Python 3.10, sqlite3 (stdlib), pytest. Reuses `lfg_core/swap_meta.py` (`TRAIT_ORDER`, `get_attr`, `detect_body`) and `lfg_core/nft_index.py` (`OnchainNft`, `live_nfts`, `init_db`, `index_db_path`).

## Global Constraints

- **Python version floor:** 3.10 (ruff `target-version = py310`, mypy `python_version = 3.10`).
- **Lint:** ruff, line-length 100, rules `E,W,F,I,UP,B,C4` (`E501` ignored — formatter enforces width).
- **Types:** `lfg_core/` is under **mypy strict** — every function in `trait_economy.py` and `economy_store.py` needs full annotations and no leaked `Any`. `scripts/` is excluded from mypy; `tests/` ignore errors.
- **No XRPL writes anywhere in Phase 1.** No `SourceTag` concerns (no transactions are built).
- **Test file header (verbatim, every new test file):** the env-`setdefault` block + `sys.path.insert` used by existing tests (see Task 1 Step 1) — importing `lfg_core` triggers `config` which requires these env vars.
- **The 9 slots come from `swap_meta.TRAIT_ORDER`**; the 8 non-body slots are all of them except `"Body"`. Never hardcode the list separately — derive it.
- **Pre-commit gate:** ruff + mypy(strict) + pytest + gitleaks must pass before each commit. Run `.venv/bin/pre-commit run --files <changed files>` (or the individual tools) before committing.
- **Dedupe rule (verbatim from spec):** for editions with >1 live token, keep one — **prefer the mutable token, tie-break by highest `ledger_index` (newest)**.
- **`"None"` is a real, slot-typed, conserved asset.** A missing non-body slot value is recorded as the asset `(slot, "None")`, counted like any other.
- **Bodies are identity-bound:** tracked per edition as `(body_value, body_class)`; never pooled.

---

## File Structure

- **Create** `lfg_core/trait_economy.py` — pure core: constants, `Genesis`/`Census`/`ConservationReport`/`CompletenessReport` dataclasses, `dedupe_editions`, `build_genesis`, `asset_census`, `verify_conservation`, `verify_completeness`, plus `slot_value` helper. No I/O.
- **Create** `lfg_core/economy_store.py` — DB layer over the same `onchain_{network}.db`: schema (`trait_genesis`, `edition_bodies`, `genesis_meta`, `bucket_assets`, `bucket_bodies`, `trait_tokens`), `freeze_genesis`, `read_genesis`, `read_bucket_assets`/`read_bucket_bodies`/`read_trait_tokens`, `genesis_exists`, `read_meta`.
- **Create** `scripts/freeze_genesis.py` — CLI: reconcile live index, write reconciliation report, freeze genesis. Holds pure `format_reconciliation_report`.
- **Create** `scripts/audit_trait_economy.py` — CLI: verify invariants, write report, nonzero exit on drift. Holds pure `format_economy_report`.
- **Create** `tests/test_trait_economy.py`, `tests/test_economy_store.py`, `tests/test_freeze_genesis.py`, `tests/test_audit_trait_economy.py`.

---

### Task 1: Core constants + `slot_value` + `dedupe_editions`

**Files:**
- Create: `lfg_core/trait_economy.py`
- Test: `tests/test_trait_economy.py`

**Interfaces:**
- Consumes: `lfg_core.swap_meta.TRAIT_ORDER`, `swap_meta.get_attr`; `lfg_core.nft_index.OnchainNft`.
- Produces:
  - `NON_BODY_SLOTS: list[str]`
  - `slot_value(rec: OnchainNft, slot: str) -> str`
  - `dedupe_editions(records: list[OnchainNft], max_edition: int) -> tuple[dict[int, OnchainNft], dict[str, Any]]` — returns `(canonical edition→record, reconciliation)`, where `reconciliation` has keys `duplicates: dict[int, list[str]]`, `missing: list[int]`, `out_of_range: list[str]`, `unparsed: list[str]`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_trait_economy.py`:

```python
# Tests for lfg_core/trait_economy.py (pure trait-economy accounting).
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DISCORD_BOT_TOKEN", "x")
os.environ.setdefault("XUMM_API_KEY", "x")
os.environ.setdefault("XUMM_API_SECRET", "x")
os.environ.setdefault("BUNNY_CDN_ACCESS_KEY", "x")
os.environ.setdefault("BUNNY_CDN_STORAGE_ZONE", "x")
os.environ.setdefault("SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2")
os.environ.setdefault("TOKEN_ISSUER_ADDRESS", "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
os.environ.setdefault("TOKEN_CURRENCY_HEX", "4C46474F00000000000000000000000000000000")
os.environ.setdefault("XRPL_NETWORK", "testnet")
os.environ.setdefault("BUNNY_PULL_ZONE", "nft.pullzone.example")

from lfg_core import nft_index, trait_economy  # noqa: E402


def _attrs(body="Straight", **slots):
    out = [{"trait_type": "Body", "value": body}]
    for slot, value in slots.items():
        out.append({"trait_type": slot, "value": value})
    return out


def _nft(nft_id, number, *, mutable=True, ledger=1, body_class="male", attrs=None):
    return nft_index.OnchainNft(
        nft_id=nft_id,
        nft_number=number,
        owner="rOwner",
        is_burned=False,
        mutable=mutable,
        uri_hex="6868",
        body=body_class,
        attributes=attrs if attrs is not None else _attrs(),
        image="",
        ledger_index=ledger,
    )


def test_non_body_slots_excludes_body():
    assert "Body" not in trait_economy.NON_BODY_SLOTS
    assert len(trait_economy.NON_BODY_SLOTS) == 8
    assert "Background" in trait_economy.NON_BODY_SLOTS


def test_slot_value_defaults_to_none():
    rec = _nft("A", 1, attrs=_attrs(Background="Sky"))
    assert trait_economy.slot_value(rec, "Background") == "Sky"
    assert trait_economy.slot_value(rec, "Head") == "None"


def test_dedupe_prefers_mutable_then_newest_ledger():
    a = _nft("imm-old", 5, mutable=False, ledger=10)
    b = _nft("mut-old", 5, mutable=True, ledger=20)
    c = _nft("mut-new", 5, mutable=True, ledger=99)
    canonical, recon = trait_economy.dedupe_editions([a, b, c], max_edition=10)
    assert canonical[5].nft_id == "mut-new"
    assert recon["duplicates"][5] == ["mut-old", "imm-old"]


def test_dedupe_classifies_missing_unparsed_out_of_range():
    good = _nft("g", 2)
    unparsed = _nft("u", None)
    oor = _nft("o", 9999)
    canonical, recon = trait_economy.dedupe_editions([good, unparsed, oor], max_edition=3)
    assert set(canonical) == {2}
    assert recon["missing"] == [1, 3]
    assert recon["unparsed"] == ["u"]
    assert recon["out_of_range"] == ["o"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_trait_economy.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'lfg_core.trait_economy'` (or `AttributeError`).

- [ ] **Step 3: Write minimal implementation**

Create `lfg_core/trait_economy.py`:

```python
# lfg_core/trait_economy.py
# Pure accounting core for the NFT dress-up trait economy. No I/O.
# An "asset" is a (slot, value) pair over the 9 TRAIT_ORDER slots. The Body slot
# is identity-bound (one body == one edition); the 8 non-body slots are pooled
# and counted by (slot, value), where "None" is itself a real, conserved asset.

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from lfg_core import swap_meta
from lfg_core.nft_index import OnchainNft

NON_BODY_SLOTS: list[str] = [s for s in swap_meta.TRAIT_ORDER if s != "Body"]


def slot_value(rec: OnchainNft, slot: str) -> str:
    """The asset value held in `slot` for this character; absent -> "None"."""
    return swap_meta.get_attr(rec.attributes, slot) or "None"


def dedupe_editions(
    records: list[OnchainNft], max_edition: int
) -> tuple[dict[int, OnchainNft], dict[str, Any]]:
    """Reduce live records to one canonical token per edition.

    Dedupe rule: prefer the mutable token, tie-break by highest ledger_index.
    Returns (canonical edition->record, reconciliation) where reconciliation has
    duplicates: {edition: [dropped nft_id]}, missing: [edition], out_of_range:
    [nft_id], unparsed: [nft_id].
    """
    by_edition: dict[int, list[OnchainNft]] = defaultdict(list)
    unparsed: list[str] = []
    out_of_range: list[str] = []
    for r in records:
        if r.nft_number is None:
            unparsed.append(r.nft_id)
        elif 1 <= r.nft_number <= max_edition:
            by_edition[r.nft_number].append(r)
        else:
            out_of_range.append(r.nft_id)

    canonical: dict[int, OnchainNft] = {}
    duplicates: dict[int, list[str]] = {}
    for edition, recs in by_edition.items():
        ordered = sorted(
            recs,
            key=lambda r: (1 if r.mutable else 0, r.ledger_index or 0),
            reverse=True,
        )
        canonical[edition] = ordered[0]
        if len(ordered) > 1:
            duplicates[edition] = [r.nft_id for r in ordered[1:]]

    missing = [n for n in range(1, max_edition + 1) if n not in canonical]
    reconciliation: dict[str, Any] = {
        "duplicates": duplicates,
        "missing": missing,
        "out_of_range": out_of_range,
        "unparsed": unparsed,
    }
    return canonical, reconciliation
```

(`Counter` is imported now; later tasks use it.)

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_trait_economy.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add lfg_core/trait_economy.py tests/test_trait_economy.py
git commit -m "feat: trait-economy core constants + edition dedupe"
```

---

### Task 2: `build_genesis` + `Genesis` dataclass

**Files:**
- Modify: `lfg_core/trait_economy.py`
- Test: `tests/test_trait_economy.py`

**Interfaces:**
- Consumes: `dedupe_editions` output (`dict[int, OnchainNft]`), `slot_value`, `NON_BODY_SLOTS`, `swap_meta.get_attr`.
- Produces:
  - `Genesis` dataclass: `trait_counts: dict[tuple[str, str], int]`, `edition_bodies: dict[int, tuple[str, str]]` (`edition -> (body_value, body_class)`).
  - `build_genesis(canonical: dict[int, OnchainNft]) -> Genesis`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_trait_economy.py`:

```python
def test_build_genesis_counts_traits_and_bodies():
    a = _nft("a", 1, body_class="male", attrs=_attrs(body="Straight", Background="Sky", Head="Crown"))
    b = _nft("b", 2, body_class="male", attrs=_attrs(body="Straight", Background="Sky"))
    g = trait_economy.build_genesis({1: a, 2: b})
    # Background:Sky appears on both editions.
    assert g.trait_counts[("Background", "Sky")] == 2
    # Head:Crown only on edition 1; edition 2's Head is absent -> ("Head","None").
    assert g.trait_counts[("Head", "Crown")] == 1
    assert g.trait_counts[("Head", "None")] == 1
    # Bodies are identity-bound per edition.
    assert g.edition_bodies[1] == ("Straight", "male")
    assert g.edition_bodies[2] == ("Straight", "male")
    # Body is never a non-body trait key.
    assert not any(slot == "Body" for slot, _ in g.trait_counts)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_trait_economy.py::test_build_genesis_counts_traits_and_bodies -v`
Expected: FAIL with `AttributeError: module 'lfg_core.trait_economy' has no attribute 'build_genesis'`.

- [ ] **Step 3: Write minimal implementation**

Add a `dataclass` import and the following to `lfg_core/trait_economy.py` (add `from dataclasses import dataclass` near the top imports):

```python
@dataclass
class Genesis:
    trait_counts: dict[tuple[str, str], int]
    edition_bodies: dict[int, tuple[str, str]]


def build_genesis(canonical: dict[int, OnchainNft]) -> Genesis:
    """Freeze the canonical reconciled editions into a conservation baseline:
    per non-body (slot, value) counts (incl. "None") and the per-edition body."""
    trait_counts: Counter[tuple[str, str]] = Counter()
    edition_bodies: dict[int, tuple[str, str]] = {}
    for edition, rec in canonical.items():
        body_value = swap_meta.get_attr(rec.attributes, "Body") or ""
        edition_bodies[edition] = (body_value, rec.body)
        for slot in NON_BODY_SLOTS:
            trait_counts[(slot, slot_value(rec, slot))] += 1
    return Genesis(trait_counts=dict(trait_counts), edition_bodies=edition_bodies)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_trait_economy.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add lfg_core/trait_economy.py tests/test_trait_economy.py
git commit -m "feat: build_genesis trait/body baseline"
```

---

### Task 3: `asset_census` + `Census` dataclass

**Files:**
- Modify: `lfg_core/trait_economy.py`
- Test: `tests/test_trait_economy.py`

**Interfaces:**
- Consumes: `slot_value`, `NON_BODY_SLOTS`.
- Produces:
  - `Census` dataclass: `trait_counts: dict[tuple[str, str], int]`, `body_presence: dict[int, int]`.
  - `asset_census(characters: dict[int, OnchainNft], bucket_assets: list[tuple[str, str, str, int]], bucket_bodies: list[tuple[str, int]], trait_tokens: list[tuple[str, str, str, str]]) -> Census`.
    - `bucket_assets` rows are `(owner, slot, value, count)`; `bucket_bodies` rows are `(owner, edition)`; `trait_tokens` rows are `(nft_id, owner, slot, value)`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_trait_economy.py`:

```python
def test_asset_census_sums_chars_buckets_and_tokens():
    char = _nft("c", 1, attrs=_attrs(Background="Sky"))
    census = trait_economy.asset_census(
        characters={1: char},
        bucket_assets=[("rA", "Background", "Sky", 2), ("rA", "Head", "None", 1)],
        bucket_bodies=[("rA", 7)],
        trait_tokens=[("tok1", "rB", "Background", "Sky")],
    )
    # 1 on the live character + 2 in a bucket + 1 standalone token.
    assert census.trait_counts[("Background", "Sky")] == 4
    assert census.trait_counts[("Head", "None")] == 1 + 1  # char's empty Head + bucket
    # Body presence: edition 1 live, edition 7 loose in a bucket.
    assert census.body_presence == {1: 1, 7: 1}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_trait_economy.py::test_asset_census_sums_chars_buckets_and_tokens -v`
Expected: FAIL with `AttributeError: ... 'asset_census'`.

- [ ] **Step 3: Write minimal implementation**

Add to `lfg_core/trait_economy.py`:

```python
@dataclass
class Census:
    trait_counts: dict[tuple[str, str], int]
    body_presence: dict[int, int]


def asset_census(
    characters: dict[int, OnchainNft],
    bucket_assets: list[tuple[str, str, str, int]],
    bucket_bodies: list[tuple[str, int]],
    trait_tokens: list[tuple[str, str, str, str]],
) -> Census:
    """Tally every asset across live characters, Buckets and standalone trait
    tokens. trait_counts are non-body (slot, value); body_presence counts how
    many places each edition's body currently exists (should be exactly 1)."""
    trait_counts: Counter[tuple[str, str]] = Counter()
    body_presence: Counter[int] = Counter()
    for edition, rec in characters.items():
        body_presence[edition] += 1
        for slot in NON_BODY_SLOTS:
            trait_counts[(slot, slot_value(rec, slot))] += 1
    for _owner, slot, value, count in bucket_assets:
        trait_counts[(slot, value)] += count
    for _nft_id, _owner, slot, value in trait_tokens:
        trait_counts[(slot, value)] += 1
    for _owner, edition in bucket_bodies:
        body_presence[edition] += 1
    return Census(trait_counts=dict(trait_counts), body_presence=dict(body_presence))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_trait_economy.py -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add lfg_core/trait_economy.py tests/test_trait_economy.py
git commit -m "feat: asset_census across characters, buckets, tokens"
```

---

### Task 4: `verify_conservation`

**Files:**
- Modify: `lfg_core/trait_economy.py`
- Test: `tests/test_trait_economy.py`

**Interfaces:**
- Consumes: `Genesis`, `Census`.
- Produces:
  - `ConservationReport` dataclass: `trait_drift: dict[tuple[str, str], int]` (census − genesis, nonzero only), `body_drift: dict[int, int]` (edition → presence when ≠ 1, plus editions present in census but absent from genesis), `ok: bool`.
  - `verify_conservation(genesis: Genesis, census: Census) -> ConservationReport`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_trait_economy.py`:

```python
def test_verify_conservation_ok_when_census_matches_genesis():
    g = trait_economy.Genesis(trait_counts={("Background", "Sky"): 2}, edition_bodies={1: ("S", "male")})
    c = trait_economy.Census(trait_counts={("Background", "Sky"): 2}, body_presence={1: 1})
    rep = trait_economy.verify_conservation(g, c)
    assert rep.ok
    assert rep.trait_drift == {}
    assert rep.body_drift == {}


def test_verify_conservation_flags_trait_and_body_drift():
    g = trait_economy.Genesis(
        trait_counts={("Background", "Sky"): 2, ("Head", "Crown"): 1},
        edition_bodies={1: ("S", "male"), 2: ("S", "male")},
    )
    c = trait_economy.Census(
        trait_counts={("Background", "Sky"): 3},  # +1 created; Crown destroyed
        body_presence={1: 2},  # edition 1 duplicated, edition 2 vanished
    )
    rep = trait_economy.verify_conservation(g, c)
    assert not rep.ok
    assert rep.trait_drift[("Background", "Sky")] == 1
    assert rep.trait_drift[("Head", "Crown")] == -1
    assert rep.body_drift[1] == 2
    assert rep.body_drift[2] == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_trait_economy.py -k verify_conservation -v`
Expected: FAIL with `AttributeError: ... 'verify_conservation'`.

- [ ] **Step 3: Write minimal implementation**

Add to `lfg_core/trait_economy.py`:

```python
@dataclass
class ConservationReport:
    trait_drift: dict[tuple[str, str], int]
    body_drift: dict[int, int]
    ok: bool


def verify_conservation(genesis: Genesis, census: Census) -> ConservationReport:
    """Conservation: census must equal genesis for every (slot, value), and each
    genesis edition's body must exist in exactly one place. Drift is reported."""
    trait_drift: dict[tuple[str, str], int] = {}
    for key in set(genesis.trait_counts) | set(census.trait_counts):
        delta = census.trait_counts.get(key, 0) - genesis.trait_counts.get(key, 0)
        if delta != 0:
            trait_drift[key] = delta

    body_drift: dict[int, int] = {}
    for edition in genesis.edition_bodies:
        presence = census.body_presence.get(edition, 0)
        if presence != 1:
            body_drift[edition] = presence
    for edition, presence in census.body_presence.items():
        if edition not in genesis.edition_bodies:
            body_drift[edition] = presence

    ok = not trait_drift and not body_drift
    return ConservationReport(trait_drift=trait_drift, body_drift=body_drift, ok=ok)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_trait_economy.py -v`
Expected: PASS (8 tests).

- [ ] **Step 5: Commit**

```bash
git add lfg_core/trait_economy.py tests/test_trait_economy.py
git commit -m "feat: verify_conservation drift detection"
```

---

### Task 5: `verify_completeness`

**Files:**
- Modify: `lfg_core/trait_economy.py`
- Test: `tests/test_trait_economy.py`

**Interfaces:**
- Consumes: `Genesis`, `NON_BODY_SLOTS`, `swap_meta.get_attr`.
- Produces:
  - `CompletenessReport` dataclass: `wrong_body: dict[int, tuple[str, str]]` (`edition -> (found_body_value, expected_body_value)`), `orphan_bodies: list[int]` (live editions with no genesis body row), `slot_anomalies: dict[int, list[str]]` (`edition -> slots not present exactly once`), `ok: bool`.
  - `verify_completeness(characters: dict[int, OnchainNft], genesis: Genesis) -> CompletenessReport`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_trait_economy.py`:

```python
def test_verify_completeness_ok_for_normalized_characters():
    a = _nft("a", 1, body_class="male", attrs=_attrs(body="Straight", Background="Sky"))
    g = trait_economy.build_genesis({1: a})
    rep = trait_economy.verify_completeness({1: a}, g)
    assert rep.ok
    assert rep.wrong_body == {}
    assert rep.orphan_bodies == []
    assert rep.slot_anomalies == {}


def test_verify_completeness_flags_wrong_body_and_orphan():
    a = _nft("a", 1, body_class="male", attrs=_attrs(body="Straight"))
    g = trait_economy.build_genesis({1: a})
    # Edition 1 now shows a different body value; edition 9 isn't in genesis.
    mutated = _nft("a2", 1, body_class="male", attrs=_attrs(body="Curved"))
    orphan = _nft("z", 9, attrs=_attrs(body="Straight"))
    rep = trait_economy.verify_completeness({1: mutated, 9: orphan}, g)
    assert not rep.ok
    assert rep.wrong_body[1] == ("Curved", "Straight")
    assert rep.orphan_bodies == [9]


def test_verify_completeness_flags_duplicate_slot():
    dup = _nft(
        "d", 1,
        attrs=[
            {"trait_type": "Body", "value": "Straight"},
            {"trait_type": "Head", "value": "Crown"},
            {"trait_type": "Head", "value": "Hat"},  # Head twice
        ],
    )
    g = trait_economy.Genesis(trait_counts={}, edition_bodies={1: ("Straight", "male")})
    rep = trait_economy.verify_completeness({1: dup}, g)
    assert "Head" in rep.slot_anomalies[1]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_trait_economy.py -k verify_completeness -v`
Expected: FAIL with `AttributeError: ... 'verify_completeness'`.

- [ ] **Step 3: Write minimal implementation**

Add to `lfg_core/trait_economy.py`:

```python
@dataclass
class CompletenessReport:
    wrong_body: dict[int, tuple[str, str]]
    orphan_bodies: list[int]
    slot_anomalies: dict[int, list[str]]
    ok: bool


def verify_completeness(
    characters: dict[int, OnchainNft], genesis: Genesis
) -> CompletenessReport:
    """Completeness: every live character holds exactly one asset per non-body
    slot and its body matches the genesis body ledger."""
    wrong_body: dict[int, tuple[str, str]] = {}
    orphan_bodies: list[int] = []
    slot_anomalies: dict[int, list[str]] = {}

    for edition, rec in characters.items():
        seen: Counter[str] = Counter(
            a["trait_type"] for a in rec.attributes if a.get("trait_type") in NON_BODY_SLOTS
        )
        bad_slots = [s for s in NON_BODY_SLOTS if seen.get(s, 0) != 1]
        if bad_slots:
            slot_anomalies[edition] = bad_slots

        expected = genesis.edition_bodies.get(edition)
        if expected is None:
            orphan_bodies.append(edition)
            continue
        found_body = swap_meta.get_attr(rec.attributes, "Body") or ""
        if found_body != expected[0]:
            wrong_body[edition] = (found_body, expected[0])

    ok = not wrong_body and not orphan_bodies and not slot_anomalies
    return CompletenessReport(
        wrong_body=wrong_body,
        orphan_bodies=sorted(orphan_bodies),
        slot_anomalies=slot_anomalies,
        ok=ok,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_trait_economy.py -v`
Expected: PASS (11 tests).

- [ ] **Step 5: Commit**

```bash
git add lfg_core/trait_economy.py tests/test_trait_economy.py
git commit -m "feat: verify_completeness for live characters"
```

---

### Task 6: `economy_store.py` — schema + genesis freeze/read

**Files:**
- Create: `lfg_core/economy_store.py`
- Test: `tests/test_economy_store.py`

**Interfaces:**
- Consumes: `sqlite3.Connection`; `trait_economy.Genesis`.
- Produces:
  - `init_economy_schema(conn: sqlite3.Connection) -> None`
  - `genesis_exists(conn: sqlite3.Connection) -> bool`
  - `clear_genesis(conn: sqlite3.Connection) -> None`
  - `freeze_genesis(conn: sqlite3.Connection, genesis: trait_economy.Genesis, meta: dict[str, str]) -> None`
  - `read_genesis(conn: sqlite3.Connection) -> trait_economy.Genesis`
  - `read_meta(conn: sqlite3.Connection, key: str) -> str | None`

- [ ] **Step 1: Write the failing test**

Create `tests/test_economy_store.py`:

```python
# Tests for lfg_core/economy_store.py (genesis + live-state persistence).
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DISCORD_BOT_TOKEN", "x")
os.environ.setdefault("XUMM_API_KEY", "x")
os.environ.setdefault("XUMM_API_SECRET", "x")
os.environ.setdefault("BUNNY_CDN_ACCESS_KEY", "x")
os.environ.setdefault("BUNNY_CDN_STORAGE_ZONE", "x")
os.environ.setdefault("SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2")
os.environ.setdefault("TOKEN_ISSUER_ADDRESS", "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
os.environ.setdefault("TOKEN_CURRENCY_HEX", "4C46474F00000000000000000000000000000000")
os.environ.setdefault("XRPL_NETWORK", "testnet")
os.environ.setdefault("BUNNY_PULL_ZONE", "nft.pullzone.example")

from lfg_core import economy_store, trait_economy  # noqa: E402


def _conn():
    conn = sqlite3.connect(":memory:")
    economy_store.init_economy_schema(conn)
    return conn


def test_genesis_round_trips():
    conn = _conn()
    assert economy_store.genesis_exists(conn) is False
    g = trait_economy.Genesis(
        trait_counts={("Background", "Sky"): 2, ("Head", "None"): 1},
        edition_bodies={1: ("Straight", "male"), 2: ("Curved", "female")},
    )
    economy_store.freeze_genesis(conn, g, {"network": "testnet", "max_edition": "3535"})
    assert economy_store.genesis_exists(conn) is True
    got = economy_store.read_genesis(conn)
    assert got.trait_counts == g.trait_counts
    assert got.edition_bodies == g.edition_bodies
    assert economy_store.read_meta(conn, "max_edition") == "3535"
    assert economy_store.read_meta(conn, "absent") is None


def test_clear_genesis_empties_baseline():
    conn = _conn()
    g = trait_economy.Genesis(trait_counts={("Head", "None"): 1}, edition_bodies={1: ("S", "male")})
    economy_store.freeze_genesis(conn, g, {})
    economy_store.clear_genesis(conn)
    assert economy_store.genesis_exists(conn) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_economy_store.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'lfg_core.economy_store'`.

- [ ] **Step 3: Write minimal implementation**

Create `lfg_core/economy_store.py`:

```python
# lfg_core/economy_store.py
# Persistence for the trait economy: the frozen genesis baseline plus the
# (initially empty) live-state tables (Buckets, standalone trait tokens). Lives
# in the same per-network onchain_{network}.db as the nft_index.

from __future__ import annotations

import sqlite3

from lfg_core import trait_economy

_ECONOMY_SCHEMA = """
CREATE TABLE IF NOT EXISTS trait_genesis (
    slot          TEXT,
    value         TEXT,
    genesis_count INTEGER,
    PRIMARY KEY (slot, value)
);
CREATE TABLE IF NOT EXISTS edition_bodies (
    edition    INTEGER PRIMARY KEY,
    body_value TEXT,
    body_class TEXT
);
CREATE TABLE IF NOT EXISTS genesis_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS bucket_assets (
    owner TEXT,
    slot  TEXT,
    value TEXT,
    count INTEGER,
    PRIMARY KEY (owner, slot, value)
);
CREATE TABLE IF NOT EXISTS bucket_bodies (
    owner   TEXT,
    edition INTEGER PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS trait_tokens (
    nft_id TEXT PRIMARY KEY,
    owner  TEXT,
    slot   TEXT,
    value  TEXT
);
"""


def init_economy_schema(conn: sqlite3.Connection) -> None:
    """Create the genesis + live-state tables if absent."""
    conn.executescript(_ECONOMY_SCHEMA)
    conn.commit()


def genesis_exists(conn: sqlite3.Connection) -> bool:
    cur = conn.execute("SELECT 1 FROM trait_genesis LIMIT 1")
    if cur.fetchone() is not None:
        return True
    cur = conn.execute("SELECT 1 FROM edition_bodies LIMIT 1")
    return cur.fetchone() is not None


def clear_genesis(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM trait_genesis")
    conn.execute("DELETE FROM edition_bodies")
    conn.execute("DELETE FROM genesis_meta")
    conn.commit()


def freeze_genesis(
    conn: sqlite3.Connection, genesis: trait_economy.Genesis, meta: dict[str, str]
) -> None:
    """Persist a genesis baseline (replacing any existing one)."""
    clear_genesis(conn)
    conn.executemany(
        "INSERT INTO trait_genesis (slot, value, genesis_count) VALUES (?, ?, ?)",
        [(slot, value, count) for (slot, value), count in genesis.trait_counts.items()],
    )
    conn.executemany(
        "INSERT INTO edition_bodies (edition, body_value, body_class) VALUES (?, ?, ?)",
        [(ed, bv, bc) for ed, (bv, bc) in genesis.edition_bodies.items()],
    )
    conn.executemany(
        "INSERT INTO genesis_meta (key, value) VALUES (?, ?)",
        list(meta.items()),
    )
    conn.commit()


def read_genesis(conn: sqlite3.Connection) -> trait_economy.Genesis:
    trait_counts: dict[tuple[str, str], int] = {
        (slot, value): count
        for slot, value, count in conn.execute(
            "SELECT slot, value, genesis_count FROM trait_genesis"
        )
    }
    edition_bodies: dict[int, tuple[str, str]] = {
        ed: (bv, bc)
        for ed, bv, bc in conn.execute(
            "SELECT edition, body_value, body_class FROM edition_bodies"
        )
    }
    return trait_economy.Genesis(trait_counts=trait_counts, edition_bodies=edition_bodies)


def read_meta(conn: sqlite3.Connection, key: str) -> str | None:
    cur = conn.execute("SELECT value FROM genesis_meta WHERE key = ?", (key,))
    row = cur.fetchone()
    return None if row is None else str(row[0])
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_economy_store.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add lfg_core/economy_store.py tests/test_economy_store.py
git commit -m "feat: economy_store schema + genesis freeze/read"
```

---

### Task 7: `economy_store.py` — live-state readers

**Files:**
- Modify: `lfg_core/economy_store.py`
- Test: `tests/test_economy_store.py`

**Interfaces:**
- Produces:
  - `read_bucket_assets(conn: sqlite3.Connection) -> list[tuple[str, str, str, int]]` — `(owner, slot, value, count)`
  - `read_bucket_bodies(conn: sqlite3.Connection) -> list[tuple[str, int]]` — `(owner, edition)`
  - `read_trait_tokens(conn: sqlite3.Connection) -> list[tuple[str, str, str, str]]` — `(nft_id, owner, slot, value)`
  - (Shapes match `trait_economy.asset_census` parameters exactly.)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_economy_store.py`:

```python
def test_live_state_readers_empty_then_populated():
    conn = _conn()
    assert economy_store.read_bucket_assets(conn) == []
    assert economy_store.read_bucket_bodies(conn) == []
    assert economy_store.read_trait_tokens(conn) == []

    conn.execute(
        "INSERT INTO bucket_assets (owner, slot, value, count) VALUES (?, ?, ?, ?)",
        ("rA", "Background", "Sky", 3),
    )
    conn.execute("INSERT INTO bucket_bodies (owner, edition) VALUES (?, ?)", ("rA", 7))
    conn.execute(
        "INSERT INTO trait_tokens (nft_id, owner, slot, value) VALUES (?, ?, ?, ?)",
        ("tok1", "rB", "Head", "Crown"),
    )
    conn.commit()

    assert economy_store.read_bucket_assets(conn) == [("rA", "Background", "Sky", 3)]
    assert economy_store.read_bucket_bodies(conn) == [("rA", 7)]
    assert economy_store.read_trait_tokens(conn) == [("tok1", "rB", "Head", "Crown")]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_economy_store.py::test_live_state_readers_empty_then_populated -v`
Expected: FAIL with `AttributeError: ... 'read_bucket_assets'`.

- [ ] **Step 3: Write minimal implementation**

Add to `lfg_core/economy_store.py`:

```python
def read_bucket_assets(conn: sqlite3.Connection) -> list[tuple[str, str, str, int]]:
    return [
        (str(owner), str(slot), str(value), int(count))
        for owner, slot, value, count in conn.execute(
            "SELECT owner, slot, value, count FROM bucket_assets"
        )
    ]


def read_bucket_bodies(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    return [
        (str(owner), int(edition))
        for owner, edition in conn.execute("SELECT owner, edition FROM bucket_bodies")
    ]


def read_trait_tokens(conn: sqlite3.Connection) -> list[tuple[str, str, str, str]]:
    return [
        (str(nft_id), str(owner), str(slot), str(value))
        for nft_id, owner, slot, value in conn.execute(
            "SELECT nft_id, owner, slot, value FROM trait_tokens"
        )
    ]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_economy_store.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add lfg_core/economy_store.py tests/test_economy_store.py
git commit -m "feat: economy_store live-state readers"
```

---

### Task 8: `freeze_genesis.py` — reconciliation report + CLI

**Files:**
- Create: `scripts/freeze_genesis.py`
- Test: `tests/test_freeze_genesis.py`

**Interfaces:**
- Consumes: `lfg_core.trait_economy` (`dedupe_editions`, `build_genesis`), `lfg_core.economy_store`, `lfg_core.nft_index` (`index_db_path`, `init_db`, `live_nfts`), `lfg_core.config`.
- Produces (pure, importable for tests):
  - `format_reconciliation_report(reconciliation: dict[str, Any], network: str, max_edition: int, live_count: int, genesis_editions: int, timestamp: str) -> str`
  - `main() -> int` (CLI; not unit-tested directly).

- [ ] **Step 1: Write the failing test**

Create `tests/test_freeze_genesis.py`:

```python
# Tests for scripts/freeze_genesis.py (reconciliation report formatting).
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DISCORD_BOT_TOKEN", "x")
os.environ.setdefault("XUMM_API_KEY", "x")
os.environ.setdefault("XUMM_API_SECRET", "x")
os.environ.setdefault("BUNNY_CDN_ACCESS_KEY", "x")
os.environ.setdefault("BUNNY_CDN_STORAGE_ZONE", "x")
os.environ.setdefault("SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2")
os.environ.setdefault("TOKEN_ISSUER_ADDRESS", "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
os.environ.setdefault("TOKEN_CURRENCY_HEX", "4C46474F00000000000000000000000000000000")
os.environ.setdefault("XRPL_NETWORK", "testnet")
os.environ.setdefault("BUNNY_PULL_ZONE", "nft.pullzone.example")

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
)
import freeze_genesis as fg  # noqa: E402


def test_format_reconciliation_report():
    recon = {
        "duplicates": {1001: ["DUPID"]},
        "missing": [220, 1017],
        "out_of_range": ["OOR"],
        "unparsed": ["UNP"],
    }
    md = fg.format_reconciliation_report(
        recon, "mainnet", 3535, live_count=3537, genesis_editions=3533,
        timestamp="2026-06-22T00-00-00Z",
    )
    assert "Trait Economy Reconciliation (mainnet)" in md
    assert "Genesis editions: **3533**" in md
    assert "Duplicate editions: **1**" in md
    assert "1001" in md and "DUPID" in md
    assert "220, 1017" in md
    assert "OOR" in md
    assert "UNP" in md
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_freeze_genesis.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'freeze_genesis'`.

- [ ] **Step 3: Write minimal implementation**

Create `scripts/freeze_genesis.py`:

```python
#!/usr/bin/env python3
"""Reconcile today's on-chain index into a clean, frozen genesis baseline for the
dress-up trait economy.

Resolves duplicate editions (prefer mutable, newest ledger), excludes missing /
unparsed / out-of-range tokens, freezes the per-(slot,value) trait supply and the
per-edition body ledger into the per-network onchain DB, and writes a Markdown
reconciliation report.

  python scripts/freeze_genesis.py --network mainnet

Refuses to overwrite an existing genesis without --force.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from typing import Any

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)

from lfg_core import config, economy_store, nft_index, trait_economy  # noqa: E402

COLLECTION_SIZE = 3535


def _fmt_ints(nums: list[int]) -> str:
    return ", ".join(str(n) for n in nums) if nums else "—"


def _fmt_strs(items: list[str]) -> str:
    return ", ".join(items) if items else "—"


def format_reconciliation_report(
    reconciliation: dict[str, Any],
    network: str,
    max_edition: int,
    live_count: int,
    genesis_editions: int,
    timestamp: str,
) -> str:
    r = reconciliation
    dupes: dict[int, list[str]] = r["duplicates"]
    lines: list[str] = []
    lines.append(f"# Trait Economy Reconciliation ({network}) — {timestamp}")
    lines.append("")
    lines.append(f"- Expected editions: **1..{max_edition}**")
    lines.append(f"- Live tokens: **{live_count}**")
    lines.append(f"- Genesis editions: **{genesis_editions}**")
    lines.append(f"- Duplicate editions: **{len(dupes)}**")
    lines.append(f"- Missing editions: **{len(r['missing'])}**")
    lines.append(f"- Out-of-range tokens: **{len(r['out_of_range'])}**")
    lines.append(f"- Unparsed-name tokens: **{len(r['unparsed'])}**")
    lines.append("")

    lines.append("## Duplicate editions (kept newest mutable; dropped the rest)")
    lines.append("")
    if dupes:
        lines.append("| Edition | dropped nft_ids |")
        lines.append("| --- | --- |")
        for ed, ids in sorted(dupes.items()):
            lines.append(f"| {ed} | {_fmt_strs(ids)} |")
    else:
        lines.append("_None._")
    lines.append("")

    lines.append("## Missing editions (no live token; excluded from genesis)")
    lines.append("")
    lines.append(_fmt_ints(r["missing"]))
    lines.append("")

    lines.append("## Out-of-range tokens (edition outside range; excluded)")
    lines.append("")
    lines.append(_fmt_strs(r["out_of_range"]))
    lines.append("")

    lines.append("## Unparsed-name tokens (no edition number; excluded)")
    lines.append("")
    lines.append(_fmt_strs(r["unparsed"]))
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Freeze the trait-economy genesis baseline.")
    parser.add_argument("--network", choices=["mainnet", "testnet"], default=config.XRPL_NETWORK)
    parser.add_argument("--max-edition", type=int, default=COLLECTION_SIZE)
    parser.add_argument("--report-dir", default=os.path.join(REPO_ROOT, "reports"))
    parser.add_argument("--force", action="store_true", help="overwrite an existing genesis")
    args = parser.parse_args()

    db_path = nft_index.index_db_path(args.network)
    if not os.path.isfile(db_path):
        print(f"No index DB at {db_path}. Run the backfill / Bithomp import first.")
        return 2

    conn = nft_index.init_db(db_path)
    economy_store.init_economy_schema(conn)
    if economy_store.genesis_exists(conn) and not args.force:
        print("Genesis already frozen. Re-run with --force to overwrite.")
        return 2

    live = nft_index.live_nfts(conn)
    canonical, reconciliation = trait_economy.dedupe_editions(live, args.max_edition)
    genesis = trait_economy.build_genesis(canonical)

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    meta = {
        "network": args.network,
        "max_edition": str(args.max_edition),
        "genesis_editions": str(len(canonical)),
        "frozen_at": timestamp,
    }
    economy_store.freeze_genesis(conn, genesis, meta)

    report = format_reconciliation_report(
        reconciliation, args.network, args.max_edition, len(live), len(canonical), timestamp
    )
    os.makedirs(args.report_dir, exist_ok=True)
    report_path = os.path.join(
        args.report_dir, f"trait-economy-reconciliation-{args.network}-{timestamp}.md"
    )
    with open(report_path, "w") as f:
        f.write(report)

    print(f"Network: {args.network}  live tokens: {len(live)}  genesis editions: {len(canonical)}")
    print(f"Duplicates: {len(reconciliation['duplicates'])}  missing: {len(reconciliation['missing'])}")
    print(f"Genesis frozen in {db_path}")
    print(f"Report: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_freeze_genesis.py -v`
Expected: PASS (1 test).

- [ ] **Step 5: Commit**

```bash
git add scripts/freeze_genesis.py tests/test_freeze_genesis.py
git commit -m "feat: freeze_genesis reconciliation + CLI"
```

---

### Task 9: `audit_trait_economy.py` — economy report + CLI

**Files:**
- Create: `scripts/audit_trait_economy.py`
- Test: `tests/test_audit_trait_economy.py`

**Interfaces:**
- Consumes: `lfg_core.trait_economy` (`ConservationReport`, `CompletenessReport`, `dedupe_editions`, `asset_census`, `verify_conservation`, `verify_completeness`), `lfg_core.economy_store`, `lfg_core.nft_index`, `lfg_core.config`.
- Produces (pure, importable):
  - `format_economy_report(conservation: trait_economy.ConservationReport, completeness: trait_economy.CompletenessReport, network: str, live_count: int, genesis_editions: int, timestamp: str) -> str`
  - `main() -> int` — returns `1` on any drift/violation, `0` when clean, `2` when no genesis/index.

- [ ] **Step 1: Write the failing test**

Create `tests/test_audit_trait_economy.py`:

```python
# Tests for scripts/audit_trait_economy.py (economy report formatting).
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DISCORD_BOT_TOKEN", "x")
os.environ.setdefault("XUMM_API_KEY", "x")
os.environ.setdefault("XUMM_API_SECRET", "x")
os.environ.setdefault("BUNNY_CDN_ACCESS_KEY", "x")
os.environ.setdefault("BUNNY_CDN_STORAGE_ZONE", "x")
os.environ.setdefault("SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2")
os.environ.setdefault("TOKEN_ISSUER_ADDRESS", "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
os.environ.setdefault("TOKEN_CURRENCY_HEX", "4C46474F00000000000000000000000000000000")
os.environ.setdefault("XRPL_NETWORK", "testnet")
os.environ.setdefault("BUNNY_PULL_ZONE", "nft.pullzone.example")

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
)
import audit_trait_economy as ate  # noqa: E402

from lfg_core import trait_economy  # noqa: E402


def test_economy_report_clean():
    cons = trait_economy.ConservationReport(trait_drift={}, body_drift={}, ok=True)
    comp = trait_economy.CompletenessReport(
        wrong_body={}, orphan_bodies=[], slot_anomalies={}, ok=True
    )
    md = ate.format_economy_report(cons, comp, "mainnet", 3533, 3533, "2026-06-22T00-00-00Z")
    assert "Trait Economy Audit (mainnet)" in md
    assert "Conservation: **OK**" in md
    assert "Completeness: **OK**" in md


def test_economy_report_flags_drift():
    cons = trait_economy.ConservationReport(
        trait_drift={("Background", "Sky"): 1, ("Head", "Crown"): -1},
        body_drift={2: 0},
        ok=False,
    )
    comp = trait_economy.CompletenessReport(
        wrong_body={1: ("Curved", "Straight")},
        orphan_bodies=[9],
        slot_anomalies={3: ["Head"]},
        ok=False,
    )
    md = ate.format_economy_report(cons, comp, "mainnet", 100, 100, "2026-06-22T00-00-00Z")
    assert "Conservation: **DRIFT**" in md
    assert "Background" in md and "Sky" in md
    assert "Crown" in md
    assert "| 1 | Curved | Straight |" in md
    assert "9" in md  # orphan body
    assert "Head" in md  # slot anomaly
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_audit_trait_economy.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'audit_trait_economy'`.

- [ ] **Step 3: Write minimal implementation**

Create `scripts/audit_trait_economy.py`:

```python
#!/usr/bin/env python3
"""Audit the dress-up trait economy against the frozen genesis baseline.

Verifies the two invariants over the on-chain index + Bucket/trait-token state:
  - Completeness: every live character holds one asset per slot and the right body
  - Conservation: no asset is silently created/destroyed; each body lives in
    exactly one place

  python scripts/audit_trait_economy.py --network mainnet

Run scripts/freeze_genesis.py first. Exit code is non-zero on any drift.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)

from lfg_core import config, economy_store, nft_index, trait_economy  # noqa: E402


def format_economy_report(
    conservation: trait_economy.ConservationReport,
    completeness: trait_economy.CompletenessReport,
    network: str,
    live_count: int,
    genesis_editions: int,
    timestamp: str,
) -> str:
    lines: list[str] = []
    lines.append(f"# Trait Economy Audit ({network}) — {timestamp}")
    lines.append("")
    lines.append(f"- Live characters: **{live_count}**")
    lines.append(f"- Genesis editions: **{genesis_editions}**")
    lines.append(f"- Conservation: **{'OK' if conservation.ok else 'DRIFT'}**")
    lines.append(f"- Completeness: **{'OK' if completeness.ok else 'VIOLATIONS'}**")
    lines.append("")

    lines.append("## Trait conservation drift (census − genesis)")
    lines.append("")
    if conservation.trait_drift:
        lines.append("| Slot | Value | Drift |")
        lines.append("| --- | --- | --- |")
        for (slot, value), delta in sorted(conservation.trait_drift.items()):
            lines.append(f"| {slot} | {value} | {delta:+d} |")
    else:
        lines.append("_None._")
    lines.append("")

    lines.append("## Body conservation drift (edition presence ≠ 1)")
    lines.append("")
    if conservation.body_drift:
        lines.append("| Edition | Places |")
        lines.append("| --- | --- |")
        for ed, presence in sorted(conservation.body_drift.items()):
            lines.append(f"| {ed} | {presence} |")
    else:
        lines.append("_None._")
    lines.append("")

    lines.append("## Wrong body (live edition body ≠ genesis)")
    lines.append("")
    if completeness.wrong_body:
        lines.append("| Edition | Found | Expected |")
        lines.append("| --- | --- | --- |")
        for ed, (found, expected) in sorted(completeness.wrong_body.items()):
            lines.append(f"| {ed} | {found} | {expected} |")
    else:
        lines.append("_None._")
    lines.append("")

    lines.append("## Orphan bodies (live edition not in genesis)")
    lines.append("")
    lines.append(
        ", ".join(str(e) for e in completeness.orphan_bodies)
        if completeness.orphan_bodies
        else "—"
    )
    lines.append("")

    lines.append("## Slot anomalies (slot not present exactly once)")
    lines.append("")
    if completeness.slot_anomalies:
        lines.append("| Edition | Slots |")
        lines.append("| --- | --- |")
        for ed, slots in sorted(completeness.slot_anomalies.items()):
            lines.append(f"| {ed} | {', '.join(slots)} |")
    else:
        lines.append("_None._")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit the trait economy against genesis.")
    parser.add_argument("--network", choices=["mainnet", "testnet"], default=config.XRPL_NETWORK)
    parser.add_argument("--report-dir", default=os.path.join(REPO_ROOT, "reports"))
    args = parser.parse_args()

    db_path = nft_index.index_db_path(args.network)
    if not os.path.isfile(db_path):
        print(f"No index DB at {db_path}. Run the backfill / Bithomp import first.")
        return 2

    conn = nft_index.init_db(db_path)
    economy_store.init_economy_schema(conn)
    if not economy_store.genesis_exists(conn):
        print("No frozen genesis. Run scripts/freeze_genesis.py first.")
        return 2

    genesis = economy_store.read_genesis(conn)
    max_edition = int(economy_store.read_meta(conn, "max_edition") or "3535")

    live = nft_index.live_nfts(conn)
    canonical, _ = trait_economy.dedupe_editions(live, max_edition)
    census = trait_economy.asset_census(
        canonical,
        economy_store.read_bucket_assets(conn),
        economy_store.read_bucket_bodies(conn),
        economy_store.read_trait_tokens(conn),
    )
    conservation = trait_economy.verify_conservation(genesis, census)
    completeness = trait_economy.verify_completeness(canonical, genesis)

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    report = format_economy_report(
        conservation, completeness, args.network, len(canonical), len(genesis.edition_bodies), timestamp
    )
    os.makedirs(args.report_dir, exist_ok=True)
    report_path = os.path.join(args.report_dir, f"trait-economy-audit-{args.network}-{timestamp}.md")
    with open(report_path, "w") as f:
        f.write(report)

    print(f"Network: {args.network}  live characters: {len(canonical)}")
    print(f"Conservation: {'OK' if conservation.ok else 'DRIFT'}")
    print(f"Completeness: {'OK' if completeness.ok else 'VIOLATIONS'}")
    print(f"Report: {report_path}")
    return 0 if conservation.ok and completeness.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_audit_trait_economy.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add scripts/audit_trait_economy.py tests/test_audit_trait_economy.py
git commit -m "feat: audit_trait_economy verification + CLI"
```

---

### Task 10: Full-suite gate + end-to-end validation against the real index

**Files:** none created (validation + docs only).

**Interfaces:** none.

- [ ] **Step 1: Run the whole test suite + lint + types**

Run:
```bash
.venv/bin/pytest tests/ -q
.venv/bin/ruff check lfg_core/trait_economy.py lfg_core/economy_store.py scripts/freeze_genesis.py scripts/audit_trait_economy.py
.venv/bin/ruff format --check lfg_core/trait_economy.py lfg_core/economy_store.py scripts/freeze_genesis.py scripts/audit_trait_economy.py
.venv/bin/mypy lfg_core/trait_economy.py lfg_core/economy_store.py
```
Expected: all green (pytest passes; ruff clean; mypy `Success: no issues found`). Fix any finding and re-run before continuing.

- [ ] **Step 2: Freeze genesis from the real mainnet index**

Run: `.venv/bin/python scripts/freeze_genesis.py --network mainnet`
Expected: prints live-token / genesis-edition counts, writes `reports/trait-economy-reconciliation-mainnet-<ts>.md`, exit 0. Open the report and confirm the duplicate/missing counts roughly match the prior collection-integrity findings (~4 missing, ~4 duplicate per project notes).

- [ ] **Step 3: Audit the economy at t0 and confirm zero drift**

Run: `.venv/bin/python scripts/audit_trait_economy.py --network mainnet`
Expected: `Conservation: OK` and `Completeness: OK`, exit 0 — at t0 Buckets/trait-tokens are empty, so `census == genesis` by construction. This is the end-to-end proof that the accounting is correct.

If completeness reports `wrong_body`/`orphan_bodies`/`slot_anomalies`, that is a real pre-existing index/data issue (not a code bug) — record it in the reconciliation notes; conservation must still be OK.

- [ ] **Step 4: Commit the genesis reports**

```bash
git add reports/trait-economy-reconciliation-mainnet-*.md reports/trait-economy-audit-mainnet-*.md
git commit -m "chore: freeze + audit mainnet trait-economy genesis at t0"
```

(The `onchain_mainnet.db` itself is gitignored/regenerable — do not commit it.)

---

## Self-Review

**1. Spec coverage:**
- Asset model (body vs 8 non-body, `"None"` as asset) → Tasks 1–2 (`NON_BODY_SLOTS`, `slot_value`, `build_genesis`). ✓
- Genesis (`trait_genesis`, `edition_bodies`, `genesis_meta`) → Tasks 2, 6. ✓
- Reconciliation (dedupe rule, missing/oor/unparsed, report) → Tasks 1, 8. ✓
- Live-state tables (`bucket_assets`, `bucket_bodies`, `trait_tokens`, empty at t0) → Tasks 6, 7. ✓
- Completeness invariant → Task 5. Conservation invariant → Tasks 3–4. ✓
- Auditor script + Markdown report + nonzero exit → Task 9. ✓
- End-to-end validation against real 5.5k-token index → Task 10. ✓
- Code layout (`trait_economy.py` pure, `economy_store.py` I/O, two scripts) → matches spec §6. ✓
- Out-of-scope items (XRPL writes, UI, extract/deposit, fees, listener) correctly absent. ✓

**2. Placeholder scan:** No TBD/TODO; every code step shows complete code; every test step shows complete assertions. ✓

**3. Type consistency:** `Genesis(trait_counts, edition_bodies)`, `Census(trait_counts, body_presence)`, `ConservationReport(trait_drift, body_drift, ok)`, `CompletenessReport(wrong_body, orphan_bodies, slot_anomalies, ok)` are used identically across producer tasks and the script tasks. `asset_census` parameter shapes `(owner, slot, value, count)` / `(owner, edition)` / `(nft_id, owner, slot, value)` match `economy_store.read_*` return shapes exactly (Task 7 ↔ Task 3/9). `dedupe_editions(records, max_edition)` signature consistent across Tasks 1, 8, 9. ✓
</content>
