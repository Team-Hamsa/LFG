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
