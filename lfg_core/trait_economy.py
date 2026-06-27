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
    closet_assets: list[tuple[str, str, str, int]],
    closet_bodies: list[tuple[str, int]],
    trait_tokens: list[tuple[str, str, str, str]],
) -> Census:
    """Tally every asset across live characters, Closets and standalone trait
    tokens. trait_counts are non-body (slot, value); body_presence counts how
    many places each edition's body currently exists (should be exactly 1)."""
    trait_counts: Counter[tuple[str, str]] = Counter()
    body_presence: Counter[int] = Counter()
    for edition, rec in characters.items():
        body_presence[edition] += 1
        for slot in NON_BODY_SLOTS:
            trait_counts[(slot, slot_value(rec, slot))] += 1
    for _owner, slot, value, count in closet_assets:
        trait_counts[(slot, value)] += count
    for _nft_id, _owner, slot, value in trait_tokens:
        trait_counts[(slot, value)] += 1
    for _owner, edition in closet_bodies:
        body_presence[edition] += 1
    return Census(trait_counts=dict(trait_counts), body_presence=dict(body_presence))


def effective_genesis(genesis: Genesis, supply_changes: list[dict[str, Any]]) -> Genesis:
    """Genesis with the intentional supply-change ledger folded in: the moving
    conservation target. `trait_deltas` are signed (mint positive, burn
    negative) and applied to the per-(slot,value) counts; a 'mint' adds the
    edition's body, a 'burn' removes it. Genesis itself is never mutated."""
    trait_counts: dict[tuple[str, str], int] = dict(genesis.trait_counts)
    edition_bodies: dict[int, tuple[str, str]] = dict(genesis.edition_bodies)
    for change in supply_changes:
        for key, delta in change.get("trait_deltas", {}).items():
            slot, _, value = str(key).partition("|")
            trait_counts[(slot, value)] = trait_counts.get((slot, value), 0) + int(delta)
        edition = change.get("edition")
        if edition is None:
            continue
        if change.get("kind") == "mint":
            edition_bodies[int(edition)] = (
                str(change.get("body_value", "")),
                str(change.get("body_class", "")),
            )
        elif change.get("kind") == "burn":
            edition_bodies.pop(int(edition), None)
    return Genesis(trait_counts=trait_counts, edition_bodies=edition_bodies)


def effective_max_edition(genesis: Genesis, supply_changes: list[dict[str, Any]]) -> int:
    """Highest edition number ever in scope — genesis editions plus every
    edition named in the ledger (kept even if later burned, so the number stays
    a valid re-mint target). Replaces the hard 3535 dedupe cap for new mints."""
    editions: set[int] = set(genesis.edition_bodies)
    for change in supply_changes:
        edition = change.get("edition")
        if edition is not None:
            editions.add(int(edition))
    return max(editions) if editions else 0


def verify_conservation(
    genesis: Genesis, census: Census, supply_changes: list[dict[str, Any]] | None = None
) -> ConservationReport:
    """Conservation: census must equal the EFFECTIVE genesis (genesis + the
    intentional supply-change ledger) for every (slot, value), and each
    effective edition's body must exist in exactly one place. A delta NOT
    explained by the ledger is reported as drift. Back-compatible: an empty/
    omitted ledger reduces to the original census-vs-genesis check."""
    eff = effective_genesis(genesis, supply_changes or [])
    trait_drift: dict[tuple[str, str], int] = {}
    for key in set(eff.trait_counts) | set(census.trait_counts):
        delta = census.trait_counts.get(key, 0) - eff.trait_counts.get(key, 0)
        if delta != 0:
            trait_drift[key] = delta

    body_drift: dict[int, int] = {}
    for edition in eff.edition_bodies:
        presence = census.body_presence.get(edition, 0)
        if presence != 1:
            body_drift[edition] = presence
    for edition, presence in census.body_presence.items():
        if edition not in eff.edition_bodies:
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
        bad_slots = [s for s in NON_BODY_SLOTS if seen.get(s, 0) != 1]
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


@dataclass(frozen=True)
class Precheck:
    """Result of an op precondition check: ok plus a human-readable reason
    (empty when ok). Flows refuse to touch the chain unless `ok`."""

    ok: bool
    reason: str = ""


_OK = Precheck(True, "")


def can_harvest(rec: OnchainNft, genesis: Genesis, burnable: bool) -> Precheck:
    """A character can be harvested iff it is a live, burnable token of a known
    edition whose on-chain body matches the (effective) body ledger. `genesis`
    is the EFFECTIVE genesis so harvested new editions are recognised too."""
    if rec.is_burned:
        return Precheck(False, "character is already burned")
    edition = rec.nft_number
    if edition is None or edition not in genesis.edition_bodies:
        return Precheck(False, "character has no known genesis edition")
    if not burnable:
        return Precheck(
            False, "character is not burnable (mutable-only); equip-only until re-minted"
        )
    expected = genesis.edition_bodies[edition][0]
    found = swap_meta.get_attr(rec.attributes, "Body") or ""
    if found != expected:
        return Precheck(False, f"body mismatch: on-chain {found!r} != ledger {expected!r}")
    return _OK


def can_assemble(
    edition: int,
    chosen: dict[str, str],
    owner_bodies: set[int],
    owner_assets: dict[tuple[str, str], int],
    live_editions: set[int],
    genesis: Genesis,
) -> Precheck:
    """An edition can be (re)assembled iff it is currently dead, its body is in
    the owner's bucket, and the owner's bucket covers a full, valid asset set
    (exactly one chosen value per non-body slot). `genesis` is effective."""
    if edition in live_editions:
        return Precheck(False, f"edition {edition} is already live")
    if edition not in genesis.edition_bodies:
        return Precheck(False, f"edition {edition} has no known body")
    if edition not in owner_bodies:
        return Precheck(False, f"bucket does not hold edition {edition}'s body")
    missing = [s for s in NON_BODY_SLOTS if s not in chosen]
    if missing:
        return Precheck(False, f"incomplete set, missing slots: {', '.join(missing)}")
    extra = [s for s in chosen if s not in NON_BODY_SLOTS]
    if extra:
        return Precheck(False, f"unknown slots in set: {', '.join(extra)}")
    need = Counter((s, chosen[s]) for s in NON_BODY_SLOTS)
    for (slot, value), qty in need.items():
        if owner_assets.get((slot, value), 0) < qty:
            return Precheck(False, f"bucket lacks asset {slot}={value}")
    return _OK


def can_equip(
    rec: OnchainNft,
    slot: str,
    value: str,
    owner_assets: dict[tuple[str, str], int],
    mutable: bool,
) -> Precheck:
    """A loose asset can be equipped onto a live, mutable character iff the slot
    is a non-body slot and the owner's bucket holds the incoming asset."""
    if rec.is_burned:
        return Precheck(False, "character is burned")
    if not mutable:
        return Precheck(False, "character is not mutable")
    if slot not in NON_BODY_SLOTS:
        return Precheck(False, f"{slot} is not an equippable slot")
    if owner_assets.get((slot, value), 0) < 1:
        return Precheck(False, f"bucket lacks asset {slot}={value}")
    return _OK
