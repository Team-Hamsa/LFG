import sqlite3

import pytest

from lfg_core import closet_market_store as cms
from lfg_core import closet_token as ct
from lfg_core import economy_store as es

SELLER, BUYER, OTHER = "rSeller", "rBuyer", "rOther"


def _conn(seller_count=1):
    c = sqlite3.connect(":memory:")
    es.init_economy_schema(c)
    for owner in (SELLER, BUYER, OTHER):
        es.set_closet_token(c, owner, f"C-{owner}", "00", status=ct.ACTIVE)
    es.set_closet_contents(c, SELLER, [("Head", "Crown", seller_count)], [])
    return c


def _open_bid(c, owner=BUYER, price="10", now=100):
    bid = cms.create_pending_bid(
        c,
        owner=owner,
        slot="Head",
        value="Crown",
        price_brix=price,
        platform="web",
        condition="A0",
        fulfillment_enc="sealed",
        cancel_after=999999,
        payload_uuid="U",
        xumm_url="x",
        qr_url="q",
        push=None,
        now=now,
    )
    return cms.mark_bid_open(c, bid["id"], escrow_tx_hash=f"H{bid['id']}", escrow_owner_seq=5)


def test_one_live_bid_per_key():
    c = _conn()
    _open_bid(c)
    assert cms.has_live_bid(c, BUYER, "Head", "Crown")
    with pytest.raises(cms.OrderError) as e:
        _open_bid(c)
    assert e.value.code == "bid_exists"


def test_incoming_ask_crosses_resting_bid_at_bid_price():
    c = _conn()
    bid = _open_bid(c, price="10")
    ask = cms.create_ask(c, owner=SELLER, slot="Head", value="Crown", price_brix="8", platform=None)
    fill = cms.cross_incoming(c, ask["id"], fee_bps=700)
    assert fill["price_brix"] == "10" and fill["overshoot_brix"] == "0"
    assert fill["fee_brix"] == "0.7" and fill["funds_source"] == cms.FUNDS_ESCROW
    assert (fill["seller"], fill["buyer"], fill["state"]) == (SELLER, BUYER, cms.FUNDS_PENDING)
    assert cms.get_order(c, bid["id"])["state"] == cms.MATCHED
    assert cms.get_order(c, ask["id"])["state"] == cms.MATCHED


def test_incoming_bid_crosses_resting_ask_at_ask_price_with_overshoot():
    c = _conn()
    cms.create_ask(c, owner=SELLER, slot="Head", value="Crown", price_brix="8", platform=None)
    bid = _open_bid(c, price="10")
    fill = cms.cross_incoming(c, bid["id"], fee_bps=0)
    assert (fill["price_brix"], fill["overshoot_brix"], fill["fee_brix"]) == ("8", "2", "0")
    assert cms.fill_in_amount(fill) == "10"


def test_best_counter_is_best_price_then_fifo_and_never_self():
    c = _conn(seller_count=3)
    es.set_closet_contents(c, BUYER, [("Head", "Crown", 1)], [])
    cms.create_ask(c, owner=BUYER, slot="Head", value="Crown", price_brix="1", platform=None, now=1)
    cms.create_ask(
        c, owner=SELLER, slot="Head", value="Crown", price_brix="7", platform=None, now=2
    )
    first7 = cms.book_levels(c, "Head", "Crown")["asks"][1]["id"]
    cms.create_ask(
        c, owner=SELLER, slot="Head", value="Crown", price_brix="7", platform=None, now=3
    )
    bid = _open_bid(c, owner=BUYER, price="9")
    fill = cms.cross_incoming(c, bid["id"], fee_bps=0)
    assert fill["ask_order_id"] == first7  # own ask at 1 skipped; FIFO among the 7s


def test_no_cross_when_prices_do_not_meet():
    c = _conn()
    cms.create_ask(c, owner=SELLER, slot="Head", value="Crown", price_brix="11", platform=None)
    bid = _open_bid(c, price="10")
    assert cms.cross_incoming(c, bid["id"], fee_bps=0) is None


def test_holder_fill_encumbers_and_refuses_self_and_unheld():
    c = _conn()
    bid = _open_bid(c)
    with pytest.raises(cms.OrderError) as e:
        cms.fill_bid(c, bid["id"], BUYER, fee_bps=0, platform=None)
    assert e.value.code == "self_cross"
    with pytest.raises(cms.OrderError) as e:
        cms.fill_bid(c, bid["id"], OTHER, fee_bps=0, platform=None)
    assert e.value.code == "not_available"
    fill = cms.fill_bid(c, bid["id"], SELLER, fee_bps=0, platform=None)
    assert fill["ask_order_id"] is None
    assert cms.available_count(c, SELLER, "Head", "Crown") == 0  # encumbered by the fill


def test_move_asset_is_atomic_and_closes_orders():
    c = _conn()
    bid = _open_bid(c)
    fill = cms.fill_bid(c, bid["id"], SELLER, fee_bps=0, platform=None)
    cms.update_fill(c, fill["id"], state=cms.FUNDED, escrow_finish_hash="F")
    assert cms.move_asset(c, fill["id"]) == cms.ASSET_MOVED
    assert cms.holding_count(c, SELLER, "Head", "Crown") == 0
    assert cms.holding_count(c, BUYER, "Head", "Crown") == 1
    assert (
        c.execute("SELECT COUNT(*) FROM closet_assets WHERE owner=?", (SELLER,)).fetchone()[0] == 0
    )
    assert cms.get_order(c, bid["id"])["state"] == cms.FILLED
    assert cms.has_unmirrored_fill(c, SELLER) and cms.has_unmirrored_fill(c, BUYER)
    cms.mark_side_mirrored(c, fill["id"], "seller")
    assert not cms.has_unmirrored_fill(c, SELLER) and cms.has_unmirrored_fill(c, BUYER)
    cms.update_fill(c, fill["id"], state=cms.PAID)
    cms.mark_side_mirrored(c, fill["id"], "buyer")
    assert cms.get_fill(c, fill["id"])["state"] == cms.MIRRORED


def test_has_recent_fill_covers_a_just_mirrored_side():
    """(#443 final review I2) clio can serve the pre-mirror Closet metadata
    for a while after the mirror modify, so a side stays guarded for a
    window after it is marked mirrored."""
    c = _conn()
    bid = _open_bid(c)
    fill = cms.fill_bid(c, bid["id"], SELLER, fee_bps=0, platform=None)
    assert not cms.has_recent_fill(c, SELLER)  # pre-move: the DB is not ahead yet
    cms.update_fill(c, fill["id"], state=cms.FUNDED, escrow_finish_hash="F")
    cms.move_asset(c, fill["id"])
    assert cms.has_recent_fill(c, SELLER) and cms.has_recent_fill(c, BUYER)  # unmirrored
    cms.update_fill(c, fill["id"], state=cms.PAID)
    cms.mark_side_mirrored(c, fill["id"], "seller")
    cms.mark_side_mirrored(c, fill["id"], "buyer")
    assert cms.get_fill(c, fill["id"])["state"] == cms.MIRRORED
    mirrored_at = cms.get_fill(c, fill["id"])["updated_ts"]
    assert cms.has_recent_fill(c, SELLER, now=mirrored_at + 10)
    assert cms.has_recent_fill(c, BUYER, now=mirrored_at + 300)
    assert not cms.has_recent_fill(c, SELLER, now=mirrored_at + 301)
    assert not cms.has_recent_fill(c, BUYER, window_seconds=5, now=mirrored_at + 10)
    assert not cms.has_recent_fill(c, OTHER, now=mirrored_at)


def test_move_asset_refunds_when_seller_no_longer_holds():
    c = _conn()
    ask = cms.create_ask(c, owner=SELLER, slot="Head", value="Crown", price_brix="5", platform=None)
    fill = cms.create_take_fill(c, ask["id"], BUYER, fee_bps=0, platform=None)
    assert cms.claim_ask_for_payment(c, fill["id"], "PAY1") == cms.FUNDED
    es.set_closet_contents(c, SELLER, [], [])  # the unit vanished (e.g. listener rebuild)
    assert cms.move_asset(c, fill["id"]) == cms.REFUND_PENDING
    f = cms.get_fill(c, fill["id"])
    assert (f["refund_to"], f["refund_brix"]) == (BUYER, "5")
    assert cms.get_order(c, ask["id"])["state"] == cms.CANCELLED


def test_second_payment_for_same_ask_is_refunded():
    c = _conn()
    ask = cms.create_ask(c, owner=SELLER, slot="Head", value="Crown", price_brix="5", platform=None)
    f1 = cms.create_take_fill(c, ask["id"], BUYER, fee_bps=0, platform=None)
    f2 = cms.create_take_fill(c, ask["id"], OTHER, fee_bps=0, platform=None)
    assert cms.claim_ask_for_payment(c, f1["id"], "P1") == cms.FUNDED
    assert cms.claim_ask_for_payment(c, f2["id"], "P2") == cms.REFUND_PENDING
    with pytest.raises(sqlite3.IntegrityError):  # a payment hash can fund one fill only
        cms.update_fill(c, f2["id"], payment_tx_hash="P1")


def test_abort_unfunded_fill_reopens_bid_and_cancels_short_ask():
    c = _conn()
    cms.create_ask(c, owner=SELLER, slot="Head", value="Crown", price_brix="5", platform=None)
    bid = _open_bid(c, price="5")
    fill = cms.cross_incoming(c, bid["id"], fee_bps=0)
    es.set_closet_contents(c, SELLER, [], [])
    assert cms.fill_blocker(c, fill["id"]) == "the seller no longer holds the trait"
    cms.abort_unfunded_fill(c, fill["id"], "the seller no longer holds the trait")
    assert cms.get_fill(c, fill["id"])["state"] == cms.FAILED
    assert cms.get_order(c, bid["id"])["state"] == cms.OPEN
    assert cms.get_order(c, fill["ask_order_id"])["state"] == cms.CANCELLED


def test_abort_unfunded_fill_cancel_reason_sets_atomically():
    """(#443 review fix round 2) A caller that needs both bid_state and
    cancel_reason must get them in the same guarded UPDATE, not a second
    non-atomic update_order call."""
    c = _conn()
    bid = _open_bid(c)
    fill = cms.fill_bid(c, bid["id"], SELLER, fee_bps=0, platform=None)
    cms.abort_unfunded_fill(
        c,
        fill["id"],
        "the bid expired before it could be filled",
        bid_state=cms.CANCELLING,
        cancel_reason="expired",
    )
    got = cms.get_order(c, bid["id"])
    assert (got["state"], got["cancel_reason"]) == (cms.CANCELLING, "expired")


def test_begin_cancel_only_from_open():
    c = _conn()
    bid = _open_bid(c)
    got = cms.begin_cancel_bid(c, bid["id"], owner=BUYER, reason="user")
    assert (got["state"], got["cancel_reason"]) == (cms.CANCELLING, "user")
    with pytest.raises(cms.OrderError):
        cms.begin_cancel_bid(c, bid["id"], owner=BUYER, reason="user")


def test_bids_on_holdings_prefers_closet_then_token():
    c = _conn()
    es.upsert_trait_token(c, "TOK1", OTHER, "Head", "Crown")
    bid = _open_bid(c)
    [mine] = cms.bids_on_holdings(c, SELLER)
    assert (mine["id"], mine["source"], mine["nft_id"]) == (bid["id"], "closet", None)
    [theirs] = cms.bids_on_holdings(c, OTHER)
    assert (theirs["source"], theirs["nft_id"]) == ("token", "TOK1")
    assert cms.bids_on_holdings(c, BUYER) == []  # never your own bid


def test_meta_orders_and_sweep_queries():
    c = _conn(seller_count=2)  # one copy listed, one free to fill a bid with
    assert cms.open_orders_for_meta(c, SELLER) is None
    cms.create_ask(c, owner=SELLER, slot="Head", value="Crown", price_brix="5", platform=None)
    assert cms.open_orders_for_meta(c, SELLER) == [
        {"side": "ask", "slot": "Head", "value": "Crown", "price_brix": "5"}
    ]
    bid = _open_bid(c, owner=OTHER, price="1")
    assert bid["id"] in cms.orders_needing_attention(c)
    assert cms.fills_needing_attention(c) == []
    fill = cms.fill_bid(c, bid["id"], SELLER, fee_bps=0, platform=None)
    assert cms.fills_needing_attention(c) == [fill["id"]]


def test_orders_for_owner_live_states_and_fills():
    c = _conn(seller_count=5)
    es.set_closet_contents(c, BUYER, [("Head", "Crown", 2)], [])
    # SELLER creates asks and bids
    ask1 = cms.create_ask(
        c,
        owner=SELLER,
        slot="Head",
        value="Crown",
        price_brix="5",
        platform=None,
        now=10,
    )
    ask2 = cms.create_ask(
        c,
        owner=SELLER,
        slot="Head",
        value="Crown",
        price_brix="6",
        platform=None,
        now=30,
    )
    seller_bid = _open_bid(c, owner=SELLER, price="3", now=70)
    # BUYER creates a bid
    buyer_bid1 = _open_bid(c, owner=BUYER, price="5", now=20)
    # Cancel ask1 to have a non-live order
    cms.cancel_ask(c, ask1["id"], SELLER)
    # Fill buyer_bid1 (holder fill: SELLER fills BUYER's bid directly)
    fill_bid1 = cms.fill_bid(c, buyer_bid1["id"], SELLER, fee_bps=0, platform=None, now=40)
    cms.update_fill(c, fill_bid1["id"], state=cms.FUNDED, escrow_finish_hash="F")
    cms.move_asset(c, fill_bid1["id"])
    # Fill ask2 with a payment (BUYER pays for SELLER's ask)
    fill_ask2 = cms.create_take_fill(c, ask2["id"], BUYER, fee_bps=0, platform=None, now=50)
    cms.claim_ask_for_payment(c, fill_ask2["id"], "PAY1")

    # orders_for_owner for SELLER: seller_bid (open) + ask2 (matched) only
    # ask1 is cancelled (not live), buyer_bid1 is not SELLER's
    orders = cms.orders_for_owner(c, SELLER)
    live_orders = orders["orders"]
    # Should be newest-first: seller_bid (open, 70) then ask2 (matched, 30)
    assert len(live_orders) == 2
    assert live_orders[0]["id"] == seller_bid["id"] and live_orders[0]["state"] == cms.OPEN
    assert live_orders[1]["id"] == ask2["id"] and live_orders[1]["state"] == cms.MATCHED
    # No fulfillment_enc/condition in returned dicts for asks
    for order in live_orders:
        if order["side"] == cms.SIDE_ASK:
            assert "fulfillment_enc" not in order
            assert "condition" not in order

    # fills for SELLER: fill_bid1 (seller) and fill_ask2 (seller)
    fills = orders["fills"]
    assert len(fills) == 2
    assert all(f["seller"] == SELLER or f["buyer"] == SELLER for f in fills)
    # fills are newest-first: fill_ask2 (50) then fill_bid1 (40)
    assert fills[0]["id"] == fill_ask2["id"]
    assert fills[1]["id"] == fill_bid1["id"]

    # orders_for_owner for BUYER: no live orders (buyer_bid1 is filled, not live)
    # orders_for_owner only returns pending_escrow/open/matched/cancelling states
    orders_buyer = cms.orders_for_owner(c, BUYER)
    buyer_orders = orders_buyer["orders"]
    assert len(buyer_orders) == 0  # buyer_bid1 is filled after move_asset
    # fills: fill_ask2 (buyer) and fill_bid1 (buyer) where BUYER is seller or buyer
    buyer_fills = orders_buyer["fills"]
    assert len(buyer_fills) == 2
    assert all(f["seller"] == BUYER or f["buyer"] == BUYER for f in buyer_fills)

    # Test fills_limit truncation
    orders_limited = cms.orders_for_owner(c, SELLER, fills_limit=1)
    assert len(orders_limited["fills"]) == 1
    assert orders_limited["fills"][0]["id"] == fill_ask2["id"]  # newest


# --- PR #502 G1: the user tx's pinned LastLedgerSequence ------------------------


def test_user_lls_is_persisted_on_bids_and_fills():
    c = _conn()
    bid = cms.create_pending_bid(
        c,
        owner=BUYER,
        slot="Head",
        value="Crown",
        price_brix="10",
        platform="web",
        condition="A0",
        fulfillment_enc="sealed",
        cancel_after=999999,
        payload_uuid="U",
        xumm_url="x",
        qr_url="q",
        push=None,
        user_lls=4321,
    )
    assert cms.get_order(c, bid["id"])["user_lls"] == 4321
    ask = cms.create_ask(c, owner=SELLER, slot="Head", value="Crown", price_brix="5", platform=None)
    fl = cms.create_take_fill(c, ask["id"], OTHER, fee_bps=0, platform=None)
    assert fl["user_lls"] is None
    cms.update_fill(c, fl["id"], payload_uuid="PU", user_lls=777)
    got = cms.get_fill(c, fl["id"])
    assert (got["payload_uuid"], got["user_lls"]) == ("PU", 777)


def test_ensure_schema_adds_user_lls_to_existing_tables():
    c = sqlite3.connect(":memory:")
    legacy = "\n".join(line for line in cms._SCHEMA.splitlines() if "user_lls" not in line)
    c.executescript(legacy)
    for table in ("closet_orders", "closet_fills"):
        cols = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
        assert "user_lls" not in cols
    cms.ensure_schema(c)
    cms.ensure_schema(c)  # idempotent
    for table in ("closet_orders", "closet_fills"):
        cols = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
        assert "user_lls" in cols
