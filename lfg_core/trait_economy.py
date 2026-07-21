# lfg_core/trait_economy.py
# Pure accounting core for the NFT dress-up trait economy. No I/O.
# An "asset" is a (slot, value) pair over the 9 TRAIT_ORDER slots, including
# Body. In the census, a dressed (non-blank) character contributes all 9 of
# its own slot values (8 non-body incl. "None", plus Body); a BLANK character
# contributes nothing at all — harvest is defined as moving every one of its
# 9 slot values into the owner's Closet as loose assets, so the blank
# character is not itself a separate asset holder. For non-body slots, "None"
# is itself a real, conserved asset; Body has no "None" state to conserve
# (no genesis edition was ever bodyless), which is why a blank character's
# Body contributes nothing rather than ("Body", "None").

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


@dataclass
class ConservationReport:
    trait_drift: dict[tuple[str, str], int]
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


def blank_attributes() -> list[dict[str, str]]:
    """The canonical attribute list of a BLANK character: every TRAIT_ORDER
    slot (including Body) explicitly "None"."""
    return [{"trait_type": s, "value": "None"} for s in swap_meta.TRAIT_ORDER]


def is_blank(rec: OnchainNft) -> bool:
    """A character is blank iff every slot (including Body) reads "None"."""
    return all(
        (swap_meta.get_attr(rec.attributes, s) or "None") == "None" for s in swap_meta.TRAIT_ORDER
    )


def body_class_map(genesis: Genesis) -> dict[str, str]:
    """body value -> layer-dir class, derived from the frozen genesis."""
    return dict(genesis.edition_bodies.values())


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
    trait_tokens: list[tuple[str, str, str, str]],
) -> Census:
    """Tally every asset across live characters, Closets and standalone trait
    tokens. A dressed (non-blank) character contributes all 9 slots — its 8
    non-body (slot, value) pairs (including any "None") plus its
    ("Body", value). A BLANK character contributes NOTHING AT ALL: harvest
    moves every one of its 9 slot values (all 8 non-body values, whatever
    they were, plus the body) into the owner's Closet as loose assets, so the
    blank character itself is not a separate asset holder — counting it too
    would double-count exactly what the Closet now holds."""
    trait_counts: Counter[tuple[str, str]] = Counter()
    for _edition, rec in characters.items():
        if is_blank(rec):
            continue
        for slot in NON_BODY_SLOTS:
            trait_counts[(slot, slot_value(rec, slot))] += 1
        trait_counts[("Body", slot_value(rec, "Body"))] += 1
    for _owner, slot, value, count in closet_assets:
        trait_counts[(slot, value)] += count
    for _nft_id, _owner, slot, value in trait_tokens:
        trait_counts[(slot, value)] += 1
    return Census(trait_counts=dict(trait_counts))


def genesis_trait_counts_with_bodies(genesis: Genesis) -> dict[tuple[str, str], int]:
    """Genesis trait_counts (non-body) plus per-edition body values folded in
    under ("Body", value) keys — the conservation baseline that covers Body
    as a first-class asset."""
    counts: Counter[tuple[str, str]] = Counter(genesis.trait_counts)
    counts.update(("Body", value) for value, _cls in genesis.edition_bodies.values())
    return dict(counts)


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
    intentional supply-change ledger), for every (slot, value) INCLUDING
    Body — folded into the baseline via `genesis_trait_counts_with_bodies` so
    a dressed character's body, a Closet ("Body", v) asset, and a genesis
    body all count the same asset. A delta NOT explained by the ledger is
    reported as drift. Back-compatible: an empty/omitted ledger reduces to
    the original census-vs-genesis check."""
    eff = effective_genesis(genesis, supply_changes or [])
    eff_counts = genesis_trait_counts_with_bodies(eff)
    trait_drift: dict[tuple[str, str], int] = {}
    for key in set(eff_counts) | set(census.trait_counts):
        delta = census.trait_counts.get(key, 0) - eff_counts.get(key, 0)
        if delta != 0:
            trait_drift[key] = delta

    ok = not trait_drift
    return ConservationReport(trait_drift=trait_drift, ok=ok)


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


def can_harvest(rec: OnchainNft, *, mutable: bool, burnable: bool) -> Precheck:
    """A character can be harvested iff it is live, not already blank, and
    either mutable (modify-in-place strip) or burnable (legacy one-time
    burn+remint-as-blank upgrade)."""
    if rec.is_burned:
        return Precheck(False, "character is already burned")
    if is_blank(rec):
        return Precheck(False, "character is already blank")
    if not (mutable or burnable):
        return Precheck(False, "character is neither mutable nor burnable")
    return _OK


def can_assemble(
    rec: OnchainNft,
    body_value: str,
    chosen: dict[str, str],
    owner_assets: dict[tuple[str, str], int],
    *,
    mutable: bool,
) -> Precheck:
    """A blank the caller owns can be dressed iff it is live+mutable+blank and
    the Closet covers the body plus a full valid non-body set."""
    if rec.is_burned:
        return Precheck(False, "character is burned")
    if not mutable:
        return Precheck(False, "character is not mutable")
    if not is_blank(rec):
        return Precheck(False, "character is not blank — harvest it first")
    missing = [s for s in NON_BODY_SLOTS if s not in chosen]
    if missing:
        return Precheck(False, f"incomplete set, missing slots: {', '.join(missing)}")
    extra = [s for s in chosen if s not in NON_BODY_SLOTS]
    if extra:
        return Precheck(False, f"unknown slots in set: {', '.join(extra)}")
    need = Counter((s, chosen[s]) for s in NON_BODY_SLOTS)
    need[("Body", body_value)] += 1
    for (slot, value), qty in need.items():
        if owner_assets.get((slot, value), 0) < qty:
            return Precheck(False, f"Closet lacks asset {slot}={value}")
    return _OK


def can_equip(
    rec: OnchainNft,
    slot: str,
    value: str,
    owner_assets: dict[tuple[str, str], int],
    mutable: bool,
) -> Precheck:
    """A loose asset can be equipped onto a live, mutable character iff the slot
    is a non-body slot and the owner's Closet holds the incoming asset."""
    if rec.is_burned:
        return Precheck(False, "character is burned")
    if not mutable:
        return Precheck(False, "character is not mutable")
    if slot not in NON_BODY_SLOTS:
        return Precheck(False, f"{slot} is not an equippable slot")
    if owner_assets.get((slot, value), 0) < 1:
        return Precheck(False, f"Closet lacks asset {slot}={value}")
    return _OK
