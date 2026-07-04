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
        (1, "female", _attrs(Body="Curved", Clothing="Summer Dress")),
        (2, "female", _attrs(Body="Curved 2", Clothing="Summer Dress")),
        (3, "male", _attrs(Body="Straight", Clothing="Hoodie")),
        (4, "female", _attrs(Body="Curved", Clothing="Hoodie")),
    ]
    counts = affinity_audit.count_affinities(rows)
    assert counts[("Clothing", "Summer Dress")] == Counter({"female": 2})
    assert counts[("Clothing", "Hoodie")] == Counter({"male": 1, "female": 1})


def test_count_affinities_derives_body_when_column_empty():
    rows = [(None, None, _attrs(Body="Ape Strong", Eyes="Hypno"))]
    counts = affinity_audit.count_affinities(rows)
    assert counts[("Eyes", "Hypno")] == Counter({"ape": 1})


def test_count_affinities_dedupes_duplicate_editions():
    # Same edition (nft_number=1) minted twice on chain (remint/trait-swap
    # history) with the identical (value, body) pair — must count once, not
    # twice, per Greptile P1: onchain_nfts is keyed by nft_id so duplicate
    # tokens for one edition would otherwise inflate the count.
    rows = [
        (1, "female", _attrs(Body="Curved", Clothing="Summer Dress")),
        (1, "female", _attrs(Body="Curved", Clothing="Summer Dress")),
    ]
    counts = affinity_audit.count_affinities(rows)
    assert counts[("Clothing", "Summer Dress")] == Counter({"female": 1})


def test_count_affinities_edition_swap_history_counts_each_value():
    # Same edition, but the Hat value differs across its two on-chain tokens
    # (a real trait-swap) — both values are genuine historical evidence and
    # must each be counted once for that edition.
    rows = [
        (1, "female", _attrs(Hat="Wizard Hat")),
        (1, "female", _attrs(Hat="Crown")),
    ]
    counts = affinity_audit.count_affinities(rows)
    assert counts[("Hat", "Wizard Hat")] == Counter({"female": 1})
    assert counts[("Hat", "Crown")] == Counter({"female": 1})


def test_count_affinities_null_nft_number_skips_dedupe():
    # NULL nft_number rows fall back to row position as the uniqueness key,
    # i.e. dedupe is skipped for them entirely (documented tradeoff).
    rows = [
        (None, "female", _attrs(Clothing="Summer Dress")),
        (None, "female", _attrs(Clothing="Summer Dress")),
    ]
    counts = affinity_audit.count_affinities(rows)
    assert counts[("Clothing", "Summer Dress")] == Counter({"female": 2})


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


def test_cross_check_coverage_gaps_skip_structural_none():
    # A ("Eyes", "None") count with no None file on that body must not
    # produce a coverage gap — None is a structural empty-slot marker, not a
    # real value that could be "missing from the dir" (CodeRabbit Major).
    counts = {("Eyes", "None"): Counter({"ape": 3})}
    dir_tree = {"ape": {"Eyes": {"Hypno"}}}  # no None file present
    _misplacements, gaps = affinity_audit.cross_check(counts, dir_tree)
    assert gaps == []


def test_render_affinity_yaml_lists_bodies_and_flags_low_confidence():
    counts = {
        ("Clothing", "Summer Dress"): Counter({"female": 4}),
        ("Eyes", "Rare Glint"): Counter({"male": 1}),  # < LOW_CONFIDENCE_THRESHOLD
    }
    out = affinity_audit.render_affinity_yaml(counts)
    assert '"Summer Dress": [female]' in out
    assert "LOW CONFIDENCE" in out and "Rare Glint" in out


def test_render_affinity_yaml_skips_shared_layers():
    # Background/Back are shared (identical per-body copies) — per-body
    # affinity for them is sampling noise, so they must not appear in the
    # draft YAML at all (Greptile P1).
    counts = {("Background", "Sunset"): Counter({"female": 4, "male": 4})}
    out = affinity_audit.render_affinity_yaml(counts)
    assert "Sunset" not in out
    assert "Background" not in out


def test_render_report_md_sections():
    counts = {("Clothing", "Summer Dress"): Counter({"female": 4})}
    out = affinity_audit.render_report_md(counts, [("male", "Clothing", "X")], [])
    assert "## Candidate misplacements" in out
    assert "male/Clothing/X" in out
    assert "female-only" in out


def test_none_value_excluded_from_yaml_and_report():
    # None = empty slot, structural, never a real affinity — must not leak
    # into the draft YAML or the report table as a restricted value.
    counts = {
        ("Eyes", "None"): Counter({"ape": 2, "skeleton": 1}),
        ("Clothing", "Summer Dress"): Counter({"female": 4}),
    }
    yaml_out = affinity_audit.render_affinity_yaml(counts)
    assert '"None"' not in yaml_out
    report_out = affinity_audit.render_report_md(counts, [], [])
    assert "| Eyes | None |" not in report_out
    assert "Summer Dress" in report_out  # other entries still render


def test_run_end_to_end(tmp_path):
    import sqlite3

    db = tmp_path / "onchain.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE onchain_nfts (nft_id TEXT PRIMARY KEY, nft_number INTEGER,"
        " body TEXT, attributes_json TEXT, is_burned INTEGER DEFAULT 0)"
    )
    conn.execute(
        "INSERT INTO onchain_nfts VALUES ('A', 1, 'female', ?, 0)",
        (_attrs(Body="Curved", Clothing="Summer Dress"),),
    )
    conn.execute(  # burned tokens still count — history is the point
        "INSERT INTO onchain_nfts VALUES ('B', 2, 'male', ?, 1)",
        (_attrs(Body="Straight", Clothing="Hoodie"),),
    )
    # Duplicate token for edition 1 (remint/trait-swap) with the identical
    # (value, body) pair — must not inflate the count (Greptile P1).
    conn.execute(
        "INSERT INTO onchain_nfts VALUES ('A2', 1, 'female', ?, 0)",
        (_attrs(Body="Curved", Clothing="Summer Dress"),),
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

    with open(tmp_path / "reports" / "body_affinity.json") as f:
        data = json.load(f)
    # Deduped: edition 1's duplicate token contributes the pair once, not twice.
    assert data["counts"]["Clothing/Summer Dress"] == {"female": 1}


def test_run_raises_systemexit_on_empty_layers_dir(tmp_path):
    import sqlite3

    db = tmp_path / "onchain.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE onchain_nfts (nft_id TEXT PRIMARY KEY, nft_number INTEGER,"
        " body TEXT, attributes_json TEXT, is_burned INTEGER DEFAULT 0)"
    )
    conn.commit()
    conn.close()
    layers = tmp_path / "layers"
    layers.mkdir()  # exists, but contains no trait files at all

    from scripts.audit_body_affinity import run

    try:
        run(str(db), str(layers), str(tmp_path / "reports"))
        assert False, "expected SystemExit"
    except SystemExit as exc:
        assert "no trait files" in str(exc)
