# #268 follow-up: "Infernal Wings" is a Back-layer value that legacy duplicate
# art can roll in the Accessory slot. It must be relocated to Back by
# normalize_attributes exactly like the two Angel Wings variants, or the
# composed image renders wings at Accessory z (in front of Clothing) and the
# metadata diverges from the on-chain index (NFTs #4039/#4053 class of bug).
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DISCORD_BOT_TOKEN", "x")
os.environ.setdefault("XUMM_API_KEY", "x")
os.environ.setdefault("XUMM_API_SECRET", "x")
os.environ.setdefault("BUNNY_CDN_ACCESS_KEY", "x")
os.environ.setdefault("BUNNY_CDN_STORAGE_ZONE", "x")
os.environ.setdefault("SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2")
os.environ.setdefault("TOKEN_ISSUER_ADDRESS", "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
os.environ.setdefault("TOKEN_CURRENCY_HEX", "4C46474F00000000000000000000000000000000")
os.environ.setdefault("BUNNY_PULL_ZONE", "nft.pullzone.example")
os.environ.setdefault("LAYER_SOURCE", "local")

from typing import Any  # noqa: E402

from lfg_core import swap_meta  # noqa: E402


def _attr(attrs: list[dict[str, Any]], trait_type: str) -> str | None:
    return next((a["value"] for a in attrs if a["trait_type"] == trait_type), None)


def test_infernal_wings_relocates_to_back():
    attrs = swap_meta.normalize_attributes([{"trait_type": "Accessory", "value": "Infernal Wings"}])
    assert _attr(attrs, "Accessory") == "None"
    assert _attr(attrs, "Back") == "Infernal Wings"


def test_infernal_wings_in_back_configured():
    assert "Infernal Wings" in swap_meta.BACK_VALUES
