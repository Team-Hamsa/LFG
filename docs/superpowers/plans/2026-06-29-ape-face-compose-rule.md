# Ape Face Compose Rule Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Inject a fixed nose layer on all ape NFTs and clip the right side of face-anchored Eyes/Eyebrows/Mouth features on the three melt/xray ape bodies, in the single shared compositor so mint, swap, and economy all inherit it.

**Architecture:** A new pure module `lfg_core/ape_face.py` holds the rule (constants, `should_mask`, `apply_alpha_mask`, async `inject_and_mask`). `swap_compose.compose_nft` calls it as one transform step over a canonical-ordered layer list, then floats TOP effect traits to the top as today. `layer_store` gains `resolve_asset` to fetch the `ape/Nose.png` / `ape/Ape Mask.png` root assets. `missing_layers` checks those assets before any burn.

**Tech Stack:** Python 3.10, Pillow (image masking), ffmpeg-python (unchanged overlay), pytest.

## Global Constraints

- **Python 3.10**; follow existing `lfg_core` style (type hints, `from __future__ import annotations` where the module uses `X | None`).
- **No `pytest-asyncio` / no `asyncio_mode`.** Tests are sync `def test_*` and drive coroutines through a private `_run(coro)` event-loop helper (mirror `tests/test_event_endpoints.py`). A bare `async def test_*` is silently skipped — never write one.
- **Pre-commit hooks must pass:** `ruff format`, `ruff` lint, `mypy` (strict), `pytest`, `gitleaks`. Run `ruff format` before committing.
- **`Nose` is never a trait type:** do not add it to `TRAIT_ORDER` or `SWAPPABLE_TRAITS`. It must never appear in NFT metadata.
- **No XRPL transactions in this work** — compose only; the SourceTag rule does not apply here.
- DRY, YAGNI, TDD, frequent commits.

---

### Task 1: `resolve_asset` on both layer stores

Fetch an arbitrary file under the layer root (e.g. `ape/Nose.png`), which the trait-keyed `resolve(body, trait_type, value)` cannot address.

**Files:**
- Modify: `lfg_core/layer_store.py` (add `resolve_asset` to `LocalLayerStore` after line 64, and to `CdnLayerStore` after line 121)
- Test: `tests/test_layer_store.py` (create)

**Interfaces:**
- Produces: `LocalLayerStore.resolve_asset(self, rel_path: str) -> str | None` and `CdnLayerStore.resolve_asset(self, rel_path: str) -> str | None` — returns a local filesystem path or `None` if absent.

- [ ] **Step 1: Write the failing test**

Create `tests/test_layer_store.py`:

```python
# tests/test_layer_store.py
import asyncio
import os

from lfg_core import layer_store


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_local_resolve_asset_found(tmp_path):
    ape = tmp_path / "ape"
    ape.mkdir()
    (ape / "Nose.png").write_bytes(b"x")
    store = layer_store.LocalLayerStore(str(tmp_path))
    assert _run(store.resolve_asset("ape/Nose.png")) == os.path.join(str(tmp_path), "ape", "Nose.png")


def test_local_resolve_asset_missing(tmp_path):
    store = layer_store.LocalLayerStore(str(tmp_path))
    assert _run(store.resolve_asset("ape/Nose.png")) is None


def test_cdn_resolve_asset_lists_parent_then_downloads(monkeypatch):
    store = layer_store.CdnLayerStore()

    async def fake_list(rel_path):
        assert rel_path == "ape"
        return [("Nose.png", False), ("Eyes", True)]

    async def fake_download(rel_path):
        assert rel_path == "ape/Nose.png"
        return "/cache/ape/Nose.png"

    monkeypatch.setattr(store, "_list_dir", fake_list)
    monkeypatch.setattr(store, "_download", fake_download)
    assert _run(store.resolve_asset("ape/Nose.png")) == "/cache/ape/Nose.png"


def test_cdn_resolve_asset_absent_returns_none(monkeypatch):
    store = layer_store.CdnLayerStore()

    async def fake_list(rel_path):
        return [("Eyes", True)]

    monkeypatch.setattr(store, "_list_dir", fake_list)
    assert _run(store.resolve_asset("ape/Nose.png")) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_layer_store.py -q`
Expected: FAIL with `AttributeError: 'LocalLayerStore' object has no attribute 'resolve_asset'`

- [ ] **Step 3: Write minimal implementation**

In `lfg_core/layer_store.py`, add to `LocalLayerStore` (after `resolve`, line 64):

```python
    async def resolve_asset(self, rel_path: str) -> str | None:
        """Local path of an arbitrary file under the layer root (e.g.
        'ape/Nose.png'), or None if it doesn't exist."""
        path = os.path.join(self.base_dir, rel_path)
        return path if os.path.isfile(path) else None
```

Add to `CdnLayerStore` (after `resolve`, before `_download`):

```python
    async def resolve_asset(self, rel_path: str) -> str | None:
        """Download (or reuse cached) an arbitrary file under the layer root
        (e.g. 'ape/Nose.png'); returns local path or None if absent."""
        parent, _, name = rel_path.rpartition("/")
        listing = await self._list_dir(parent)
        if name in {n for n, is_dir in listing if not is_dir}:
            return await self._download(rel_path)
        return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_layer_store.py -q`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
ruff format lfg_core/layer_store.py tests/test_layer_store.py
git add lfg_core/layer_store.py tests/test_layer_store.py
git commit -m "feat(layer_store): resolve_asset for root layer assets (ape nose/mask)"
```

---

### Task 2: `ape_face.py` — constants + `should_mask`, relocate `TOP_TRAITS`

Create the rule module and move the effect-trait set here so `should_mask` can reuse it without a circular import.

**Files:**
- Create: `lfg_core/ape_face.py`
- Modify: `lfg_core/swap_compose.py:14-22` (replace the local `TOP_TRAITS` definition with an import from `ape_face`)
- Test: `tests/test_ape_face.py` (create)

**Interfaces:**
- Produces:
  - `ape_face.TOP_TRAITS: list[dict[str, str]]`
  - `ape_face.NOSE_ASSET = "Nose.png"`, `ape_face.MASK_ASSET = "Ape Mask.png"`
  - `ape_face.MASKED_BODY_VALUES: set[str]`, `ape_face.MASKED_TRAITS: set[str]`, `ape_face.NO_MASK_VALUES: list[dict[str, str]]`
  - `ape_face.should_mask(trait_type: str, value: str, body_value: str) -> bool`
- Consumes: nothing from earlier tasks.

- [ ] **Step 1: Write the failing test**

Create `tests/test_ape_face.py`:

```python
# tests/test_ape_face.py
from lfg_core import ape_face


def test_should_mask_normal_face_feature_on_melt_body():
    assert ape_face.should_mask("Eyes", "Creepy", "Ape Melting XRay") is True
    assert ape_face.should_mask("Mouth", "Serious", "Ape Xray") is True
    assert ape_face.should_mask("Eyebrows", "Flat", "Ape Melting") is True


def test_should_mask_exempts_top_effect():
    assert ape_face.should_mask("Mouth", "Rainbow Puke", "Ape Xray") is False
    assert ape_face.should_mask("Eyes", "Laser Eyes", "Ape Xray") is False


def test_should_mask_only_melt_bodies():
    assert ape_face.should_mask("Eyes", "Creepy", "Ape Gold") is False
    assert ape_face.should_mask("Eyes", "Creepy", "Straight Dark") is False


def test_should_mask_only_face_trait_types():
    assert ape_face.should_mask("Clothing", "Wonder", "Ape Xray") is False
    assert ape_face.should_mask("Background", "Wave", "Ape Xray") is False


def test_should_mask_respects_skip_list(monkeypatch):
    monkeypatch.setattr(
        ape_face, "NO_MASK_VALUES", [{"trait_type": "Mouth", "value": "Cigar"}]
    )
    assert ape_face.should_mask("Mouth", "Cigar", "Ape Xray") is False
    assert ape_face.should_mask("Mouth", "Smile", "Ape Xray") is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_ape_face.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'lfg_core.ape_face'`

- [ ] **Step 3: Write minimal implementation**

Create `lfg_core/ape_face.py`:

```python
# lfg_core/ape_face.py
"""Ape-specific compose rules.

Two structural assets live at the ape layer root:
  * ape/Nose.png      — a single fixed nose overlay injected on ALL apes, just
                        above the Eyes layer.
  * ape/Ape Mask.png  — a shared alpha mask (transparent left, opaque right)
                        used to clip the right side of face-anchored features
                        on the melt/xray ape bodies.

These are NOT trait attributes: never rarity-selected, never in NFT metadata,
never swappable.
"""

from __future__ import annotations

import os

from PIL import Image, ImageChops

# Effect traits that render on top of everything and extend past the face.
# Lives here (not swap_compose) so the masking rule can reuse it without a
# circular import; swap_compose imports TOP_TRAITS from this module.
TOP_TRAITS: list[dict[str, str]] = [
    {"trait_type": "Eyes", "value": "Wavy"},
    {"trait_type": "Mouth", "value": "Rainbow Puke"},
    {"trait_type": "Eyes", "value": "Laser Eyes"},
    {"trait_type": "Eyes", "value": "Laser"},
]

NOSE_ASSET = "Nose.png"
MASK_ASSET = "Ape Mask.png"

MASKED_BODY_VALUES = {"Ape Xray", "Ape Melting", "Ape Melting XRay"}
MASKED_TRAITS = {"Eyes", "Eyebrows", "Mouth"}

# Curated, art-driven exemptions: face features that extend past the face and
# must not be clipped. Ships empty; grown one line at a time after art review.
NO_MASK_VALUES: list[dict[str, str]] = []


def should_mask(trait_type: str, value: str, body_value: str) -> bool:
    """True if this face feature's right side should be clipped on a melt/xray
    ape body."""
    if body_value not in MASKED_BODY_VALUES:
        return False
    if trait_type not in MASKED_TRAITS:
        return False
    pair = {"trait_type": trait_type, "value": value}
    if pair in TOP_TRAITS:
        return False
    if pair in NO_MASK_VALUES:
        return False
    return True
```

(`Image`/`ImageChops` are imported now because Task 3 adds `apply_alpha_mask` to this module; keeping the import here avoids a churn edit. If your linter flags unused imports before Task 3, add Task 3 in the same branch — they ship together.)

In `lfg_core/swap_compose.py`, delete the local `TOP_TRAITS = [...]` block (lines 16-22) and import it. Update the imports near line 12-14:

```python
from lfg_core import ape_face
from lfg_core.ape_face import TOP_TRAITS
from lfg_core.swap_meta import TRAIT_ORDER
```

(`TOP_TRAITS` stays referenced by name in `_ordered_traits`, so the import keeps that code working unchanged.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_ape_face.py -q`
Expected: PASS (5 passed)

Run the existing suite to confirm the `TOP_TRAITS` move didn't break composition:
Run: `.venv/bin/python -m pytest -q`
Expected: PASS (no new failures)

- [ ] **Step 5: Commit**

```bash
ruff format lfg_core/ape_face.py lfg_core/swap_compose.py tests/test_ape_face.py
git add lfg_core/ape_face.py lfg_core/swap_compose.py tests/test_ape_face.py
git commit -m "feat(ape_face): rule module + should_mask; relocate TOP_TRAITS"
```

---

### Task 3: `apply_alpha_mask` — pure Pillow masking

Subtractively clear a layer's alpha where the mask is opaque.

**Files:**
- Modify: `lfg_core/ape_face.py` (append `apply_alpha_mask`)
- Test: `tests/test_ape_face.py` (append)

**Interfaces:**
- Produces: `ape_face.apply_alpha_mask(layer_path: str, mask_path: str, out_dir: str) -> str` — returns the temp PNG path; raises `ValueError` if `layer_path` is not a PNG.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_ape_face.py`:

```python
import pytest
from PIL import Image


def _mask_right_opaque(path, size=(4, 4)):
    """Mask: left half fully transparent, right half fully opaque white."""
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    for y in range(size[1]):
        for x in range(size[0] // 2, size[0]):
            img.putpixel((x, y), (255, 255, 255, 255))
    img.save(path)


def test_apply_alpha_mask_clips_right_keeps_left(tmp_path):
    layer = tmp_path / "Creepy.png"
    Image.new("RGBA", (4, 4), (0, 255, 0, 255)).save(layer)  # opaque green
    mask = tmp_path / "Ape Mask.png"
    _mask_right_opaque(mask)

    out = ape_face.apply_alpha_mask(str(layer), str(mask), str(tmp_path / "gen"))
    res = Image.open(out).convert("RGBA")

    assert res.getpixel((0, 0))[3] == 255   # left half kept
    assert res.getpixel((3, 0))[3] == 0     # right half cleared
    assert res.getpixel((0, 0))[:3] == (0, 255, 0)  # RGB preserved where kept


def test_apply_alpha_mask_resizes_mismatched_mask(tmp_path):
    layer = tmp_path / "Creepy.png"
    Image.new("RGBA", (8, 8), (0, 255, 0, 255)).save(layer)
    mask = tmp_path / "Ape Mask.png"
    _mask_right_opaque(mask, size=(4, 4))  # smaller than layer

    out = ape_face.apply_alpha_mask(str(layer), str(mask), str(tmp_path / "gen"))
    res = Image.open(out).convert("RGBA")
    assert res.size == (8, 8)
    assert res.getpixel((1, 1))[3] == 255   # left kept
    assert res.getpixel((6, 1))[3] == 0     # right cleared


def test_apply_alpha_mask_rejects_non_png(tmp_path):
    with pytest.raises(ValueError):
        ape_face.apply_alpha_mask(
            str(tmp_path / "x.mp4"), str(tmp_path / "m.png"), str(tmp_path / "gen")
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_ape_face.py -k alpha_mask -q`
Expected: FAIL with `AttributeError: module 'lfg_core.ape_face' has no attribute 'apply_alpha_mask'`

- [ ] **Step 3: Write minimal implementation**

Append to `lfg_core/ape_face.py`:

```python
def apply_alpha_mask(layer_path: str, mask_path: str, out_dir: str) -> str:
    """Write a temp PNG of ``layer`` with its alpha cleared where ``mask`` is
    opaque (subtractive: out_alpha = layer_alpha * (255 - mask_alpha) / 255).
    Returns the temp path. Raises ValueError if ``layer_path`` is not a PNG."""
    if not layer_path.lower().endswith(".png"):
        raise ValueError(f"cannot mask non-PNG slot: {layer_path}")
    layer = Image.open(layer_path).convert("RGBA")
    mask = Image.open(mask_path).convert("RGBA")
    if mask.size != layer.size:
        mask = mask.resize(layer.size, Image.Resampling.NEAREST)
    inv = ImageChops.invert(mask.getchannel("A"))
    new_alpha = ImageChops.multiply(layer.getchannel("A"), inv)
    layer.putalpha(new_alpha)
    os.makedirs(out_dir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(layer_path))[0]
    out_path = os.path.join(out_dir, f"{stem}.masked.png")
    layer.save(out_path)
    return out_path
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_ape_face.py -q`
Expected: PASS (8 passed)

- [ ] **Step 5: Commit**

```bash
ruff format lfg_core/ape_face.py tests/test_ape_face.py
git add lfg_core/ape_face.py tests/test_ape_face.py
git commit -m "feat(ape_face): apply_alpha_mask subtractive right-clip"
```

---

### Task 4: `inject_and_mask` — async layer transform

Inject the nose above Eyes (all apes) and substitute masked paths for clipped face features (melt/xray apes), operating on a canonical-ordered layer list.

**Files:**
- Modify: `lfg_core/ape_face.py` (append `inject_and_mask` and `_nose_index`; add `from typing import Any` and `from lfg_core import swap_meta` imports)
- Test: `tests/test_ape_face.py` (append)

**Interfaces:**
- Consumes: `should_mask`, `apply_alpha_mask` (Tasks 2-3); `store.resolve_asset` (Task 1); `swap_meta.TRAIT_ORDER`.
- Produces: `ape_face.inject_and_mask(layers, body, body_value, store, out_dir) -> list[tuple[str, str, str]]` where `layers` is a list of `(trait_type, value, path)` in **canonical order** (no TOP float yet). Returns the same shape with the nose inserted (as `("Nose", "Nose", nose_path)`) and masked face features' paths replaced. Non-ape bodies return the list unchanged.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_ape_face.py`:

```python
from lfg_core import layer_store


def _ape_store_with_assets(tmp_path):
    ape = tmp_path / "layers" / "ape"
    ape.mkdir(parents=True)
    Image.new("RGBA", (4, 4), (0, 0, 0, 0)).save(ape / "Nose.png")
    _mask_right_opaque(ape / "Ape Mask.png")
    return layer_store.LocalLayerStore(str(tmp_path / "layers")), ape


def test_inject_and_mask_non_ape_unchanged(tmp_path):
    store, _ = _ape_store_with_assets(tmp_path)
    layers = [("Body", "Straight Dark", "/x/body.png"), ("Eyes", "Standard", "/x/eyes.png")]
    out = _run(ape_face.inject_and_mask(layers, "male", "Straight Dark", store, str(tmp_path / "gen")))
    assert out == layers


def test_inject_and_mask_inserts_nose_above_eyes(tmp_path):
    store, _ = _ape_store_with_assets(tmp_path)
    layers = [
        ("Body", "Ape Gold", "/x/body.png"),
        ("Eyes", "Standard", "/x/eyes.png"),
        ("Head", "Cap", "/x/head.png"),
    ]
    out = _run(ape_face.inject_and_mask(layers, "ape", "Ape Gold", store, str(tmp_path / "gen")))
    types = [t for t, _v, _p in out]
    assert types == ["Body", "Eyes", "Nose", "Head"]
    # Ape Gold is not a melt body -> nose injected, no masking.
    assert all(not p.endswith(".masked.png") for _t, _v, p in out)


def test_inject_and_mask_clips_face_features_on_melt_body(tmp_path):
    store, ape = _ape_store_with_assets(tmp_path)
    eyes = ape / "Eyes"
    eyes.mkdir()
    Image.new("RGBA", (4, 4), (0, 255, 0, 255)).save(eyes / "Creepy.png")
    layers = [
        ("Body", "Ape Melting XRay", "/x/body.png"),
        ("Eyes", "Creepy", str(eyes / "Creepy.png")),
    ]
    out = _run(
        ape_face.inject_and_mask(layers, "ape", "Ape Melting XRay", store, str(tmp_path / "gen"))
    )
    eyes_path = next(p for t, _v, p in out if t == "Eyes")
    assert eyes_path.endswith(".masked.png")
    res = Image.open(eyes_path).convert("RGBA")
    assert res.getpixel((0, 0))[3] == 255   # left kept
    assert res.getpixel((3, 0))[3] == 0     # right cleared


def test_inject_and_mask_nose_fallback_when_no_eyes(tmp_path):
    store, _ = _ape_store_with_assets(tmp_path)
    layers = [("Eyebrows", "Flat", "/x/brow.png"), ("Head", "Cap", "/x/head.png")]
    out = _run(ape_face.inject_and_mask(layers, "ape", "Ape Gold", store, str(tmp_path / "gen")))
    types = [t for t, _v, _p in out]
    assert types == ["Eyebrows", "Nose", "Head"]  # nose at canonical Eyes slot
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_ape_face.py -k inject_and_mask -q`
Expected: FAIL with `AttributeError: module 'lfg_core.ape_face' has no attribute 'inject_and_mask'`

- [ ] **Step 3: Write minimal implementation**

At the top of `lfg_core/ape_face.py`, extend the imports:

```python
from typing import Any

from lfg_core import swap_meta
```

Append to `lfg_core/ape_face.py`:

```python
def _nose_index(layers: list[tuple[str, str, str]]) -> int:
    """Index at which to insert the nose: directly after the Eyes layer, else
    the canonical Eyes slot (before the first layer that sorts after Eyes)."""
    for i, (trait_type, _v, _p) in enumerate(layers):
        if trait_type == "Eyes":
            return i + 1
    eyes_rank = swap_meta.TRAIT_ORDER.index("Eyes")
    for i, (trait_type, _v, _p) in enumerate(layers):
        if (
            trait_type in swap_meta.TRAIT_ORDER
            and swap_meta.TRAIT_ORDER.index(trait_type) > eyes_rank
        ):
            return i
    return len(layers)


async def inject_and_mask(
    layers: list[tuple[str, str, str]],
    body: str,
    body_value: str,
    store: Any,
    out_dir: str,
) -> list[tuple[str, str, str]]:
    """Apply ape compose rules to a canonical-ordered (trait_type, value, path)
    list: clip masked face features (melt/xray apes) and inject the fixed nose
    above Eyes (all apes). Non-ape bodies are returned unchanged."""
    if body != "ape":
        return layers

    result = list(layers)
    if body_value in MASKED_BODY_VALUES:
        mask_path = await store.resolve_asset(f"{body}/{MASK_ASSET}")
        if mask_path is None:
            raise FileNotFoundError(f"{body}/{MASK_ASSET}")
        result = [
            (t, v, apply_alpha_mask(p, mask_path, out_dir) if should_mask(t, v, body_value) else p)
            for (t, v, p) in result
        ]

    nose_path = await store.resolve_asset(f"{body}/{NOSE_ASSET}")
    if nose_path is None:
        raise FileNotFoundError(f"{body}/{NOSE_ASSET}")
    result.insert(_nose_index(result), ("Nose", "Nose", nose_path))
    return result
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_ape_face.py -q`
Expected: PASS (12 passed)

- [ ] **Step 5: Commit**

```bash
ruff format lfg_core/ape_face.py tests/test_ape_face.py
git add lfg_core/ape_face.py tests/test_ape_face.py
git commit -m "feat(ape_face): inject_and_mask transform (nose + melt-ape clip)"
```

---

### Task 5: Wire `ape_face` into `compose_nft` + ordering refactor + temp cleanup

Split the ordering into a canonical pass and a TOP-float pass so the ape transform can run in between, then route compose through it and clean up masked temp files.

**Files:**
- Modify: `lfg_core/swap_compose.py` (replace `_ordered_traits` with `_canonical`/`_float_tops`; rewrite `compose_nft`; update imports)
- Test: `tests/test_swap_compose.py` (create)

**Interfaces:**
- Consumes: `ape_face.inject_and_mask` (Task 4); `swap_meta.get_attr`.
- Produces (internal): `_canonical(attributes) -> list[dict]`, `_float_tops(layers: list[tuple[str, str, str]]) -> list[tuple[str, str, str]]`. `compose_nft` signature is unchanged: `compose_nft(attributes, body, store, output_basename, out_dir="generated") -> tuple[str, bool]`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_swap_compose.py`:

```python
# tests/test_swap_compose.py
import asyncio
import os

from PIL import Image

from lfg_core import layer_store, swap_compose


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _png(path, color=(1, 2, 3, 255), size=(4, 4)):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    Image.new("RGBA", size, color).save(path)


def _mask_right_opaque(path, size=(4, 4)):
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    for y in range(size[1]):
        for x in range(size[0] // 2, size[0]):
            img.putpixel((x, y), (255, 255, 255, 255))
    img.save(path)


def _attrs(**kw):
    # minimal normalized-style attribute list
    return [{"trait_type": t, "value": v} for t, v in kw.items()]


def test_compose_nft_ape_inserts_nose_and_masks(tmp_path, monkeypatch):
    captured = {}

    def fake_run(files, output_path, is_video):
        captured["files"] = list(files)
        with open(output_path, "wb") as f:
            f.write(b"x")

    monkeypatch.setattr(swap_compose, "_run_ffmpeg", fake_run)

    base = tmp_path / "layers" / "ape"
    _png(str(base / "Body" / "Ape Melting XRay.png"))
    _png(str(base / "Eyes" / "Creepy.png"), color=(0, 255, 0, 255))
    _png(str(base / "Head" / "Cap.png"))
    _png(str(base / "Nose.png"), color=(0, 0, 0, 0))
    _mask_right_opaque(base / "Ape Mask.png")
    store = layer_store.LocalLayerStore(str(tmp_path / "layers"))

    attrs = _attrs(Body="Ape Melting XRay", Eyes="Creepy", Head="Cap")
    out_dir = str(tmp_path / "gen")
    path, is_video = _run(swap_compose.compose_nft(attrs, "ape", store, "out", out_dir=out_dir))

    files = captured["files"]
    names = [os.path.basename(f) for f in files]
    assert "Nose.png" in names
    assert names.index("Nose.png") == names.index("Creepy.masked.png") + 1  # nose above eyes
    assert is_video is False
    assert os.path.isfile(path)
    # masked temp cleaned up after compose
    assert not os.path.isfile(os.path.join(out_dir, "Creepy.masked.png"))


def test_compose_nft_non_ape_has_no_nose(tmp_path, monkeypatch):
    captured = {}
    monkeypatch.setattr(
        swap_compose, "_run_ffmpeg",
        lambda files, output_path, is_video: captured.__setitem__("files", list(files))
        or open(output_path, "wb").write(b"x"),
    )
    base = tmp_path / "layers" / "male"
    _png(str(base / "Body" / "Straight Dark.png"))
    _png(str(base / "Eyes" / "Standard.png"))
    store = layer_store.LocalLayerStore(str(tmp_path / "layers"))

    attrs = _attrs(Body="Straight Dark", Eyes="Standard")
    _run(swap_compose.compose_nft(attrs, "male", store, "out", out_dir=str(tmp_path / "gen")))
    names = [os.path.basename(f) for f in captured["files"]]
    assert "Nose.png" not in names
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_swap_compose.py -q`
Expected: FAIL — `Nose.png` not in the file list (compose does not yet inject it).

- [ ] **Step 3: Write minimal implementation**

In `lfg_core/swap_compose.py`, ensure imports include (Task 2 already added `ape_face` and `TOP_TRAITS`):

```python
from lfg_core import ape_face, swap_meta
from lfg_core.ape_face import TOP_TRAITS
from lfg_core.swap_meta import TRAIT_ORDER
```

Replace `_ordered_traits` (lines ~25-36) with:

```python
def _canonical(attributes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Canonical layer order; 'None'/empty values skipped (no layer file)."""
    return sorted(
        (a for a in attributes if a.get("value") and a["value"] != "None"),
        key=lambda a: TRAIT_ORDER.index(a["trait_type"]),
    )


def _float_tops(layers: list[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
    """Move TOP_TRAITS effect layers to the end (rendered on top)."""
    tops = [lyr for lyr in layers if {"trait_type": lyr[0], "value": lyr[1]} in TOP_TRAITS]
    rest = [lyr for lyr in layers if lyr not in tops]
    return rest + tops
```

Rewrite `compose_nft` (lines ~52-77):

```python
async def compose_nft(
    attributes: list[dict[str, Any]],
    body: str,
    store: Any,
    output_basename: str,
    out_dir: str = "generated",
) -> tuple[str, bool]:
    """Resolve all trait layers through the store, apply the ape face rule
    (nose + melt-ape masking), float TOP effects, and overlay.
    Returns (output_path, is_video)."""
    canonical = _canonical(attributes)
    paths = await asyncio.gather(
        *(store.resolve(body, a["trait_type"], a["value"]) for a in canonical)
    )
    for a, path in zip(canonical, paths, strict=False):
        if not path:
            raise FileNotFoundError(f"Layer not found: {body}/{a['trait_type']}/{a['value']}")
    if not canonical:
        raise ValueError("No trait layers to compose")

    layers = [(a["trait_type"], a["value"], p) for a, p in zip(canonical, paths, strict=False)]
    body_value = swap_meta.get_attr(attributes, "Body") or ""
    layers = await ape_face.inject_and_mask(layers, body, body_value, store, out_dir)
    layers = _float_tops(layers)
    files = [p for _t, _v, p in layers]
    masked_temps = [p for p in files if p.endswith(".masked.png")]

    os.makedirs(out_dir, exist_ok=True)
    is_video = any(not f.endswith(".png") for f in files)
    ext = "mp4" if is_video else "png"
    output_path = os.path.join(out_dir, f"{output_basename}.{ext}")
    try:
        await asyncio.to_thread(_run_ffmpeg, files, output_path, is_video)
    finally:
        for tmp in masked_temps:
            if os.path.exists(tmp):
                os.remove(tmp)
    logging.info(f"Composed NFT: {output_path}")
    return output_path, is_video
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_swap_compose.py -q`
Expected: PASS (2 passed)

Run the full suite (the `_ordered_traits` → `_canonical`/`_float_tops` split must not regress mint/swap):
Run: `.venv/bin/python -m pytest -q`
Expected: PASS (no new failures)

- [ ] **Step 5: Commit**

```bash
ruff format lfg_core/swap_compose.py tests/test_swap_compose.py
git add lfg_core/swap_compose.py tests/test_swap_compose.py
git commit -m "feat(swap_compose): apply ape face rule in compose_nft; split ordering"
```

---

### Task 6: Pre-burn `missing_layers` checks ape assets

Make the pre-burn gate report missing `ape/Nose.png` / `ape/Ape Mask.png` so a melt-ape swap never burns and then fails at compose.

**Files:**
- Modify: `lfg_core/swap_compose.py` (rewrite `missing_layers`, lines ~39-49)
- Test: `tests/test_swap_compose.py` (append)

**Interfaces:**
- Consumes: `ape_face.NOSE_ASSET`, `ape_face.MASK_ASSET`, `ape_face.MASKED_BODY_VALUES`; `store.resolve_asset`; `swap_meta.get_attr`.
- Produces (unchanged signature): `missing_layers(attributes, body, store) -> list[str]`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_swap_compose.py`:

```python
def test_missing_layers_flags_ape_assets(tmp_path):
    base = tmp_path / "layers" / "ape"
    _png(str(base / "Body" / "Ape Melting.png"))
    _png(str(base / "Eyes" / "Creepy.png"))
    # No Nose.png and no Ape Mask.png present.
    store = layer_store.LocalLayerStore(str(tmp_path / "layers"))

    attrs = _attrs(Body="Ape Melting", Eyes="Creepy")
    missing = _run(swap_compose.missing_layers(attrs, "ape", store))
    assert "ape/Nose.png" in missing
    assert "ape/Ape Mask.png" in missing


def test_missing_layers_non_melt_ape_needs_nose_not_mask(tmp_path):
    base = tmp_path / "layers" / "ape"
    _png(str(base / "Body" / "Ape Gold.png"))
    _png(str(base / "Eyes" / "Creepy.png"))
    _png(str(base / "Nose.png"))
    store = layer_store.LocalLayerStore(str(tmp_path / "layers"))

    attrs = _attrs(Body="Ape Gold", Eyes="Creepy")
    missing = _run(swap_compose.missing_layers(attrs, "ape", store))
    assert missing == []  # nose present; mask not required for Ape Gold


def test_missing_layers_non_ape_ignores_ape_assets(tmp_path):
    base = tmp_path / "layers" / "male"
    _png(str(base / "Body" / "Straight Dark.png"))
    store = layer_store.LocalLayerStore(str(tmp_path / "layers"))
    attrs = _attrs(Body="Straight Dark")
    assert _run(swap_compose.missing_layers(attrs, "male", store)) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_swap_compose.py -k missing_layers -q`
Expected: FAIL — `ape/Nose.png` not reported (current `missing_layers` ignores ape assets).

- [ ] **Step 3: Write minimal implementation**

Replace `missing_layers` in `lfg_core/swap_compose.py`:

```python
async def missing_layers(attributes: list[dict[str, Any]], body: str, store: Any) -> list[str]:
    """Trait + ape-structural files the store can't provide — checked BEFORE
    any burn."""
    canonical = _canonical(attributes)
    resolved = await asyncio.gather(
        *(store.resolve(body, a["trait_type"], a["value"]) for a in canonical)
    )
    missing = [
        f"{body}/{a['trait_type']}/{a['value']}"
        for a, path in zip(canonical, resolved, strict=False)
        if not path
    ]
    if body == "ape":
        if await store.resolve_asset(f"{body}/{ape_face.NOSE_ASSET}") is None:
            missing.append(f"{body}/{ape_face.NOSE_ASSET}")
        body_value = swap_meta.get_attr(attributes, "Body") or ""
        if (
            body_value in ape_face.MASKED_BODY_VALUES
            and await store.resolve_asset(f"{body}/{ape_face.MASK_ASSET}") is None
        ):
            missing.append(f"{body}/{ape_face.MASK_ASSET}")
    return missing
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_swap_compose.py -q`
Expected: PASS (5 passed)

Run the full suite:
Run: `.venv/bin/python -m pytest -q`
Expected: PASS (no new failures)

- [ ] **Step 5: Commit**

```bash
ruff format lfg_core/swap_compose.py tests/test_swap_compose.py
git add lfg_core/swap_compose.py tests/test_swap_compose.py
git commit -m "feat(swap_compose): missing_layers gates ape nose/mask pre-burn"
```

---

## Final verification

- [ ] Run the whole suite + lint + types:
  - `.venv/bin/python -m pytest -q`
  - `.venv/bin/ruff format --check .` (or `ruff format .`)
  - `.venv/bin/ruff check .`
  - `.venv/bin/mypy lfg_core/ape_face.py lfg_core/swap_compose.py lfg_core/layer_store.py`
- [ ] Manual render sanity check on a melt ape (testnet mint or `scripts/_economy_deps` compose) to **confirm the mask clips the correct (right) side**. If it clips the left, drop the `255 -` / the `ImageChops.invert` in `apply_alpha_mask` (one-line flip) and re-run Task 3 tests with the direction inverted.
- [ ] Populate `ape_face.NO_MASK_VALUES` from an art review of ape Eyes/Eyebrows/Mouth values vs. the melt silhouette (post-merge, low-risk one-line additions).

## Notes for the implementer

- The compose integration tests monkeypatch `_run_ffmpeg` so the suite never shells out to ffmpeg — keep it that way; the masking correctness is covered by the pure `apply_alpha_mask` tests.
- `inject_and_mask` raising `FileNotFoundError` is the hard stop; `missing_layers` is the pre-burn soft gate. Both must agree on asset paths (`ape/Nose.png`, `ape/Ape Mask.png`).
- Do not add `Nose` to `TRAIT_ORDER`/`SWAPPABLE_TRAITS`; it is injected positionally and must stay out of metadata.
