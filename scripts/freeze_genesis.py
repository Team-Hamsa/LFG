#!/usr/bin/env python3
"""Reconcile today's on-chain index into a clean, frozen genesis baseline for the
dress-up trait economy.

Resolves duplicate editions (prefer mutable, newest ledger), excludes missing /
unparsed / out-of-range tokens, freezes the per-(slot,value) trait supply and the
per-edition body ledger into the per-network onchain DB, and writes a Markdown
reconciliation report.

  python scripts/freeze_genesis.py --network mainnet

Refuses to overwrite an existing genesis without --force.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from typing import Any

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)

from lfg_core import config, economy_store, nft_index, trait_economy  # noqa: E402

COLLECTION_SIZE = 3535


def _fmt_ints(nums: list[int]) -> str:
    return ", ".join(str(n) for n in nums) if nums else "—"


def _fmt_strs(items: list[str]) -> str:
    return ", ".join(items) if items else "—"


def format_reconciliation_report(
    reconciliation: dict[str, Any],
    network: str,
    max_edition: int,
    live_count: int,
    genesis_editions: int,
    timestamp: str,
) -> str:
    r = reconciliation
    dupes: dict[int, list[str]] = r["duplicates"]
    lines: list[str] = []
    lines.append(f"# Trait Economy Reconciliation ({network}) — {timestamp}")
    lines.append("")
    lines.append(f"- Expected editions: **1..{max_edition}**")
    lines.append(f"- Live tokens: **{live_count}**")
    lines.append(f"- Genesis editions: **{genesis_editions}**")
    lines.append(f"- Duplicate editions: **{len(dupes)}**")
    lines.append(f"- Missing editions: **{len(r['missing'])}**")
    lines.append(f"- Out-of-range tokens: **{len(r['out_of_range'])}**")
    lines.append(f"- Unparsed-name tokens: **{len(r['unparsed'])}**")
    lines.append("")

    lines.append("## Duplicate editions (kept newest mutable; dropped the rest)")
    lines.append("")
    if dupes:
        lines.append("| Edition | dropped nft_ids |")
        lines.append("| --- | --- |")
        for ed, ids in sorted(dupes.items()):
            lines.append(f"| {ed} | {_fmt_strs(ids)} |")
    else:
        lines.append("_None._")
    lines.append("")

    lines.append("## Missing editions (no live token; excluded from genesis)")
    lines.append("")
    lines.append(_fmt_ints(r["missing"]))
    lines.append("")

    lines.append("## Out-of-range tokens (edition outside range; excluded)")
    lines.append("")
    lines.append(_fmt_strs(r["out_of_range"]))
    lines.append("")

    lines.append("## Unparsed-name tokens (no edition number; excluded)")
    lines.append("")
    lines.append(_fmt_strs(r["unparsed"]))
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Freeze the trait-economy genesis baseline.")
    parser.add_argument("--network", choices=["mainnet", "testnet"], default=config.XRPL_NETWORK)
    parser.add_argument("--max-edition", type=int, default=COLLECTION_SIZE)
    parser.add_argument("--report-dir", default=os.path.join(REPO_ROOT, "reports"))
    parser.add_argument("--force", action="store_true", help="overwrite an existing genesis")
    args = parser.parse_args()

    db_path = nft_index.index_db_path(args.network)
    if not os.path.isfile(db_path):
        print(f"No index DB at {db_path}. Run the backfill / Bithomp import first.")
        return 2

    conn = nft_index.init_db(db_path)
    economy_store.init_economy_schema(conn)
    if economy_store.genesis_exists(conn) and not args.force:
        print("Genesis already frozen. Re-run with --force to overwrite.")
        return 2

    live = nft_index.live_nfts(conn)
    canonical, reconciliation = trait_economy.dedupe_editions(live, args.max_edition)
    genesis = trait_economy.build_genesis(canonical)

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    meta = {
        "network": args.network,
        "max_edition": str(args.max_edition),
        "genesis_editions": str(len(canonical)),
        "frozen_at": timestamp,
    }
    economy_store.freeze_genesis(conn, genesis, meta)

    report = format_reconciliation_report(
        reconciliation, args.network, args.max_edition, len(live), len(canonical), timestamp
    )
    os.makedirs(args.report_dir, exist_ok=True)
    report_path = os.path.join(
        args.report_dir, f"trait-economy-reconciliation-{args.network}-{timestamp}.md"
    )
    with open(report_path, "w") as f:
        f.write(report)

    print(f"Network: {args.network}  live tokens: {len(live)}  genesis editions: {len(canonical)}")
    print(
        f"Duplicates: {len(reconciliation['duplicates'])}  missing: {len(reconciliation['missing'])}"
    )
    print(f"Genesis frozen in {db_path}")
    print(f"Report: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
