# Phase 2 trait_economy additions: the supply-change ledger folded into
# conservation, the dynamic max-edition, and the op preconditions.

from lfg_core import trait_economy as te
from lfg_core.nft_index import OnchainNft

NON_BODY = te.NON_BODY_SLOTS


def _char(
    edition: int, body: str = "Straight Blue", *, burned: bool = False, mutable: bool = True
) -> OnchainNft:
    attrs = [{"trait_type": "Body", "value": body}]
    attrs += [{"trait_type": s, "value": "None"} for s in NON_BODY]
    return OnchainNft(
        nft_id=f"NFT{edition}",
        nft_number=edition,
        owner="rOwner",
        is_burned=burned,
        mutable=mutable,
        uri_hex="",
        body="male",
        attributes=attrs,
        image="",
        ledger_index=1,
    )


def _blank(edition: int, *, burned: bool = False, mutable: bool = True) -> OnchainNft:
    return OnchainNft(
        nft_id=f"NFT{edition}",
        nft_number=edition,
        owner="rOwner",
        is_burned=burned,
        mutable=mutable,
        uri_hex="",
        body="male",
        attributes=te.blank_attributes(),
        image="",
        ledger_index=1,
    )


def _full_set() -> dict[str, str]:
    return dict.fromkeys(NON_BODY, "None")


# --- effective genesis / max edition ---


def test_effective_genesis_adds_mint():
    g = te.Genesis(trait_counts={("Head", "None"): 1}, edition_bodies={1: ("B", "male")})
    sc = [
        {
            "kind": "mint",
            "edition": 2,
            "body_value": "B2",
            "body_class": "ape",
            "trait_deltas": {"Head|None": 1},
        }
    ]
    eff = te.effective_genesis(g, sc)
    assert eff.trait_counts[("Head", "None")] == 2
    assert eff.edition_bodies[2] == ("B2", "ape")


def test_effective_genesis_removes_burn():
    g = te.Genesis(
        trait_counts={("Head", "None"): 2}, edition_bodies={1: ("B", "male"), 2: ("B2", "ape")}
    )
    sc = [
        {
            "kind": "burn",
            "edition": 2,
            "body_value": "B2",
            "body_class": "ape",
            "trait_deltas": {"Head|None": -1},
        }
    ]
    eff = te.effective_genesis(g, sc)
    assert eff.trait_counts[("Head", "None")] == 1
    assert 2 not in eff.edition_bodies


def test_effective_max_edition():
    g = te.Genesis(trait_counts={}, edition_bodies={1: ("B", "male")})
    sc = [
        {
            "kind": "mint",
            "edition": 3536,
            "body_value": "B",
            "body_class": "male",
            "trait_deltas": {},
        }
    ]
    assert te.effective_max_edition(g, sc) == 3536
    assert te.effective_max_edition(g, []) == 1


# --- conservation with the ledger ---


def test_conservation_with_ledger_ok():
    g = te.Genesis(trait_counts={("Head", "None"): 0}, edition_bodies={})
    sc = [
        {
            "kind": "mint",
            "edition": 5,
            "body_value": "B",
            "body_class": "male",
            "trait_deltas": {"Head|None": 1},
        }
    ]
    census = te.Census(trait_counts={("Head", "None"): 1, ("Body", "B"): 1})
    assert te.verify_conservation(g, census, sc).ok


def test_unlogged_growth_is_drift():
    g = te.Genesis(trait_counts={("Head", "None"): 0}, edition_bodies={})
    census = te.Census(trait_counts={("Head", "None"): 1, ("Body", "B"): 1})
    report = te.verify_conservation(g, census, [])  # no ledger row -> drift
    assert not report.ok
    assert report.trait_drift[("Head", "None")] == 1
    assert report.trait_drift[("Body", "B")] == 1


def test_conservation_backcompat_no_ledger_arg():
    g = te.Genesis(trait_counts={("Head", "None"): 1}, edition_bodies={1: ("B", "male")})
    census = te.Census(trait_counts={("Head", "None"): 1, ("Body", "B"): 1})
    assert te.verify_conservation(g, census).ok  # 2-arg call still works


# --- harvest preconditions (blank model, #Task 3) ---


def test_can_harvest_ok_mutable_non_burnable():
    # The old "equip-only" refusal for mutable-but-non-burnable is GONE.
    r = te.can_harvest(_char(7), mutable=True, burnable=False)
    assert r.ok, r.reason


def test_can_harvest_ok_burnable_non_mutable():
    r = te.can_harvest(_char(7, mutable=False), mutable=False, burnable=True)
    assert r.ok, r.reason


def test_can_harvest_rejects_burned():
    assert not te.can_harvest(_char(7, burned=True), mutable=True, burnable=True).ok


def test_can_harvest_rejects_blank():
    assert not te.can_harvest(_blank(7), mutable=True, burnable=True).ok


def test_can_harvest_rejects_neither_mutable_nor_burnable():
    r = te.can_harvest(_char(7, mutable=False), mutable=False, burnable=False)
    assert not r.ok and "neither" in r.reason


# --- assemble preconditions (blank model, #Task 3) ---


def test_can_assemble_ok():
    assets = {(s, "None"): 1 for s in NON_BODY}
    assets[("Body", "Straight Blue")] = 1
    r = te.can_assemble(_blank(7), "Straight Blue", _full_set(), assets, mutable=True)
    assert r.ok, r.reason


def test_can_assemble_rejects_non_blank_target():
    assets = {(s, "None"): 1 for s in NON_BODY}
    assets[("Body", "Straight Blue")] = 1
    assert not te.can_assemble(_char(7), "Straight Blue", _full_set(), assets, mutable=True).ok


def test_can_assemble_rejects_non_mutable_target():
    assets = {(s, "None"): 1 for s in NON_BODY}
    assets[("Body", "Straight Blue")] = 1
    r = te.can_assemble(
        _blank(7, mutable=False), "Straight Blue", _full_set(), assets, mutable=False
    )
    assert not r.ok


def test_can_assemble_rejects_missing_body_asset():
    assets = {(s, "None"): 1 for s in NON_BODY}
    r = te.can_assemble(_blank(7), "Straight Blue", _full_set(), assets, mutable=True)
    assert not r.ok and "Body" in r.reason


def test_can_assemble_rejects_missing_slot():
    assets = {(s, "None"): 1 for s in NON_BODY}
    assets[("Body", "Straight Blue")] = 1
    incomplete = dict.fromkeys(NON_BODY[:-1], "None")
    r = te.can_assemble(_blank(7), "Straight Blue", incomplete, assets, mutable=True)
    assert not r.ok and "missing" in r.reason


def test_can_assemble_rejects_unknown_slot():
    assets = {(s, "None"): 1 for s in NON_BODY}
    assets[("Body", "Straight Blue")] = 1
    chosen = dict(_full_set())
    chosen["NotASlot"] = "Whatever"
    r = te.can_assemble(_blank(7), "Straight Blue", chosen, assets, mutable=True)
    assert not r.ok and "unknown" in r.reason


def test_can_assemble_rejects_short_multiplicity():
    # body_value doubles as the value for a non-body slot too, so the needed
    # multiplicity for that (slot, value) pool key is 2 -- but the Closet
    # only holds 1. Distinct (slot, value) keys never collide across slots,
    # so this is the only way a single-value shortfall can be "short by 2".
    chosen = dict(_full_set())
    assets = {(s, "None"): 1 for s in NON_BODY}
    assets[("Body", "Straight Blue")] = 1
    # Reuse "Straight Blue" as a Head asset too, but only stock one unit
    # total between the Body demand and this Head demand.
    chosen["Head"] = "Straight Blue"
    assets[("Head", "Straight Blue")] = 1  # have 1, need 1 -- fine on its own
    assets[("Body", "Straight Blue")] = 1  # have 1, need 1 -- fine on its own
    # Now starve the Head slot specifically to hit the shortfall path.
    assets[("Head", "Straight Blue")] = 0
    r = te.can_assemble(_blank(7), "Straight Blue", chosen, assets, mutable=True)
    assert not r.ok and "Closet lacks asset" in r.reason and "Head" in r.reason


# --- equip preconditions ---


def test_can_equip_ok():
    assets = {("Head", "Crown"): 1}
    assert te.can_equip(_char(7), "Head", "Crown", assets, mutable=True).ok


def test_can_equip_rejects_immutable_bad_slot_and_lacking():
    assets = {("Head", "Crown"): 1}
    assert not te.can_equip(_char(7), "Head", "Crown", assets, mutable=False).ok
    assert not te.can_equip(_char(7), "Body", "X", assets, mutable=True).ok  # not equippable
    assert not te.can_equip(_char(7), "Head", "Tiara", assets, mutable=True).ok  # not in bucket


# --- extract + deposit conservation round-trip ---


def test_extract_then_deposit_conserves_census():
    """Moving a trait between the Closet and a standalone trait token is
    supply-neutral: asset_census tallies both, so no supply_changes are needed
    and verify_conservation reports OK throughout."""
    import sqlite3

    from lfg_core import economy_store as es

    c = sqlite3.connect(":memory:")
    es.init_economy_schema(c)

    genesis = te.Genesis(trait_counts={("Hat", "Cap"): 1}, edition_bodies={})
    es.freeze_genesis(c, genesis, {})

    # Trait starts loose in a Closet (count=1, no bodies).
    es.set_closet_contents(c, "rA", [("Hat", "Cap", 1)], [])
    base = te.asset_census(
        characters={},
        closet_assets=es.read_closet_assets(c),
        trait_tokens=es.read_trait_tokens(c),
    )
    assert base.trait_counts[("Hat", "Cap")] == 1

    # EXTRACT: Closet -1, standalone trait token +1 — census must not change.
    es.set_closet_contents(c, "rA", [("Hat", "Cap", 0)], [])
    es.upsert_trait_token(c, "T1", "rA", "Hat", "Cap")
    after_extract = te.asset_census(
        characters={},
        closet_assets=es.read_closet_assets(c),
        trait_tokens=es.read_trait_tokens(c),
    )
    assert after_extract == base

    # DEPOSIT: standalone token -1, Closet +1 — census returns to baseline.
    es.delete_trait_token(c, "T1")
    es.set_closet_contents(c, "rA", [("Hat", "Cap", 1)], [])
    after_deposit = te.asset_census(
        characters={},
        closet_assets=es.read_closet_assets(c),
        trait_tokens=es.read_trait_tokens(c),
    )
    assert after_deposit == base

    # No supply_changes needed: conservation is clean at every step.
    report = te.verify_conservation(te.effective_genesis(genesis, []), after_deposit, [])
    assert report.ok


def test_harvest_invariance_body_moves_into_closet_census_unchanged():
    """Harvesting a character (char -> blank, ALL 8 non-body values (incl.
    any "None") + its ("Body", v) move into the owner's Closet as loose
    assets) must leave the total census exactly equal to genesis.

    A blank character contributes NOTHING to the census (see
    `asset_census`'s docstring) — it is not a separate asset holder, since
    harvest is defined as relocating every one of its 9 slot values into the
    Closet. `_char()` builds a dressed character with a real Body value and
    every non-body slot "None"; harvest produces 9 Closet rows (8 non-body +
    Body), and the blank character contributes 0 — so the total is unchanged."""
    dressed = _char(7, body="Straight Blue")
    genesis = te.build_genesis({7: dressed})

    # Before harvest: dressed character live, empty Closet -> matches genesis.
    before = te.asset_census(characters={7: dressed}, closet_assets=[], trait_tokens=[])
    assert before.trait_counts == te.genesis_trait_counts_with_bodies(genesis)
    assert te.verify_conservation(genesis, before).ok

    # Harvest: character goes blank; ALL 8 non-body values (here, all "None")
    # plus ("Body", "Straight Blue") move into the owner's Closet.
    blank = _blank(7)
    closet_assets = [("rOwner", s, "None", 1) for s in NON_BODY]
    closet_assets.append(("rOwner", "Body", "Straight Blue", 1))
    after = te.asset_census(characters={7: blank}, closet_assets=closet_assets, trait_tokens=[])
    assert after.trait_counts == before.trait_counts
    assert te.verify_conservation(genesis, after).ok
