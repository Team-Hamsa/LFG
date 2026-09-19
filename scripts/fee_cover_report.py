#!/usr/bin/env python3
"""Fee-cover campaign report: budget burn-down, promises/refunds by state and
reason, refunds by wallet (where abuse shows up first). --audit exits non-zero
when any refund breaks its invariants (promise, observed fee × coverage, 50% of
observed royalty) or a campaign paid more than its budget.

  .venv/bin/python scripts/fee_cover_report.py --network mainnet [--audit]
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)

from lfg_core import db_path, fee_cover, fee_cover_store, market_ops  # noqa: E402
from scripts._alerts import post_alert  # noqa: E402


def _xrp(drops: int) -> str:
    return market_ops.drops_to_xrp_str(str(int(drops)))


def render(status: dict[str, Any], violations: list[str] | None) -> str:
    lines = [f"network: {status['network']}", f"state: {status['state']}"]
    campaign = status.get("campaign")
    if campaign:
        lines += [
            f"campaign: #{campaign['id']} coverage {campaign['coverage_bps'] / 100:g}% "
            f"budget {_xrp(campaign['budget_drops'])} XRP, cap {_xrp(campaign['wallet_cap_drops'])} XRP/wallet",
            f"committed: {_xrp(status['committed_drops'])} XRP  paid: {_xrp(status['paid_drops'])} XRP  "
            f"remaining: {_xrp(status['remaining_drops'])} XRP",
            f"open promises: {status['open_promises']}",
            f"refunds by state: {status['refunds_by_state']}",
            f"declines by reason: {status['declines_by_reason']}",
            "top wallets:",
            *[f"  {w['wallet']}  {_xrp(w['drops'])} XRP" for w in status["top_wallets"]],
        ]
    if violations is not None:
        lines.append("AUDIT PASS" if not violations else "AUDIT FAIL")
        lines += [f"  {v}" for v in violations]
    return "\n".join(lines)


def build_alert_body(network: str, violations: list[str]) -> str:
    """Compact Discord-webhook message for a non-clean `--audit` run (#560).
    Every row is already one of `fee_cover.audit_violations()`'s own
    human-readable lines, naming the offending accept hash, so the alert is
    actionable without opening a DB."""
    plural = "" if len(violations) == 1 else "s"
    lines = [f"**Fee-cover audit: FAIL** ({network}, {len(violations)} violation{plural})"]
    lines.extend(f"- {v}" for v in violations)
    lines.append(f"Report: scripts/fee_cover_report.py --network {network} --audit")
    lines.append(
        "After fixing the cause, requeue with scripts/recover_fee_cover_refunds.py "
        f"--network {network} --requeue <accept_hash>"
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Marketplace fee-cover report.")
    parser.add_argument("--network", required=True, choices=("testnet", "mainnet"))
    parser.add_argument("--audit", action="store_true")
    parser.add_argument(
        "--alert-webhook",
        default=os.environ.get("ECONOMY_AUDIT_WEBHOOK_URL"),
        help="Discord webhook URL to post to when --audit finds a violation "
        "(default: $ECONOMY_AUDIT_WEBHOOK_URL; unset = no alert)",
    )
    args = parser.parse_args(argv)
    conn = fee_cover_store.connect(db_path.app_db_path(args.network))
    try:
        status = fee_cover_store.status_summary(conn, args.network)
        violations = fee_cover.audit_violations(conn, args.network) if args.audit else None
    finally:
        conn.close()
    print(render(status, violations))
    if args.audit and violations and args.alert_webhook:
        post_alert(args.alert_webhook, build_alert_body(args.network, violations))
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
