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

# Effect traits that render on top of everything else (e.g. laser eyes).
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
