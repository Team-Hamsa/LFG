# tests/test_market_bids.py
# #283: native buy offers (bids) — extraction/verification (market_ops), the
# buy_offers store (market_store), listener indexing (nft_listener), and the
# Bid/BidAccept session state machines (market_flow).
#
# Env-guard preamble: importing lfg_core.config freezes its constants (e.g.
# IMG_PROXY_ALLOWED_BASES, LAYER_SOURCE) at import time; set the same defaults
# test_smoke.py uses so collection order can't strand them.
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

import asyncio  # noqa: E402
import sqlite3  # noqa: E402

import pytest  # noqa: E402
from xrpl.core import addresscodec  # noqa: E402

from lfg_core import (  # noqa: E402  # noqa: E402
    config,
    economy_store,
    market_flow,
    market_ops,
    market_store,
    nft_index,
    nft_listener,
)

BIDDER = "rBidderAddress0000000000000000000"
OWNER = "rOwnerAddress000000000000000000000"


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.executescript(nft_index._SCHEMA)
    economy_store.init_economy_schema(c)
    market_store.init_db(c)
    market_store.init_bid_schema(c)
    return c


def _our_nft_id(seq: int = 1) -> str:
    acct = addresscodec.decode_classic_address(config.SWAP_ISSUER_ADDRESS).hex().upper()
    return f"000A0000{acct}00000000{seq:08X}"


def _seed_character(conn, nft_id, owner=OWNER):
    conn.execute(
        "INSERT INTO onchain_nfts (nft_id, nft_number, owner, is_burned, mutable, uri_hex, body) "
        "VALUES (?, 1, ?, 0, 0, '', NULL)",
        (nft_id, owner),
    )
    conn.commit()


def _buy_meta(nft_id, *, offer_index="BID1", amount="2000000", flags=0, expiration=None):
    new_fields = {"NFTokenID": nft_id, "Flags": flags, "Amount": amount, "Owner": BIDDER}
    if expiration is not None:
        new_fields["Expiration"] = expiration
    return {
        "TransactionResult": "tesSUCCESS",
        "AffectedNodes": [
            {
                "CreatedNode": {
                    "LedgerEntryType": "NFTokenOffer",
                    "LedgerIndex": offer_index,
                    "NewFields": new_fields,
                }
            }
        ],
    }


def _bid(offer_index="BID1", nft_id="NFT1", bidder=BIDDER, amount=2_000_000, **kw):
    return market_store.BuyOffer(
        offer_index=offer_index, nft_id=nft_id, bidder=bidder, amount_drops=amount, **kw
    )


# --- market_ops.extract_created_buy_offer ---


class TestExtractCreatedBuyOffer:
    def test_extracts_buy_offer(self):
        meta = _buy_meta("NFT1", expiration=999)
        out = market_ops.extract_created_buy_offer(meta, "NFT1")
        assert out == {
            "offer_index": "BID1",
            "amount_drops": 2000000,
            "owner": BIDDER,
            "destination": None,
            "expiration": 999,
            "flags": 0,
        }

    def test_rejects_sell_offer(self):
        meta = _buy_meta("NFT1", flags=market_ops.LSF_SELL_NFTOKEN)
        assert market_ops.extract_created_buy_offer(meta, "NFT1") is None

    def test_rejects_iou_amount(self):
        meta = _buy_meta("NFT1")
        meta["AffectedNodes"][0]["CreatedNode"]["NewFields"]["Amount"] = {
            "currency": "ABC",
            "issuer": "rX",
            "value": "1",
        }
        assert market_ops.extract_created_buy_offer(meta, "NFT1") is None

    def test_rejects_wrong_nft(self):
        assert market_ops.extract_created_buy_offer(_buy_meta("NFT1"), "NFT2") is None


# --- market_ops.verify_buy_offer ---


def _fetch(offers):
    async def f(_nft_id):
        return offers

    return f


def _bid_offer(**over):
    base = {
        "offer_index": "BID1",
        "amount": "2000000",
        "destination": None,
        "flags": 0,
        "owner": BIDDER,
        "expiration": None,
    }
    base.update(over)
    return base


class TestVerifyBuyOffer:
    def test_happy_path(self):
        ok = _run(
            market_ops.verify_buy_offer(
                "NFT1",
                "BID1",
                2_000_000,
                expected_bidder=BIDDER,
                fetch_offers=_fetch([_bid_offer()]),
            )
        )
        assert ok is True

    def test_amount_mismatch(self):
        ok = _run(
            market_ops.verify_buy_offer("NFT1", "BID1", 999, fetch_offers=_fetch([_bid_offer()]))
        )
        assert ok is False

    def test_bidder_mismatch(self):
        ok = _run(
            market_ops.verify_buy_offer(
                "NFT1",
                "BID1",
                2_000_000,
                expected_bidder="rSomeoneElse",
                fetch_offers=_fetch([_bid_offer()]),
            )
        )
        assert ok is False

    def test_destination_locked_rejected(self):
        ok = _run(
            market_ops.verify_buy_offer(
                "NFT1", "BID1", 2_000_000, fetch_offers=_fetch([_bid_offer(destination="rD")])
            )
        )
        assert ok is False

    def test_expired_rejected(self):
        async def now():
            return 1000

        ok = _run(
            market_ops.verify_buy_offer(
                "NFT1",
                "BID1",
                2_000_000,
                fetch_offers=_fetch([_bid_offer(expiration=1000)]),
                fetch_ledger_time=now,
            )
        )
        assert ok is False

    def test_absent_offer_false(self):
        ok = _run(market_ops.verify_buy_offer("NFT1", "BID1", 2_000_000, fetch_offers=_fetch([])))
        assert ok is False

    def test_strict_lookup_failure_raises(self):
        async def boom(_nft_id):
            raise RuntimeError("rpc down")

        with pytest.raises(RuntimeError):
            _run(
                market_ops.verify_buy_offer(
                    "NFT1", "BID1", 2_000_000, fetch_offers=boom, strict=True
                )
            )


# --- market_store buy_offers ---


class TestBidStore:
    def test_upsert_and_get(self):
        conn = _conn()
        market_store.upsert_bid(conn, _bid())
        row = market_store.get_bid(conn, "BID1")
        assert row["bidder"] == BIDDER
        assert row["amount_drops"] == 2_000_000
        assert row["is_live"] == 1

    def test_close_and_reasons(self):
        conn = _conn()
        market_store.upsert_bid(conn, _bid())
        market_store.close_bid(conn, "BID1", "accepted")
        row = market_store.get_bid(conn, "BID1")
        assert row["is_live"] == 0
        assert row["closed_reason"] == "accepted"
        with pytest.raises(ValueError):
            market_store.close_bid(conn, "BID1", "sold")

    def test_live_bids_for_nft_ordered_highest_first(self):
        conn = _conn()
        market_store.upsert_bid(conn, _bid(offer_index="B1", amount=1_000_000))
        market_store.upsert_bid(conn, _bid(offer_index="B2", amount=3_000_000))
        rows = market_store.live_bids_for_nft(conn, "NFT1")
        assert [r["offer_index"] for r in rows] == ["B2", "B1"]

    def test_live_bids_on_owner_nfts_join(self):
        conn = _conn()
        nft = _our_nft_id(1)
        _seed_character(conn, nft, owner=OWNER)
        market_store.upsert_bid(conn, _bid(offer_index="B1", nft_id=nft))
        # A bid on someone else's NFT never shows for OWNER.
        market_store.upsert_bid(conn, _bid(offer_index="B2", nft_id="OTHERNFT"))
        # OWNER's own bid on their own NFT is excluded.
        market_store.upsert_bid(conn, _bid(offer_index="B3", nft_id=nft, bidder=OWNER))
        rows = market_store.live_bids_on_owner_nfts(conn, OWNER)
        assert [r["offer_index"] for r in rows] == ["B1"]

    def test_live_bids_by_bidder(self):
        conn = _conn()
        market_store.upsert_bid(conn, _bid(offer_index="B1"))
        market_store.close_bid(conn, "B1", "cancelled")
        market_store.upsert_bid(conn, _bid(offer_index="B2"))
        rows = market_store.live_bids_by(conn, BIDDER)
        assert [r["offer_index"] for r in rows] == ["B2"]


# --- listener indexing ---


def _bid_create_tx(nft_id, *, offer_index="BID1", amount="2000000", expiration=None):
    meta = _buy_meta(nft_id, offer_index=offer_index, amount=amount, expiration=expiration)
    return {
        "TransactionType": "NFTokenCreateOffer",
        "Account": BIDDER,
        "NFTokenID": nft_id,
        "ledger_index": 555,
        "date": 800000000,
        "meta": meta,
    }


def _accept_bid_tx(nft_id, *, offer_index="BID1"):
    return {
        "TransactionType": "NFTokenAcceptOffer",
        "Account": OWNER,  # owner accepts the buy offer directly
        "meta": {
            "TransactionResult": "tesSUCCESS",
            "AffectedNodes": [
                {
                    "DeletedNode": {
                        "LedgerEntryType": "NFTokenOffer",
                        "LedgerIndex": offer_index,
                        "FinalFields": {
                            "NFTokenID": nft_id,
                            "Flags": 0,
                            "Owner": BIDDER,
                            "Amount": "2000000",
                        },
                    }
                }
            ],
        },
    }


class TestListenerBids:
    def test_offer_create_indexes_bid(self):
        conn = _conn()
        nft = _our_nft_id(1)
        _seed_character(conn, nft)
        _run(nft_listener.apply_market_tx(conn, _bid_create_tx(nft, expiration=12345)))
        row = market_store.get_bid(conn, "BID1")
        assert row is not None
        assert row["bidder"] == BIDDER
        assert row["amount_drops"] == 2_000_000
        assert row["expiration"] == 12345
        assert row["is_live"] == 1
        # And no sell listing was created for it.
        assert market_store.get_listing(conn, "BID1") is None

    def test_offer_create_ignores_bid_on_foreign_nft(self):
        conn = _conn()
        _run(nft_listener.apply_market_tx(conn, _bid_create_tx("F" * 64)))
        assert market_store.get_bid(conn, "BID1") is None

    def test_cancel_closes_bid(self):
        conn = _conn()
        nft = _our_nft_id(1)
        _seed_character(conn, nft)
        _run(nft_listener.apply_market_tx(conn, _bid_create_tx(nft)))
        cancel = {
            "TransactionType": "NFTokenCancelOffer",
            "Account": BIDDER,
            "meta": {
                "TransactionResult": "tesSUCCESS",
                "AffectedNodes": [
                    {
                        "DeletedNode": {
                            "LedgerEntryType": "NFTokenOffer",
                            "LedgerIndex": "BID1",
                            "FinalFields": {},
                        }
                    }
                ],
            },
        }
        _run(nft_listener.apply_market_tx(conn, cancel))
        row = market_store.get_bid(conn, "BID1")
        assert row["is_live"] == 0
        assert row["closed_reason"] == "cancelled"

    def test_accept_closes_bid_accepted(self):
        conn = _conn()
        nft = _our_nft_id(1)
        _seed_character(conn, nft)
        _run(nft_listener.apply_market_tx(conn, _bid_create_tx(nft)))
        _run(nft_listener.apply_market_tx(conn, _accept_bid_tx(nft)))
        row = market_store.get_bid(conn, "BID1")
        assert row["is_live"] == 0
        assert row["closed_reason"] == "accepted"


# --- market_flow sessions ---


def _status(signed, txid="TX1", expired=False):
    async def f(_uuid):
        return {"signed": signed, "txid": txid if signed else None, "expired": expired}

    return f


def _tx(validated=True, result="tesSUCCESS", meta_extra=None):
    async def f(_txid):
        meta = {"TransactionResult": result}
        if meta_extra:
            meta.update(meta_extra)
        return {"validated": validated, "meta": meta}

    return f


def _bid_session(**over):
    base = {
        "discord_id": "u1",
        "wallet_address": BIDDER,
        "nft_id": "NFT1",
        "owner": OWNER,
        "amount_drops": 2_000_000,
    }
    base.update(over)
    s = market_flow.BidSession(**base)
    s.payload_uuid = "uuid1"
    return s


class TestBidSession:
    def test_happy_path_returns_bid_row(self):
        s = _bid_session()
        meta = _buy_meta("NFT1", expiration=777)
        row = _run(
            market_flow.advance_bid_session(
                s,
                get_payload_status=_status(signed=True),
                get_tx=_tx(meta_extra={"AffectedNodes": meta["AffectedNodes"]}),
            )
        )
        assert s.state == market_flow.DONE
        assert row == {
            "offer_index": "BID1",
            "nft_id": "NFT1",
            "bidder": BIDDER,
            "amount_drops": 2000000,
            "expiration": 777,
        }

    def test_unsigned_stays_awaiting(self):
        s = _bid_session()
        row = _run(market_flow.advance_bid_session(s, get_payload_status=_status(signed=False)))
        assert row is None
        assert s.state == market_flow.AWAITING_SIGNATURE

    def test_tec_failure_fails_session(self):
        s = _bid_session()
        row = _run(
            market_flow.advance_bid_session(
                s,
                get_payload_status=_status(signed=True),
                get_tx=_tx(result="tecUNFUNDED_OFFER"),
            )
        )
        assert row is None
        assert s.state == market_flow.FAILED

    def test_missing_created_offer_fails(self):
        s = _bid_session()
        row = _run(
            market_flow.advance_bid_session(
                s,
                get_payload_status=_status(signed=True),
                get_tx=_tx(meta_extra={"AffectedNodes": []}),
            )
        )
        assert row is None
        assert s.state == market_flow.FAILED


def _accept_session(**over):
    base = {
        "discord_id": "u1",
        "wallet_address": OWNER,
        "offer_index": "BID1",
        "nft_id": "NFT1",
        "network": "testnet",
        "amount_drops": 2_000_000,
    }
    base.update(over)
    s = market_flow.BidAcceptSession(**base)
    s.payload_uuid = "uuid1"
    return s


class TestBidAcceptSession:
    def test_happy_path_returns_offer_index(self):
        s = _accept_session()
        out = _run(
            market_flow.advance_bid_accept_session(
                s, get_payload_status=_status(signed=True), get_tx=_tx()
            )
        )
        assert out == "BID1"
        assert s.state == market_flow.DONE

    def test_tec_failure_sets_bid_unavailable(self):
        s = _accept_session()
        out = _run(
            market_flow.advance_bid_accept_session(
                s,
                get_payload_status=_status(signed=True),
                get_tx=_tx(result="tecOBJECT_NOT_FOUND"),
            )
        )
        assert out is None
        assert s.state == market_flow.FAILED
        assert s.reason == "bid_unavailable"
