import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from webapp import mock_economy, mock_market


@pytest.fixture
def market():
    # Fresh instances each test — MockMarket references mock_economy.INSTANCE
    # (module-level singleton), so reset both to avoid cross-test bleed.
    mock_economy.INSTANCE = mock_economy.MockEconomy()
    m = mock_market.MockMarket()
    return m


def test_browse_character_kind_only_returns_live_characters(market):
    rows = market.browse(
        kind="character", trait_filters={}, min_drops=None, max_drops=None, sort="price_asc"
    )
    assert rows, "seed at least one live character listing"
    assert all(r["kind"] == "character" for r in rows)
    assert all("nft_number" in r for r in rows)


def test_browse_trait_kind_excludes_non_live(market):
    rows = market.browse(
        kind="trait", trait_filters={}, min_drops=None, max_drops=None, sort="price_asc"
    )
    assert all(r["offer_index"] != "MOCKOFFER-9199" for r in rows), "stale listing must be excluded"


def test_browse_price_bounds(market):
    rows = market.browse(
        kind="trait", trait_filters={}, min_drops=5_000_000, max_drops=None, sort="price_asc"
    )
    assert all(r["amount_drops"] >= 5_000_000 for r in rows)


def test_browse_sort_price_asc_and_desc(market):
    asc = market.browse(
        kind="trait", trait_filters={}, min_drops=None, max_drops=None, sort="price_asc"
    )
    assert asc == sorted(asc, key=lambda r: r["amount_drops"])
    desc = market.browse(
        kind="trait", trait_filters={}, min_drops=None, max_drops=None, sort="price_desc"
    )
    assert desc == sorted(desc, key=lambda r: -r["amount_drops"])


def test_browse_trait_filter_matches_slot_value(market):
    rows = market.browse(
        kind="trait",
        trait_filters={"Head": ["Tophat"]},
        min_drops=None,
        max_drops=None,
        sort="price_asc",
    )
    assert rows and all(r["slot"] == "Head" and r["value"] == "Tophat" for r in rows)


def test_mine_returns_dev_owner_groups(market):
    data = market.mine(mock_market.DEV_OWNER)
    assert "listings" in data
    assert data["unlisted_characters"], "mock_economy seeds characters for DEV_OWNER"
    assert isinstance(data["closet_assets"], list) and data["closet_assets"]


def test_mine_returns_empty_for_other_wallet(market):
    data = market.mine("rSomeoneElse")
    assert data["unlisted_characters"] == []
    assert data["closet_assets"] == []
    assert data["listings"] == []


def test_history_by_slot_value_returns_only_sold(market):
    hist = market.history(slot="Head", value="Tophat")
    assert hist["sales"] == []  # nothing sold yet in a fresh fixture


def test_start_list_rejects_unowned_nft(market):
    with pytest.raises(mock_market.MockMarketError):
        market.start_list(mock_market.DEV_OWNER, "NOT-OWNED", 1_000_000)


def test_mine_excludes_listed_character_from_unlisted_group(market):
    """Regression: a just-listed character must disappear from
    unlisted_characters (not appear in BOTH groups) — mirrors the real
    handler's listed_char_ids exclusion in _compute_mine_data."""
    econ = mock_economy.INSTANCE
    nft_id = econ.characters[0]["nft_id"]
    s = market.start_list(mock_market.DEV_OWNER, nft_id, 10_000_000)
    for _ in range(10):
        s = market.status(s["id"])
        if s["state"] == "done":
            break
    assert s["state"] == "done"

    data = market.mine(mock_market.DEV_OWNER)
    assert nft_id in {r["nft_id"] for r in data["listings"]}
    assert nft_id not in {c["nft_id"] for c in data["unlisted_characters"]}


def test_start_list_happy_path_progresses_to_done(market):
    econ = mock_economy.INSTANCE
    nft_id = econ.characters[0]["nft_id"]
    s = market.start_list(mock_market.DEV_OWNER, nft_id, 10_000_000)
    assert s["state"] == "awaiting_signature"
    sid = s["id"]
    s = market.status(sid)
    assert s["state"] in ("awaiting_signature", "pending")
    # Poll until terminal (bounded — the mock always converges quickly).
    for _ in range(10):
        s = market.status(sid)
        if s["state"] == "done":
            break
    assert s["state"] == "done"
    assert s["offer_index"]
    # The listing now appears in browse.
    rows = market.browse(
        kind="character", trait_filters={}, min_drops=None, max_drops=None, sort="price_asc"
    )
    assert any(r["nft_id"] == nft_id for r in rows)


def test_start_list_rejects_already_listed(market):
    econ = mock_economy.INSTANCE
    nft_id = econ.characters[0]["nft_id"]
    s = market.start_list(mock_market.DEV_OWNER, nft_id, 10_000_000)
    for _ in range(10):
        s = market.status(s["id"])
        if s["state"] == "done":
            break
    with pytest.raises(mock_market.MockMarketError):
        market.start_list(mock_market.DEV_OWNER, nft_id, 5_000_000)


def test_mine_listings_excludes_cancelled_listing(market):
    """Regression: mine()'s 'listings' group must filter is_live (mirrors the
    real handler's 'AND is_live = 1'). A cancelled row is kept in
    self._listings as a closed record, not deleted — before this fix it kept
    showing up under My listings forever."""
    row = next(
        r for r in market._listings if r["seller"] == mock_market.OTHER_SELLER_1 and r["is_live"]
    )
    row["seller"] = mock_market.DEV_OWNER
    offer_index = row["offer_index"]
    assert offer_index in {r["offer_index"] for r in market.mine(mock_market.DEV_OWNER)["listings"]}

    s = market.start_cancel(mock_market.DEV_OWNER, offer_index)
    for _ in range(10):
        s = market.status(s["id"])
        if s["state"] == "done":
            break
    assert s["state"] == "done"

    assert offer_index not in {
        r["offer_index"] for r in market.mine(mock_market.DEV_OWNER)["listings"]
    }


def test_start_cancel_progresses_and_closes_listing(market):
    row = next(
        r for r in market._listings if r["seller"] == mock_market.OTHER_SELLER_1 and r["is_live"]
    )
    # Force ownership for the cancel precondition by pointing seller at DEV_OWNER.
    row["seller"] = mock_market.DEV_OWNER
    s = market.start_cancel(mock_market.DEV_OWNER, row["offer_index"])
    for _ in range(10):
        s = market.status(s["id"])
        if s["state"] == "done":
            break
    assert s["state"] == "done"
    assert row["is_live"] is False


def test_start_cancel_rejects_not_your_listing(market):
    row = next(r for r in market._listings if r["is_live"])
    with pytest.raises(mock_market.MockMarketError):
        market.start_cancel("rSomeoneElse", row["offer_index"])


def test_start_buy_character_progresses_to_done(market):
    row = next(r for r in market._listings if r["kind"] == "character" and r["is_live"])
    s = market.start_buy(mock_market.DEV_OWNER, row["offer_index"])
    for _ in range(10):
        s = market.status(s["id"])
        if s["state"] == "done":
            break
    assert s["state"] == "done"
    assert row["is_live"] is False


def test_start_buy_stale_listing_raises(market):
    row = next(r for r in market._listings if not r["is_live"])
    with pytest.raises(mock_market.MockMarketError, match="listing_unavailable"):
        market.start_buy(mock_market.DEV_OWNER, row["offer_index"])


def test_start_buy_trait_without_closet_raises_closet_required(market):
    row = next(r for r in market._listings if r["kind"] == "trait" and r["is_live"])
    with pytest.raises(mock_market.MockMarketError, match="closet_required"):
        market.start_buy(mock_market.DEV_OWNER, row["offer_index"])


def test_start_buy_trait_with_active_closet_settles_to_closet(market):
    econ = mock_economy.INSTANCE
    econ.create_closet(mock_market.DEV_OWNER)  # pending_accept
    econ.create_closet(mock_market.DEV_OWNER)  # active
    row = next(r for r in market._listings if r["kind"] == "trait" and r["is_live"])
    slot, value = row["slot"], row["value"]
    before = econ.assets.get((slot, value), 0)
    s = market.start_buy(mock_market.DEV_OWNER, row["offer_index"])
    for _ in range(10):
        s = market.status(s["id"])
        if s["state"] == "done":
            break
    assert s["state"] == "done"
    assert econ.assets.get((slot, value), 0) == before + 1


def test_start_trait_list_requires_active_closet(market):
    with pytest.raises(mock_market.MockMarketError, match="Closet"):
        market.start_trait_list(mock_market.DEV_OWNER, "Head", "Halo", 5_000_000)


def test_start_trait_list_wizard_progresses_through_both_steps(market):
    econ = mock_economy.INSTANCE
    econ.create_closet(mock_market.DEV_OWNER)
    econ.create_closet(mock_market.DEV_OWNER)
    s = market.start_trait_list(mock_market.DEV_OWNER, "Head", "Halo", 5_000_000)
    assert s["state"] == "extract_pending"
    seen_states = [s["state"]]
    sid = s["id"]
    for _ in range(10):
        s = market.status(sid)
        seen_states.append(s["state"])
        if s["state"] == "listed":
            break
    assert s["state"] == "listed"
    assert "extract_done" in seen_states
    assert "list_pending" in seen_states
    assert s["offer_index"]
    rows = market.browse(
        kind="trait", trait_filters={}, min_drops=None, max_drops=None, sort="price_asc"
    )
    assert any(r["offer_index"] == s["offer_index"] for r in rows)
