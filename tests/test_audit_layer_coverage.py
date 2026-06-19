# Tests for scripts/audit_layer_coverage.py (CDN layer coverage auditor).
import asyncio
import os
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

# A minimal "available" set: female has its own traits but NOT the 'Wonder' set;
# male has 'Wonder Hair'. Mirrors how the real store reports values.
AVAILABLE = {
    ("female", "Body"): {"Curved Green"},
    ("female", "Clothing"): {"Crop Hoodie Pink"},
    ("female", "Mouth"): {"Dark Lipstick"},
    ("female", "Head"): {"Fish Bowl"},
    ("male", "Body"): {"Straight Burned"},
    ("male", "Head"): {"Wonder Hair"},  # exists for male, not female
}


def _meta(name, **traits):
    attrs = [{"trait_type": k, "value": v} for k, v in traits.items()]
    return {"name": name, "attributes": attrs}


def test_clean_nft_has_no_gaps():
    body, attributes = alc.meta_attributes(
        _meta("#1", Body="Curved Green", Clothing="Crop Hoodie Pink")
    )
    assert body == "female"
    assert alc.audit_attributes(body, attributes, AVAILABLE) == []


def test_wonder_variant_is_reported():
    # The real #3547 'Wonder' variant: its own traits have no female layer.
    body, attributes = alc.meta_attributes(
        _meta(
            "#3547",
            Body="Curved Green",
            Clothing="Wonder",
            Mouth="Lipstick Smile",
            Head="Wonder Hair",
        )
    )
    assert body == "female"
    missing = sorted(m.asset() for m in alc.audit_attributes(body, attributes, AVAILABLE))
    assert missing == [
        "female/Clothing/Wonder",
        "female/Head/Wonder Hair",
        "female/Mouth/Lipstick Smile",
    ]


def test_cross_body_gap_is_caught():
    # 'Wonder Hair' exists for male but not female -> a female NFT carrying it
    # (e.g. received via swap) is correctly flagged. This is the class the old
    # per-edition DB audit could not see.
    body, attributes = alc.meta_attributes(_meta("#9", Body="Curved Green", Head="Wonder Hair"))
    assert body == "female"
    assert [m.asset() for m in alc.audit_attributes(body, attributes, AVAILABLE)] == [
        "female/Head/Wonder Hair"
    ]


def test_all_none_nft_reports_no_gaps():
    body, attributes = alc.meta_attributes(_meta("#2"))
    assert body == "skeleton"
    assert alc.audit_attributes(body, attributes, AVAILABLE) == []


def test_aggregation_collapses_shared_asset():
    results = [
        alc.NftResult("A", 3547, "female", [alc.Missing("female", "Clothing", "Wonder")]),
        alc.NftResult("B", 3601, "female", [alc.Missing("female", "Clothing", "Wonder")]),
        alc.NftResult("C", 1, "female", []),
    ]
    report = alc.format_reports(results, "2026-06-18T00-00-00Z", "testnet")
    assert "| `female/Clothing/Wonder` | 2 |" in report
    assert "cannot be swapped: **2**" in report


def test_run_audit_with_injected_chain():
    # End-to-end with injected enumerator + metadata fetcher (no network) over a
    # LocalLayerStore fixture. Two on-chain tokens share edition #3547: the clean
    # variant passes, the 'Wonder' variant fails — the duplicate the DB hid.
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        base = os.path.join(tmp, "layers")
        for trait, value in [("Body", "Curved Green"), ("Clothing", "Crop Hoodie Pink")]:
            d = os.path.join(base, "female", trait)
            os.makedirs(d, exist_ok=True)
            open(os.path.join(d, f"{value}.png"), "wb").write(b"x")

        from lfg_core import layer_store

        store = layer_store.LocalLayerStore(base)

        tokens = [
            {"nft_id": "AAA", "uri_hex": "aa"},
            {"nft_id": "BBB", "uri_hex": "bb"},
            {"nft_id": "CCC", "uri_hex": ""},  # no URI -> error bucket
        ]
        meta = {
            "aa": _meta("#3547", Body="Curved Green", Clothing="Crop Hoodie Pink"),
            "bb": _meta("#3547", Body="Curved Green", Clothing="Wonder"),
        }

        async def enum():
            return tokens

        async def fetch(uri_hex):
            return meta.get(uri_hex)

        # Run on a fresh loop that is NOT installed as the thread default, so we
        # neither hit get_event_loop()'s 3.12 deprecation nor close the loop the
        # aiohttp-based webapp tests rely on.
        loop = asyncio.new_event_loop()
        try:
            results = loop.run_until_complete(alc.run_audit(enum, fetch, store))
        finally:
            loop.close()
        by_id = {r.nft_id: r for r in results}
        assert by_id["AAA"].missing == []
        assert [m.asset() for m in by_id["BBB"].missing] == ["female/Clothing/Wonder"]
        assert by_id["CCC"].error is not None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
