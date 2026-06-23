# lfg_core/trait_economy.py
# Pure accounting core for the NFT dress-up trait economy. No I/O.
# An "asset" is a (slot, value) pair over the 9 TRAIT_ORDER slots. The Body slot
# is identity-bound (one body == one edition); the 8 non-body slots are pooled
# and counted by (slot, value), where "None" is itself a real, conserved asset.

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any

from lfg_core import swap_meta
from lfg_core.nft_index import OnchainNft

NON_BODY_SLOTS: list[str] = [s for s in swap_meta.TRAIT_ORDER if s != "Body"]


@dataclass
class Genesis:
    trait_counts: dict[tuple[str, str], int]
    edition_bodies: dict[int, tuple[str, str]]


@dataclass
class Census:
    trait_counts: dict[tuple[str, str], int]
    body_presence: dict[int, int]


@dataclass
class ConservationReport:
    trait_drift: dict[tuple[str, str], int]
    body_drift: dict[int, int]
    ok: bool


@dataclass
class CompletenessReport:
    wrong_body: dict[int, tuple[str, str]]
    orphan_bodies: list[int]
    slot_anomalies: dict[int, list[str]]
    ok: bool


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


def verify_completeness(characters: dict[int, OnchainNft], genesis: Genesis) -> CompletenessReport:
    """Completeness: every live character holds exactly one asset per non-body
    slot and its body matches the genesis body ledger."""
    wrong_body: dict[int, tuple[str, str]] = {}
    orphan_bodies: list[int] = []
    slot_anomalies: dict[int, list[str]] = {}

    for edition, rec in characters.items():
        seen: Counter[str] = Counter(
            a["trait_type"] for a in rec.attributes if a.get("trait_type") in NON_BODY_SLOTS
        )
        bad_slots = [s for s in NON_BODY_SLOTS if seen.get(s, 0) > 1]
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
