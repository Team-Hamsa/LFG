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
from typing import Any

from PIL import Image, ImageChops

from lfg_core import swap_meta

# Effect traits that render on top of everything else (e.g. laser eyes).
# Lives here (not swap_compose) so the masking rule can reuse it without a
# circular import; swap_compose imports TOP_TRAITS from this module.
# Source of truth is trait_config.yaml (z_overrides / layers). Keep in sync;
# test_default_config_parity_with_legacy_constants enforces the parity.
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


def apply_alpha_mask(layer_path: str, mask_path: str, out_dir: str) -> str:
    """Write a temp PNG of ``layer`` with its alpha cleared where ``mask`` is
    opaque (subtractive: out_alpha = layer_alpha * (255 - mask_alpha) / 255).
    Returns the temp path. Raises ValueError if ``layer_path`` is not a PNG."""
    if not layer_path.lower().endswith(".png"):
        raise ValueError(f"cannot mask non-PNG slot: {layer_path}")
    layer = Image.open(layer_path).convert("RGBA")
    mask = Image.open(mask_path).convert("RGBA")
    if mask.size != layer.size:
        # NEAREST keeps the binary mask's hard edge (no interpolated fringe).
        mask = mask.resize(layer.size, resample=Image.Resampling.NEAREST)
    inv = ImageChops.invert(mask.getchannel("A"))
    new_alpha = ImageChops.multiply(layer.getchannel("A"), inv)
    layer.putalpha(new_alpha)
    os.makedirs(out_dir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(layer_path))[0]
    out_path = os.path.join(out_dir, f"{stem}.masked.png")
    layer.save(out_path)
    return out_path


def _nose_index(layers: list[tuple[str, str, str]]) -> int:
    """Index at which to insert the nose: directly after the Eyes layer, else
    the canonical Eyes slot (before the first layer that sorts after Eyes).

    TOP_TRAITS Eyes (Wavy / Laser…) are skipped as anchors: compose z-sorts
    them to the very end of the list (z_override 95, above Head/Accessory),
    so anchoring the nose on a floated effect Eyes tuple would drag it to
    the top of the stack instead of its face slot below Head. With the
    effect Eyes skipped, the canonical-slot fallback places the nose where
    it belongs."""
    for i, (trait_type, value, _p) in enumerate(layers):
        if trait_type == "Eyes" and {"trait_type": trait_type, "value": value} not in TOP_TRAITS:
            return i + 1
    eyes_rank = swap_meta.TRAIT_ORDER.index("Eyes")
    for i, (trait_type, value, _p) in enumerate(layers):
        # Stop before any TOP_TRAIT to keep nose below effect layers (e.g. Laser Eyes).
        if {"trait_type": trait_type, "value": value} in TOP_TRAITS:
            return i
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
