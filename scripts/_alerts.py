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
    the return value is never longer than `limit` (#560 review round 3) while
    keeping `trailer` — the reproduction/remediation commands, the part an
    operator acts on — intact ahead of everything else:

    1. If it all fits, return it as-is.
    2. Otherwise elide `rows` from the back, in order, replaced with an
       honest "... and N more" count, keeping `header` and `trailer` whole
       (#560 review round 1: post_alert()'s own truncation is a blind
       `body[:1900]` that has no idea `trailer` matters, so a long finding
       list was silently eating the only actionable content on exactly the
       worst nights).
    3. If `header` + `trailer` alone (every row elided) still doesn't fit,
       trim `header` — never `trailer` — down to whatever room is left,
       even to nothing.
    4. Only if `trailer` alone already exceeds `limit` (e.g. #560 review
       round 3: audit_trait_economy's `report_path` is caller-supplied via an
       unconstrained `--report-dir` and lives inside a trailer line) does the
       trailer itself give: keep as many WHOLE trailer lines as fit,
       preferring the SHORTEST ones first, since a single unbounded line
       (like a report path) is what can blow up, never the short, universal
       remediation commands — so a short line always wins a spot over a long
       one. If even the shortest single line alone doesn't fit, it is
       character-truncated as an absolute last resort.

    This makes post_alert()'s own `body[:1900]` a no-op for every caller that
    goes through this function; that slice is untouched and stays the
    backstop for any caller that does not."""
    full = "\n".join([header, *rows, *trailer])
    if len(full) <= limit:
        return full

    for keep in range(len(rows), -1, -1):
        elided = len(rows) - keep
        elision = [f"... and {elided} more"] if elided else []
        candidate = "\n".join([header, *rows[:keep], *elision, *trailer])
        if len(candidate) <= limit:
            return candidate

    trailer_block = "\n".join(trailer)
    if len(trailer_block) <= limit:
        budget = limit - len(trailer_block) - (1 if trailer_block else 0)
        pieces = [p for p in (header[: max(budget, 0)], trailer_block) if p]
        return "\n".join(pieces)

    order = sorted(range(len(trailer)), key=lambda i: len(trailer[i]))
    keep_idx: set[int] = set()
    used = 0
    for i in order:
        cost = len(trailer[i]) + (1 if keep_idx else 0)
        if used + cost <= limit:
            keep_idx.add(i)
            used += cost
    if keep_idx:
        return "\n".join(trailer[i] for i in sorted(keep_idx))
    return min(trailer, key=len)[:limit]
