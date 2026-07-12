# tests/test_market_verify.py
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

import asyncio  # noqa: E402
import json  # noqa: E402
from collections.abc import Awaitable, Callable  # noqa: E402
from typing import Any  # noqa: E402

import pytest  # noqa: E402

from lfg_core import market_ops, xrpl_ops  # noqa: E402

NFT_ID = "000800001E43B0783E006F30078A64A8628F4B1B22879C8EB1CAF8C700000019"
OFFER_INDEX = "9F1C2D3E4A5B6C7D8E9F0A1B2C3D4E5F60718293A4B5C6D7E8F901234567890"


def _run(coro: Awaitable[Any]) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _FakeResponse:
    def __init__(self, result: dict[str, Any]) -> None:
        self.result = result


def _fake_json_rpc_client(result: dict[str, Any] | None = None, exc: Exception | None = None):
    class _FakeClient:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def request(self, _req: Any) -> _FakeResponse:
            if exc is not None:
                raise exc
            assert result is not None
            return _FakeResponse(result)

    return _FakeClient


class TestGetNftSellOffers:
    """Offer-index field drift guard: accept `nft_offer_index` with `index`
    fallback (mirrors Baysed market.py:386-390 — different server versions
    key the offer's ledger index differently)."""

    def test_uses_nft_offer_index_field(self, monkeypatch) -> None:
        result = {
            "offers": [
                {"nft_offer_index": OFFER_INDEX, "amount": "1000000", "owner": "rSeller"},
            ]
        }
        monkeypatch.setattr(xrpl_ops, "JsonRpcClient", _fake_json_rpc_client(result))
        offers = _run(xrpl_ops.get_nft_sell_offers(NFT_ID))
        assert offers[0]["offer_index"] == OFFER_INDEX

    def test_falls_back_to_index_field(self, monkeypatch) -> None:
        result = {
            "offers": [
                {"index": OFFER_INDEX, "amount": "2000000", "owner": "rSeller"},
            ]
        }
        monkeypatch.setattr(xrpl_ops, "JsonRpcClient", _fake_json_rpc_client(result))
        offers = _run(xrpl_ops.get_nft_sell_offers(NFT_ID))
        assert offers[0]["offer_index"] == OFFER_INDEX

    def test_surfaces_expiration_field(self, monkeypatch) -> None:
        """#183: the normalized offer must carry the raw XRPL `expiration`
        (Ripple-epoch seconds) so verify can reject an already-expired offer;
        absent Expiration surfaces as None."""
        result = {
            "offers": [
                {"nft_offer_index": OFFER_INDEX, "amount": "1000000", "expiration": 800_000_000},
                {"index": "OTHER", "amount": "2000000"},
            ]
        }
        monkeypatch.setattr(xrpl_ops, "JsonRpcClient", _fake_json_rpc_client(result))
        offers = _run(xrpl_ops.get_nft_sell_offers(NFT_ID))
        assert offers[0]["expiration"] == 800_000_000
        assert offers[1]["expiration"] is None

    def test_json_roundtrip_result_still_parses(self, monkeypatch) -> None:
        # Guard against accidental reliance on non-JSON-safe types (the RPC
        # response in production always arrives via json.loads).
        result = json.loads(
            json.dumps({"offers": [{"nft_offer_index": OFFER_INDEX, "amount": "1000000"}]})
        )
        monkeypatch.setattr(xrpl_ops, "JsonRpcClient", _fake_json_rpc_client(result))
        offers = _run(xrpl_ops.get_nft_sell_offers(NFT_ID))
        assert offers[0]["offer_index"] == OFFER_INDEX

    def test_returns_empty_list_on_rpc_exception(self, monkeypatch) -> None:
        monkeypatch.setattr(
            xrpl_ops, "JsonRpcClient", _fake_json_rpc_client(exc=RuntimeError("rpc down"))
        )
        offers = _run(xrpl_ops.get_nft_sell_offers(NFT_ID))
        assert offers == []

    def test_returns_empty_list_when_no_offers_key(self, monkeypatch) -> None:
        monkeypatch.setattr(xrpl_ops, "JsonRpcClient", _fake_json_rpc_client({}))
        offers = _run(xrpl_ops.get_nft_sell_offers(NFT_ID))
        assert offers == []

    def test_raise_on_error_reraises_rpc_exception(self, monkeypatch) -> None:
        """The backfill's failure-distinguishing path: raise_on_error=True must
        re-raise instead of collapsing an RPC blip into "no offers" — a
        swallowed failure would let the stale-close pass close a real live
        listing."""
        monkeypatch.setattr(
            xrpl_ops, "JsonRpcClient", _fake_json_rpc_client(exc=RuntimeError("rpc down"))
        )
        with pytest.raises(RuntimeError):
            _run(xrpl_ops.get_nft_sell_offers(NFT_ID, raise_on_error=True))

    def test_raise_on_error_no_offers_still_returns_empty(self, monkeypatch) -> None:
        """A genuinely-empty (or objectNotFound-shaped) response is NOT a
        failure — it must still return [] even in strict mode."""
        monkeypatch.setattr(xrpl_ops, "JsonRpcClient", _fake_json_rpc_client({}))
        offers = _run(xrpl_ops.get_nft_sell_offers(NFT_ID, raise_on_error=True))
        assert offers == []

    def test_strict_object_not_found_result_returns_empty(self, monkeypatch) -> None:
        """objectNotFound is the ONLY unsuccessful RESULT that legitimately
        means "no offers" — in strict mode it must still return [], not raise."""
        result = {"error": "objectNotFound", "status": "error"}
        monkeypatch.setattr(xrpl_ops, "JsonRpcClient", _fake_json_rpc_client(result))
        offers = _run(xrpl_ops.get_nft_sell_offers(NFT_ID, raise_on_error=True))
        assert offers == []

    def test_strict_soft_error_result_raises(self, monkeypatch) -> None:
        """A rippled soft-error RESULT (e.g. tooBusy) is NOT "no offers" — in
        strict mode it must raise, not collapse to [] (which the stale-close /
        buy-verify paths would misread as "offer absent" and close a live
        listing)."""
        result = {"error": "tooBusy", "status": "error"}
        monkeypatch.setattr(xrpl_ops, "JsonRpcClient", _fake_json_rpc_client(result))
        with pytest.raises(RuntimeError):
            _run(xrpl_ops.get_nft_sell_offers(NFT_ID, raise_on_error=True))

    def test_non_strict_soft_error_result_returns_empty(self, monkeypatch) -> None:
        """Non-strict callers are unchanged: any unsuccessful result -> []."""
        result = {"error": "tooBusy", "status": "error"}
        monkeypatch.setattr(xrpl_ops, "JsonRpcClient", _fake_json_rpc_client(result))
        offers = _run(xrpl_ops.get_nft_sell_offers(NFT_ID))
        assert offers == []


class TestGetTx:
    """Task 8's list/buy finalize poller: fetch a transaction by hash via the
    plain (non-clio) `tx` method. Returns the raw result dict verbatim —
    including the not-yet-known-to-the-server shape ({"error": "txnNotFound"},
    no "validated"/"meta" keys) — so callers checking `result.get("validated")`
    treat "not found yet" the same as "found but not validated" without any
    special-casing here. Only genuine RPC/network failures raise, so
    fail-closed callers can tell those apart from "still pending"."""

    def test_returns_raw_result_dict(self, monkeypatch) -> None:
        result = {
            "validated": True,
            "meta": {"TransactionResult": "tesSUCCESS", "AffectedNodes": []},
            "hash": "ABCDEF",
        }
        monkeypatch.setattr(xrpl_ops, "JsonRpcClient", _fake_json_rpc_client(result))
        tx = _run(xrpl_ops.get_tx("ABCDEF"))
        assert tx == result

    def test_not_found_shape_has_no_validated_key(self, monkeypatch) -> None:
        # rippled's txnNotFound response carries no validated/meta keys — the
        # caller's `tx.get("validated")` check must treat this the same as
        # "not yet validated" without this function raising or special-casing it.
        result = {"error": "txnNotFound", "error_code": 29, "status": "error"}
        monkeypatch.setattr(xrpl_ops, "JsonRpcClient", _fake_json_rpc_client(result))
        tx = _run(xrpl_ops.get_tx("UNKNOWNHASH"))
        assert tx.get("validated") is None

    def test_raises_on_rpc_exception(self, monkeypatch) -> None:
        monkeypatch.setattr(
            xrpl_ops, "JsonRpcClient", _fake_json_rpc_client(exc=RuntimeError("rpc down"))
        )
        with pytest.raises(RuntimeError):
            _run(xrpl_ops.get_tx("ABCDEF"))


def _offers_fetcher(
    offers: list[dict[str, Any]],
) -> Callable[[str], Awaitable[list[dict[str, Any]]]]:
    async def fetch(_nft_id: str) -> list[dict[str, Any]]:
        return offers

    return fetch


def _ledger_time(ts: int) -> Callable[[], Awaitable[int]]:
    async def fetch() -> int:
        return ts

    return fetch


class TestVerifySellOffer:
    """Fail-closed matrix: True ONLY when present + amount matches exactly +
    destination is None. Every other cell — RPC exception, absent offer,
    amount mismatch (including a dict/IOU Amount), foreign destination —
    is False. One assert per matrix cell (per task brief)."""

    def test_true_when_present_amount_matches_no_destination(self) -> None:
        offers = [{"offer_index": OFFER_INDEX, "amount": "5000000", "destination": None}]
        result = _run(
            market_ops.verify_sell_offer(
                NFT_ID, OFFER_INDEX, 5_000_000, fetch_offers=_offers_fetcher(offers)
            )
        )
        assert result is True

    def test_false_on_rpc_exception(self) -> None:
        async def fetch(_nft_id: str) -> list[dict[str, Any]]:
            raise RuntimeError("rpc down")

        result = _run(
            market_ops.verify_sell_offer(NFT_ID, OFFER_INDEX, 5_000_000, fetch_offers=fetch)
        )
        assert result is False

    def test_false_when_offer_absent(self) -> None:
        offers = [{"offer_index": "SOME_OTHER_OFFER", "amount": "5000000", "destination": None}]
        result = _run(
            market_ops.verify_sell_offer(
                NFT_ID, OFFER_INDEX, 5_000_000, fetch_offers=_offers_fetcher(offers)
            )
        )
        assert result is False

    def test_false_on_amount_mismatch(self) -> None:
        offers = [{"offer_index": OFFER_INDEX, "amount": "4999999", "destination": None}]
        result = _run(
            market_ops.verify_sell_offer(
                NFT_ID, OFFER_INDEX, 5_000_000, fetch_offers=_offers_fetcher(offers)
            )
        )
        assert result is False

    def test_false_on_iou_amount_dict(self) -> None:
        offers = [
            {
                "offer_index": OFFER_INDEX,
                "amount": {
                    "currency": "BRIX",
                    "issuer": "rIssuerXXXXXXXXXXXXXXXXXXXXXXXXX",
                    "value": "5",
                },
                "destination": None,
            }
        ]
        result = _run(
            market_ops.verify_sell_offer(
                NFT_ID, OFFER_INDEX, 5_000_000, fetch_offers=_offers_fetcher(offers)
            )
        )
        assert result is False

    def test_false_on_foreign_destination(self) -> None:
        offers = [
            {
                "offer_index": OFFER_INDEX,
                "amount": "5000000",
                "destination": "rSomeoneElseXXXXXXXXXXXXXXXXXXXX",
            }
        ]
        result = _run(
            market_ops.verify_sell_offer(
                NFT_ID, OFFER_INDEX, 5_000_000, fetch_offers=_offers_fetcher(offers)
            )
        )
        assert result is False

    def test_strict_reraises_on_lookup_failure(self) -> None:
        """strict=True must distinguish a lookup FAILURE from a verified
        absence: the exception propagates so the buy-start handler can respond
        503 without stale-closing a healthy listing (fix #3)."""

        async def fetch(_nft_id: str) -> list[dict[str, Any]]:
            raise RuntimeError("rpc down")

        with pytest.raises(RuntimeError):
            _run(
                market_ops.verify_sell_offer(
                    NFT_ID, OFFER_INDEX, 5_000_000, fetch_offers=fetch, strict=True
                )
            )

    def test_default_fetch_offers_path_true_end_to_end(self, monkeypatch) -> None:
        """fetch_offers=None (the production default) must route through
        xrpl_ops.get_nft_sell_offers end-to-end — every other test here
        injects a fetcher, so this wiring was previously uncovered (#130)."""
        result = {
            "offers": [
                {"nft_offer_index": OFFER_INDEX, "amount": "5000000", "owner": "rSeller"},
            ]
        }
        monkeypatch.setattr(xrpl_ops, "JsonRpcClient", _fake_json_rpc_client(result))
        assert _run(market_ops.verify_sell_offer(NFT_ID, OFFER_INDEX, 5_000_000)) is True

    def test_default_fetch_offers_path_nonstrict_rpc_failure_is_false(self, monkeypatch) -> None:
        """Default path, non-strict: an RPC failure collapses to [] inside
        get_nft_sell_offers (raise_on_error=False is threaded from
        strict=False), so verify fails closed to False."""
        monkeypatch.setattr(
            xrpl_ops, "JsonRpcClient", _fake_json_rpc_client(exc=RuntimeError("rpc down"))
        )
        assert _run(market_ops.verify_sell_offer(NFT_ID, OFFER_INDEX, 5_000_000)) is False

    def test_default_fetch_offers_path_strict_rpc_failure_raises(self, monkeypatch) -> None:
        """Default path, strict: strict=True must thread
        raise_on_error=True into get_nft_sell_offers so the RPC failure
        propagates instead of reading as 'offer absent'."""
        monkeypatch.setattr(
            xrpl_ops, "JsonRpcClient", _fake_json_rpc_client(exc=RuntimeError("rpc down"))
        )
        with pytest.raises(RuntimeError):
            _run(market_ops.verify_sell_offer(NFT_ID, OFFER_INDEX, 5_000_000, strict=True))

    def test_strict_absent_offer_still_returns_false(self) -> None:
        """A successful lookup that simply doesn't contain the offer is a
        genuine absence even in strict mode — False, not a raise."""
        offers = [{"offer_index": "SOME_OTHER_OFFER", "amount": "5000000", "destination": None}]
        result = _run(
            market_ops.verify_sell_offer(
                NFT_ID, OFFER_INDEX, 5_000_000, fetch_offers=_offers_fetcher(offers), strict=True
            )
        )
        assert result is False

    def test_false_when_offer_expired(self) -> None:
        """#183: an offer whose Expiration is strictly before the current
        ledger time is dead (accept would tecEXPIRED) — verify must fail closed
        so the buyer never gets a doomed payload."""
        offers = [
            {
                "offer_index": OFFER_INDEX,
                "amount": "5000000",
                "destination": None,
                "expiration": 1_000,
            }
        ]
        result = _run(
            market_ops.verify_sell_offer(
                NFT_ID,
                OFFER_INDEX,
                5_000_000,
                fetch_offers=_offers_fetcher(offers),
                fetch_ledger_time=_ledger_time(2_000),
            )
        )
        assert result is False

    def test_false_when_offer_expiration_equals_ledger_time(self) -> None:
        """Boundary: XRPL treats an object as expired when Expiration is <=
        the last-closed ledger's close time, so an at-now Expiration is False."""
        offers = [
            {
                "offer_index": OFFER_INDEX,
                "amount": "5000000",
                "destination": None,
                "expiration": 2_000,
            }
        ]
        result = _run(
            market_ops.verify_sell_offer(
                NFT_ID,
                OFFER_INDEX,
                5_000_000,
                fetch_offers=_offers_fetcher(offers),
                fetch_ledger_time=_ledger_time(2_000),
            )
        )
        assert result is False

    def test_true_when_offer_not_yet_expired(self) -> None:
        offers = [
            {
                "offer_index": OFFER_INDEX,
                "amount": "5000000",
                "destination": None,
                "expiration": 3_000,
            }
        ]
        result = _run(
            market_ops.verify_sell_offer(
                NFT_ID,
                OFFER_INDEX,
                5_000_000,
                fetch_offers=_offers_fetcher(offers),
                fetch_ledger_time=_ledger_time(2_000),
            )
        )
        assert result is True

    def test_no_expiration_skips_ledger_lookup(self) -> None:
        """The common non-expiring offer must NOT incur a ledger-time lookup:
        an offer with no Expiration verifies True even if the ledger fetch
        would raise."""

        async def must_not_fetch() -> int:
            raise AssertionError("ledger time must not be fetched for a non-expiring offer")

        offers = [{"offer_index": OFFER_INDEX, "amount": "5000000", "destination": None}]
        result = _run(
            market_ops.verify_sell_offer(
                NFT_ID,
                OFFER_INDEX,
                5_000_000,
                fetch_offers=_offers_fetcher(offers),
                fetch_ledger_time=must_not_fetch,
            )
        )
        assert result is True

    def test_ledger_time_failure_is_false_nonstrict(self) -> None:
        """An expiring offer whose ledger-time lookup fails is fail-closed to
        False in non-strict mode (never a false positive)."""

        async def boom() -> int:
            raise RuntimeError("ledger rpc down")

        offers = [
            {
                "offer_index": OFFER_INDEX,
                "amount": "5000000",
                "destination": None,
                "expiration": 3_000,
            }
        ]
        result = _run(
            market_ops.verify_sell_offer(
                NFT_ID,
                OFFER_INDEX,
                5_000_000,
                fetch_offers=_offers_fetcher(offers),
                fetch_ledger_time=boom,
            )
        )
        assert result is False

    def test_ledger_time_failure_raises_strict(self) -> None:
        """In strict mode a ledger-time lookup failure propagates (like a
        fetch_offers failure) so the buy-start handler can respond 503 rather
        than stale-close a possibly-healthy listing."""

        async def boom() -> int:
            raise RuntimeError("ledger rpc down")

        offers = [
            {
                "offer_index": OFFER_INDEX,
                "amount": "5000000",
                "destination": None,
                "expiration": 3_000,
            }
        ]
        with pytest.raises(RuntimeError):
            _run(
                market_ops.verify_sell_offer(
                    NFT_ID,
                    OFFER_INDEX,
                    5_000_000,
                    fetch_offers=_offers_fetcher(offers),
                    strict=True,
                    fetch_ledger_time=boom,
                )
            )
