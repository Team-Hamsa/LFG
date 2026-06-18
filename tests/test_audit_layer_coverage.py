# Tests for scripts/audit_layer_coverage.py (CDN layer coverage auditor).
import asyncio
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# Dummy env so lfg_core.config import doesn't fail (same trick as other tests).
os.environ.setdefault("DISCORD_BOT_TOKEN", "x")
os.environ.setdefault("XUMM_API_KEY", "x")
os.environ.setdefault("XUMM_API_SECRET", "x")
os.environ.setdefault("BUNNY_CDN_ACCESS_KEY", "x")
os.environ.setdefault("BUNNY_CDN_STORAGE_ZONE", "x")
os.environ.setdefault("SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2")  # dummy testnet seed
os.environ.setdefault("TOKEN_ISSUER_ADDRESS", "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
os.environ.setdefault("TOKEN_CURRENCY_HEX", "4C46474F00000000000000000000000000000000")
os.environ.setdefault("XRPL_NETWORK", "testnet")
# config freezes IMG_PROXY_ALLOWED_BASES from this at import time; set it before
# importing so the order this file imports config in doesn't strip the pull zone
# (the webapp smoke tests assert on it).
os.environ.setdefault("BUNNY_PULL_ZONE", "nft.pullzone.example")

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
)
import audit_layer_coverage as alc  # noqa: E402

# A minimal "available" set: male has a Body + a Clothing, nothing under
# Accessory; skeleton has nothing. Mirrors how the real store reports values.
AVAILABLE = {
    ("male", "Body"): {"Straight Burned"},
    ("male", "Clothing"): {"Open Heart"},
    ("male", "Head"): {"Cowboy"},
    ("male", "Accessory"): {"Banana"},  # NOT "Super Soaker"
    ("male", "Background"): {"Muted Tan"},
}


def _attrs(**cols):
    row = {col: cols.get(col, "None") for col in alc.COLUMN_TO_TRAIT}
    return alc.row_attributes(row)


def test_clean_nft_has_no_gaps():
    body, attributes = _attrs(Body="Straight Burned", Clothing="Open Heart", Background="Muted Tan")
    assert body == "male"
    assert alc.audit_row(body, attributes, AVAILABLE) == []


def test_missing_accessory_is_reported():
    # The real #3536 case: Super Soaker has no layer file.
    body, attributes = _attrs(Body="Straight Burned", Accessory="Super Soaker")
    missing = alc.audit_row(body, attributes, AVAILABLE)
    assert [m.asset() for m in missing] == ["male/Accessory/Super Soaker"]


def test_all_none_nft_reports_no_gaps():
    # The #3538 case: every attribute None -> skeleton, nothing to resolve.
    body, attributes = _attrs()
    assert body == "skeleton"
    assert alc.audit_row(body, attributes, AVAILABLE) == []


def test_hat_column_maps_to_head_trait():
    # A value supplied in the DB 'Hat' column is checked under layer 'Head'.
    body, attributes = _attrs(Body="Straight Burned", Hat="Cowboy")
    assert alc.audit_row(body, attributes, AVAILABLE) == []  # Cowboy exists under male/Head

    body, attributes = _attrs(Body="Straight Burned", Hat="Sombrero")
    missing = alc.audit_row(body, attributes, AVAILABLE)
    assert [m.asset() for m in missing] == ["male/Head/Sombrero"]


def test_aggregation_collapses_shared_asset(tmp_path):
    results = [
        alc.NftResult(1, "testnet", "male", [alc.Missing("male", "Accessory", "Super Soaker")]),
        alc.NftResult(2, "testnet", "male", [alc.Missing("male", "Accessory", "Super Soaker")]),
        alc.NftResult(3, "testnet", "male", []),
    ]
    report = alc.format_reports(results, "2026-06-18T00-00-00Z", total=3)
    assert "| `male/Accessory/Super Soaker` | 2 |" in report
    assert "NFTs that cannot be swapped: **2**" in report


def test_run_audit_against_local_store(tmp_path):
    # End-to-end over a sqlite DB + LocalLayerStore fixture tree.
    base = tmp_path / "layers"
    for trait, value in [("Body", "Straight Burned"), ("Accessory", "Banana")]:
        d = base / "male" / trait
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{value}.png").write_bytes(b"x")

    from lfg_core import layer_store

    store = layer_store.LocalLayerStore(str(base))

    db = tmp_path / "lfg.db"
    conn = sqlite3.connect(db)
    cols = ", ".join(f"{c} TEXT" for c in alc.COLUMN_TO_TRAIT)
    conn.execute(f"CREATE TABLE LFG (nft_number INTEGER, network TEXT, {cols})")
    conn.execute(
        "INSERT INTO LFG (nft_number, network, Body, Accessory) VALUES (1, 'testnet', ?, ?)",
        ("Straight Burned", "Banana"),
    )
    conn.execute(
        "INSERT INTO LFG (nft_number, network, Body, Accessory) VALUES (2, 'testnet', ?, ?)",
        ("Straight Burned", "Super Soaker"),
    )
    conn.commit()
    conn.close()

    # Mirror the other suites' loop handling: asyncio.run() would close the
    # process-wide loop and break the aiohttp-based webapp tests that follow.
    results = asyncio.get_event_loop().run_until_complete(alc.run_audit(str(db), store))
    by_num = {r.nft_number: r for r in results}
    assert by_num[1].missing == []
    assert [m.asset() for m in by_num[2].missing] == ["male/Accessory/Super Soaker"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
