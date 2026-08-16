# Tests for scripts/audit_collection_integrity.py (report formatting).
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
os.environ.setdefault("XRPL_NETWORK", "testnet")
os.environ.setdefault("BUNNY_PULL_ZONE", "nft.pullzone.example")

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
)
import audit_collection_integrity as aci  # noqa: E402

from lfg_core import nft_index  # noqa: E402


def test_format_integrity_report():
    oor = nft_index.OnchainNft("IDX", 6467, "rOwner", False, None, "6868", "skeleton", [], "", None)
    anomalies = {
        "missing": [220, 1017],
        "multi_live": {1001: 2},
        "out_of_range": ["IDX"],
        "unparsed": [],
    }
    md = aci.format_integrity_report(
        anomalies, {"IDX": oor}, "mainnet", 3535, live_count=3537, timestamp="2026-06-19T00-00-00Z"
    )
    assert "Collection Integrity (mainnet)" in md
    assert "Missing editions: **2**" in md
    assert "220, 1017" in md
    assert "| 1001 | 2 |" in md
    assert "| 6467 | `IDX` | rOwner |" in md
    assert "_None._" in md  # unparsed section empty
