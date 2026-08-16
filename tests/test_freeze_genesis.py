# Tests for scripts/freeze_genesis.py (reconciliation report formatting).
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
import freeze_genesis as fg  # noqa: E402


def test_format_reconciliation_report():
    recon = {
        "duplicates": {1001: ["DUPID"]},
        "missing": [220, 1017],
        "out_of_range": ["OOR"],
        "unparsed": ["UNP"],
    }
    md = fg.format_reconciliation_report(
        recon,
        "mainnet",
        3535,
        live_count=3537,
        genesis_editions=3533,
        timestamp="2026-06-22T00-00-00Z",
    )
    assert "Trait Economy Reconciliation (mainnet)" in md
    assert "Genesis editions: **3533**" in md
    assert "Duplicate editions: **1**" in md
    assert "1001" in md and "DUPID" in md
    assert "220, 1017" in md
    assert "OOR" in md
    assert "UNP" in md
