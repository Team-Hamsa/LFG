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


def assemble_alert_body(header: str, rows: list[str], trailer: list[str], limit: int = 1900) -> str:
    """Join `header`, `rows`, and `trailer` into an alert body that GUARANTEES
    `header` and `trailer` survive, however many `rows` there are (#560 review
    round 1: post_alert()'s own truncation is a blind `body[:1900]` — it has
    no idea the trailer is the reproduction command / recovery instructions,
    so a long finding list was silently eating the only actionable content on
    exactly the worst nights). `rows` are the elidable part: kept in order
    from the front, dropped from the back with an honest "... and N more"
    count so a shortened list is never mistaken for a complete one.

    `limit` matches post_alert's own truncation point, so under normal
    operation this makes that truncation a no-op; it only still applies in a
    pathological case this function itself cannot fix (header + trailer alone
    already at or past the limit, or one single `rows` entry longer than the
    entire budget)."""
    full = "\n".join([header, *rows, *trailer])
    if len(full) <= limit:
        return full
    for keep in range(len(rows), -1, -1):
        elided = len(rows) - keep
        elision = [f"... and {elided} more"] if elided else []
        candidate = "\n".join([header, *rows[:keep], *elision, *trailer])
        if len(candidate) <= limit or keep == 0:
            return candidate
    return full  # unreachable — the keep == 0 iteration always returns
