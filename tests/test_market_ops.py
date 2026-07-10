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
