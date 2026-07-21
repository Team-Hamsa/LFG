# Tests for scripts/audit_trait_economy.py (economy report formatting).
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
import audit_trait_economy as ate  # noqa: E402

from lfg_core import trait_economy  # noqa: E402


def test_economy_report_clean():
    cons = trait_economy.ConservationReport(trait_drift={}, ok=True)
    comp = trait_economy.CompletenessReport(
        wrong_body={}, orphan_bodies=[], slot_anomalies={}, ok=True
    )
    md = ate.format_economy_report(cons, comp, "mainnet", 3533, 3533, "2026-06-22T00-00-00Z")
    assert "Trait Economy Audit (mainnet)" in md
    assert "Conservation: **OK**" in md
    assert "Completeness: **OK**" in md


def test_economy_report_flags_drift():
    cons = trait_economy.ConservationReport(
        trait_drift={("Background", "Sky"): 1, ("Head", "Crown"): -1, ("Body", "S"): 1},
        ok=False,
    )
    comp = trait_economy.CompletenessReport(
        wrong_body={1: ("Curved", "Straight")},
        orphan_bodies=[9],
        slot_anomalies={3: ["Head"]},
        ok=False,
    )
    md = ate.format_economy_report(cons, comp, "mainnet", 100, 100, "2026-06-22T00-00-00Z")
    assert "Conservation: **DRIFT**" in md
    assert "Background" in md and "Sky" in md
    assert "Crown" in md
    assert "| 1 | Curved | Straight |" in md
    assert "9" in md  # orphan body
    assert "Head" in md  # slot anomaly
