import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from webapp import mock_economy


def test_seeded_state_renders():
    m = mock_economy.MockEconomy()
    st = m.read_state(mock_economy.DEV_OWNER)
    assert st["characters"], "seed at least one character"
    assert st["bucket"]["assets"], "seed at least one bucket asset"
    assert st["trait_order"][0] == "Background"


def test_equip_swaps_and_returns_displaced():
    m = mock_economy.MockEconomy()
    owner = mock_economy.DEV_OWNER
    char = m.read_state(owner)["characters"][0]
    asset = m.read_state(owner)["bucket"]["assets"][0]
    old = next(a["value"] for a in char["attributes"] if a["trait_type"] == asset["slot"])
    res = m.equip(owner, char["nft_id"], asset["slot"], asset["value"])
    assert res["state"] == "done" and res["displaced"] == old
    # incoming now on the character; displaced now in the bucket
    char2 = m.read_state(owner)["characters"][0]
    assert any(
        a["trait_type"] == asset["slot"] and a["value"] == asset["value"]
        for a in char2["attributes"]
    )


def test_harvest_moves_parts_to_bucket():
    m = mock_economy.MockEconomy()
    owner = mock_economy.DEV_OWNER
    char = m.read_state(owner)["characters"][0]
    res = m.harvest(owner, char["nft_id"])
    assert res["state"] == "done"
    assert not any(c["nft_id"] == char["nft_id"] for c in m.read_state(owner)["characters"])
