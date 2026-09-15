import sqlite3
from decimal import Decimal

import pytest

from lfg_core import closet_market_store as cms
from lfg_core import closet_token as ct
from lfg_core import economy_store as es

SELLER, BUYER = "rSeller", "rBuyer"


def _conn():
    c = sqlite3.connect(":memory:")
    es.init_economy_schema(c)  # must also create the closet market tables
    for owner in (SELLER, BUYER):
        es.set_closet_token(c, owner, f"CLOSET-{owner}", "00", status=ct.ACTIVE)
    es.set_closet_contents(c, SELLER, [("Head", "Crown", 2)], [])
    return c


def test_init_economy_schema_creates_tables():
    c = _conn()
    names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"closet_orders", "closet_fills"} <= names


def test_active_constant_matches_closet_token():
    assert cms._ACTIVE == ct.ACTIVE


def test_create_ask_encumbers_without_touching_count():
    c = _conn()
    ask = cms.create_ask(
        c, owner=SELLER, slot="Head", value="Crown", price_brix="12.5", platform="discord"
    )
    assert ask["state"] == cms.OPEN and ask["side"] == cms.SIDE_ASK
    assert cms.holding_count(c, SELLER, "Head", "Crown") == 2
    assert cms.available_count(c, SELLER, "Head", "Crown") == 1
    assert cms.encumbrance(c, SELLER) == {("Head", "Crown"): 1}


def test_cannot_ask_more_than_held():
    c = _conn()
    for _ in range(2):
        cms.create_ask(c, owner=SELLER, slot="Head", value="Crown", price_brix="1", platform=None)
    with pytest.raises(cms.OrderError) as e:
        cms.create_ask(c, owner=SELLER, slot="Head", value="Crown", price_brix="1", platform=None)
    assert e.value.code == "not_available"


def test_ask_requires_active_closet():
    c = _conn()
    es.set_closet_status(c, SELLER, ct.PENDING_ACCEPT)
    with pytest.raises(cms.OrderError) as e:
        cms.create_ask(c, owner=SELLER, slot="Head", value="Crown", price_brix="1", platform=None)
    assert e.value.code == "closet_required"


def test_cancel_ask_frees_the_unit_and_is_owner_only():
    c = _conn()
    ask = cms.create_ask(c, owner=SELLER, slot="Head", value="Crown", price_brix="3", platform=None)
    with pytest.raises(cms.OrderError) as e:
        cms.cancel_ask(c, ask["id"], BUYER)
    assert e.value.code == "not_found"
    assert cms.cancel_ask(c, ask["id"], SELLER)["state"] == cms.CANCELLED
    assert cms.available_count(c, SELLER, "Head", "Crown") == 2
    with pytest.raises(cms.OrderError) as e:
        cms.cancel_ask(c, ask["id"], SELLER)
    assert e.value.code == "not_open"


def test_book_summary_and_levels_sort_by_decimal_not_text():
    c = _conn()
    cms.create_ask(
        c, owner=SELLER, slot="Head", value="Crown", price_brix="9", platform=None, now=1
    )
    cms.create_ask(
        c, owner=SELLER, slot="Head", value="Crown", price_brix="10", platform=None, now=2
    )
    [row] = cms.book_summary(c)
    assert row == {
        "slot": "Head",
        "value": "Crown",
        "best_ask_brix": "9",
        "ask_count": 2,
        "best_bid_brix": None,
        "bid_count": 0,
    }
    levels = cms.book_levels(c, "Head", "Crown")
    assert [a["price_brix"] for a in levels["asks"]] == ["9", "10"]
    assert levels["bids"] == []


def test_fee_rounds_down_to_micro_brix():
    assert cms.fee_for("1", 700) == "0.07"
    assert cms.fee_for("0.000001", 700) == "0"
    assert cms.fee_for("12.345678", 250) == "0.308641"
    assert cms.fmt_brix(Decimal("5.000000")) == "5"


def test_memo_tags_and_invoice_id():
    assert cms.memo_tag("forward", "abc") == "lfg:closet_forward:abc"
    with pytest.raises(ValueError):
        cms.memo_tag("nope", "abc")
    inv = cms.invoice_id("abc")
    assert len(inv) == 64 and inv == inv.upper()


def test_update_order_rejects_unknown_fields_and_states():
    c = _conn()
    ask = cms.create_ask(c, owner=SELLER, slot="Head", value="Crown", price_brix="3", platform=None)
    with pytest.raises(ValueError):
        cms.update_order(c, ask["id"], owner="rEvil")
    with pytest.raises(ValueError):
        cms.update_order(c, ask["id"], state="bogus")
