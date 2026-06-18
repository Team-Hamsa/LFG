#!/usr/bin/env python3
"""Audit CDN layer coverage for every minted NFT in the LFG table.

A trait swap recomposes an NFT's image from the CDN layer tree
(``layers/<body>/<TraitType>/<Value>``). The swap aborts — fail-safe, before any
burn — when a trait value on the NFT has no backing layer file. This script
finds every NFT that currently *cannot* be swapped and the exact layer assets
that are missing, so they can be uploaded.

It is read-only and performs NO layer downloads: existence is checked against
cached directory listings (``store.list_values``), never ``store.resolve``.

  python scripts/audit_layer_coverage.py                 # audit ../lfg_nfts.db
  python scripts/audit_layer_coverage.py --db path.db --report-dir reports

Exit code is non-zero when any coverage gap is found (CI-ready).

Column -> layer trait-type mapping (DB ``Hat`` is the layer ``Head``):
  Background, Back, Body, Clothing, Mouth, Eyebrows, Eyes, Hat->Head, Accessory
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sqlite3
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)

from lfg_core import layer_store, swap_meta  # noqa: E402

# Body classes that have their own layer subtree.
BODIES = ["male", "female", "ape", "skeleton"]

# DB trait column -> layer trait-type. Identity except Hat -> Head.
COLUMN_TO_TRAIT: dict[str, str] = {
    "Background": "Background",
    "Back": "Back",
    "Body": "Body",
    "Clothing": "Clothing",
    "Mouth": "Mouth",
    "Eyebrows": "Eyebrows",
    "Eyes": "Eyes",
    "Hat": "Head",
    "Accessory": "Accessory",
}

# Distinct layer trait-types we look up per body.
TRAIT_TYPES = sorted(set(COLUMN_TO_TRAIT.values()))


@dataclass(frozen=True)
class Missing:
    """One unbacked trait value on one NFT."""

    body: str
    trait_type: str
    value: str

    def asset(self) -> str:
        return f"{self.body}/{self.trait_type}/{self.value}"


@dataclass
class NftResult:
    nft_number: int
    network: str
    body: str
    missing: list[Missing]


async def build_available_sets(store: Any) -> dict[tuple[str, str], set[str]]:
    """One set of available values per (body, trait_type). Reads only the
    (cached) CDN directory listings; downloads nothing."""
    available: dict[tuple[str, str], set[str]] = {}
    for body in BODIES:
        for trait_type in TRAIT_TYPES:
            values = await store.list_values(body, trait_type)
            available[(body, trait_type)] = set(values)
    return available


def row_attributes(row: dict[str, Any]) -> tuple[str, list[dict[str, str]]]:
    """Map a DB row to (body, normalized attributes), running it through the
    SAME normalization the swap path uses so results cannot drift."""
    raw_attrs = [
        {"trait_type": trait, "value": str(row.get(col) or "None")}
        for col, trait in COLUMN_TO_TRAIT.items()
    ]
    attributes = swap_meta.normalize_attributes(raw_attrs)
    body = swap_meta.detect_body(attributes)
    return body, attributes


def audit_row(
    body: str, attributes: list[dict[str, str]], available: dict[tuple[str, str], set[str]]
) -> list[Missing]:
    """Missing layer files for one NFT. Pure — no I/O. 'None'/empty values are
    skipped (they need no layer file, exactly as the compose path skips them)."""
    missing: list[Missing] = []
    for attr in attributes:
        value = attr.get("value") or "None"
        if value == "None":
            continue
        trait_type = attr["trait_type"]
        if value not in available.get((body, trait_type), set()):
            missing.append(Missing(body, trait_type, value))
    return missing


def _load_rows(db_path: str) -> list[dict[str, Any]]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(
            "SELECT nft_number, network, " + ", ".join(COLUMN_TO_TRAIT) + " FROM LFG "
            "ORDER BY nft_number"
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def format_reports(results: list[NftResult], timestamp: str, total: int) -> str:
    """Markdown report: per-NFT failures + aggregated upload worklist."""
    failures = [r for r in results if r.missing]

    # Aggregate: asset -> count of NFTs it blocks.
    blocked_by: Counter[str] = Counter()
    for r in failures:
        for m in {m.asset() for m in r.missing}:
            blocked_by[m] += 1

    lines: list[str] = []
    lines.append(f"# Layer Coverage Audit — {timestamp}")
    lines.append("")
    lines.append(f"- NFTs audited: **{total}**")
    lines.append(f"- NFTs that cannot be swapped: **{len(failures)}**")
    lines.append(f"- Distinct missing layer assets: **{len(blocked_by)}**")
    lines.append("")

    lines.append("## Missing layer assets (upload worklist)")
    lines.append("")
    if blocked_by:
        lines.append("| Asset (body/TraitType/Value) | NFTs blocked |")
        lines.append("| --- | --- |")
        for asset, count in sorted(blocked_by.items(), key=lambda kv: (-kv[1], kv[0])):
            lines.append(f"| `{asset}` | {count} |")
    else:
        lines.append("_None — every minted NFT's traits are fully backed._")
    lines.append("")

    lines.append("## NFTs that cannot be swapped")
    lines.append("")
    if failures:
        lines.append("| # | network | body | missing traits |")
        lines.append("| --- | --- | --- | --- |")
        for r in sorted(failures, key=lambda r: r.nft_number):
            traits = ", ".join(f"{m.trait_type}={m.value}" for m in r.missing)
            lines.append(f"| {r.nft_number} | {r.network} | {r.body} | {traits} |")
    else:
        lines.append("_None._")
    lines.append("")
    return "\n".join(lines)


async def run_audit(db_path: str, store: Any) -> list[NftResult]:
    available = await build_available_sets(store)
    results: list[NftResult] = []
    for row in _load_rows(db_path):
        body, attributes = row_attributes(row)
        missing = audit_row(body, attributes, available)
        results.append(
            NftResult(
                nft_number=int(row["nft_number"]),
                network=str(row.get("network") or "unknown"),
                body=body,
                missing=missing,
            )
        )
    return results


async def _amain() -> int:
    parser = argparse.ArgumentParser(description="Audit CDN layer coverage for minted NFTs.")
    parser.add_argument(
        "--db", default=os.path.join(REPO_ROOT, "lfg_nfts.db"), help="path to lfg_nfts.db"
    )
    parser.add_argument(
        "--report-dir", default=os.path.join(REPO_ROOT, "reports"), help="where to write the report"
    )
    args = parser.parse_args()

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    store = layer_store.get_layer_store()
    results = await run_audit(args.db, store)

    report = format_reports(results, timestamp, total=len(results))
    os.makedirs(args.report_dir, exist_ok=True)
    report_path = os.path.join(args.report_dir, f"layer-coverage-{timestamp}.md")
    with open(report_path, "w") as f:
        f.write(report)

    failures = [r for r in results if r.missing]
    assets = {m.asset() for r in failures for m in r.missing}
    print(f"Audited {len(results)} NFTs.")
    print(f"  Cannot be swapped: {len(failures)}")
    print(f"  Distinct missing layer assets: {len(assets)}")
    print(f"  Report: {report_path}")
    return 1 if failures else 0


def main() -> int:
    return asyncio.run(_amain())


if __name__ == "__main__":
    raise SystemExit(main())
