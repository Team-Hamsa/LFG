# Tests for lfg_core/trait_economy.py (pure trait-economy accounting).
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DISCORD_BOT_TOKEN", "x")
os.environ.setdefault("XUMM_API_KEY", "x")
os.environ.setdefault("XUMM_API_SECRET", "x")
os.environ.setdefault("BUNNY_CDN_ACCESS_KEY", "x")
os.environ.setdefault("BUNNY_CDN_STORAGE_ZONE", "x")
os.environ.setdefault("SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2")
os.environ.setdefault("TOKEN_ISSUER_ADDRESS", "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
os.environ.setdefault("TOKEN_CURRENCY_HEX", "4C46474F00000000000000000000000000000000")
os.environ.setdefault("XRPL_NETWORK", "testnet")
os.environ.setdefault("BUNNY_PULL_ZONE", "nft.pullzone.example")

from lfg_core import nft_index, swap_meta, trait_economy  # noqa: E402


def _attrs(body="Straight", **slots):
    out = [{"trait_type": "Body", "value": body}]
    for slot, value in slots.items():
        out.append({"trait_type": slot, "value": value})
    return out


def _nft(nft_id, number, *, mutable=True, ledger=1, body_class="male", attrs=None):
    return nft_index.OnchainNft(
        nft_id=nft_id,
        nft_number=number,
        owner="rOwner",
        is_burned=False,
        mutable=mutable,
        uri_hex="6868",
        body=body_class,
        attributes=attrs if attrs is not None else _attrs(),
        image="",
        ledger_index=ledger,
    )


def test_non_body_slots_excludes_body():
    assert "Body" not in trait_economy.NON_BODY_SLOTS
    assert len(trait_economy.NON_BODY_SLOTS) == 8
    assert "Background" in trait_economy.NON_BODY_SLOTS


def test_slot_value_defaults_to_none():
    rec = _nft("A", 1, attrs=_attrs(Background="Sky"))
    assert trait_economy.slot_value(rec, "Background") == "Sky"
    assert trait_economy.slot_value(rec, "Head") == "None"


def test_dedupe_prefers_mutable_then_newest_ledger():
    a = _nft("imm-old", 5, mutable=False, ledger=10)
    b = _nft("mut-old", 5, mutable=True, ledger=20)
    c = _nft("mut-new", 5, mutable=True, ledger=99)
    canonical, recon = trait_economy.dedupe_editions([a, b, c], max_edition=10)
    assert canonical[5].nft_id == "mut-new"
    assert recon["duplicates"][5] == ["mut-old", "imm-old"]


def test_dedupe_classifies_missing_unparsed_out_of_range():
    good = _nft("g", 2)
    unparsed = _nft("u", None)
    oor = _nft("o", 9999)
    canonical, recon = trait_economy.dedupe_editions([good, unparsed, oor], max_edition=3)
    assert set(canonical) == {2}
    assert recon["missing"] == [1, 3]
    assert recon["unparsed"] == ["u"]
    assert recon["out_of_range"] == ["o"]


def test_build_genesis_counts_traits_and_bodies():
    a = _nft(
        "a", 1, body_class="male", attrs=_attrs(body="Straight", Background="Sky", Head="Crown")
    )
    b = _nft("b", 2, body_class="male", attrs=_attrs(body="Straight", Background="Sky"))
    g = trait_economy.build_genesis({1: a, 2: b})
    # Background:Sky appears on both editions.
    assert g.trait_counts[("Background", "Sky")] == 2
    # Head:Crown only on edition 1; edition 2's Head is absent -> ("Head","None").
    assert g.trait_counts[("Head", "Crown")] == 1
    assert g.trait_counts[("Head", "None")] == 1
    # Bodies are identity-bound per edition.
    assert g.edition_bodies[1] == ("Straight", "male")
    assert g.edition_bodies[2] == ("Straight", "male")
    # Body is never a non-body trait key.
    assert not any(slot == "Body" for slot, _ in g.trait_counts)


def test_asset_census_sums_chars_buckets_and_tokens():
    char = _nft("c", 1, attrs=_attrs(Background="Sky"))
    census = trait_economy.asset_census(
        characters={1: char},
        bucket_assets=[("rA", "Background", "Sky", 2), ("rA", "Head", "None", 1)],
        bucket_bodies=[("rA", 7)],
        trait_tokens=[("tok1", "rB", "Background", "Sky")],
    )
    # 1 on the live character + 2 in a bucket + 1 standalone token.
    assert census.trait_counts[("Background", "Sky")] == 4
    assert census.trait_counts[("Head", "None")] == 1 + 1  # char's empty Head + bucket
    # Body presence: edition 1 live, edition 7 loose in a bucket.
    assert census.body_presence == {1: 1, 7: 1}


def test_verify_conservation_ok_when_census_matches_genesis():
    g = trait_economy.Genesis(
        trait_counts={("Background", "Sky"): 2}, edition_bodies={1: ("S", "male")}
    )
    c = trait_economy.Census(trait_counts={("Background", "Sky"): 2}, body_presence={1: 1})
    rep = trait_economy.verify_conservation(g, c)
    assert rep.ok
    assert rep.trait_drift == {}
    assert rep.body_drift == {}


def test_verify_conservation_flags_trait_and_body_drift():
    g = trait_economy.Genesis(
        trait_counts={("Background", "Sky"): 2, ("Head", "Crown"): 1},
        edition_bodies={1: ("S", "male"), 2: ("S", "male")},
    )
    c = trait_economy.Census(
        trait_counts={("Background", "Sky"): 3},  # +1 created; Crown destroyed
        body_presence={1: 2},  # edition 1 duplicated, edition 2 vanished
    )
    rep = trait_economy.verify_conservation(g, c)
    assert not rep.ok
    assert rep.trait_drift[("Background", "Sky")] == 1
    assert rep.trait_drift[("Head", "Crown")] == -1
    assert rep.body_drift[1] == 2
    assert rep.body_drift[2] == 0


def test_verify_conservation_flags_ghost_edition_in_census():
    """Ghost edition: in census.body_presence but not in genesis.edition_bodies."""
    g = trait_economy.Genesis(trait_counts={}, edition_bodies={1: ("S", "male")})
    c = trait_economy.Census(trait_counts={}, body_presence={1: 1, 99: 1})
    rep = trait_economy.verify_conservation(g, c)
    assert not rep.ok
    assert rep.body_drift[99] == 1
    assert 1 not in rep.body_drift  # edition 1 is healthy (presence == 1)


def test_verify_completeness_ok_for_normalized_characters():
    attrs = swap_meta.normalize_attributes(_attrs(body="Straight", Background="Sky"))
    a = _nft("a", 1, body_class="male", attrs=attrs)
    g = trait_economy.build_genesis({1: a})
    rep = trait_economy.verify_completeness({1: a}, g)
    assert rep.ok
    assert rep.wrong_body == {}
    assert rep.orphan_bodies == []
    assert rep.slot_anomalies == {}


def test_verify_completeness_flags_wrong_body_and_orphan():
    a = _nft("a", 1, body_class="male", attrs=_attrs(body="Straight"))
    g = trait_economy.build_genesis({1: a})
    # Edition 1 now shows a different body value; edition 9 isn't in genesis.
    mutated = _nft("a2", 1, body_class="male", attrs=_attrs(body="Curved"))
    orphan = _nft("z", 9, attrs=_attrs(body="Straight"))
    rep = trait_economy.verify_completeness({1: mutated, 9: orphan}, g)
    assert not rep.ok
    assert rep.wrong_body[1] == ("Curved", "Straight")
    assert rep.orphan_bodies == [9]
    assert 1 in rep.slot_anomalies  # missing non-body slots are flagged


def test_verify_completeness_flags_duplicate_slot():
    dup = _nft(
        "d",
        1,
        attrs=[
            {"trait_type": "Body", "value": "Straight"},
            {"trait_type": "Head", "value": "Crown"},
            {"trait_type": "Head", "value": "Hat"},  # Head twice
        ],
    )
    g = trait_economy.Genesis(trait_counts={}, edition_bodies={1: ("Straight", "male")})
    rep = trait_economy.verify_completeness({1: dup}, g)
    assert "Head" in rep.slot_anomalies[1]
