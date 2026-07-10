# tests/test_market_flow.py
# Task 8: the pure(ish) List/Cancel/Buy session state machines that back
# lfg_service/app.py's /api/market/{list,cancel,buy} handlers. Unit-level
# coverage of the polling/finalize edge cases (bounded pending polls, expiry,
# terminal-state idempotency) that are cheapest to pin down directly against
# lfg_core.market_flow, without going through the full HTTP handler plumbing
# (that integration-level wiring is covered in tests/test_market_api.py).
#
# Env-guard preamble (verbatim from tests/test_seasons.py lines 1-18): importing
# lfg_core.config freezes its constants (e.g. IMG_PROXY_ALLOWED_BASES,
# LAYER_SOURCE) at import time; set the same defaults test_smoke.py uses so
# collection order can't strand them. (Copy the block verbatim from
# tests/test_server_identity_wiring.py — same keys/values.)
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

from lfg_core import market_flow  # noqa: E402

NFT_ID = "000800001E43B0783E006F30078A64A8628F4B1B22879C8EB1CAF8C700000019"
OFFER_INDEX = "A" * 64


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _list_session(**overrides):
    base = {
        "discord_id": "dev",
        "wallet_address": "rSELLER",
        "nft_id": NFT_ID,
        "listing_kind": "character",
        "amount_drops": 1_000_000,
    }
    base.update(overrides)
    s = market_flow.ListSession(**base)
    s.payload_uuid = "PAYLOAD-UUID"
    return s


def _status(*, signed, expired=False, txid=None, account="rBUYER"):
    async def fake(_uuid):
        return {
            "opened": True,
            "signed": signed,
            "expired": expired,
            "txid": txid,
            "account": account,
        }

    return fake


class TestAdvanceListSession:
    def test_not_signed_stays_awaiting_no_write(self):
        s = _list_session()
        row = _run(market_flow.advance_list_session(s, get_payload_status=_status(signed=False)))
        assert row is None
        assert s.state == market_flow.AWAITING_SIGNATURE

    def test_expired_before_signing_fails(self):
        s = _list_session()
        row = _run(
            market_flow.advance_list_session(
                s, get_payload_status=_status(signed=False, expired=True)
            )
        )
        assert row is None
        assert s.state == market_flow.FAILED

    def test_signed_but_no_txid_yet_stays_awaiting(self):
        s = _list_session()
        row = _run(
            market_flow.advance_list_session(s, get_payload_status=_status(signed=True, txid=None))
        )
        assert row is None
        assert s.state == market_flow.AWAITING_SIGNATURE

    def test_signed_tx_not_validated_pending_no_write(self):
        s = _list_session()

        async def fake_get_tx(_hash):
            return {"validated": False}

        row = _run(
            market_flow.advance_list_session(
                s,
                get_payload_status=_status(signed=True, txid="TXHASH"),
                get_tx=fake_get_tx,
            )
        )
        assert row is None
        assert s.state == market_flow.PENDING

    def test_tx_lookup_raises_unknown_no_crash_no_write(self):
        s = _list_session()

        async def boom(_hash):
            raise RuntimeError("rpc down")

        row = _run(
            market_flow.advance_list_session(
                s, get_payload_status=_status(signed=True, txid="TXHASH"), get_tx=boom
            )
        )
        assert row is None
        assert s.state == market_flow.UNKNOWN

    def test_ten_pending_polls_flips_unknown(self):
        s = _list_session()

        async def fake_get_tx(_hash):
            return {"validated": False}

        for _ in range(market_flow.MAX_FINALIZE_POLLS):
            row = _run(
                market_flow.advance_list_session(
                    s, get_payload_status=_status(signed=True, txid="TXHASH"), get_tx=fake_get_tx
                )
            )
            assert row is None
        assert s.state == market_flow.UNKNOWN

    def test_validated_tes_success_extracts_offer_and_writes_row(self):
        s = _list_session()
        meta = {
            "TransactionResult": "tesSUCCESS",
            "AffectedNodes": [
                {
                    "CreatedNode": {
                        "LedgerEntryType": "NFTokenOffer",
                        "LedgerIndex": OFFER_INDEX,
                        "NewFields": {
                            "NFTokenID": NFT_ID,
                            "Amount": "1000000",
                            "Flags": 1,
                        },
                    }
                }
            ],
        }

        async def fake_get_tx(_hash):
            return {"validated": True, "meta": meta}

        row = _run(
            market_flow.advance_list_session(
                s, get_payload_status=_status(signed=True, txid="TXHASH"), get_tx=fake_get_tx
            )
        )
        assert row is not None
        assert row["offer_index"] == OFFER_INDEX
        assert row["nft_id"] == NFT_ID
        assert row["kind"] == "character"
        assert row["seller"] == "rSELLER"
        assert row["amount_drops"] == 1_000_000
        assert s.state == market_flow.DONE
        assert s.offer_index == OFFER_INDEX

    def test_finalize_row_records_on_ledger_amount_not_session_amount(self):
        """The written amount_drops must be the on-ledger truth extracted from
        the CreatedNode, not session.amount_drops (which could drift if the
        signed offer differs from the requested price)."""
        s = _list_session()  # session amount_drops = 1_000_000
        meta = {
            "TransactionResult": "tesSUCCESS",
            "AffectedNodes": [
                {
                    "CreatedNode": {
                        "LedgerEntryType": "NFTokenOffer",
                        "LedgerIndex": OFFER_INDEX,
                        "NewFields": {
                            "NFTokenID": NFT_ID,
                            "Amount": "7777777",
                            "Flags": 1,
                        },
                    }
                }
            ],
        }

        async def fake_get_tx(_hash):
            return {"validated": True, "meta": meta}

        row = _run(
            market_flow.advance_list_session(
                s, get_payload_status=_status(signed=True, txid="TXHASH"), get_tx=fake_get_tx
            )
        )
        assert row["amount_drops"] == 7_777_777

    def test_validated_failure_result_fails_session_no_write(self):
        s = _list_session()

        async def fake_get_tx(_hash):
            return {"validated": True, "meta": {"TransactionResult": "tecNO_PERMISSION"}}

        row = _run(
            market_flow.advance_list_session(
                s, get_payload_status=_status(signed=True, txid="TXHASH"), get_tx=fake_get_tx
            )
        )
        assert row is None
        assert s.state == market_flow.FAILED

    def test_terminal_state_never_re_polls(self):
        s = _list_session()
        s.state = market_flow.DONE

        async def boom(_uuid):
            raise AssertionError("must not poll a terminal session")

        row = _run(market_flow.advance_list_session(s, get_payload_status=boom))
        assert row is None


class TestAdvanceCancelSession:
    def _session(self):
        s = market_flow.CancelSession(
            discord_id="dev",
            wallet_address="rSELLER",
            offer_index=OFFER_INDEX,
            network="testnet",
        )
        s.payload_uuid = "PAYLOAD-UUID"
        return s

    def test_not_signed_no_close(self):
        s = self._session()
        should_close = _run(
            market_flow.advance_cancel_session(s, get_payload_status=_status(signed=False))
        )
        assert should_close is False
        assert s.state == market_flow.AWAITING_SIGNATURE

    def test_expired_fails(self):
        s = self._session()
        should_close = _run(
            market_flow.advance_cancel_session(
                s, get_payload_status=_status(signed=False, expired=True)
            )
        )
        assert should_close is False
        assert s.state == market_flow.FAILED

    def test_signed_closes_exactly_once(self):
        s = self._session()
        first = _run(market_flow.advance_cancel_session(s, get_payload_status=_status(signed=True)))
        assert first is True
        assert s.state == market_flow.DONE

        async def boom(_uuid):
            raise AssertionError("must not re-poll a terminal session")

        second = _run(market_flow.advance_cancel_session(s, get_payload_status=boom))
        assert second is False


class TestAdvanceBuySession:
    def _session(self, **overrides):
        base = {
            "discord_id": "dev",
            "wallet_address": "rBUYER",
            "offer_index": OFFER_INDEX,
            "nft_id": NFT_ID,
            "listing_kind": "character",
            "network": "testnet",
            "amount_drops": 1_000_000,
        }
        base.update(overrides)
        s = market_flow.BuySession(**base)
        s.payload_uuid = "PAYLOAD-UUID"
        return s

    def test_confirmed_purchase_returns_sold(self):
        s = self._session()

        async def fake_get_tx(_hash):
            return {"validated": True, "meta": {"TransactionResult": "tesSUCCESS"}}

        outcome = _run(
            market_flow.advance_buy_session(
                s, get_payload_status=_status(signed=True, txid="TXHASH"), get_tx=fake_get_tx
            )
        )
        assert outcome == "sold"
        assert s.state == market_flow.DONE

    def test_ledger_race_failure_returns_stale_and_maps_reason(self):
        s = self._session()

        async def fake_get_tx(_hash):
            return {"validated": True, "meta": {"TransactionResult": "tecOBJECT_NOT_FOUND"}}

        outcome = _run(
            market_flow.advance_buy_session(
                s, get_payload_status=_status(signed=True, txid="TXHASH"), get_tx=fake_get_tx
            )
        )
        assert outcome == "stale"
        assert s.state == market_flow.FAILED
        assert s.reason == "listing_unavailable"
        d = s.to_dict()
        assert d["state"] == "failed"
        assert d["reason"] == "listing_unavailable"

    def test_tx_lookup_raises_unknown_no_crash(self):
        s = self._session()

        async def boom(_hash):
            raise RuntimeError("rpc down")

        outcome = _run(
            market_flow.advance_buy_session(
                s, get_payload_status=_status(signed=True, txid="TXHASH"), get_tx=boom
            )
        )
        assert outcome is None
        # Restored (#130): the session must land in UNKNOWN (self-heals via
        # the listener/backfill later), not stay pollable forever.
        assert s.state == market_flow.UNKNOWN

    def test_insufficient_funds_fails_session_but_leaves_listing_live(self):
        """A buyer-side failure (tecINSUFFICIENT_FUNDS) means the OFFER is
        still healthy — only offer-consumed/absent codes may stale-close it.
        Returning None (not 'stale') keeps the caller from delisting a live
        listing (griefing lever)."""
        s = self._session()

        async def fake_get_tx(_hash):
            return {"validated": True, "meta": {"TransactionResult": "tecINSUFFICIENT_FUNDS"}}

        outcome = _run(
            market_flow.advance_buy_session(
                s, get_payload_status=_status(signed=True, txid="TXHASH"), get_tx=fake_get_tx
            )
        )
        assert outcome is None  # NOT "stale" — the row must stay live
        assert s.state == market_flow.FAILED
        assert s.reason != "listing_unavailable"

    def test_signer_mismatch_fails_session_without_touching_listing(self):
        """The QR is signed by a wallet other than the buyer who started the
        session: fail the session (no 'sold', no tx lookup, no close) so the
        service never settles a trait via the wrong owner (Greptile #129)."""
        s = self._session()  # wallet_address="rBUYER"

        async def must_not_lookup(_hash):
            raise AssertionError("must not fetch tx for a mismatched signer")

        outcome = _run(
            market_flow.advance_buy_session(
                s,
                get_payload_status=_status(signed=True, txid="TXHASH", account="rATTACKER"),
                get_tx=must_not_lookup,
            )
        )
        assert outcome is None
        assert s.state == market_flow.FAILED
        assert s.reason == "signer_mismatch"
        assert s.txid is None  # never advanced to PENDING

    def test_missing_signer_account_fails_closed(self):
        s = self._session()

        async def must_not_lookup(_hash):
            raise AssertionError("must not fetch tx when signer is unverifiable")

        outcome = _run(
            market_flow.advance_buy_session(
                s,
                get_payload_status=_status(signed=True, txid="TXHASH", account=None),
                get_tx=must_not_lookup,
            )
        )
        assert outcome is None
        assert s.state == market_flow.FAILED
        assert s.reason == "signer_mismatch"

    def test_self_accept_own_offer_does_not_stale_close(self):
        s = self._session()

        async def fake_get_tx(_hash):
            return {"validated": True, "meta": {"TransactionResult": "tecCANT_ACCEPT_OWN_OFFER"}}

        outcome = _run(
            market_flow.advance_buy_session(
                s, get_payload_status=_status(signed=True, txid="TXHASH"), get_tx=fake_get_tx
            )
        )
        assert outcome is None
        assert s.state == market_flow.FAILED
