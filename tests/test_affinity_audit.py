# tests/test_affinity_audit.py
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
from collections import Counter  # noqa: E402

from lfg_core import affinity_audit  # noqa: E402


def _attrs(**kw):
    return json.dumps([{"trait_type": k, "value": v} for k, v in kw.items()])


def test_count_affinities_groups_by_type_value_and_body():
    rows = [
        ("female", _attrs(Body="Curved", Clothing="Summer Dress")),
        ("female", _attrs(Body="Curved 2", Clothing="Summer Dress")),
        ("male", _attrs(Body="Straight", Clothing="Hoodie")),
        ("female", _attrs(Body="Curved", Clothing="Hoodie")),
    ]
    counts = affinity_audit.count_affinities(rows)
    assert counts[("Clothing", "Summer Dress")] == Counter({"female": 2})
    assert counts[("Clothing", "Hoodie")] == Counter({"male": 1, "female": 1})


def test_count_affinities_derives_body_when_column_empty():
    rows = [(None, _attrs(Body="Ape Strong", Eyes="Hypno"))]
    counts = affinity_audit.count_affinities(rows)
    assert counts[("Eyes", "Hypno")] == Counter({"ape": 1})


def test_classify_labels():
    assert affinity_audit.classify(Counter({"female": 5})) == "female-only"
    assert affinity_audit.classify(Counter({"male": 2})) == "male-only"
    assert affinity_audit.classify(Counter({"male": 2, "female": 9})) == "shared-MF"
    assert (
        affinity_audit.classify(Counter({"male": 1, "female": 1, "ape": 1, "skeleton": 1}))
        == "universal"
    )
    assert affinity_audit.classify(Counter({"ape": 3, "skeleton": 1})) == "bodies:ape+skeleton"


def test_cross_check_flags_never_minted_dir_values_and_missing_files():
    counts = {
        ("Clothing", "Summer Dress"): Counter({"female": 4}),
        ("Clothing", "Retired Coat"): Counter({"male": 2}),
    }
    dir_tree = {
        "female": {"Clothing": {"Summer Dress"}},
        "male": {"Clothing": {"Summer Dress"}},  # present but never minted on male
        "ape": {"Clothing": set()},
        "skeleton": {"Clothing": set()},
    }
    misplacements, gaps = affinity_audit.cross_check(counts, dir_tree)
    assert ("male", "Clothing", "Summer Dress") in misplacements
    assert ("male", "Clothing", "Retired Coat") in gaps  # minted on male, no file
    assert ("female", "Clothing", "Summer Dress") not in misplacements
