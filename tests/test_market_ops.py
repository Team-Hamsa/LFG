# tests/test_market_ops.py
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

import pytest  # noqa: E402

from lfg_core.market_ops import (  # noqa: E402
    drops_to_xrp_str,
    extract_created_sell_offer,
    xrp_to_drops_str,
)

NFT_ID = "000800001E43B0783E006F30078A64A8628F4B1B22879C8EB1CAF8C700000019"
OTHER_NFT_ID = "000800001E43B0783E006F30078A64A8628F4B1B22879C8EB1CAF8C7000009999"


def _created_offer_meta(
    nft_id: str = NFT_ID,
    amount: object = "1500000",
    flags: int = 1,
    destination: str | None = None,
    ledger_index: str = "9F1C2D3E4A5B6C7D8E9F0A1B2C3D4E5F60718293A4B5C6D7E8F901234567890",
) -> dict:
    """Real-shaped NFTokenCreateOffer tx-meta AffectedNodes payload, adapted
    from Baysed's ~/Baysed-Lab/services/api/app/routers/market.py:263-297
    (_market_extract_created_nft_offers)."""
    new_fields: dict = {
        "Amount": amount,
        "Flags": flags,
        "NFTokenID": nft_id,
        "Owner": "rOwnerAddressXXXXXXXXXXXXXXXXXXXXX",
    }
    if destination is not None:
        new_fields["Destination"] = destination
    return {
        "AffectedNodes": [
            {
                "ModifiedNode": {
                    "LedgerEntryType": "NFTokenPage",
                    "FinalFields": {"Account": "rOwnerAddressXXXXXXXXXXXXXXXXXXXXX"},
                }
            },
            {
                "CreatedNode": {
                    "LedgerEntryType": "NFTokenOffer",
                    "LedgerIndex": ledger_index,
                    "NewFields": new_fields,
                }
            },
        ]
    }


class TestExtractCreatedSellOffer:
    def test_extracts_sell_offer_shape(self) -> None:
        meta = _created_offer_meta(
            amount="1500000", flags=1, destination="rDestXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"
        )
        result = extract_created_sell_offer(meta, NFT_ID)
        assert result == {
            "offer_index": "9F1C2D3E4A5B6C7D8E9F0A1B2C3D4E5F60718293A4B5C6D7E8F901234567890",
            "amount_drops": 1500000,
            "destination": "rDestXXXXXXXXXXXXXXXXXXXXXXXXXXXXX",
            "flags": 1,
        }

    def test_extracts_sell_offer_without_destination(self) -> None:
        meta = _created_offer_meta(amount="1500000", flags=1)
        result = extract_created_sell_offer(meta, NFT_ID)
        assert result is not None
        assert result["destination"] is None
        assert result["amount_drops"] == 1500000

    def test_returns_none_for_buy_side_offer(self) -> None:
        # Flags=0 means lsfSellNFToken (bit 0x1) is NOT set -> a buy offer.
        meta = _created_offer_meta(amount="1500000", flags=0)
        assert extract_created_sell_offer(meta, NFT_ID) is None

    def test_returns_none_for_wrong_nft_id(self) -> None:
        meta = _created_offer_meta(nft_id=NFT_ID, amount="1500000", flags=1)
        assert extract_created_sell_offer(meta, OTHER_NFT_ID) is None

    def test_returns_none_for_missing_created_node(self) -> None:
        meta = {
            "AffectedNodes": [
                {
                    "ModifiedNode": {
                        "LedgerEntryType": "NFTokenPage",
                        "FinalFields": {"Account": "rOwnerAddressXXXXXXXXXXXXXXXXXXXXX"},
                    }
                }
            ]
        }
        assert extract_created_sell_offer(meta, NFT_ID) is None

    def test_returns_none_for_missing_affected_nodes(self) -> None:
        assert extract_created_sell_offer({}, NFT_ID) is None

    def test_returns_none_for_iou_amount_dict(self) -> None:
        # A dict Amount (IOU currency/issuer/value) is not an XRP sell offer.
        meta = _created_offer_meta(
            amount={
                "currency": "USD",
                "issuer": "rIssuerXXXXXXXXXXXXXXXXXXXXXXXXXXX",
                "value": "10",
            },
            flags=1,
        )
        assert extract_created_sell_offer(meta, NFT_ID) is None

    def test_returns_none_for_other_ledger_entry_type(self) -> None:
        meta = {
            "AffectedNodes": [
                {
                    "CreatedNode": {
                        "LedgerEntryType": "NFTokenOffer".replace("Offer", "Page"),
                        "LedgerIndex": "ABC",
                        "NewFields": {"Amount": "1500000", "Flags": 1, "NFTokenID": NFT_ID},
                    }
                }
            ]
        }
        assert extract_created_sell_offer(meta, NFT_ID) is None

    def test_flags_with_extra_bits_still_recognized_as_sell(self) -> None:
        # lsfSellNFToken (0x1) set alongside other unrelated bits.
        meta = _created_offer_meta(amount="1500000", flags=0b101)
        result = extract_created_sell_offer(meta, NFT_ID)
        assert result is not None
        assert result["flags"] == 0b101

    def test_json_roundtrip_meta_still_parses(self) -> None:
        # Guard against accidental reliance on non-JSON-safe types (tx meta
        # in production always arrives via json.loads).
        meta = json.loads(json.dumps(_created_offer_meta(amount="1500000", flags=1)))
        result = extract_created_sell_offer(meta, NFT_ID)
        assert result is not None
        assert result["amount_drops"] == 1500000


class TestXrpToDropsStr:
    def test_basic_conversion(self) -> None:
        assert xrp_to_drops_str("1.5") == "1500000"

    def test_whole_number(self) -> None:
        assert xrp_to_drops_str("2") == "2000000"

    def test_max_precision_six_decimals(self) -> None:
        assert xrp_to_drops_str("0.000001") == "1"

    def test_rejects_float_input(self) -> None:
        # Exactly TypeError — the documented non-string rejection. A looser
        # (TypeError, ValueError) would also pass if a float silently coerced
        # through Decimal and failed some later value check instead (#130).
        with pytest.raises(TypeError):
            xrp_to_drops_str(1.5)  # type: ignore[arg-type]

    def test_rejects_more_than_six_decimal_places(self) -> None:
        with pytest.raises(ValueError):
            xrp_to_drops_str("1.1234567")

    def test_rejects_zero(self) -> None:
        with pytest.raises(ValueError):
            xrp_to_drops_str("0")

    def test_rejects_negative(self) -> None:
        with pytest.raises(ValueError):
            xrp_to_drops_str("-1.5")

    def test_rejects_garbage_string(self) -> None:
        with pytest.raises(ValueError):
            xrp_to_drops_str("not-a-number")

    def test_rejects_absurd_magnitude(self) -> None:
        # Decimal accepts scientific notation, so "1E+30" (and any plain
        # value beyond XRP's 100e9 total supply) previously converted to a
        # 36-digit drops string no ledger could ever honor (#130).
        with pytest.raises(ValueError):
            xrp_to_drops_str("1E+30")
        with pytest.raises(ValueError):
            xrp_to_drops_str("1000000000000000000000000000000")
        with pytest.raises(ValueError):
            xrp_to_drops_str("100000000000.000001")  # just over max supply

    def test_max_supply_boundary_accepted(self) -> None:
        # 100 billion XRP (the total supply) is the inclusive upper bound.
        assert xrp_to_drops_str("100000000000") == "100000000000000000"


class TestDropsToXrpStr:
    def test_basic_conversion(self) -> None:
        assert drops_to_xrp_str("1500000") == "1.5"

    def test_whole_number(self) -> None:
        assert drops_to_xrp_str("2000000") == "2"

    def test_trailing_zero_whole_numbers_are_not_scientific(self) -> None:
        # Decimal("10").normalize() is Decimal("1E+1"); a whole number of XRP
        # with trailing zeros must still render in plain fixed-point (regression:
        # a 10 XRP listing showed "1E+1 XRP" and broke the JS BigInt buy path).
        assert drops_to_xrp_str("10000000") == "10"
        assert drops_to_xrp_str("100000000") == "100"
        assert drops_to_xrp_str("1000000000") == "1000"
        assert drops_to_xrp_str("120000000") == "120"

    def test_zero_drops(self) -> None:
        assert drops_to_xrp_str("0") == "0"

    def test_single_drop(self) -> None:
        assert drops_to_xrp_str("1") == "0.000001"

    def test_rejects_float_input(self) -> None:
        # Exactly TypeError — see TestXrpToDropsStr.test_rejects_float_input.
        with pytest.raises(TypeError):
            drops_to_xrp_str(1500000.0)  # type: ignore[arg-type]

    def test_zero_is_asymmetric_with_xrp_to_drops(self) -> None:
        # drops_to_xrp_str accepts "0" (a 0-drop amount is representable and
        # third-party 0-drop offers exist on-ledger), but the round trip is
        # deliberately asymmetric: xrp_to_drops_str rejects the resulting "0"
        # because WE never create a 0-price listing (#130).
        assert drops_to_xrp_str("0") == "0"
        with pytest.raises(ValueError):
            xrp_to_drops_str(drops_to_xrp_str("0"))

    def test_rejects_garbage_string(self) -> None:
        with pytest.raises(ValueError):
            drops_to_xrp_str("not-a-number")

    @pytest.mark.parametrize(
        "xrp",
        ["1.5", "2", "0.000001", "100.25", "0.5", "10", "100", "120", "1000"],
    )
    def test_round_trip(self, xrp: str) -> None:
        assert drops_to_xrp_str(xrp_to_drops_str(xrp)) == xrp


# --- #239: BRIX-denominated trait listings — per-kind amount layer ---

from decimal import Decimal  # noqa: E402

from lfg_core import config, market_ops  # noqa: E402

BRIX_AMOUNT = {
    "currency": config.TOKEN_CURRENCY_HEX,
    "issuer": config.TOKEN_ISSUER_ADDRESS,
    "value": "10",
}


def _brix(value="10", currency=None, issuer=None):
    return {
        "currency": currency or config.TOKEN_CURRENCY_HEX,
        "issuer": issuer or config.TOKEN_ISSUER_ADDRESS,
        "value": value,
    }


class TestValidateBrixValue:
    def test_normalizes_trailing_zeros(self) -> None:
        assert market_ops.validate_brix_value("10.500000") == "10.5"

    def test_whole_number_passthrough(self) -> None:
        assert market_ops.validate_brix_value("10") == "10"

    def test_never_scientific_notation(self) -> None:
        assert market_ops.validate_brix_value("1E+3") == "1000"

    def test_rejects_float_input(self) -> None:
        with pytest.raises(TypeError):
            market_ops.validate_brix_value(1.5)  # type: ignore[arg-type]

    def test_rejects_zero_negative_garbage(self) -> None:
        for bad in ("0", "-1", "abc", "", "NaN", "Infinity"):
            with pytest.raises(ValueError):
                market_ops.validate_brix_value(bad)

    def test_rejects_more_than_six_decimals(self) -> None:
        with pytest.raises(ValueError):
            market_ops.validate_brix_value("1.1234567")

    def test_rejects_over_cap(self) -> None:
        with pytest.raises(ValueError):
            market_ops.validate_brix_value("1000000000000001")  # > 1e15

    def test_cap_boundary_accepted(self) -> None:
        assert market_ops.validate_brix_value("1000000000000000") == "1000000000000000"


class TestBrixAmountDict:
    def test_shape_uses_token_currency_and_issuer(self) -> None:
        assert market_ops.brix_amount_dict("10.50") == {
            "currency": config.TOKEN_CURRENCY_HEX,
            "issuer": config.TOKEN_ISSUER_ADDRESS,
            "value": "10.5",
        }


class TestExtractCreatedSellOfferBrix:
    def test_brix_dict_accepted_and_normalized(self) -> None:
        meta = _created_offer_meta(amount=_brix("10.500000"), flags=1)
        result = extract_created_sell_offer(meta, NFT_ID, expect="brix")
        assert result is not None
        assert result["amount_brix"] == "10.5"
        assert "amount_drops" not in result

    def test_xrp_string_amount_rejected_for_brix(self) -> None:
        meta = _created_offer_meta(amount="1500000", flags=1)
        assert extract_created_sell_offer(meta, NFT_ID, expect="brix") is None

    def test_wrong_issuer_rejected(self) -> None:
        meta = _created_offer_meta(
            amount=_brix(issuer="rWrongIssuerXXXXXXXXXXXXXXXXXXXXXX"), flags=1
        )
        assert extract_created_sell_offer(meta, NFT_ID, expect="brix") is None

    def test_wrong_currency_rejected(self) -> None:
        meta = _created_offer_meta(amount=_brix(currency="USD"), flags=1)
        assert extract_created_sell_offer(meta, NFT_ID, expect="brix") is None

    def test_bad_value_rejected(self) -> None:
        for bad in ("0", "-5", "abc", "1.1234567", "1000000000000001"):
            meta = _created_offer_meta(amount=_brix(bad), flags=1)
            assert extract_created_sell_offer(meta, NFT_ID, expect="brix") is None

    def test_buy_side_brix_offer_rejected(self) -> None:
        meta = _created_offer_meta(amount=_brix(), flags=0)
        assert extract_created_sell_offer(meta, NFT_ID, expect="brix") is None

    def test_brix_dict_rejected_for_default_xrp(self) -> None:
        meta = _created_offer_meta(amount=_brix(), flags=1)
        assert extract_created_sell_offer(meta, NFT_ID) is None

    def test_unknown_expect_raises(self) -> None:
        with pytest.raises(ValueError):
            extract_created_sell_offer(_created_offer_meta(), NFT_ID, expect="usd")


def _run_verify(coro):
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _offers(amount, destination=None, expiration=None, offer_index=None):
    async def fetch(_nft_id):
        return [
            {
                "offer_index": offer_index
                or "9F1C2D3E4A5B6C7D8E9F0A1B2C3D4E5F60718293A4B5C6D7E8F901234567890",
                "amount": amount,
                "destination": destination,
                "flags": 1,
                "expiration": expiration,
            }
        ]

    return fetch


OFFER_INDEX = "9F1C2D3E4A5B6C7D8E9F0A1B2C3D4E5F60718293A4B5C6D7E8F901234567890"


class TestVerifySellOfferBrix:
    def test_matching_brix_offer_verifies(self) -> None:
        ok = _run_verify(
            market_ops.verify_sell_offer(
                NFT_ID,
                OFFER_INDEX,
                None,
                fetch_offers=_offers(_brix("10")),
                expect="brix",
                expected_brix="10",
            )
        )
        assert ok is True

    def test_decimal_equivalent_value_matches(self) -> None:
        ok = _run_verify(
            market_ops.verify_sell_offer(
                NFT_ID,
                OFFER_INDEX,
                None,
                fetch_offers=_offers(_brix("10.0")),
                expect="brix",
                expected_brix="10",
            )
        )
        assert ok is True

    def test_value_mismatch_fails(self) -> None:
        ok = _run_verify(
            market_ops.verify_sell_offer(
                NFT_ID,
                OFFER_INDEX,
                None,
                fetch_offers=_offers(_brix("11")),
                expect="brix",
                expected_brix="10",
            )
        )
        assert ok is False

    def test_xrp_amount_is_mismatch_for_brix(self) -> None:
        ok = _run_verify(
            market_ops.verify_sell_offer(
                NFT_ID,
                OFFER_INDEX,
                None,
                fetch_offers=_offers("10000000"),
                expect="brix",
                expected_brix="10",
            )
        )
        assert ok is False

    def test_wrong_issuer_is_mismatch(self) -> None:
        ok = _run_verify(
            market_ops.verify_sell_offer(
                NFT_ID,
                OFFER_INDEX,
                None,
                fetch_offers=_offers(_brix(issuer="rWrongIssuerXXXXXXXXXXXXXXXXXXXXXX")),
                expect="brix",
                expected_brix="10",
            )
        )
        assert ok is False

    def test_foreign_destination_still_rejected(self) -> None:
        ok = _run_verify(
            market_ops.verify_sell_offer(
                NFT_ID,
                OFFER_INDEX,
                None,
                fetch_offers=_offers(_brix("10"), destination="rSomeoneElseXXXXXXXXXXXXXXXXXXXXXX"),
                expect="brix",
                expected_brix="10",
            )
        )
        assert ok is False

    def test_brix_dict_is_mismatch_for_default_xrp(self) -> None:
        ok = _run_verify(
            market_ops.verify_sell_offer(
                NFT_ID, OFFER_INDEX, 10_000_000, fetch_offers=_offers(_brix("10"))
            )
        )
        assert ok is False

    def test_missing_expected_brix_raises(self) -> None:
        with pytest.raises(ValueError):
            _run_verify(
                market_ops.verify_sell_offer(
                    NFT_ID, OFFER_INDEX, None, fetch_offers=_offers(_brix()), expect="brix"
                )
            )

    def test_decimal_normalized_equality(self) -> None:
        assert Decimal("10.0") == Decimal("10")
