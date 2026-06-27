import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from webapp import mock_economy


def test_seeded_state_renders():
    m = mock_economy.MockEconomy()
    st = m.read_state(mock_economy.DEV_OWNER)
    assert st["characters"], "seed at least one character"
    assert st["closet"]["assets"], "seed at least one bucket asset"
    assert st["trait_order"][0] == "Background"


def test_equip_swaps_and_returns_displaced():
    m = mock_economy.MockEconomy()
    owner = mock_economy.DEV_OWNER
    char = m.read_state(owner)["characters"][0]
    asset = m.read_state(owner)["closet"]["assets"][0]
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
    # Grant an active closet so harvest is not gated.
    m.create_closet(owner)  # -> pending_accept
    m.create_closet(owner)  # -> active
    res = m.harvest(owner, char["nft_id"])
    assert res["state"] == "done"
    assert not any(c["nft_id"] == char["nft_id"] for c in m.read_state(owner)["characters"])


def test_mock_closet_lifecycle_gates_harvest():
    m = mock_economy.MockEconomy()
    owner = mock_economy.DEV_OWNER
    # 1. read_state returns closet.token with status "none" before any closet.
    state = m.read_state(owner)
    assert "token" in state["closet"], "closet block must include 'token' key"
    assert state["closet"]["token"]["status"] == "none"
    assert state["closet"]["token"]["nft_id"] is None

    # 2. harvest raises when there is no active closet.
    char_id = m.read_state(owner)["characters"][0]["nft_id"]
    with pytest.raises(mock_economy.MockEconomyError):
        m.harvest(owner, char_id)

    # 3. First create_closet -> pending_accept with a fake accept link.
    result = m.create_closet(owner)
    assert result["status"] == "pending_accept"
    assert result["nft_id"] == "DEV_CLOSET"
    assert result["accept"].startswith("https://")
    assert m.read_state(owner)["closet"]["token"]["status"] == "pending_accept"

    # 4. Second create_closet (represents accept) -> active.
    result2 = m.create_closet(owner)
    assert result2["status"] == "active"
    assert m.read_state(owner)["closet"]["token"]["status"] == "active"

    # 5. Idempotent: third call still returns active.
    result3 = m.create_closet(owner)
    assert result3["status"] == "active"

    # 6. harvest is no longer blocked by the closet gate.
    char = m.read_state(owner)["characters"][0]
    res = m.harvest(owner, char["nft_id"])
    assert res["state"] == "done"
