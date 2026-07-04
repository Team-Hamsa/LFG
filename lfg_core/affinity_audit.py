# lfg_core/affinity_audit.py
# Derive per-(trait_type, value) body affinity from historical mint data.
# Pure logic — no file or DB I/O — so scripts/audit_body_affinity.py stays a
# thin CLI and the derivation is unit-testable.

import json
from collections import Counter

from lfg_core.swap_meta import detect_body

LOW_CONFIDENCE_THRESHOLD = 3

BODIES = ["ape", "female", "male", "skeleton"]


def count_affinities(
    rows: list[tuple[str | None, str]],
) -> dict[tuple[str, str], Counter]:
    """rows: (body, attributes_json) per historical token (burned included).
    Body falls back to detect_body(attributes) when the column is empty."""
    counts: dict[tuple[str, str], Counter] = {}
    for body, attributes_json in rows:
        try:
            attributes = json.loads(attributes_json) if attributes_json else []
        except (TypeError, ValueError):
            attributes = []
        if not attributes:
            continue
        body = body or detect_body(attributes)
        for attr in attributes:
            trait_type, value = attr.get("trait_type"), attr.get("value")
            if not trait_type or not value or trait_type == "Body":
                continue
            counts.setdefault((trait_type, value), Counter())[body] += 1
    return counts


def classify(counts: Counter) -> str:
    bodies = {b for b, n in counts.items() if n > 0}
    if bodies == {"female"}:
        return "female-only"
    if bodies == {"male"}:
        return "male-only"
    if bodies == {"male", "female"}:
        return "shared-MF"
    if bodies == set(BODIES):
        return "universal"
    return "bodies:" + "+".join(sorted(bodies))
