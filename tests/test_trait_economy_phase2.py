# Phase 2 trait_economy additions: the supply-change ledger folded into
# conservation, the dynamic max-edition, and the op preconditions.

from lfg_core import trait_economy as te
from lfg_core.nft_index import OnchainNft

NON_BODY = te.NON_BODY_SLOTS


def _char(edition: int, body: str = "Straight Blue", *, burned: bool = False) -> OnchainNft:
    attrs = [{"trait_type": "Body", "value": body}]
    attrs += [{"trait_type": s, "value": "None"} for s in NON_BODY]
    return OnchainNft(
        nft_id=f"NFT{edition}",
        nft_number=edition,
        owner="rOwner",
        is_burned=burned,
        mutable=True,
        uri_hex="",
        body="male",
        attributes=attrs,
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
    census = te.Census(trait_counts={("Head", "None"): 1}, body_presence={5: 1})
    assert te.verify_conservation(g, census, sc).ok


def test_unlogged_growth_is_drift():
    g = te.Genesis(trait_counts={("Head", "None"): 0}, edition_bodies={})
    census = te.Census(trait_counts={("Head", "None"): 1}, body_presence={5: 1})
    report = te.verify_conservation(g, census, [])  # no ledger row -> drift
    assert not report.ok
    assert report.trait_drift[("Head", "None")] == 1
    assert report.body_drift[5] == 1


def test_conservation_backcompat_no_ledger_arg():
    g = te.Genesis(trait_counts={("Head", "None"): 1}, edition_bodies={1: ("B", "male")})
    census = te.Census(trait_counts={("Head", "None"): 1}, body_presence={1: 1})
    assert te.verify_conservation(g, census).ok  # 2-arg call still works


# --- harvest preconditions ---


def test_can_harvest_ok():
    g = te.Genesis(trait_counts={}, edition_bodies={7: ("Straight Blue", "male")})
    assert te.can_harvest(_char(7), g, burnable=True).ok


def test_can_harvest_rejects_non_burnable():
    g = te.Genesis(trait_counts={}, edition_bodies={7: ("Straight Blue", "male")})
    r = te.can_harvest(_char(7), g, burnable=False)
    assert not r.ok and "burnable" in r.reason


def test_can_harvest_rejects_burned_unknown_and_wrong_body():
    g = te.Genesis(trait_counts={}, edition_bodies={7: ("Straight Blue", "male")})
    assert not te.can_harvest(_char(7, burned=True), g, burnable=True).ok
    assert not te.can_harvest(_char(99), g, burnable=True).ok  # unknown edition
    assert not te.can_harvest(_char(7, body="Curved Pink"), g, burnable=True).ok  # wrong body


# --- assemble preconditions ---


def test_can_assemble_ok():
    g = te.Genesis(trait_counts={}, edition_bodies={7: ("Straight Blue", "male")})
    assets = {(s, "None"): 1 for s in NON_BODY}
    assert te.can_assemble(7, _full_set(), {7}, assets, set(), g).ok


def test_can_assemble_rejects_live_missing_body_incomplete_and_lacking():
    g = te.Genesis(trait_counts={}, edition_bodies={7: ("Straight Blue", "male")})
    assets = {(s, "None"): 1 for s in NON_BODY}
    assert not te.can_assemble(7, _full_set(), {7}, assets, {7}, g).ok  # already live
    assert not te.can_assemble(7, _full_set(), set(), assets, set(), g).ok  # body not owned
    incomplete = dict.fromkeys(NON_BODY[:-1], "None")
    assert not te.can_assemble(7, incomplete, {7}, assets, set(), g).ok  # missing slot
    assert not te.can_assemble(7, _full_set(), {7}, {}, set(), g).ok  # bucket empty


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
        closet_bodies=es.read_closet_bodies(c),
        trait_tokens=es.read_trait_tokens(c),
    )
    assert base.trait_counts[("Hat", "Cap")] == 1

    # EXTRACT: Closet -1, standalone trait token +1 — census must not change.
    es.set_closet_contents(c, "rA", [("Hat", "Cap", 0)], [])
    es.upsert_trait_token(c, "T1", "rA", "Hat", "Cap")
    after_extract = te.asset_census(
        characters={},
        closet_assets=es.read_closet_assets(c),
        closet_bodies=es.read_closet_bodies(c),
        trait_tokens=es.read_trait_tokens(c),
    )
    assert after_extract == base

    # DEPOSIT: standalone token -1, Closet +1 — census returns to baseline.
    es.delete_trait_token(c, "T1")
    es.set_closet_contents(c, "rA", [("Hat", "Cap", 1)], [])
    after_deposit = te.asset_census(
        characters={},
        closet_assets=es.read_closet_assets(c),
        closet_bodies=es.read_closet_bodies(c),
        trait_tokens=es.read_trait_tokens(c),
    )
    assert after_deposit == base

    # No supply_changes needed: conservation is clean at every step.
    report = te.verify_conservation(te.effective_genesis(genesis, []), after_deposit, [])
    assert report.ok
