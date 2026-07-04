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
        # None.png placeholder never minted on skeleton — structural, exempt
        "skeleton": {"Clothing": {"None"}},
    }
    misplacements, gaps = affinity_audit.cross_check(counts, dir_tree)
    assert ("male", "Clothing", "Summer Dress") in misplacements
    assert ("male", "Clothing", "Retired Coat") in gaps  # minted on male, no file
    assert ("female", "Clothing", "Summer Dress") not in misplacements
    assert ("skeleton", "Clothing", "None") not in misplacements  # None exempt


def test_render_affinity_yaml_lists_bodies_and_flags_low_confidence():
    counts = {
        ("Clothing", "Summer Dress"): Counter({"female": 4}),
        ("Eyes", "Rare Glint"): Counter({"male": 1}),  # < LOW_CONFIDENCE_THRESHOLD
    }
    out = affinity_audit.render_affinity_yaml(counts)
    assert '"Summer Dress": [female]' in out
    assert "LOW CONFIDENCE" in out and "Rare Glint" in out


def test_render_report_md_sections():
    counts = {("Clothing", "Summer Dress"): Counter({"female": 4})}
    out = affinity_audit.render_report_md(counts, [("male", "Clothing", "X")], [])
    assert "## Candidate misplacements" in out
    assert "male/Clothing/X" in out
    assert "female-only" in out


def test_run_end_to_end(tmp_path):
    import sqlite3

    db = tmp_path / "onchain.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE onchain_nfts (nft_id TEXT PRIMARY KEY, body TEXT,"
        " attributes_json TEXT, is_burned INTEGER DEFAULT 0)"
    )
    conn.execute(
        "INSERT INTO onchain_nfts VALUES ('A', 'female', ?, 0)",
        (_attrs(Body="Curved", Clothing="Summer Dress"),),
    )
    conn.execute(  # burned tokens still count — history is the point
        "INSERT INTO onchain_nfts VALUES ('B', 'male', ?, 1)",
        (_attrs(Body="Straight", Clothing="Hoodie"),),
    )
    conn.commit()
    conn.close()
    layers = tmp_path / "layers"
    (layers / "female" / "Clothing").mkdir(parents=True)
    (layers / "female" / "Clothing" / "Summer Dress.png").write_bytes(b"x")
    (layers / "male" / "Clothing").mkdir(parents=True)
    # Body/ dirs are the shape itself — never misplacement candidates
    (layers / "female" / "Body").mkdir(parents=True)
    (layers / "female" / "Body" / "Curved.png").write_bytes(b"x")

    from scripts.audit_body_affinity import run

    result = run(str(db), str(layers), str(tmp_path / "reports"))
    assert (tmp_path / "reports" / "body_affinity_report.md").exists()
    assert (tmp_path / "reports" / "body_affinity_draft.yaml").exists()
    assert result["values"] == 2
    assert ("male", "Clothing", "Hoodie") in result["coverage_gaps"]
    assert not any(t == "Body" for _b, t, _v in result["misplacements"])
