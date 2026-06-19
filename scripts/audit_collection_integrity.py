#!/usr/bin/env python3
"""Collection-integrity report over the on-chain NFT index.

Surfaces the artifacts that imperfect (non-atomic) trait-swaps leave behind,
reconciling the live collection against its expected edition range:
  - missing editions (burned, never re-minted) — the collection is short
  - editions with >1 live token (original burn failed → duplicate)
  - live tokens whose edition name is out of range (bad re-mint naming)
  - live tokens whose name yields no edition number (metadata naming bug)

  python scripts/audit_collection_integrity.py --network mainnet

Reads the per-network index DB (run the backfill / Bithomp import first).
Exit code is non-zero when any anomaly is found.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from typing import Any

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)

from lfg_core import config, nft_index  # noqa: E402

# The LFG collection tops out at edition 3535 (SWAP_MAX_NFT_NUMBER is a broader
# validity cap, not the minted size, so it would flood "missing" with numbers
# that were never minted).
COLLECTION_SIZE = 3535


def _fmt_list(nums: list[int]) -> str:
    return ", ".join(str(n) for n in nums) if nums else "—"


def format_integrity_report(
    anomalies: dict[str, Any],
    by_id: dict[str, nft_index.OnchainNft],
    network: str,
    max_edition: int,
    live_count: int,
    timestamp: str,
) -> str:
    """Markdown integrity report (pure). `by_id` maps nft_id -> record for the
    out-of-range / unparsed detail lines."""
    a = anomalies
    lines: list[str] = []
    lines.append(f"# Collection Integrity ({network}) — {timestamp}")
    lines.append("")
    lines.append(f"- Expected editions: **1..{max_edition}**")
    lines.append(f"- Live tokens: **{live_count}**")
    lines.append(f"- Missing editions: **{len(a['missing'])}**")
    lines.append(f"- Editions with >1 live token: **{len(a['multi_live'])}**")
    lines.append(f"- Out-of-range edition names: **{len(a['out_of_range'])}**")
    lines.append(f"- Unparsed-name live tokens: **{len(a['unparsed'])}**")
    lines.append("")

    lines.append("## Missing editions (burned, never re-minted)")
    lines.append("")
    lines.append(_fmt_list(a["missing"]))
    lines.append("")

    lines.append("## Editions with >1 live token (original burn likely failed)")
    lines.append("")
    if a["multi_live"]:
        lines.append("| Edition | live tokens |")
        lines.append("| --- | --- |")
        for ed, count in a["multi_live"].items():
            lines.append(f"| {ed} | {count} |")
    else:
        lines.append("_None._")
    lines.append("")

    lines.append("## Out-of-range edition names (bad re-mint naming)")
    lines.append("")
    if a["out_of_range"]:
        lines.append("| # | nft_id | owner |")
        lines.append("| --- | --- | --- |")
        for nid in a["out_of_range"]:
            r = by_id[nid]
            lines.append(f"| {r.nft_number} | `{nid}` | {r.owner or ''} |")
    else:
        lines.append("_None._")
    lines.append("")

    lines.append("## Live tokens with no parsed edition (metadata naming bug)")
    lines.append("")
    if a["unparsed"]:
        lines.append("| nft_id | uri |")
        lines.append("| --- | --- |")
        for nid in a["unparsed"]:
            r = by_id[nid]
            uri = bytes.fromhex(r.uri_hex).decode("ascii", "ignore") if r.uri_hex else ""
            lines.append(f"| `{nid}` | {uri} |")
    else:
        lines.append("_None._")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Collection-integrity report over the index.")
    parser.add_argument("--network", choices=["mainnet", "testnet"], default=config.XRPL_NETWORK)
    parser.add_argument(
        "--max-edition",
        type=int,
        default=COLLECTION_SIZE,
        help="highest expected edition number (default: the 3535 collection size)",
    )
    parser.add_argument("--report-dir", default=os.path.join(REPO_ROOT, "reports"))
    args = parser.parse_args()

    db_path = nft_index.index_db_path(args.network)
    if not os.path.isfile(db_path):
        print(f"No index DB at {db_path}. Run the backfill / Bithomp import first.")
        return 2

    conn = nft_index.init_db(db_path)
    live = nft_index.live_nfts(conn)
    a = nft_index.collection_anomalies(live, args.max_edition)

    # nft_id -> edition for friendly labels in the out-of-range / unparsed lists.
    by_id = {n.nft_id: n for n in live}

    print(f"Network: {args.network}  expected editions: 1..{args.max_edition}")
    print(f"Live tokens: {len(live)}")
    print()
    print(f"Missing editions (no live token): {len(a['missing'])}")
    print(f"  {a['missing']}")
    print(f"Editions with >1 live token: {len(a['multi_live'])}")
    for ed, count in a["multi_live"].items():
        print(f"  #{ed}: {count} live")
    print(f"Out-of-range edition names: {len(a['out_of_range'])}")
    for nid in a["out_of_range"]:
        print(f"  #{by_id[nid].nft_number}  {nid}")
    print(f"Live tokens with no parsed edition (naming bug): {len(a['unparsed'])}")
    for nid in a["unparsed"]:
        uri = (
            bytes.fromhex(by_id[nid].uri_hex).decode("ascii", "ignore")
            if by_id[nid].uri_hex
            else ""
        )
        print(f"  {nid}  uri={uri}")

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    report = format_integrity_report(a, by_id, args.network, args.max_edition, len(live), timestamp)
    os.makedirs(args.report_dir, exist_ok=True)
    report_path = os.path.join(
        args.report_dir, f"collection-integrity-{args.network}-{timestamp}.md"
    )
    with open(report_path, "w") as f:
        f.write(report)
    print(f"\n  Report: {report_path}")

    total = len(a["missing"]) + len(a["multi_live"]) + len(a["out_of_range"]) + len(a["unparsed"])
    return 1 if total else 0


if __name__ == "__main__":
    raise SystemExit(main())
