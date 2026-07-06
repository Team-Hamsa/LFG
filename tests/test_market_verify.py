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


def _offers_fetcher(
    offers: list[dict[str, Any]],
) -> Callable[[str], Awaitable[list[dict[str, Any]]]]:
    async def fetch(_nft_id: str) -> list[dict[str, Any]]:
        return offers

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
