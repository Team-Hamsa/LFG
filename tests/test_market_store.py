# tests/test_market_store.py
# Env-guard preamble: importing lfg_core.config freezes its constants (e.g.
# IMG_PROXY_ALLOWED_BASES, LAYER_SOURCE) at import time; set the same defaults
# test_smoke.py uses so collection order can't strand them. (Copy the block
# verbatim from tests/test_server_identity_wiring.py — same keys/values.)
import os

os.environ.setdefault("XUMM_API_KEY", "test")
os.environ.setdefault("XUMM_API_SECRET", "test")
os.environ.setdefault("SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2")
os.environ.setdefault("TOKEN_ISSUER_ADDRESS", "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
os.environ.setdefault("TOKEN_CURRENCY_HEX", "4C46474F00000000000000000000000000000000")
os.environ.setdefault("BUNNY_CDN_ACCESS_KEY", "test")
os.environ.setdefault("BUNNY_CDN_STORAGE_ZONE", "test")
os.environ.setdefault("LAYER_SOURCE", "local")
os.environ.setdefault("BUNNY_PULL_ZONE", "nft.pullzone.example")

import json  # noqa: E402
import sqlite3  # noqa: E402

import pytest  # noqa: E402

from lfg_core import market_store  # noqa: E402
from lfg_core.economy_store import (
    _ECONOMY_SCHEMA,  # noqa: E402
    upsert_trait_token,  # noqa: E402
)
from lfg_core.market_store import MarketListing  # noqa: E402
from lfg_core.nft_index import _SCHEMA as ONCHAIN_SCHEMA  # noqa: E402
from lfg_core.nft_index import OnchainNft  # noqa: E402
from lfg_core.nft_index import upsert as upsert_onchain_nft

SELLER = "rSellerAddress0000000000000000000"
BUYER = "rBuyerAddress000000000000000000000"
OTHER = "rOtherAddress00000000000000000000"
CHAR_NFT = "000800001E43B0783E006F30078A64A8628F4B1B22879C8EB1CAF8C700000019"
TRAIT_NFT = "000900001E43B0783E006F30078A64A8628F4B1B22879C8EB1CAF8C700000abc"


@pytest.fixture
def conn(tmp_path):
    path = str(tmp_path / "onchain_test.db")
    c = sqlite3.connect(path)
    # Sibling tables this module joins against, seeded directly from their own
    # modules' DDL (same convention as production: one file, several stores).
    c.executescript(ONCHAIN_SCHEMA)
    c.executescript(_ECONOMY_SCHEMA)
    c.commit()
    yield c
    c.close()


def _seed_character(
    conn, nft_id=CHAR_NFT, owner=SELLER, is_burned=False, nft_number=42, attrs=None
):
    attrs = (
        attrs
        if attrs is not None
        else [
            {"trait_type": "Hat", "value": "Wizard Hat"},
            {"trait_type": "Body", "value": "Ape"},
        ]
    )
    upsert_onchain_nft(
        conn,
        OnchainNft(
            nft_id=nft_id,
            nft_number=nft_number,
            owner=owner,
            is_burned=is_burned,
            mutable=True,
            uri_hex="",
            body="Ape",
            attributes=attrs,
            image="https://cdn.example/img.png",
            ledger_index=1,
        ),
    )


def _seed_trait_token(conn, nft_id=TRAIT_NFT, owner=SELLER, slot="Hat", value="Wizard Hat"):
    upsert_trait_token(conn, nft_id, owner, slot, value)


def _character_listing(**overrides):
    base = {
        "offer_index": "A" * 64,
        "nft_id": CHAR_NFT,
        "kind": "character",
        "seller": SELLER,
        "amount_drops": 1_000_000,
        "created_ledger": 100,
        "created_ts": 1000,
    }
    base.update(overrides)
    return MarketListing(**base)


def _trait_listing(**overrides):
    base = {
        "offer_index": "B" * 64,
        "nft_id": TRAIT_NFT,
        "kind": "trait",
        "seller": SELLER,
        "amount_drops": 500_000,
        "slot": "Hat",
        "value": "Wizard Hat",
        "created_ledger": 101,
        "created_ts": 1001,
    }
    base.update(overrides)
    return MarketListing(**base)


class TestInitDb:
    def test_idempotent(self, conn):
        market_store.init_db(conn)
        market_store.init_db(conn)  # second call must not raise
        cols = {row[1] for row in conn.execute("PRAGMA table_info(market_listings)")}
        assert cols == {
            "offer_index",
            "nft_id",
            "kind",
            "seller",
            "amount_drops",
            "destination",
            "slot",
            "value",
            "created_ledger",
            "created_ts",
            "is_live",
            "closed_reason",
            "settled",
            "buyer",
        }

    def test_index_created(self, conn):
        market_store.init_db(conn)
        names = {row[1] for row in conn.execute("PRAGMA index_list(market_listings)")}
        assert "idx_market_live" in names


class TestUpsertListing:
    def test_insert_character_listing(self, conn):
        market_store.init_db(conn)
        market_store.upsert_listing(conn, _character_listing())
        row = conn.execute(
            "SELECT nft_id, kind, seller, amount_drops, is_live, settled FROM market_listings WHERE offer_index=?",
            ("A" * 64,),
        ).fetchone()
        assert row == (CHAR_NFT, "character", SELLER, 1_000_000, 1, None)

    def test_insert_trait_listing(self, conn):
        market_store.init_db(conn)
        market_store.upsert_listing(conn, _trait_listing())
        row = conn.execute(
            "SELECT nft_id, kind, slot, value, amount_drops FROM market_listings WHERE offer_index=?",
            ("B" * 64,),
        ).fetchone()
        assert row == (TRAIT_NFT, "trait", "Hat", "Wizard Hat", 500_000)

    def test_upsert_updates_in_place(self, conn):
        market_store.init_db(conn)
        market_store.upsert_listing(conn, _character_listing(amount_drops=1_000_000))
        market_store.upsert_listing(conn, _character_listing(amount_drops=2_000_000))
        count = conn.execute("SELECT COUNT(*) FROM market_listings").fetchone()[0]
        amount = conn.execute(
            "SELECT amount_drops FROM market_listings WHERE offer_index=?", ("A" * 64,)
        ).fetchone()[0]
        assert count == 1
        assert amount == 2_000_000

    def test_rejects_unknown_kind(self, conn):
        market_store.init_db(conn)
        with pytest.raises(ValueError):
            market_store.upsert_listing(conn, _character_listing(kind="bogus"))

    def test_upsert_preserves_created_fields_when_incoming_none(self, conn):
        """A backfill re-confirmation (which has no ledger/timestamp for the
        offer's creation) must NOT wipe the listener-written created_ledger/
        created_ts — nothing ever repopulates them, and sort=newest degrades
        silently."""
        market_store.init_db(conn)
        market_store.upsert_listing(conn, _character_listing(created_ledger=100, created_ts=1000))
        market_store.upsert_listing(conn, _character_listing(created_ledger=None, created_ts=None))
        row = conn.execute(
            "SELECT created_ledger, created_ts FROM market_listings WHERE offer_index=?",
            ("A" * 64,),
        ).fetchone()
        assert row == (100, 1000)

    def test_upsert_overwrites_created_fields_when_incoming_non_null(self, conn):
        market_store.init_db(conn)
        market_store.upsert_listing(conn, _character_listing(created_ledger=100, created_ts=1000))
        market_store.upsert_listing(conn, _character_listing(created_ledger=200, created_ts=2000))
        row = conn.execute(
            "SELECT created_ledger, created_ts FROM market_listings WHERE offer_index=?",
            ("A" * 64,),
        ).fetchone()
        assert row == (200, 2000)


class TestRecordListingCreation:
    """The service *finalize* write (list status handler + trait-sell wizard).
    Unlike upsert_listing (listener/backfill fresh on-ledger truth, full
    overwrite), this creates a row live if absent but must NEVER touch an
    existing row's lifecycle (is_live/closed_reason/settled) — a slow finalize
    poll can land AFTER the listener already closed the row sold/settled=0."""

    def test_inserts_live_row_when_absent(self, conn):
        market_store.init_db(conn)
        market_store.record_listing_creation(conn, _character_listing())
        row = conn.execute(
            "SELECT nft_id, kind, seller, amount_drops, is_live, closed_reason, settled "
            "FROM market_listings WHERE offer_index=?",
            ("A" * 64,),
        ).fetchone()
        assert row == (CHAR_NFT, "character", SELLER, 1_000_000, 1, None, None)

    def test_does_not_resurrect_a_closed_sold_row(self, conn):
        """Finalize-after-listener-sold: the row must stay is_live=0,
        closed_reason='sold', settled=0 (the sweep predicate) — not flip back
        to live with NULL lifecycle (phantom listing + lost settlement)."""
        market_store.init_db(conn)
        market_store.upsert_listing(conn, _trait_listing())
        market_store.close_listing(conn, "B" * 64, "sold")  # settled -> 0
        market_store.record_listing_creation(conn, _trait_listing())
        row = conn.execute(
            "SELECT is_live, closed_reason, settled FROM market_listings WHERE offer_index=?",
            ("B" * 64,),
        ).fetchone()
        assert row == (0, "sold", 0)

    def test_preserves_listener_created_fields_on_conflict(self, conn):
        market_store.init_db(conn)
        market_store.upsert_listing(conn, _character_listing(created_ledger=555, created_ts=9999))
        market_store.record_listing_creation(
            conn, _character_listing(created_ledger=None, created_ts=None)
        )
        count = conn.execute("SELECT COUNT(*) FROM market_listings").fetchone()[0]
        row = conn.execute(
            "SELECT created_ledger, created_ts FROM market_listings WHERE offer_index=?",
            ("A" * 64,),
        ).fetchone()
        assert count == 1
        assert row == (555, 9999)

    def test_rejects_unknown_kind(self, conn):
        market_store.init_db(conn)
        with pytest.raises(ValueError):
            market_store.record_listing_creation(conn, _character_listing(kind="bogus"))


class TestCloseListing:
    def test_close_character_sold_leaves_settled_null(self, conn):
        market_store.init_db(conn)
        market_store.upsert_listing(conn, _character_listing())
        market_store.close_listing(conn, "A" * 64, "sold")
        row = conn.execute(
            "SELECT is_live, closed_reason, settled FROM market_listings WHERE offer_index=?",
            ("A" * 64,),
        ).fetchone()
        assert row == (0, "sold", None)

    def test_close_trait_sold_auto_sets_settled_zero(self, conn):
        market_store.init_db(conn)
        market_store.upsert_listing(conn, _trait_listing())
        market_store.close_listing(conn, "B" * 64, "sold")
        row = conn.execute(
            "SELECT is_live, closed_reason, settled FROM market_listings WHERE offer_index=?",
            ("B" * 64,),
        ).fetchone()
        assert row == (0, "sold", 0)

    def test_close_trait_cancelled_does_not_set_settled(self, conn):
        market_store.init_db(conn)
        market_store.upsert_listing(conn, _trait_listing())
        market_store.close_listing(conn, "B" * 64, "cancelled")
        row = conn.execute(
            "SELECT is_live, closed_reason, settled FROM market_listings WHERE offer_index=?",
            ("B" * 64,),
        ).fetchone()
        assert row == (0, "cancelled", None)

    def test_rejects_unknown_reason(self, conn):
        market_store.init_db(conn)
        market_store.upsert_listing(conn, _character_listing())
        with pytest.raises(ValueError):
            market_store.close_listing(conn, "A" * 64, "bogus")

    def test_close_sold_persists_buyer(self, conn):
        market_store.init_db(conn)
        market_store.upsert_listing(conn, _trait_listing())
        market_store.close_listing(conn, "B" * 64, "sold", buyer=BUYER)
        row = conn.execute(
            "SELECT settled, buyer FROM market_listings WHERE offer_index=?", ("B" * 64,)
        ).fetchone()
        assert row == (0, BUYER)

    def test_close_without_buyer_preserves_prior_buyer(self, conn):
        # COALESCE keeps a previously recorded buyer if a later close passes None.
        market_store.init_db(conn)
        market_store.upsert_listing(conn, _trait_listing())
        market_store.close_listing(conn, "B" * 64, "sold", buyer=BUYER)
        market_store.close_listing(conn, "B" * 64, "stale")  # no buyer arg
        row = conn.execute(
            "SELECT buyer FROM market_listings WHERE offer_index=?", ("B" * 64,)
        ).fetchone()
        assert row[0] == BUYER


class TestBuyerMigration:
    def test_init_db_adds_buyer_column_to_legacy_db(self, conn):
        # A pre-existing DB created before `buyer` shipped: build the table
        # WITHOUT the column, then init_db must ALTER it in (not just rely on
        # CREATE TABLE IF NOT EXISTS, which no-ops on an existing table).
        conn.executescript(
            """
            CREATE TABLE market_listings (
                offer_index TEXT PRIMARY KEY, nft_id TEXT NOT NULL, kind TEXT NOT NULL,
                seller TEXT NOT NULL, amount_drops INTEGER NOT NULL, destination TEXT,
                slot TEXT, value TEXT, created_ledger INTEGER, created_ts INTEGER,
                is_live INTEGER NOT NULL DEFAULT 1, closed_reason TEXT, settled INTEGER
            );
            """
        )
        conn.commit()
        cols_before = {r[1] for r in conn.execute("PRAGMA table_info(market_listings)")}
        assert "buyer" not in cols_before
        market_store.init_db(conn)
        cols_after = {r[1] for r in conn.execute("PRAGMA table_info(market_listings)")}
        assert "buyer" in cols_after
        # Idempotent: a second init_db must not raise (column already present).
        market_store.init_db(conn)


class TestMarkSettled:
    def test_marks_settled(self, conn):
        market_store.init_db(conn)
        market_store.upsert_listing(conn, _trait_listing())
        market_store.close_listing(conn, "B" * 64, "sold")
        market_store.mark_settled(conn, "B" * 64)
        settled = conn.execute(
            "SELECT settled FROM market_listings WHERE offer_index=?", ("B" * 64,)
        ).fetchone()[0]
        assert settled == 1

    def test_returns_true_when_a_trait_row_was_settled(self, conn):
        market_store.init_db(conn)
        market_store.upsert_listing(conn, _trait_listing())
        market_store.close_listing(conn, "B" * 64, "sold")
        assert market_store.mark_settled(conn, "B" * 64) is True

    def test_nonexistent_offer_index_returns_false(self, conn):
        # Settling a row that was never indexed must be a safe, explicit
        # no-op — False, not a silent success (#130 rowcount guard).
        market_store.init_db(conn)
        assert market_store.mark_settled(conn, "F" * 64) is False

    def test_character_row_is_not_settled_and_returns_false(self, conn):
        # `settled` is a trait-only lifecycle (NULL for characters, per the
        # spec) — mark_settled must never flip a character row (#130 kind
        # guard).
        market_store.init_db(conn)
        _seed_character(conn)
        market_store.upsert_listing(conn, _character_listing())
        market_store.close_listing(conn, "A" * 64, "sold")
        assert market_store.mark_settled(conn, "A" * 64) is False
        settled = conn.execute(
            "SELECT settled FROM market_listings WHERE offer_index=?", ("A" * 64,)
        ).fetchone()[0]
        assert settled is None


class TestUnsettledTraitSales:
    def test_only_sold_and_unsettled_trait_rows(self, conn):
        market_store.init_db(conn)
        # sold + unsettled -> included
        market_store.upsert_listing(conn, _trait_listing(offer_index="1" * 64, nft_id=TRAIT_NFT))
        market_store.close_listing(conn, "1" * 64, "sold")
        # sold + settled -> excluded
        market_store.upsert_listing(conn, _trait_listing(offer_index="2" * 64, nft_id=TRAIT_NFT))
        market_store.close_listing(conn, "2" * 64, "sold")
        market_store.mark_settled(conn, "2" * 64)
        # cancelled -> excluded
        market_store.upsert_listing(conn, _trait_listing(offer_index="3" * 64, nft_id=TRAIT_NFT))
        market_store.close_listing(conn, "3" * 64, "cancelled")
        # character kind, sold -> excluded (not a trait)
        market_store.upsert_listing(conn, _character_listing(offer_index="4" * 64))
        market_store.close_listing(conn, "4" * 64, "sold")

        rows = market_store.unsettled_trait_sales(conn)
        indexes = {r["offer_index"] for r in rows}
        assert indexes == {"1" * 64}


class TestLiveListingForNft:
    """Task 8's list-start dedup check: is there already a live listing for
    this nft_id? (Distinct from get_listing below, which is keyed on
    offer_index for cancel/buy.)"""

    def test_returns_row_when_live_listing_exists(self, conn):
        market_store.init_db(conn)
        market_store.upsert_listing(conn, _character_listing())
        row = market_store.live_listing_for_nft(conn, CHAR_NFT)
        assert row is not None
        assert row["offer_index"] == "A" * 64

    def test_none_when_no_listing_at_all(self, conn):
        market_store.init_db(conn)
        assert market_store.live_listing_for_nft(conn, CHAR_NFT) is None

    def test_none_when_listing_closed(self, conn):
        market_store.init_db(conn)
        market_store.upsert_listing(conn, _character_listing())
        market_store.close_listing(conn, "A" * 64, "cancelled")
        assert market_store.live_listing_for_nft(conn, CHAR_NFT) is None


class TestGetListing:
    """Task 8's cancel/buy lookup: fetch a listing by offer_index regardless
    of liveness, so callers can tell 'never existed' (None) from 'existed but
    is now dead' (row with is_live=0) apart."""

    def test_returns_live_row(self, conn):
        market_store.init_db(conn)
        market_store.upsert_listing(conn, _character_listing())
        row = market_store.get_listing(conn, "A" * 64)
        assert row is not None
        assert row["is_live"] == 1
        assert row["seller"] == SELLER

    def test_returns_dead_row(self, conn):
        market_store.init_db(conn)
        market_store.upsert_listing(conn, _character_listing())
        market_store.close_listing(conn, "A" * 64, "cancelled")
        row = market_store.get_listing(conn, "A" * 64)
        assert row is not None
        assert row["is_live"] == 0
        assert row["closed_reason"] == "cancelled"

    def test_none_when_unknown(self, conn):
        market_store.init_db(conn)
        assert market_store.get_listing(conn, "F" * 64) is None


class TestBrowseCharacters:
    def test_returns_live_listing_with_matching_owner(self, conn):
        market_store.init_db(conn)
        _seed_character(conn)
        market_store.upsert_listing(conn, _character_listing())
        rows = market_store.browse(conn, kind="character")
        assert len(rows) == 1
        assert rows[0]["nft_id"] == CHAR_NFT
        assert rows[0]["nft_number"] == 42
        assert json.loads(rows[0]["attributes_json"])[0]["trait_type"] == "Hat"

    def test_hidden_when_seller_no_longer_owns(self, conn):
        market_store.init_db(conn)
        _seed_character(conn, owner=OTHER)
        market_store.upsert_listing(conn, _character_listing(seller=SELLER))
        rows = market_store.browse(conn, kind="character")
        assert rows == []

    def test_hidden_when_burned(self, conn):
        market_store.init_db(conn)
        _seed_character(conn, is_burned=True)
        market_store.upsert_listing(conn, _character_listing())
        rows = market_store.browse(conn, kind="character")
        assert rows == []

    def test_hidden_when_not_live(self, conn):
        market_store.init_db(conn)
        _seed_character(conn)
        market_store.upsert_listing(conn, _character_listing())
        market_store.close_listing(conn, "A" * 64, "cancelled")
        rows = market_store.browse(conn, kind="character")
        assert rows == []

    def test_hidden_when_destination_set(self, conn):
        market_store.init_db(conn)
        _seed_character(conn)
        market_store.upsert_listing(conn, _character_listing(destination=OTHER))
        rows = market_store.browse(conn, kind="character")
        assert rows == []

    def test_kind_filter_excludes_traits(self, conn):
        market_store.init_db(conn)
        _seed_character(conn)
        _seed_trait_token(conn)
        market_store.upsert_listing(conn, _character_listing())
        market_store.upsert_listing(conn, _trait_listing())
        rows = market_store.browse(conn, kind="character")
        assert {r["kind"] for r in rows} == {"character"}

    def test_attribute_filter_and_across_or_within(self, conn):
        market_store.init_db(conn)
        _seed_character(
            conn,
            nft_id="char-match",
            nft_number=1,
            attrs=[
                {"trait_type": "Hat", "value": "Wizard Hat"},
                {"trait_type": "Body", "value": "Ape"},
            ],
        )
        market_store.upsert_listing(
            conn, _character_listing(offer_index="1" * 64, nft_id="char-match")
        )
        _seed_character(
            conn,
            nft_id="char-miss",
            nft_number=2,
            attrs=[
                {"trait_type": "Hat", "value": "Party Hat"},
                {"trait_type": "Body", "value": "Ape"},
            ],
        )
        market_store.upsert_listing(
            conn, _character_listing(offer_index="2" * 64, nft_id="char-miss")
        )

        rows = market_store.browse(
            conn,
            kind="character",
            trait_filters={"Hat": ["Wizard Hat", "Crown"], "Body": ["Ape"]},
        )
        assert [r["nft_number"] for r in rows] == [1]


class TestBrowseTraits:
    def test_returns_live_listing_with_matching_owner(self, conn):
        market_store.init_db(conn)
        _seed_trait_token(conn)
        market_store.upsert_listing(conn, _trait_listing())
        rows = market_store.browse(conn, kind="trait")
        assert len(rows) == 1
        assert rows[0]["slot"] == "Hat"
        assert rows[0]["value"] == "Wizard Hat"

    def test_hidden_when_seller_no_longer_owns(self, conn):
        market_store.init_db(conn)
        _seed_trait_token(conn, owner=OTHER)
        market_store.upsert_listing(conn, _trait_listing(seller=SELLER))
        rows = market_store.browse(conn, kind="trait")
        assert rows == []

    def test_slot_value_equality_filter(self, conn):
        market_store.init_db(conn)
        _seed_trait_token(conn, nft_id="trait-a", slot="Hat", value="Wizard Hat")
        market_store.upsert_listing(
            conn,
            _trait_listing(offer_index="1" * 64, nft_id="trait-a", slot="Hat", value="Wizard Hat"),
        )
        _seed_trait_token(conn, nft_id="trait-b", slot="Hat", value="Party Hat")
        market_store.upsert_listing(
            conn,
            _trait_listing(offer_index="2" * 64, nft_id="trait-b", slot="Hat", value="Party Hat"),
        )

        rows = market_store.browse(conn, kind="trait", trait_filters={"Hat": ["Wizard Hat"]})
        assert [r["nft_id"] for r in rows] == ["trait-a"]


class TestBrowseAmountAndSortAndPaging:
    def _seed_three(self, conn):
        _seed_character(conn, nft_id="c1", nft_number=1)
        _seed_character(conn, nft_id="c2", nft_number=2)
        _seed_character(conn, nft_id="c3", nft_number=3)
        market_store.upsert_listing(
            conn,
            _character_listing(
                offer_index="1" * 64, nft_id="c1", amount_drops=3_000_000, created_ts=300
            ),
        )
        market_store.upsert_listing(
            conn,
            _character_listing(
                offer_index="2" * 64, nft_id="c2", amount_drops=1_000_000, created_ts=100
            ),
        )
        market_store.upsert_listing(
            conn,
            _character_listing(
                offer_index="3" * 64, nft_id="c3", amount_drops=2_000_000, created_ts=200
            ),
        )

    def test_min_max_amount(self, conn):
        market_store.init_db(conn)
        self._seed_three(conn)
        rows = market_store.browse(
            conn, kind="character", min_amount_drops=1_500_000, max_amount_drops=2_500_000
        )
        assert [r["nft_number"] for r in rows] == [3]

    def test_sort_price_asc(self, conn):
        market_store.init_db(conn)
        self._seed_three(conn)
        rows = market_store.browse(conn, kind="character", sort="price_asc")
        assert [r["nft_number"] for r in rows] == [2, 3, 1]

    def test_sort_price_desc(self, conn):
        market_store.init_db(conn)
        self._seed_three(conn)
        rows = market_store.browse(conn, kind="character", sort="price_desc")
        assert [r["nft_number"] for r in rows] == [1, 3, 2]

    def test_sort_newest(self, conn):
        market_store.init_db(conn)
        self._seed_three(conn)
        rows = market_store.browse(conn, kind="character", sort="newest")
        assert [r["nft_number"] for r in rows] == [1, 3, 2]

    def test_limit_offset(self, conn):
        market_store.init_db(conn)
        self._seed_three(conn)
        rows = market_store.browse(conn, kind="character", sort="price_asc", limit=1, offset=1)
        assert [r["nft_number"] for r in rows] == [3]

    def test_rejects_unknown_sort(self, conn):
        market_store.init_db(conn)
        self._seed_three(conn)
        with pytest.raises(ValueError):
            market_store.browse(conn, kind="character", sort="bogus")

    def test_rejects_unknown_kind(self, conn):
        market_store.init_db(conn)
        with pytest.raises(ValueError):
            market_store.browse(conn, kind="bogus")

    def test_rejects_negative_limit(self, conn):
        # A negative limit would silently return a nonsense Python-slice page
        # (rows[offset:offset-1]) rather than error — reject it explicitly,
        # matching the ValueError style of the kind/sort guards (#130).
        market_store.init_db(conn)
        self._seed_three(conn)
        with pytest.raises(ValueError):
            market_store.browse(conn, kind="character", limit=-1)

    def test_rejects_negative_offset(self, conn):
        # A negative offset would wrap around and page from the END of the
        # result set — reject it explicitly (#130).
        market_store.init_db(conn)
        self._seed_three(conn)
        with pytest.raises(ValueError):
            market_store.browse(conn, kind="character", offset=-1)
