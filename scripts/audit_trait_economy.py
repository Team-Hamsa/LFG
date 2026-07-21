#!/usr/bin/env python3
"""Audit the dress-up trait economy against the frozen genesis baseline.

Verifies the two invariants over the on-chain index + Closet/trait-token state:
  - Completeness: every live character holds one asset per slot and the right body
  - Conservation: no asset is silently created/destroyed; each body lives in
    exactly one place

  python scripts/audit_trait_economy.py --network mainnet

Run scripts/freeze_genesis.py first. Exit code is non-zero on any drift.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)

from lfg_core import config, economy_store, nft_index, trait_economy  # noqa: E402


def format_economy_report(
    conservation: trait_economy.ConservationReport,
    completeness: trait_economy.CompletenessReport,
    network: str,
    live_count: int,
    genesis_editions: int,
    timestamp: str,
    supply_changes: list[dict] | None = None,
) -> str:
    supply_changes = supply_changes or []
    lines: list[str] = []
    lines.append(f"# Trait Economy Audit ({network}) — {timestamp}")
    lines.append("")
    lines.append(f"- Live characters: **{live_count}**")
    lines.append(f"- Genesis editions: **{genesis_editions}**")
    lines.append(f"- Supply changes (ledger): **{len(supply_changes)}**")
    lines.append(f"- Conservation: **{'OK' if conservation.ok else 'DRIFT'}**")
    lines.append(f"- Completeness: **{'OK' if completeness.ok else 'VIOLATIONS'}**")
    lines.append("")

    lines.append("## Supply changes (intentional growth/shrinkage, from ledger)")
    lines.append("")
    if supply_changes:
        lines.append("| Kind | Edition | Body | Actor | Reason |")
        lines.append("| --- | --- | --- | --- | --- |")
        for ch in supply_changes:
            lines.append(
                f"| {ch['kind']} | {ch['edition']} | {ch['body_value']} "
                f"| {ch['actor']} | {ch['reason']} |"
            )
    else:
        lines.append("_None._")
    lines.append("")

    lines.append("## Trait conservation drift (census − genesis, incl. Body)")
    lines.append("")
    if conservation.trait_drift:
        lines.append("| Slot | Value | Drift |")
        lines.append("| --- | --- | --- |")
        for (slot, value), delta in sorted(conservation.trait_drift.items()):
            lines.append(f"| {slot} | {value} | {delta:+d} |")
    else:
        lines.append("_None._")
    lines.append("")

    lines.append("## Wrong body (live edition body ≠ genesis)")
    lines.append("")
    if completeness.wrong_body:
        lines.append("| Edition | Found | Expected |")
        lines.append("| --- | --- | --- |")
        for ed, (found, expected) in sorted(completeness.wrong_body.items()):
            lines.append(f"| {ed} | {found} | {expected} |")
    else:
        lines.append("_None._")
    lines.append("")

    lines.append("## Orphan bodies (live edition not in genesis)")
    lines.append("")
    lines.append(
        ", ".join(str(e) for e in completeness.orphan_bodies) if completeness.orphan_bodies else "—"
    )
    lines.append("")

    lines.append("## Slot anomalies (slot not present exactly once)")
    lines.append("")
    if completeness.slot_anomalies:
        lines.append("| Edition | Slots |")
        lines.append("| --- | --- |")
        for ed, slots in sorted(completeness.slot_anomalies.items()):
            lines.append(f"| {ed} | {', '.join(slots)} |")
    else:
        lines.append("_None._")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit the trait economy against genesis.")
    parser.add_argument("--network", choices=["mainnet", "testnet"], default=config.XRPL_NETWORK)
    parser.add_argument("--report-dir", default=os.path.join(REPO_ROOT, "reports"))
    args = parser.parse_args()

    db_path = nft_index.index_db_path(args.network)
    if not os.path.isfile(db_path):
        print(f"No index DB at {db_path}. Run the backfill / Bithomp import first.")
        return 2

    conn = nft_index.init_db(db_path)
    economy_store.init_economy_schema(conn)
    if not economy_store.genesis_exists(conn):
        print("No frozen genesis. Run scripts/freeze_genesis.py first.")
        return 2

    genesis = economy_store.read_genesis(conn)
    supply_changes = economy_store.read_supply_changes(conn)
    live = nft_index.live_nfts(conn)
    # The dedupe cap spans the genesis max, the ledger, AND any live edition, so
    # NO live token is silently dropped as out-of-range: a legitimately-minted
    # edition is explained by the ledger, while an UNLOGGED one stays in the
    # census and surfaces as conservation drift rather than vanishing.
    live_max = max((r.nft_number for r in live if r.nft_number is not None), default=0)
    max_edition = max(trait_economy.effective_max_edition(genesis, supply_changes), live_max)

    canonical, _ = trait_economy.dedupe_editions(live, max_edition)
    census = trait_economy.asset_census(
        canonical,
        economy_store.read_closet_assets(conn),
        economy_store.read_trait_tokens(conn),
    )
    conservation = trait_economy.verify_conservation(genesis, census, supply_changes)
    # Completeness checks bodies against the EFFECTIVE genesis so legitimately
    # minted new editions are not mistaken for orphan bodies.
    effective = trait_economy.effective_genesis(genesis, supply_changes)
    completeness = trait_economy.verify_completeness(canonical, effective)

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    report = format_economy_report(
        conservation,
        completeness,
        args.network,
        len(canonical),
        len(genesis.edition_bodies),
        timestamp,
        supply_changes,
    )
    os.makedirs(args.report_dir, exist_ok=True)
    report_path = os.path.join(
        args.report_dir, f"trait-economy-audit-{args.network}-{timestamp}.md"
    )
    with open(report_path, "w") as f:
        f.write(report)

    print(f"Network: {args.network}  live characters: {len(canonical)}")
    print(f"Conservation: {'OK' if conservation.ok else 'DRIFT'}")
    print(f"Completeness: {'OK' if completeness.ok else 'VIOLATIONS'}")
    print(f"Report: {report_path}")
    return 0 if conservation.ok and completeness.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
