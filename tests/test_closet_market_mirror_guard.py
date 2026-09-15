import asyncio
import importlib.util
import pathlib
import sqlite3

from lfg_core import closet_market_store as cms
from lfg_core import closet_token as ct
from lfg_core import economy_store as es
from lfg_core import nft_listener

SELLER, BUYER = "rSeller", "rBuyer"


def _moved_fill_conn():
    c = sqlite3.connect(":memory:")
    es.init_economy_schema(c)
    for owner in (SELLER, BUYER):
        es.set_closet_token(c, owner, f"C-{owner}", "00", status=ct.ACTIVE)
    es.set_closet_contents(c, SELLER, [("Head", "Crown", 1)], [])
    bid = cms.create_pending_bid(
        c,
        owner=BUYER,
        slot="Head",
        value="Crown",
        price_brix="3",
        platform=None,
        condition="A0",
        fulfillment_enc="s",
        cancel_after=9,
        payload_uuid=None,
        xumm_url=None,
        qr_url=None,
        push=None,
    )
    cms.mark_bid_open(c, bid["id"], escrow_tx_hash="E", escrow_owner_seq=1)
    fill = cms.fill_bid(c, bid["id"], SELLER, fee_bps=0, platform=None)
    cms.update_fill(c, fill["id"], state=cms.FUNDED)
    assert cms.move_asset(c, fill["id"]) == cms.ASSET_MOVED
    return c


def _stale_seller_meta():  # the seller's Closet metadata from BEFORE the fill
    return ct.build_closet_metadata(SELLER, [("Head", "Crown", 1)], [])


def test_metadata_orders_block_is_optional_and_ignored_by_parse():
    meta = ct.build_closet_metadata("rX", [("Head", "Crown", 1)], [], orders=[{"side": "ask"}])
    assert meta["lfg_closet"]["orders"] == [{"side": "ask"}]
    assert "orders" not in ct.build_closet_metadata("rX", [], [])["lfg_closet"]
    assert ct.parse_closet_metadata(meta) == ([("Head", "Crown", 1)], [])


def test_sync_closet_writes_open_orders():
    c = sqlite3.connect(":memory:")
    es.init_economy_schema(c)
    es.set_closet_token(c, SELLER, "C1", "00", status=ct.ACTIVE)
    es.set_closet_contents(c, SELLER, [("Head", "Crown", 1)], [])
    cms.create_ask(c, owner=SELLER, slot="Head", value="Crown", price_brix="4", platform=None)
    uploaded = []

    async def upload(meta):
        uploaded.append(meta)
        return "https://cdn/x.json"

    async def modify(nft_id, owner, url):
        return "MODHASH"

    asyncio.new_event_loop().run_until_complete(
        ct.sync_closet(c, SELLER, [("Head", "Crown", 1)], [], upload_fn=upload, modify_fn=modify)
    )
    assert uploaded[0]["lfg_closet"]["orders"] == [
        {"side": "ask", "slot": "Head", "value": "Crown", "price_brix": "4"}
    ]


def test_listener_does_not_resurrect_a_moved_unit():
    c = _moved_fill_conn()
    nft_listener._apply_closet(
        c, {"owner": SELLER, "nft_id": "C-rSeller", "uri_hex": "00"}, _stale_seller_meta()
    )
    assert cms.holding_count(c, SELLER, "Head", "Crown") == 0


def test_backfill_does_not_resurrect_a_moved_unit():
    path = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "backfill_economy.py"
    spec = importlib.util.spec_from_file_location("backfill_economy_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    c = _moved_fill_conn()
    mod._reconcile_closet(
        c,
        {"owner": SELLER, "nft_id": "C-rSeller", "uri_hex": "00"},
        _stale_seller_meta(),
        "rIssuer",
    )
    assert cms.holding_count(c, SELLER, "Head", "Crown") == 0


def test_listener_rebuilds_normally_once_mirrored():
    c = _moved_fill_conn()
    fill_id = c.execute("SELECT id FROM closet_fills").fetchone()[0]
    cms.update_fill(c, fill_id, state=cms.PAID)
    cms.mark_side_mirrored(c, fill_id, "seller")
    cms.mark_side_mirrored(c, fill_id, "buyer")
    nft_listener._apply_closet(
        c,
        {"owner": SELLER, "nft_id": "C-rSeller", "uri_hex": "00"},
        ct.build_closet_metadata(SELLER, [("Head", "Tiara", 1)], []),
    )
    assert cms.holding_count(c, SELLER, "Head", "Tiara") == 1
