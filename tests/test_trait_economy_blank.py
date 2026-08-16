import os

os.environ.setdefault("BUNNY_PULL_ZONE", "test.b-cdn.net")
os.environ.setdefault("LAYER_SOURCE", "local")

from lfg_core import trait_economy as te
from lfg_core.nft_index import OnchainNft
from lfg_core.swap_meta import TRAIT_ORDER


def _rec(attrs):
    return OnchainNft(
        nft_id="A" * 64,
        nft_number=7,
        owner="rOwner",
        is_burned=False,
        mutable=True,
        uri_hex="",
        body="milady",
        attributes=attrs,
        image="",
        ledger_index=1,
    )


def test_blank_attributes_covers_every_slot_with_none():
    attrs = te.blank_attributes()
    assert [a["trait_type"] for a in attrs] == TRAIT_ORDER
    assert all(a["value"] == "None" for a in attrs)


def test_is_blank_true_for_blank_attrs():
    assert te.is_blank(_rec(te.blank_attributes()))


def test_is_blank_false_when_any_slot_set():
    attrs = te.blank_attributes()
    attrs[2] = {"trait_type": "Body", "value": "Milady"}
    assert not te.is_blank(_rec(attrs))


def test_is_blank_false_for_missing_attrs():
    # Empty/unparsed attributes are NOT blank: is_blank requires every slot to
    # be EXPLICITLY present with "None" so a metadata-parse failure can't be
    # mistaken for a dressable blank (which Assemble would overwrite).
    assert not te.is_blank(_rec([]))


def test_is_blank_false_when_one_slot_missing():
    # A blank set with a single slot dropped is not blank.
    attrs = [a for a in te.blank_attributes() if a["trait_type"] != "Accessory"]
    assert not te.is_blank(_rec(attrs))


def test_body_class_map_from_genesis():
    g = te.Genesis(
        trait_counts={},
        edition_bodies={
            1: ("Milady", "milady"),
            2: ("Skeleton", "skeleton"),
            3: ("Milady", "milady"),
        },
    )
    assert te.body_class_map(g) == {"Milady": "milady", "Skeleton": "skeleton"}
