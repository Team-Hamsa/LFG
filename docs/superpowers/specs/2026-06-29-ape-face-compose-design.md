# Ape Face Compose Rule — Design

**Date:** 2026-06-29
**Status:** Design (pending implementation plan)
**Scope:** A compose-time rendering rule for ape NFTs: inject a fixed nose
layer on all apes, and clip the right side of face-anchored features on the
melt/xray ape bodies so they conform to the melted face silhouette.

## Background

The ape artwork was reworked: the built-in eyes/eyebrows/mouth (and nose) were
removed from the ape **Body** PNGs and split into separate, independently
selectable trait layers under `layers/ape/<TraitType>/`. Two new fixed assets
were added at the ape root on the CDN:

- `layers/ape/Nose.png` — a single fixed nose overlay (1080², RGBA, mostly
  transparent). Not a selectable trait set; there is exactly one nose.
- `layers/ape/Ape Mask.png` — a single shared alpha mask (1080², palette+alpha):
  **transparent on the left half, opaque white on the right half**. Its opaque
  region marks the part of a face feature to remove.

On the three "melted"/"xray" ape bodies — `Ape Xray`, `Ape Melting`,
`Ape Melting XRay` — the right side of the face is melted/transparent, so a
normally-positioned eye/eyebrow/mouth would float in empty space. The mask clips
that right side away.

This rule must apply to **every** composition path. As of this design there is a
single unified compositor, `lfg_core/swap_compose.py` (`compose_nft`), used by:

- `lfg_core/mint_flow.py` (original mint),
- `lfg_core/swap_flow.py` (trait swap),
- `scripts/_economy_deps.py` (economy assemble/extract).

`ts_helpers.makeNft()` is legacy and has **no non-test callers**; it is out of
scope. Implementing the rule once in `swap_compose` covers all live paths.

## Goals

1. Inject the fixed `Nose.png` overlay on **all** ape bodies, directly above the
   Eyes layer.
2. Clip the right side of **face-anchored** Eyes/Eyebrows/Mouth features on the
   three melt/xray bodies, using `Ape Mask.png`.
3. Leave **effect** traits (lasers, rainbow puke, wavy) and any curated
   exemptions un-clipped — clipping an emanation in half looks broken.
4. Keep the rule out of NFT metadata: the nose and mask are structural compose
   assets, never trait attributes, never rarity-selected, never swappable.
5. Fail **before** any irreversible step (the swap burn) if a required ape asset
   is missing.

## Non-goals

- No change to trait *selection* (`traits.py`). The ape-specific trait set is
  already selected automatically because the values live under `layers/ape/...`.
- No change to `TRAIT_ORDER` / `SWAPPABLE_TRAITS` — `Nose` is deliberately NOT a
  trait type.
- No video-layer masking. All ape face traits (Eyes/Eyebrows/Mouth) are
  confirmed PNG; a non-PNG masked slot is treated as an error (future-proofing).
- No per-body masks. One shared `Ape Mask.png` covers all three bodies.

## Architecture

```
lfg_core/ape_face.py        # NEW — constants, should_mask(), plan helpers,
                            #       apply_alpha_mask() (pure Pillow)
lfg_core/swap_compose.py    # calls ape_face from compose_nft + missing_layers
lfg_core/layer_store.py     # NEW — resolve_asset(rel_path) on both stores
```

The novel/risky logic (the alpha math and the masked-set decision) is isolated
in `ape_face.py` as pure, unit-testable functions. The proven ffmpeg overlay
chain in `swap_compose._run_ffmpeg` is unchanged — it still just overlays a list
of files. Masking happens by substituting masked temp PNGs into that list before
it reaches ffmpeg.

### Constants (`ape_face.py`)

```python
NOSE_ASSET = "Nose.png"          # resolves under the ape body dir: "ape/Nose.png"
MASK_ASSET = "Ape Mask.png"      # resolves to "ape/Ape Mask.png"

MASKED_BODY_VALUES = {"Ape Xray", "Ape Melting", "Ape Melting XRay"}
MASKED_TRAITS      = {"Eyes", "Eyebrows", "Mouth"}

# Effects that extend past the face and must never be clipped. Shares the shape
# of swap_compose.TOP_TRAITS. The TOP_TRAITS effect set is auto-exempt; this list
# is the curated, art-driven addition, grown as the artist reviews renders.
NO_MASK_VALUES: list[dict[str, str]] = [
    # {"trait_type": "Mouth", "value": "Cigar"},
    # {"trait_type": "Eyes",  "value": "Side Lashes"},
]
```

The `TOP_TRAITS` effect set currently lives in `swap_compose.py`. To avoid a
circular import, the implementation will either (a) have `ape_face` reference it
one-way, or (b) move the constant into `ape_face` and have `swap_compose` import
it. Either is acceptable; the implementer picks the cleaner direction. This
design assumes the effect set is reachable from `ape_face`.

### Masking decision

```python
def should_mask(trait_type: str, value: str, body_value: str) -> bool:
    if body_value not in MASKED_BODY_VALUES:   return False
    if trait_type not in MASKED_TRAITS:        return False
    pair = {"trait_type": trait_type, "value": value}
    if pair in TOP_TRAITS:                     return False   # Laser/Wavy/Rainbow Puke
    if pair in NO_MASK_VALUES:                 return False   # curated skip-list
    return True
```

`body_value` is the on-chain **Body** trait value (e.g. `"Ape Melting XRay"`),
read from the NFT's normalized attributes via `swap_meta.get_attr(attributes,
"Body")`. The `body` argument already passed to `compose_nft` is the body *class*
(`"ape"`), which determines nose injection but is too coarse for the masked set.

### Nose injection + layer order

The nose is injected for every ape (body class `"ape"`) as a normal,
non-top layer placed directly **above** the Eyes layer. Final overlay order for
a melt ape (bottom → top):

```
Background · Back · Body · Clothing · Mouth* · Eyebrows* · Eyes* · Nose · Head · Accessory
                                       └──── *right-clipped via Ape Mask.png ────┘
   (TOP_TRAITS — Laser / Laser Eyes / Wavy / Rainbow Puke — still float to the very
    top, above Nose; they are also exempt from masking.)
```

**Placement rule:** insert the nose immediately after the Eyes layer in the
ordered list. If Eyes is `None`/absent, insert it at the canonical Eyes slot
(after Eyebrows, before Head). Because the nose is a non-top layer, animated
laser/wavy eyes (TOP_TRAITS) still render above it — matching today's behavior.

### Masking function (pure Pillow)

```python
def apply_alpha_mask(layer_path: str, mask_path: str, out_dir: str) -> str:
    """Return a temp PNG of `layer` with its alpha cleared where `mask` is
    opaque (subtractive). out_alpha = layer_alpha * (255 - mask_alpha) / 255."""
```

- Opens both as RGBA. If the mask size differs from the layer size, the mask is
  resized to the layer (both are 1080² today; this is a cheap safety net).
- Computes `inv = 255 - mask.alpha`, then `new_alpha = layer.alpha * inv / 255`
  (via `ImageChops.multiply`), reattaches it to the layer's RGB, and writes a
  temp PNG into `out_dir` alongside the other generated files.
- **Direction:** the mask is opaque-right, so this clips the right side. This is
  the one visual assumption to confirm on the first render; reversing it is a
  one-line change (drop the `255 -`).
- If `layer_path` is not a PNG, raise `ValueError(f"cannot mask non-PNG slot:
  ...")`. All ape face traits are PNG today; this guards against an animated eye
  being added later without revisiting masking.

Masked temp files live in the compose `out_dir` and are removed alongside the
composed output (existing cleanup in `upload_output` / compose teardown).

### Integration in `compose_nft`

After resolving the ordered trait files, a single transform step:

1. If body class is `"ape"`: resolve `ape/Nose.png` via `store.resolve_asset`
   and insert it above Eyes (per placement rule).
2. If `body_value in MASKED_BODY_VALUES`: resolve `ape/Ape Mask.png` once, then
   for each resolved face-feature file where `should_mask(...)` is true, replace
   its path with `apply_alpha_mask(path, mask_path, out_dir)`.
3. Hand the resulting file list to the unchanged `_run_ffmpeg`.

### Pre-burn safety (`missing_layers`) + `resolve_asset`

`missing_layers` runs **before any burn** in the swap path. It must also confirm
the ape-face assets resolve, or a melt-ape swap could burn and then fail at
compose:

- For any ape: `ape/Nose.png` must resolve.
- For a melt/xray ape: `ape/Ape Mask.png` must resolve.

Missing assets are reported in the same list as missing trait layers (e.g.
`ape/Nose.png`, `ape/Ape Mask.png`), keeping the existing fail-safe contract.

New store method on both `LocalLayerStore` and `CdnLayerStore`:

```python
async def resolve_asset(self, rel_path: str) -> str | None:
    """Resolve an arbitrary file under the layer root (e.g. 'ape/Nose.png'),
    downloading + caching for CDN. Returns a local path, or None if absent."""
```

- Local: return the path if the file exists, else `None`.
- CDN: split `rel_path` into parent dir + filename, list the parent (reusing the
  cached listing), and `_download` the file if present. No empty-trait-type
  hacks, no double-slash URLs.

## Error handling

| Condition | Behavior |
| --- | --- |
| Affected ape, mask/nose asset unresolvable | Reported by `missing_layers` **before** any burn; named like a missing trait. |
| Masked slot resolves to non-PNG | `ValueError` with the slot name; compose aborts. Cannot occur today (all ape face traits are PNG); `missing_layers` checks existence, not file type, so this is a hard guard, not a pre-burn check. |
| Mask / layer size mismatch | Mask resized to layer size; no error. |
| Non-ape body | Rule is a no-op; compose unchanged. |

## Test plan

New `tests/test_ape_face.py` plus additions to the compose tests:

- **`apply_alpha_mask`** with synthetic 4×4 RGBA images: opaque layer + opaque-
  right mask → output alpha `0` on the right, `255` on the left; RGB preserved on
  the kept side. Explicitly assert the clip direction (right removed).
- **`apply_alpha_mask`** raises `ValueError` on a non-PNG path.
- **`should_mask`**: `"Ape Xray"` + normal Eyes → True; `"Ape Xray"` + Rainbow
  Puke (TOP) → False; `"Ape Xray"` + a `NO_MASK_VALUES` entry → False;
  `"Ape Gold"` (ape, not melt) + Eyes → False; `"Straight Dark"` (male) → False.
- **Nose placement**: nose lands directly above Eyes; with a TOP eyes effect the
  nose stays below it; Eyes=`None` fallback slot (after Eyebrows, before Head).
- **`compose_nft` integration** over a `LocalLayerStore` fixture (tiny ape tree +
  `Nose.png` + `Ape Mask.png`): produces output for a melt ape; nose present;
  masked-trait alpha cleared on the right in the output (pixel check).
- **`missing_layers`**: includes `ape/Nose.png` for any ape and `ape/Ape Mask.png`
  for a melt ape when those assets are absent from the fixture.

## Explicit decisions

1. **Nose** is a single fixed asset (not a selectable trait set) and never
   appears in NFT metadata.
2. **Nose is not masked** — rendered whole even on melt apes.
3. **Mask is subtractive and clips the right side** (verify by eye on the first
   render; reversing is one line).
4. **TOP_TRAITS effects float above the nose** and are exempt from masking.
5. **Masked set is opt-out**: all melt-ape Eyes/Eyebrows/Mouth are clipped except
   TOP_TRAITS effects (auto) and curated `NO_MASK_VALUES` entries (artist-grown,
   ships empty).

## Open items for implementation

- Decide the `TOP_TRAITS` constant location (reference vs. relocate) to avoid a
  circular import between `ape_face` and `swap_compose`.
- Confirm the mask clip direction on the first real melt-ape render and, if
  inverted, drop the `255 -` in `apply_alpha_mask`.
- Populate `NO_MASK_VALUES` from an art review of the ape Eyes/Eyebrows/Mouth
  values against the melt silhouette (post-merge, low-risk one-line additions).
