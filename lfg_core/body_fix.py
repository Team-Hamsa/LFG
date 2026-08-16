# lfg_core/body_fix.py — #301: pure Body-value rewrite for the misspelled
# "Iridescent Skeleton" (single-r, no art) → "Irridescent Skeleton" (double-r,
# the canonical spelling the layer tree carries). No I/O; the correction
# driver (scripts/fix_iridescent_body.py) and its tests are the consumers.

import copy
from typing import Any

BAD = "Iridescent Skeleton"  # single-r — no layer art, fails missing_layers
GOOD = "Irridescent Skeleton"  # double-r — canonical, has art


def rewrite_body_value(meta: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Return a copy of `meta` with any Body attribute valued BAD swapped to
    GOOD, plus a `changed` flag. Touches nothing else; idempotent (already-GOOD
    or Body-absent metadata comes back unchanged with changed=False)."""
    out = copy.deepcopy(meta)
    changed = False
    for attr in out.get("attributes") or []:
        if isinstance(attr, dict) and attr.get("trait_type") == "Body" and attr.get("value") == BAD:
            attr["value"] = GOOD
            changed = True
    return out, changed
