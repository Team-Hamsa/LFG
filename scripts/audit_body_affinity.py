#!/usr/bin/env python3
# scripts/audit_body_affinity.py
# One-time (re-runnable) audit: derive per-value body affinity from the
# on-chain index (burned included) and cross-check against layers/.
# Output feeds the human review gate before trait_config.yaml is committed.
#
#   .venv/bin/python scripts/audit_body_affinity.py --network mainnet

import argparse
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lfg_core import affinity_audit, nft_index  # noqa: E402


def _dir_tree(layers_dir: str) -> dict[str, dict[str, set[str]]]:
    tree: dict[str, dict[str, set[str]]] = {}
    for body in sorted(os.listdir(layers_dir)):
        body_path = os.path.join(layers_dir, body)
        if not os.path.isdir(body_path) or body.startswith("."):
            continue
        tree[body] = {}
        for trait_type in sorted(os.listdir(body_path)):
            type_path = os.path.join(body_path, trait_type)
            if not os.path.isdir(type_path) or trait_type.startswith("."):
                continue
            if trait_type == "Body":
                # Body IS the shape — untracked in counts by design, so its
                # files would all read as false-positive misplacements.
                continue
            tree[body][trait_type] = {
                os.path.splitext(f)[0]
                for f in os.listdir(type_path)
                if not f.startswith(".")
                and os.path.splitext(f)[1].lower() in (".png", ".gif", ".mp4")
            }
    return tree


def run(db_path: str, layers_dir: str, out_dir: str) -> dict:
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT nft_number, body, attributes_json FROM onchain_nfts"
    ).fetchall()
    conn.close()
    counts = affinity_audit.count_affinities(rows)
    dir_tree = _dir_tree(layers_dir)
    if not any(values for types in dir_tree.values() for values in types.values()):
        raise SystemExit(
            f"layers dir '{layers_dir}' contains no trait files — sync layers "
            "before auditing"
        )
    misplacements, gaps = affinity_audit.cross_check(counts, dir_tree)
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "body_affinity_report.md"), "w") as f:
        f.write(affinity_audit.render_report_md(counts, misplacements, gaps))
    with open(os.path.join(out_dir, "body_affinity_draft.yaml"), "w") as f:
        f.write(affinity_audit.render_affinity_yaml(counts))
    with open(os.path.join(out_dir, "body_affinity.json"), "w") as f:
        json.dump(
            {
                "counts": {f"{t}/{v}": dict(c) for (t, v), c in sorted(counts.items())},
                "misplacements": misplacements,
                "coverage_gaps": gaps,
            },
            f,
            indent=2,
        )
    return {
        # None = empty slot, structural, never a real affinity — excluded
        # here so this count matches what actually lands in the draft/report.
        "values": sum(1 for (_t, v) in counts if v != "None"),
        "misplacements": misplacements,
        "coverage_gaps": gaps,
    }


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--network", choices=["testnet", "mainnet"], default="mainnet")
    p.add_argument("--layers-dir", default="layers")
    p.add_argument("--out-dir", default="reports")
    args = p.parse_args()
    result = run(nft_index.index_db_path(args.network), args.layers_dir, args.out_dir)
    print(
        f"{result['values']} values audited; "
        f"{len(result['misplacements'])} candidate misplacements; "
        f"{len(result['coverage_gaps'])} coverage gaps -> {args.out_dir}/"
    )


if __name__ == "__main__":
    main()
