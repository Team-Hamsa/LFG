# Legacy Ape Faces — Auto-Roll on Swap (#168) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The first time a `None`-faced legacy ape goes through the Trait Swapper, its Eyes/Eyebrows/Mouth are rolled through the mint's rarity engine and carried by the existing swap machinery.

**Architecture:** One new async helper `fill_missing_face_traits` in `lfg_core/traits.py` (mirrors `select_random_attributes`'s candidate filtering + `rarity.weighted_pick`), called from `swap_flow.run_swap_session` immediately after `swap_meta.swap_traits` and before the `missing_layers` pre-check. No new UI, no new on-chain machinery, no schema changes.

**Tech Stack:** Python 3.12, pytest, existing `lfg_core` modules (`traits`, `rarity`, `trait_config`, `swap_meta`, `swap_flow`).

## Global Constraints

- All XRPL txs carry `SourceTag = 2606160021` + provenance memos — already handled by existing builders; this plan adds no new tx path.
- No forced burns; the roll only lands via the owner-signed swap the user initiated.
- Face slots are exactly `["Mouth", "Eyebrows", "Eyes"]` (in `swap_meta.TRAIT_ORDER` order); ape-only (`body == "ape"`).
- `"None"` is never a rollable candidate value.
- Test files importing `lfg_core` at module top MUST carry the env-guard preamble (see Task 1 test header) and use `asyncio.new_event_loop()` / `get_event_loop().run_until_complete` — never `asyncio.run()` (it closes the shared suite loop).
- Pre-push gate (ruff/mypy/gitleaks/pytest/validate-trait-config) must pass; never `--no-verify`.

---

### Task 1: `fill_missing_face_traits` helper

**Files:**
- Modify: `lfg_core/traits.py` (append after `select_random_attributes`)
- Test: `tests/test_fill_face_traits.py` (create)

**Interfaces:**
- Consumes: `rarity.weighted_pick(conn, body, trait_type, values, network=, now=, rng=)`, `trait_config.get_config()` (`.value_allowed(body, t, v)`, `.conflicts(attributes, t, v)`), `store.list_values(body, trait_type)`.
- Produces: `async def fill_missing_face_traits(store, body, attributes, *, conn=None, network=None, now=None, rng=random) -> bool` — mutates `attributes` in place, returns True if any slot was rolled. Task 2 calls it with positional `(store, nft["gender"], attrs)`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_fill_face_traits.py`:

```python
# Legacy ape face auto-roll (#168): None face slots are filled through the
# rarity engine the first time an ape goes through the swapper.
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
os.environ.setdefault("BUNNY_PULL_ZONE", "nft.pullzone.example")
os.environ.setdefault("LAYER_SOURCE", "local")

import asyncio  # noqa: E402
import random  # noqa: E402
import sqlite3  # noqa: E402

import pytest  # noqa: E402

from lfg_core import rarity, traits  # noqa: E402
from lfg_core.swap_meta import normalize_attributes  # noqa: E402


class FakeStore:
    """list_values-only store; values per (body, trait_type)."""

    def __init__(self, values):
        self.values = values

    async def list_values(self, body, trait_type):
        return self.values.get((body, trait_type), [])


FACE_VALUES = {
    ("ape", "Eyes"): ["None", "Wide Eyes", "Laser Eyes"],
    ("ape", "Eyebrows"): ["None", "Angry"],
    ("ape", "Mouth"): ["None", "Grin", "Cigar"],
}


def _conn():
    conn = sqlite3.connect(":memory:")
    rarity.init_db(conn)
    return conn


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _faceless_ape():
    return normalize_attributes(
        [{"trait_type": "Body", "value": "Xray"}, {"trait_type": "Accessory", "value": "Bible"}]
    )


def _get(attrs, t):
    return next(a["value"] for a in attrs if a["trait_type"] == t)


def test_fills_all_none_face_slots_for_ape():
    attrs = _faceless_ape()
    rolled = _run(
        traits.fill_missing_face_traits(
            FakeStore(FACE_VALUES), "ape", attrs, conn=_conn(), rng=random.Random(1)
        )
    )
    assert rolled is True
    for slot in ("Eyes", "Eyebrows", "Mouth"):
        assert _get(attrs, slot) not in ("None", "", None)
    # Non-face slots untouched.
    assert _get(attrs, "Accessory") == "Bible"


def test_never_rolls_the_none_value():
    attrs = _faceless_ape()
    for seed in range(20):
        a = [dict(x) for x in attrs]
        _run(
            traits.fill_missing_face_traits(
                FakeStore(FACE_VALUES), "ape", a, conn=_conn(), rng=random.Random(seed)
            )
        )
        for slot in ("Eyes", "Eyebrows", "Mouth"):
            assert _get(a, slot) != "None"


def test_non_ape_body_is_untouched():
    attrs = _faceless_ape()
    rolled = _run(
        traits.fill_missing_face_traits(
            FakeStore(FACE_VALUES), "skeleton", attrs, conn=_conn(), rng=random.Random(1)
        )
    )
    assert rolled is False
    for slot in ("Eyes", "Eyebrows", "Mouth"):
        assert _get(attrs, slot) == "None"


def test_existing_real_face_value_is_preserved():
    attrs = _faceless_ape()
    for a in attrs:
        if a["trait_type"] == "Eyes":
            a["value"] = "Wide Eyes"
    _run(
        traits.fill_missing_face_traits(
            FakeStore(FACE_VALUES), "ape", attrs, conn=_conn(), rng=random.Random(1)
        )
    )
    assert _get(attrs, "Eyes") == "Wide Eyes"
    assert _get(attrs, "Mouth") != "None"


def test_deterministic_under_seeded_rng():
    a1, a2 = _faceless_ape(), _faceless_ape()
    _run(
        traits.fill_missing_face_traits(
            FakeStore(FACE_VALUES), "ape", a1, conn=_conn(), rng=random.Random(7)
        )
    )
    _run(
        traits.fill_missing_face_traits(
            FakeStore(FACE_VALUES), "ape", a2, conn=_conn(), rng=random.Random(7)
        )
    )
    assert a1 == a2


def test_no_candidates_for_a_slot_is_skipped_not_error():
    # Store has no Eyebrows values at all (layer absent): slot stays None,
    # the others still roll — mirrors select_random_attributes's "no raw
    # values -> skip layer" behavior.
    values = {k: v for k, v in FACE_VALUES.items() if k != ("ape", "Eyebrows")}
    attrs = _faceless_ape()
    rolled = _run(
        traits.fill_missing_face_traits(
            FakeStore(values), "ape", attrs, conn=_conn(), rng=random.Random(1)
        )
    )
    assert rolled is True
    assert _get(attrs, "Eyebrows") == "None"
    assert _get(attrs, "Eyes") != "None"


def test_over_constrained_slot_raises(monkeypatch):
    # Candidates exist but config rules eliminate them all -> fail loud,
    # same contract as select_random_attributes.
    from lfg_core import trait_config

    cfg = trait_config.get_config()
    monkeypatch.setattr(
        type(cfg), "value_allowed", lambda self, body, t, v: t not in ("Eyes", "Eyebrows", "Mouth")
    )
    attrs = _faceless_ape()
    with pytest.raises(ValueError, match="no legal"):
        _run(
            traits.fill_missing_face_traits(
                FakeStore(FACE_VALUES), "ape", attrs, conn=_conn(), rng=random.Random(1)
            )
        )
```

Note: if `rarity.init_db` is not the in-memory schema initializer's real name, check `lfg_core/rarity.py` for the function `rarity.connect()` uses to create tables (grep `CREATE TABLE`) and call that instead; `weighted_pick` must work against the fresh in-memory DB exactly as the existing `tests/test_rarity*.py` files set it up — copy their fixture pattern.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_fill_face_traits.py -v`
Expected: FAIL / ERROR with `AttributeError: module 'lfg_core.traits' has no attribute 'fill_missing_face_traits'`

- [ ] **Step 3: Implement the helper**

Append to `lfg_core/traits.py`:

```python
# Face slots auto-rolled for legacy apes the first time they pass through the
# Trait Swapper (#168). Ape-only: other bodies have no face art. Order follows
# TRAIT_ORDER so earlier rolls constrain later ones via cfg.conflicts.
FACE_TRAITS = [t for t in TRAIT_ORDER if t in ("Mouth", "Eyebrows", "Eyes")]


async def fill_missing_face_traits(
    store: Any,
    body: str | None,
    attributes: list[dict[str, Any]],
    *,
    conn: sqlite3.Connection | None = None,
    network: str | None = None,
    now: datetime | None = None,
    rng: Any = random,
) -> bool:
    """Roll a rarity-weighted value into every empty ('None'/''/missing) face
    slot of an ape's attribute list, in place. Same candidate filtering and
    weighted_pick as select_random_attributes, so armed boosts/floors apply
    identically to mint. Returns True if anything was rolled. No-op for
    non-ape bodies. Raises ValueError if rules eliminate every candidate for
    a slot that has values (over-constrained config — fail loud, like mint)."""
    if body != "ape":
        return False
    empty = [t for t in FACE_TRAITS if (get_attr(attributes, t) or "None") in ("", "None")]
    if not empty:
        return False
    own_conn = conn is None
    if own_conn:
        conn = rarity.connect()
    assert conn is not None
    rolled = False
    try:
        cfg = trait_config.get_config()
        for trait_type in empty:
            raw_values = await store.list_values(body, trait_type)
            candidates = [
                v
                for v in raw_values
                if v != "None"
                and cfg.value_allowed(body, trait_type, v)
                and not cfg.conflicts(attributes, trait_type, v)
            ]
            if not candidates:
                if [v for v in raw_values if v != "None"]:
                    raise ValueError(
                        f"trait rules leave no legal {trait_type} value for body '{body}'"
                    )
                continue  # layer genuinely absent on this body: leave None
            value = rarity.weighted_pick(
                conn, body, trait_type, candidates, network=network, now=now, rng=rng
            )
            for a in attributes:
                if a["trait_type"] == trait_type:
                    a["value"] = value
                    break
            else:
                attributes.append({"trait_type": trait_type, "value": value})
            rolled = True
    finally:
        if own_conn:
            conn.close()
    return rolled
```

Also add the imports this needs at the top of `lfg_core/traits.py` if not already present: `from lfg_core.swap_meta import TRAIT_ORDER` is already imported; add `get_attr`:

```python
from lfg_core.swap_meta import TRAIT_ORDER, get_attr
```

(`cfg.conflicts(attributes, ...)` sees the full attribute list including already-rolled faces, which is exactly the constraint chaining we want. Entries filled by `normalize_attributes` always exist, so the `append` branch is defensive only.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_fill_face_traits.py -v`
Expected: 7 passed

- [ ] **Step 5: Run neighboring suites to catch regressions**

Run: `.venv/bin/python -m pytest tests/test_traits*.py tests/test_rarity*.py tests/test_fill_face_traits.py -q`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add lfg_core/traits.py tests/test_fill_face_traits.py
git commit -m "feat(traits): fill_missing_face_traits — rarity-rolled faces for empty ape slots (#168)"
```

### Task 2: Wire the roll into `run_swap_session`

**Files:**
- Modify: `lfg_core/swap_flow.py` (in `run_swap_session`, right after the `swap_meta.swap_traits(...)` call, before the `missing_layers` pre-check)
- Test: `tests/test_swap_face_roll.py` (create)

**Interfaces:**
- Consumes: `traits.fill_missing_face_traits(store, body, attributes, ...)` from Task 1 (called with defaults for `conn`/`network`/`now`/`rng`, matching how the mint path calls `select_random_attributes`).
- Produces: post-swap `new_attrs1`/`new_attrs2` carry rolled faces for ape NFTs; everything downstream (compose, upload, modify/remint, #163 archive) is unchanged.

- [ ] **Step 1: Write the failing test**

Create `tests/test_swap_face_roll.py`:

```python
# Legacy apes get faces rolled on their first pass through the swapper (#168):
# run_swap_session fills None face slots after swap application, before the
# layer pre-check, for ape bodies only.
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
os.environ.setdefault("BUNNY_PULL_ZONE", "nft.pullzone.example")
os.environ.setdefault("LAYER_SOURCE", "local")

import asyncio  # noqa: E402

from lfg_core import swap_flow, swap_meta  # noqa: E402


def _nft(number, gender, attributes, mutable=True):
    return {
        "number": number,
        "gender": gender,
        "attributes": swap_meta.normalize_attributes(attributes),
        "mutable": mutable,
        "nft_id": f"ID{number}",
        "burn_count": 1,
    }


def test_swap_session_rolls_faces_for_apes_only(monkeypatch):
    captured = {}

    async def fake_fill(store, body, attributes, **kwargs):
        captured.setdefault("calls", []).append((body, [dict(a) for a in attributes]))
        if body != "ape":
            return False
        for a in attributes:
            if a["trait_type"] in ("Eyes", "Eyebrows", "Mouth"):
                a["value"] = "ROLLED"
        return True

    async def fake_missing(attributes, body, store):
        # Capture what the pre-check sees: rolled faces must already be there.
        captured.setdefault("prechecked", []).append((body, [dict(a) for a in attributes]))
        return ["stop/here/now"]  # fail the session before any payment/on-chain step

    monkeypatch.setattr(swap_flow.traits, "fill_missing_face_traits", fake_fill)
    monkeypatch.setattr(swap_flow.swap_compose, "missing_layers", fake_missing)
    monkeypatch.setattr(swap_flow.layer_store, "get_layer_store", lambda: object())

    ape = _nft(814, "ape", [{"trait_type": "Body", "value": "Xray"},
                            {"trait_type": "Accessory", "value": "Scythe"}])
    skel = _nft(59, "skeleton", [{"trait_type": "Body", "value": "White"},
                                 {"trait_type": "Accessory", "value": "Bible"}])
    session = swap_flow.SwapSession(
        discord_id="1", wallet_address="rUser", nft1=ape, nft2=skel,
        traits_to_swap=["Accessory"],
    )
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(swap_flow.run_swap_session(session))
    finally:
        loop.close()

    assert session.state == swap_flow.FAILED  # stopped at the layer pre-check
    bodies = [b for b, _ in captured["calls"]]
    assert sorted(bodies) == ["ape", "skeleton"]
    ape_precheck = next(a for b, a in captured["prechecked"] if b == "ape")
    skel_precheck = next(a for b, a in captured["prechecked"] if b == "skeleton")
    for slot in ("Eyes", "Eyebrows", "Mouth"):
        assert swap_meta.get_attr(ape_precheck, slot) == "ROLLED"
        assert swap_meta.get_attr(skel_precheck, slot) != "ROLLED"
```

Adjust the `SwapSession(...)` constructor kwargs to the real dataclass fields — read the `SwapSession` definition at the top of `lfg_core/swap_flow.py` first and copy the construction pattern from an existing test (grep `SwapSession(` under `tests/`). The assertions are the contract; the fixture plumbing should follow whatever the existing swap-flow tests do.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_swap_face_roll.py -v`
Expected: FAIL — `AttributeError` (no `swap_flow.traits`) or the `prechecked` attrs lack `"ROLLED"` (fill never called).

- [ ] **Step 3: Implement the wiring**

In `lfg_core/swap_flow.py`:

1. Add `traits` to the existing `from lfg_core import (...)` block.
2. In `run_swap_session`, right after the `swap_meta.swap_traits(...)` call, insert:

```python
        # Legacy ape faces (#168): fill any still-empty face slot via the
        # rarity engine — after swap application (a real face moved in by
        # the swap is never overwritten), before the layer pre-check (rolled
        # art gets the same existence check as everything else). Skeletons
        # and other bodies are a no-op inside the helper.
        store = layer_store.get_layer_store()
        await traits.fill_missing_face_traits(store, nft1["gender"], new_attrs1)
        await traits.fill_missing_face_traits(store, nft2["gender"], new_attrs2)
```

3. Delete the now-duplicate `store = layer_store.get_layer_store()` line that currently sits just below (step 0 comment block) — the store is created once, earlier.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_swap_face_roll.py -v`
Expected: PASS

- [ ] **Step 5: Run the swap suites**

Run: `.venv/bin/python -m pytest tests/test_swap*.py tests/test_fill_face_traits.py tests/test_swap_face_roll.py -q`
Expected: all pass (existing swap-flow tests use non-ape fixtures or real stores; if one now fails because its fake store lacks `list_values`, give the fake a `list_values` returning `[]` — that makes the roll a no-op, preserving the old behavior for that test).

- [ ] **Step 6: Commit**

```bash
git add lfg_core/swap_flow.py tests/test_swap_face_roll.py
git commit -m "feat(swap): auto-roll faces for None-faced apes on swap (#168)"
```

### Task 3: Full gate + PR

**Files:** none new.

- [ ] **Step 1: Full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: everything passes (baseline before this work: 1469 passed, 1 skipped).

- [ ] **Step 2: Push and open draft PR**

```bash
git push -u origin HEAD
gh pr create --draft --repo Team-Hamsa/LFG \
  --title "feat(swap): legacy apes get faces on their first swap (#168)" \
  --body "Closes #168. First time a None-faced ape goes through the Trait Swapper, Eyes/Eyebrows/Mouth are rolled via the rarity engine (same weighted_pick as mint, boosts/floors apply) and carried by the existing modify/remint machinery. Spec: docs/superpowers/specs/2026-07-11-legacy-ape-faces-design.md. Plan: docs/superpowers/plans/2026-07-11-legacy-ape-faces.md.

🤖 Generated with [Claude Code](https://claude.com/claude-code)"
```

- [ ] **Step 3: Bot review triage**

Wait for Greptile + CodeRabbit; resolve or explicitly address every actionable finding before marking ready/merging (repo rule).
