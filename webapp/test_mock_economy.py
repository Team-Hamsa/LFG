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


# --- Task 9: trait_tokens in mock read_state ---


def test_read_state_includes_empty_trait_tokens_initially():
    """read_state always includes a trait_tokens key (empty list when none extracted)."""
    m = mock_economy.MockEconomy()
    state = m.read_state(mock_economy.DEV_OWNER)
    assert "trait_tokens" in state, "read_state must include trait_tokens key"
    assert state["trait_tokens"] == []


# --- Task 9: extract ---


def _active_closet(m: mock_economy.MockEconomy, owner: str) -> None:
    """Helper: advance the closet through none -> pending -> active."""
    m.create_closet(owner)  # -> pending_accept
    m.create_closet(owner)  # -> active


def test_extract_raises_without_active_closet():
    """extract raises MockEconomyError when the closet is not active."""
    m = mock_economy.MockEconomy()
    owner = mock_economy.DEV_OWNER
    body = {"slot": "Hat", "value": "Cap"}
    with pytest.raises(mock_economy.MockEconomyError):
        m.extract(owner, body)


def test_extract_removes_asset_from_closet_and_adds_trait_token():
    """extract with active closet removes the (slot,value) from closet and adds a trait_token."""
    m = mock_economy.MockEconomy()
    owner = mock_economy.DEV_OWNER
    # Seed a Hat/Cap asset directly into the mock closet.
    m.assets[("Hat", "Cap")] = 1
    _active_closet(m, owner)

    body = {"slot": "Hat", "value": "Cap"}
    res = m.extract(owner, body)

    # Return shape matches economy_session_dict("extract",...): terminal state.
    assert res["state"] == "done"
    assert res["error"] is None
    assert res["nft_id"] is not None, "extract must return a fabricated nft_id"
    assert res["accept"] is not None, "extract must return a fake accept value"

    # Closet asset count for Hat/Cap should have decreased by 1.
    state = m.read_state(owner)
    cap_asset = next(
        (a for a in state["closet"]["assets"] if a["slot"] == "Hat" and a["value"] == "Cap"),
        None,
    )
    assert cap_asset is None or cap_asset["count"] == 0, "Hat/Cap should be gone from closet"

    # trait_tokens should now contain one entry.
    trait_tokens = state["trait_tokens"]
    assert len(trait_tokens) == 1
    tok = trait_tokens[0]
    assert tok["slot"] == "Hat"
    assert tok["value"] == "Cap"
    assert tok["nft_id"] == res["nft_id"]


def test_extract_raises_when_asset_not_in_closet():
    """extract raises MockEconomyError when the requested (slot,value) has count 0."""
    m = mock_economy.MockEconomy()
    owner = mock_economy.DEV_OWNER
    _active_closet(m, owner)
    # "Hat"/"Cap" is not seeded in the default closet.
    body = {"slot": "Hat", "value": "Cap"}
    with pytest.raises(mock_economy.MockEconomyError):
        m.extract(owner, body)


# --- Task 9: deposit ---


def test_deposit_raises_without_active_closet():
    """deposit raises MockEconomyError when the closet is not active."""
    m = mock_economy.MockEconomy()
    owner = mock_economy.DEV_OWNER
    body = {"nft_id": "DEVTRAIT1"}
    with pytest.raises(mock_economy.MockEconomyError):
        m.deposit(owner, body)


def test_deposit_reverses_extract():
    """deposit of an extracted nft_id returns the trait to the closet and removes the trait_token."""
    m = mock_economy.MockEconomy()
    owner = mock_economy.DEV_OWNER
    m.assets[("Hat", "Cap")] = 1
    _active_closet(m, owner)

    # Extract first.
    extract_res = m.extract(owner, {"slot": "Hat", "value": "Cap"})
    nft_id = extract_res["nft_id"]

    # Verify trait_token is present.
    assert any(t["nft_id"] == nft_id for t in m.read_state(owner)["trait_tokens"])

    # Now deposit.
    dep_res = m.deposit(owner, {"nft_id": nft_id})
    assert dep_res["state"] == "done"
    assert dep_res["error"] is None
    assert dep_res["slot"] == "Hat"
    assert dep_res["value"] == "Cap"

    state_after = m.read_state(owner)
    # trait_token should be gone.
    assert not any(t["nft_id"] == nft_id for t in state_after["trait_tokens"])
    # Hat/Cap should be back in the closet.
    cap_asset = next(
        (a for a in state_after["closet"]["assets"] if a["slot"] == "Hat" and a["value"] == "Cap"),
        None,
    )
    assert cap_asset is not None and cap_asset["count"] >= 1, "Hat/Cap should be back in closet"


def test_deposit_raises_for_unknown_nft_id():
    """deposit raises MockEconomyError when the nft_id is not in the wallet's trait_tokens."""
    m = mock_economy.MockEconomy()
    owner = mock_economy.DEV_OWNER
    _active_closet(m, owner)
    with pytest.raises(mock_economy.MockEconomyError):
        m.deposit(owner, {"nft_id": "UNKNOWN_NFT_ID"})
