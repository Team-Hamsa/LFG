import asyncio
import importlib.util
import pathlib
import sqlite3
import time

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


def _mirrored_fill_conn(seconds_ago):
    c = _moved_fill_conn()
    fill_id = c.execute("SELECT id FROM closet_fills").fetchone()[0]
    cms.update_fill(c, fill_id, state=cms.PAID)
    cms.mark_side_mirrored(c, fill_id, "seller")
    cms.mark_side_mirrored(c, fill_id, "buyer")
    c.execute(
        "UPDATE closet_fills SET updated_ts = ? WHERE id = ?",
        (int(time.time()) - seconds_ago, fill_id),
    )
    c.commit()
    return c


def test_listener_still_skips_a_just_mirrored_owner():
    """(#443 final review I2) the mirror modify just landed but clio can still
    serve the stale pre-fill metadata — rebuilding now resurrects the unit."""
    c = _mirrored_fill_conn(10)
    nft_listener._apply_closet(
        c, {"owner": SELLER, "nft_id": "C-rSeller", "uri_hex": "00"}, _stale_seller_meta()
    )
    assert cms.holding_count(c, SELLER, "Head", "Crown") == 0


def test_backfill_still_skips_a_just_mirrored_owner():
    path = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "backfill_economy.py"
    spec = importlib.util.spec_from_file_location("backfill_economy_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    c = _mirrored_fill_conn(10)
    mod._reconcile_closet(
        c,
        {"owner": SELLER, "nft_id": "C-rSeller", "uri_hex": "00"},
        _stale_seller_meta(),
        "rIssuer",
    )
    assert cms.holding_count(c, SELLER, "Head", "Crown") == 0


def test_listener_rebuilds_normally_once_mirrored_long_enough():
    c = _mirrored_fill_conn(400)
    nft_listener._apply_closet(
        c,
        {"owner": SELLER, "nft_id": "C-rSeller", "uri_hex": "00"},
        ct.build_closet_metadata(SELLER, [("Head", "Tiara", 1)], []),
    )
    assert cms.holding_count(c, SELLER, "Head", "Tiara") == 1


# --- #535: during a fill the mirror's record never names a version the DB lacks ---
#
# While an owner has an unmirrored (or just-mirrored) fill, the listener and the
# backfill keep the DB contents (#443). The record's URI + stamp tell the flows'
# and the settlement's stale-mirror guard which version those contents are; it
# may name the new version only when the DB holds exactly its contents.

from lfg_core import history_store  # noqa: E402
from tests.closet_archive_helpers import closet_archive, closet_uri  # noqa: E402

SELLER_CLOSET = "C-" + SELLER


def _stamped_seller(c):
    es.set_closet_token(
        c,
        SELLER,
        SELLER_CLOSET,
        closet_uri("u"),
        status=ct.ACTIVE,
        applied_ledger_index=1000,
        applied_tx_index=1,
    )


def _landed_flow_version():
    """A version the DB lacks: a flow's Closet modify came back indeterminate
    but landed (the flow wrote no mirror) — a deposit credit on top of the
    post-fill contents."""
    return ct.build_closet_metadata(SELLER, [("Eyes", "Wavy", 1)], [])


def test_listener_never_names_a_version_the_kept_contents_lack(tmp_path):
    c = _moved_fill_conn()
    _stamped_seller(c)

    nft_listener._apply_closet(
        c,
        {"owner": SELLER, "nft_id": SELLER_CLOSET, "uri_hex": closet_uri("v")},
        _landed_flow_version(),
        ledger_index=1005,
        tx_index=3,
    )

    assert es.get_closet_token(c, SELLER) == (SELLER_CLOSET, closet_uri("u"))
    assert es.get_closet_applied_position(c, SELLER) == (1000, 1)
    # So the guard sees the mirror as behind and the settlement waits instead of
    # erasing the credit with its full overwrite.
    archive = closet_archive(
        tmp_path,
        SELLER_CLOSET,
        [("modify", closet_uri("u"), 1000, 1), ("modify", closet_uri("v"), 1005, 3)],
    )
    try:
        ct.ensure_mirror_current(c, SELLER, history_db_path=archive)
    except ct.ClosetError as e:
        assert e.code == ct.CLOSET_MIRROR_BEHIND
    else:
        raise AssertionError("the guard read a mirror lacking version v as current")


def test_listener_names_the_version_the_kept_contents_are():
    """The settlement wrote version v from these very DB contents but its record
    write failed: the DB holds exactly v, so the record may name it."""
    c = _moved_fill_conn()
    _stamped_seller(c)

    nft_listener._apply_closet(
        c,
        {"owner": SELLER, "nft_id": SELLER_CLOSET, "uri_hex": closet_uri("v")},
        ct.build_closet_metadata(SELLER, [], []),
        ledger_index=1005,
        tx_index=3,
    )

    assert es.get_closet_token(c, SELLER) == (SELLER_CLOSET, closet_uri("v"))
    assert es.get_closet_applied_position(c, SELLER) == (1005, 3)


def _backfill_module():
    path = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "backfill_economy.py"
    spec = importlib.util.spec_from_file_location("backfill_economy_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_backfill_never_names_a_version_the_kept_contents_lack(tmp_path):
    c = _moved_fill_conn()
    _stamped_seller(c)
    archive = history_store.connect_readonly(
        closet_archive(
            tmp_path,
            SELLER_CLOSET,
            [("modify", closet_uri("u"), 1000, 1), ("modify", closet_uri("v"), 1005, 3)],
        )
    )
    try:
        _backfill_module()._reconcile_closet(
            c,
            {"owner": SELLER, "nft_id": SELLER_CLOSET, "uri_hex": closet_uri("v")},
            _landed_flow_version(),
            "rIssuer",
            archive=archive,
        )
    finally:
        archive.close()

    assert es.get_closet_token(c, SELLER) == (SELLER_CLOSET, closet_uri("u"))
    assert es.get_closet_applied_position(c, SELLER) == (1000, 1)


def test_backfill_names_and_dates_the_version_the_kept_contents_are(tmp_path):
    c = _moved_fill_conn()
    _stamped_seller(c)
    archive = history_store.connect_readonly(
        closet_archive(
            tmp_path,
            SELLER_CLOSET,
            [("modify", closet_uri("u"), 1000, 1), ("modify", closet_uri("v"), 1005, 3)],
        )
    )
    try:
        _backfill_module()._reconcile_closet(
            c,
            {"owner": SELLER, "nft_id": SELLER_CLOSET, "uri_hex": closet_uri("v")},
            ct.build_closet_metadata(SELLER, [], []),
            "rIssuer",
            archive=archive,
        )
    finally:
        archive.close()

    assert es.get_closet_token(c, SELLER) == (SELLER_CLOSET, closet_uri("v"))
    assert es.get_closet_applied_position(c, SELLER) == (1005, 3)
