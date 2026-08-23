#!/usr/bin/env python3
"""Audit the dress-up trait economy against the frozen genesis baseline.

Verifies the three invariants over the on-chain index + Closet/trait-token state:
  - Completeness: every live character holds one asset per slot and the right body
  - Conservation: no asset is silently created/destroyed; each body lives in
    exactly one place
  - Closet ownership: no Closet is keyed to a project signing account, and no
    Closet NFToken is claimed by two owners (#383)

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

from lfg_core import (  # noqa: E402
    closet_reconcile,
    config,
    economy_store,
    nft_index,
    trait_economy,
)


def classify_drift(
    conservation: trait_economy.ConservationReport,
) -> dict[str, dict[tuple[str, str], int]]:
    """Partition per-(slot, value) conservation drift into `benign_swap` —
    every entry of a slot whose signed deltas sum to zero (the trait-swap
    substitution pattern: -1 old value, +1 new value, net zero) — and `real`,
    a slot whose total is non-zero (assets actually created/destroyed
    unaccounted; investigate)."""
    by_slot: dict[str, dict[tuple[str, str], int]] = {}
    for (slot, value), delta in conservation.trait_drift.items():
        by_slot.setdefault(slot, {})[(slot, value)] = delta
    out: dict[str, dict[tuple[str, str], int]] = {"benign_swap": {}, "real": {}}
    for entries in by_slot.values():
        bucket = "benign_swap" if sum(entries.values()) == 0 else "real"
        out[bucket].update(entries)
    return out


def build_alert_body(
    network: str,
    live_count: int,
    conservation: trait_economy.ConservationReport,
    completeness: trait_economy.CompletenessReport,
    report_path: str,
    closet_ownership: closet_reconcile.ClosetOwnershipReport | None = None,
) -> str:
    """Compact Discord-webhook message for a NON-CLEAN audit run, labelling
    benign swap substitution separately from real conservation drift."""
    classes = classify_drift(conservation)
    lines = [
        f"**Trait economy audit: {'DRIFT' if not conservation.ok else 'VIOLATIONS'}** "
        f"({network}, {live_count} live characters)"
    ]
    if classes["real"]:
        lines.append("Real conservation drift — investigate (do NOT re-freeze genesis):")
        for (slot, value), delta in sorted(classes["real"].items()):
            lines.append(f"- {slot} | {value}: {delta:+d}")
    if classes["benign_swap"]:
        lines.append(
            "Net-zero-per-slot pattern — benign swap substitution, review, likely not a leak:"
        )
        for (slot, value), delta in sorted(classes["benign_swap"].items()):
            lines.append(f"- {slot} | {value}: {delta:+d}")
    if not completeness.ok:
        lines.append(
            f"Completeness violations: orphan bodies {completeness.orphan_bodies or '—'}, "
            f"slot anomalies in editions {sorted(completeness.slot_anomalies) or '—'}"
        )
    if closet_ownership is not None and not closet_ownership.ok:
        for row in closet_ownership.project_rows:
            lines.append(f"- Closet keyed to project account {row.owner} -> {row.nft_id} (#383)")
        for nft_id, owners in sorted(closet_ownership.unresolved_duplicates.items()):
            lines.append(
                f"- Closet {nft_id} claimed by {', '.join(owners)} — needs clio arbitration"
            )
        lines.append("Run scripts/reconcile_closet_tokens.py (dry-run first) for the above.")
    lines.append(
        "Run scripts/reconcile_supply_growth.py + reconcile_supply_shrinkage.py "
        "(dry-run first), then re-audit."
    )
    lines.append(f"Report: {report_path}")
    return "\n".join(lines)


def post_alert(webhook_url: str, body: str) -> bool:
    """Best-effort POST to a Discord webhook; failures only log."""
    import json
    import urllib.request

    try:
        req = urllib.request.Request(
            webhook_url,
            data=json.dumps({"content": body[:1900]}).encode(),
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=10)
        return True
    except Exception as exc:  # noqa: BLE001 — alerting must never fail the audit
        print(f"alert webhook failed: {exc}", file=sys.stderr)
        return False


def format_economy_report(
    conservation: trait_economy.ConservationReport,
    completeness: trait_economy.CompletenessReport,
    network: str,
    live_count: int,
    genesis_editions: int,
    timestamp: str,
    supply_changes: list[dict] | None = None,
    closet_ownership: closet_reconcile.ClosetOwnershipReport | None = None,
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
    if closet_ownership is not None:
        lines.append(f"- Closet ownership: **{'OK' if closet_ownership.ok else 'ANOMALIES'}**")
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

    lines.append("## Orphan bodies (dressed live edition not in genesis)")
    lines.append("")
    lines.append(
        ", ".join(str(e) for e in completeness.orphan_bodies) if completeness.orphan_bodies else "—"
    )
    lines.append("")

    lines.append("## Closet ownership anomalies (#383)")
    lines.append("")
    if closet_ownership is not None and not closet_ownership.ok:
        lines.append("| Anomaly | Closet NFToken | Owner(s) |")
        lines.append("| --- | --- | --- |")
        for row in closet_ownership.project_rows:
            lines.append(f"| project-account row ({row.status}) | {row.nft_id} | {row.owner} |")
        for nft_id, owners in sorted(closet_ownership.unresolved_duplicates.items()):
            lines.append(f"| duplicate token | {nft_id} | {', '.join(owners)} |")
    else:
        lines.append("_None._")
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
    parser.add_argument(
        "--alert-webhook",
        default=os.environ.get("ECONOMY_AUDIT_WEBHOOK_URL"),
        help="Discord webhook URL to post to when the run is non-clean "
        "(default: $ECONOMY_AUDIT_WEBHOOK_URL; unset = no alert)",
    )
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
    closet_ownership = closet_reconcile.audit_closet_ownership(conn)

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    report = format_economy_report(
        conservation,
        completeness,
        args.network,
        len(canonical),
        len(genesis.edition_bodies),
        timestamp,
        supply_changes,
        closet_ownership,
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
    print(f"Closet ownership: {'OK' if closet_ownership.ok else 'ANOMALIES'}")
    print(f"Report: {report_path}")
    clean = conservation.ok and completeness.ok and closet_ownership.ok
    if not clean and args.alert_webhook:
        post_alert(
            args.alert_webhook,
            build_alert_body(
                args.network,
                len(canonical),
                conservation,
                completeness,
                report_path,
                closet_ownership,
            ),
        )
    return 0 if clean else 1


if __name__ == "__main__":
    raise SystemExit(main())
