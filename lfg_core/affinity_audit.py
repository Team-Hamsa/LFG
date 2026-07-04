# lfg_core/affinity_audit.py
# Derive per-(trait_type, value) body affinity from historical mint data.
# Pure logic — no file or DB I/O — so scripts/audit_body_affinity.py stays a
# thin CLI and the derivation is unit-testable.

import json
from collections import Counter

from lfg_core.swap_meta import detect_body

LOW_CONFIDENCE_THRESHOLD = 3

BODIES = ["ape", "female", "male", "skeleton"]

# Physically 4 identical per-body copies today (not per-body art) — per-body
# affinity for these is sampling noise, not signal. They become `shared: true`
# in trait_config, so the draft YAML must not propose per-body restrictions
# for them. (The report table still lists their rows; its existing note
# already tells reviewers to be skeptical of them.)
SHARED_LAYERS = ("Background", "Back")


def count_affinities(
    rows: list[tuple[int | None, str | None, str]],
) -> dict[tuple[str, str], Counter[str]]:
    """rows: (nft_number, body, attributes_json) per historical token (burned
    included). Body falls back to detect_body(attributes) when the column is
    empty.

    `onchain_nfts` is keyed by nft_id, not nft_number — one edition can have
    several tokens on chain (remints / trait-swaps), so without dedupe a
    single edition's (value, body) pair would be counted once per token
    instead of once per edition. Evidence is deduped on
    (nft_number, body, trait_type, value): each edition contributes a given
    (value, body) pair at most once, but different values seen across an
    edition's swap history (e.g. Hat swapped from A to B) still each count —
    that's real historical evidence the value was minted on that body.

    Rows with a NULL nft_number (should not happen post-backfill, but the
    column is nullable) fall back to their row position as a uniqueness key,
    i.e. dedupe is skipped for them — simpler than a synthetic identity, and
    they're rare/legacy so a little over-counting there is an acceptable
    tradeoff.
    """
    counts: dict[tuple[str, str], Counter[str]] = {}
    seen: set[tuple[int, str, str, str]] = set()
    for idx, (nft_number, body, attributes_json) in enumerate(rows):
        try:
            attributes = json.loads(attributes_json) if attributes_json else []
        except (TypeError, ValueError):
            attributes = []
        if not attributes:
            continue
        body = body or detect_body(attributes)
        edition_key = nft_number if nft_number is not None else -(idx + 1)
        for attr in attributes:
            trait_type, value = attr.get("trait_type"), attr.get("value")
            if not trait_type or not value or trait_type == "Body":
                continue
            if nft_number is not None:
                dedupe_key = (edition_key, body, trait_type, value)
                if dedupe_key in seen:
                    continue
                seen.add(dedupe_key)
            counts.setdefault((trait_type, value), Counter())[body] += 1
    return counts


def classify(counts: Counter[str]) -> str:
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


def cross_check(
    counts: dict[tuple[str, str], Counter[str]],
    dir_tree: dict[str, dict[str, set[str]]],
) -> tuple[list[tuple[str, str, str]], list[tuple[str, str, str]]]:
    """misplacements: value present in a body dir but never minted on that
    body (candidate misplacement OR intentionally-new — human decides).
    coverage_gaps: value minted on a body historically but absent from its
    dir today."""
    misplacements = []
    for body, types in dir_tree.items():
        for trait_type, values in types.items():
            for value in values:
                if value == "None":
                    continue
                if counts.get((trait_type, value), Counter()).get(body, 0) == 0:
                    misplacements.append((body, trait_type, value))
    coverage_gaps = []
    for (trait_type, value), body_counts in counts.items():
        if value == "None":
            continue
        for body, n in body_counts.items():
            if n > 0 and value not in dir_tree.get(body, {}).get(trait_type, set()):
                coverage_gaps.append((body, trait_type, value))
    return sorted(misplacements), sorted(coverage_gaps)


def render_affinity_yaml(counts: dict[tuple[str, str], Counter[str]]) -> str:
    """Draft affinity: section, values grouped by trait type, alphabetical,
    low-confidence entries commented with their counts."""
    by_type: dict[str, list[str]] = {}
    for (trait_type, value), body_counts in sorted(counts.items()):
        if value == "None":
            # None = empty slot, structural, never a real affinity.
            continue
        if trait_type in SHARED_LAYERS:
            # Shared layers (see SHARED_LAYERS) get no per-body affinity
            # entries — they become `shared: true` in trait_config instead.
            continue
        bodies = sorted(b for b, n in body_counts.items() if n > 0)
        total = sum(body_counts.values())
        line = f'    "{value}": [{", ".join(bodies)}]'
        if total < LOW_CONFIDENCE_THRESHOLD:
            line += f"  # LOW CONFIDENCE: only {total} mint(s) — verify by eye"
        by_type.setdefault(trait_type, []).append(line)
    out = ["affinity:"]
    for trait_type in sorted(by_type):
        out.append(f"  {trait_type}:")
        out.extend(by_type[trait_type])
    return "\n".join(out) + "\n"


def render_report_md(
    counts: dict[tuple[str, str], Counter[str]],
    misplacements: list[tuple[str, str, str]],
    coverage_gaps: list[tuple[str, str, str]],
) -> str:
    """Render body-affinity audit report with counts table, misplacements, and
    coverage gaps."""
    lines = ["# Body-affinity audit report", ""]
    lines.append(
        "> Note: Background and Back are shared layers (4 identical per-body "
        "copies today); treat their per-body restrictions in this report as "
        "sampling noise, not signal."
    )
    lines.append("")
    lines.append("## Per-value affinity (from mint history, burned included)")
    lines.append("")
    lines.append("| Trait type | Value | Classification | Counts |")
    lines.append("|---|---|---|---|")
    for (trait_type, value), body_counts in sorted(counts.items()):
        if value == "None":
            # None = empty slot, structural, never a real affinity.
            continue
        label = classify(body_counts)
        detail = ", ".join(f"{b}:{n}" for b, n in sorted(body_counts.items()) if n)
        flag = " ⚠️" if sum(body_counts.values()) < LOW_CONFIDENCE_THRESHOLD else ""
        lines.append(f"| {trait_type} | {value} | {label}{flag} | {detail} |")
    lines += ["", "## Candidate misplacements (in dir, never minted there)", ""]
    lines += [f"- {b}/{t}/{v}" for b, t, v in misplacements] or ["- none"]
    lines += ["", "## Coverage gaps (minted historically, missing from dir)", ""]
    lines += [f"- {b}/{t}/{v}" for b, t, v in coverage_gaps] or ["- none"]
    return "\n".join(lines) + "\n"
