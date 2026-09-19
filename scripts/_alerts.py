"""Shared Discord-webhook alerting for the nightly audit scripts
(scripts/audit_trait_economy.py, scripts/audit_closet_market.py,
scripts/fee_cover_report.py).

Kept private to scripts/ (not lfg_core) — same posture as scripts/_brand.py
and scripts/_economy_deps.py: this is ops tooling, not app-domain logic, and
scripts/ is excluded from mypy --strict.
"""

from __future__ import annotations

import sys


def post_alert(webhook_url: str, body: str) -> bool:
    """Best-effort POST of `body` to a Discord webhook.

    Never raises and never changes the caller's exit code: an audit's verdict
    (and process exit status) must never depend on whether Discord happens to
    be reachable. `body` is truncated to Discord's message-length limit."""
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
    except Exception as exc:  # noqa: BLE001 — alerting must never fail the caller
        print(f"alert webhook failed: {exc}", file=sys.stderr)
        return False
