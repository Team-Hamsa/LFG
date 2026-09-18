"""Browse > Traits lists Closet asks beside the NFT trait listings.

With the Closet market on, every open ask is a listing like any other: it joins
its (slot, value) group, counts toward the group's count and floor, and goes
through the same price / seller / trait filters, sorts and paging."""

import pytest
from cryptography.fernet import Fernet

from lfg_core import closet_market_store as cms
from lfg_core import closet_token as ct
from lfg_core import economy_store as es
from lfg_core.nft_index import init_db as init_onchain_db
from lfg_service import app as server
from tests import test_market_api as market_tests
from tests.test_market_api import _browse, _seed_grouped_traits

onchain_env = market_tests.onchain_env  # re-export the fixture (a direct import trips ruff F811)

CLOSET_SELLER = "rClosetSeller"


@pytest.fixture
def closet_on(onchain_env, monkeypatch):
    for name, value in {
        "ECONOMY_ENABLED": True,
        "CLOSET_MARKET_ENABLED": True,
        "CLOSET_MARKET_ENC_KEY": Fernet.generate_key().decode(),
    }.items():
        monkeypatch.setattr(server.config, name, value)
    conn = _db(onchain_env)
    es.set_closet_token(conn, CLOSET_SELLER, "C-closet-seller", "00", status=ct.ACTIVE)
    es.set_closet_contents(
        conn, CLOSET_SELLER, [("Hat", "Wizard Hat", 2), ("Mouth", "Grin", 1)], []
    )
    conn.commit()
    conn.close()
    return onchain_env


def _db(path):
    conn = init_onchain_db(path)
    es.init_economy_schema(conn)
    return conn


def _ask(path, slot, value, price, now=2000):
    conn = _db(path)
    try:
        return cms.create_ask(
            conn,
            owner=CLOSET_SELLER,
            slot=slot,
            value=value,
            price_brix=price,
            platform=None,
            now=now,
        )
    finally:
        conn.close()


def _key(row):
    return row["order_id"] if row.get("source") == "closet" else row["offer_index"]


def test_closet_asks_join_the_trait_groups(closet_on):
    _seed_grouped_traits(closet_on)  # NFT: Wizard Hat at 12 / 5 / 9.5, Hypno at 7
    hat_ask = _ask(closet_on, "Hat", "Wizard Hat", "4")
    grin_ask = _ask(closet_on, "Mouth", "Grin", "3")

    body = _browse("kind=trait&group=1&sort=price_asc")

    assert body["total"] == 3
    assert [r["value"] for r in body["rows"]] == ["Grin", "Wizard Hat", "Hypno"]
    grin, hat, _ = body["rows"]
    assert grin["source"] == "closet" and grin["order_id"] == grin_ask["id"]
    assert grin["count"] == 1 and grin["floor_brix"] == "3"
    assert hat["count"] == 4 and hat["floor_brix"] == "4"
    # The group row is its cheapest offer, here the Closet ask, so Buy on the
    # card acts on the ask.
    assert hat["source"] == "closet" and hat["order_id"] == hat_ask["id"]
    assert hat["offer_index"] is None and hat["nft_id"] is None
    assert hat["seller"] == CLOSET_SELLER and hat["buyable"] is True
    assert hat["amount_brix"] == "4"
    assert [_key(o) for o in hat["offers"]] == [hat_ask["id"], "B" * 64, "C" * 64, "A" * 64]
    assert all("source" not in o for o in hat["offers"][1:])  # NFT offers unchanged


def test_closet_asks_are_left_out_when_the_closet_market_is_off(closet_on, monkeypatch):
    _seed_grouped_traits(closet_on)
    _ask(closet_on, "Mouth", "Grin", "3")
    monkeypatch.setattr(server.config, "CLOSET_MARKET_ENABLED", False)

    body = _browse("kind=trait&group=1")

    assert body["total"] == 2
    assert all(r.get("source") != "closet" for r in body["rows"])


def test_price_bounds_apply_to_closet_asks(closet_on):
    _seed_grouped_traits(closet_on)
    _ask(closet_on, "Hat", "Wizard Hat", "4")
    _ask(closet_on, "Mouth", "Grin", "3")

    body = _browse("kind=trait&group=1&min_brix=3.5&max_brix=6")

    # Grin (3) is below the floor and Hypno (7) above the ceiling; the Wizard
    # Hat group keeps the 4 BRIX ask and the 5 BRIX NFT offer.
    assert [r["value"] for r in body["rows"]] == ["Wizard Hat"]
    assert body["rows"][0]["count"] == 2


def test_seller_filter_shows_a_sellers_closet_asks(closet_on):
    _seed_grouped_traits(closet_on)
    _ask(closet_on, "Hat", "Wizard Hat", "4")
    _ask(closet_on, "Mouth", "Grin", "3")

    body = _browse(f"kind=trait&group=1&seller={CLOSET_SELLER}")

    assert [r["value"] for r in body["rows"]] == ["Grin", "Wizard Hat"]
    assert all(r["source"] == "closet" and r["count"] == 1 for r in body["rows"])


def test_trait_filter_matches_closet_asks(closet_on):
    _seed_grouped_traits(closet_on)
    grin_ask = _ask(closet_on, "Mouth", "Grin", "3")

    body = _browse("kind=trait&group=1&trait=Mouth:Grin")

    assert [r["order_id"] for r in body["rows"]] == [grin_ask["id"]]


@pytest.mark.parametrize("sort", ["price_asc", "price_desc", "newest", "rarity_desc"])
def test_an_ask_priced_like_an_nft_offer_sorts_without_error(closet_on, sort):
    _seed_grouped_traits(closet_on)  # the "B" NFT offer is 5 BRIX
    ask = _ask(closet_on, "Hat", "Wizard Hat", "5")

    body = _browse(f"kind=trait&sort={sort}")

    keys = [_key(r) for r in body["rows"]]
    assert body["total"] == 5 and ask["id"] in keys and "B" * 64 in keys
    if sort == "newest":  # the ask (created_ts 2000) is newer than every NFT listing (1000)
        assert keys[0] == ask["id"]


def test_equal_prices_order_nft_offers_before_closet_asks(closet_on):
    _seed_grouped_traits(closet_on)
    ask = _ask(closet_on, "Hat", "Wizard Hat", "5")

    body = _browse("kind=trait&group=1&sort=price_asc")

    hat = next(r for r in body["rows"] if r["value"] == "Wizard Hat")
    assert [_key(o) for o in hat["offers"][:2]] == ["B" * 64, ask["id"]]


def test_a_new_ask_shows_up_despite_the_listings_cache(closet_on):
    _seed_grouped_traits(closet_on)
    assert _browse("kind=trait&group=1")["total"] == 2  # fills the 60 s cache
    ask = _ask(closet_on, "Mouth", "Grin", "3")

    body = _browse("kind=trait&group=1")

    assert body["total"] == 3
    assert ask["id"] in [r.get("order_id") for r in body["rows"]]
