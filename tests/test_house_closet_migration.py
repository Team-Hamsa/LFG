"""House Closet migration (#548): plan, plan file, phases."""

import pytest
from cryptography.fernet import Fernet

from lfg_core import closet_market_store as cms
from lfg_core import closet_token as ct
from lfg_core import config
from lfg_core import economy_store as es
from lfg_core import house_closet as hc
from lfg_core import market_store
from lfg_core.market_store import MarketListing, upsert_listing
from lfg_core.nft_index import init_db as init_onchain_db

APP, HOUSE, USER, BIDDER = "rApp", "rHouse", "rUser", "rBidder"


def _db(tmp_path):
    conn = init_onchain_db(str(tmp_path / "onchain.db"))
    es.init_economy_schema(conn)
    market_store.init_db(conn)
    es.set_closet_token(conn, HOUSE, "C-house", "AB", status=ct.ACTIVE, offer_id=None)
    es.set_closet_contents(conn, HOUSE, [], [])
    conn.commit()
    return conn


def _listed(conn, nft_id, slot, value, price, *, seller=APP, destination=None):
    es.upsert_trait_token(conn, nft_id, seller, slot, value)
    upsert_listing(
        conn,
        MarketListing(
            offer_index=f"O{nft_id}".ljust(64, "0"),
            nft_id=nft_id,
            kind="trait",
            seller=seller,
            amount_brix=price,
            destination=destination,
            slot=slot,
            value=value,
            created_ledger=1,
            created_ts=1,
        ),
    )
    conn.commit()


def _stock(conn):
    _listed(conn, "TA", "Hat", "Cap", "12")
    _listed(conn, "TB", "Back", "None", "5")
    _listed(conn, "TC", "Hat", "Cap", "7", seller=USER)  # a user's listing
    _listed(conn, "TD", "Eyes", "Laser", "9", destination="rBroker")  # external
    es.upsert_trait_token(conn, "TE", APP, "Mouth", "Grin")  # app-held, unlisted
    conn.commit()


def test_build_plan_takes_the_app_wallets_listed_tokens(tmp_path):
    conn = _db(tmp_path)
    _stock(conn)
    assert hc.build_plan(conn, APP) == [
        hc.PlanItem("TB", "Back", "None", "OTB".ljust(64, "0"), "5", hc.HOLD),
        hc.PlanItem("TA", "Hat", "Cap", "OTA".ljust(64, "0"), "12", hc.LIST),
    ]


def test_merge_plan_resumes_and_pins_the_house(tmp_path):
    conn = _db(tmp_path)
    _stock(conn)
    state = hc.load_state(str(tmp_path / "plan.json"))
    assert hc.merge_plan(state, hc.build_plan(conn, APP), HOUSE) == 2
    state["items"]["TA"]["status"] = hc.DEPOSITED
    assert hc.merge_plan(state, hc.build_plan(conn, APP), HOUSE) == 0
    assert state["items"]["TA"]["status"] == hc.DEPOSITED  # a re-run resumes
    with pytest.raises(hc.MigrationRefused, match="house"):
        hc.merge_plan(state, [], "rOtherHouse")


def test_the_plan_file_round_trips(tmp_path):
    conn = _db(tmp_path)
    _stock(conn)
    path = str(tmp_path / "reports" / "plan.json")
    state = hc.load_state(path)
    hc.merge_plan(state, hc.build_plan(conn, APP), HOUSE)
    hc.save_state(path, state)
    assert hc.load_state(path) == state


def test_house_busy_sees_live_orders_and_unfinished_fills(tmp_path):
    conn = _db(tmp_path)
    assert not hc.house_busy(conn, HOUSE)
    es.set_closet_contents(conn, HOUSE, [("Hat", "Cap", 1)], [])
    cms.create_ask(conn, owner=HOUSE, slot="Hat", value="Cap", price_brix="3", platform=None)
    assert hc.house_busy(conn, HOUSE)


def test_crossing_bids_previews_fills_at_the_bid_price(tmp_path):
    conn = _db(tmp_path)
    _stock(conn)
    _open_bid(conn, "Hat", "Cap", "20")
    _open_bid(conn, "Hat", "Cap", "4", owner="rCheap")  # below the ask: no fill
    assert hc.crossing_bids(conn, hc.build_plan(conn, APP), HOUSE) == [
        {"slot": "Hat", "value": "Cap", "ask_brix": "12", "bid_brix": "20", "bidder": BIDDER}
    ]


def _open_bid(conn, slot, value, price, owner=BIDDER):
    es.set_closet_token(conn, owner, f"C-{owner}", "AB", status=ct.ACTIVE, offer_id=None)
    bid = cms.create_pending_bid(
        conn,
        owner=owner,
        slot=slot,
        value=value,
        price_brix=price,
        platform=None,
        condition="C",
        fulfillment_enc="S",
        cancel_after=9,
        payload_uuid=None,
        xumm_url=None,
        qr_url=None,
        push=None,
    )
    return cms.mark_bid_open(conn, bid["id"], escrow_tx_hash=f"E{bid['id']}", escrow_owner_seq=3)


def _ready_env(monkeypatch):
    monkeypatch.setattr(config, "ECONOMY_ENABLED", True)
    monkeypatch.setattr(config, "CLOSET_MARKET_ENABLED", True)
    monkeypatch.setattr(config, "CLOSET_MARKET_ENC_KEY", Fernet.generate_key().decode())


def test_check_ready_fails_closed(tmp_path, monkeypatch):
    conn = _db(tmp_path)
    state = hc.load_state(str(tmp_path / "plan.json"))
    line = {"limit": "1000000000"}
    _ready_env(monkeypatch)
    hc.check_ready(conn, HOUSE, state, brix_line=line, limit="1000000000")  # all good

    monkeypatch.setattr(config, "CLOSET_MARKET_ENABLED", False)
    with pytest.raises(hc.MigrationRefused, match="Closet market is off"):
        hc.check_ready(conn, HOUSE, state, brix_line=line, limit="1000000000")
    _ready_env(monkeypatch)
    with pytest.raises(hc.MigrationRefused, match="no active Closet"):
        hc.check_ready(conn, "rNoCloset", state, brix_line=line, limit="1000000000")
    with pytest.raises(hc.MigrationRefused, match="trust line"):
        hc.check_ready(conn, HOUSE, state, brix_line=None, limit="1000000000")
    with pytest.raises(hc.MigrationRefused, match="trust line"):
        hc.check_ready(conn, HOUSE, state, brix_line={"limit": "10"}, limit="1000000000")
    state["items"]["TX"] = {"status": hc.NEEDS_ATTENTION}
    with pytest.raises(hc.MigrationRefused, match="need attention"):
        hc.check_ready(conn, HOUSE, state, brix_line=line, limit="1000000000")
