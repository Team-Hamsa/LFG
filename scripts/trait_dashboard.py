#!/usr/bin/env python3
# Standalone, loopback-bound admin dashboard for the variable-rarity engine.
# Shows every trait's art + live odds (share / effective weight / boost / enabled)
# and lets an operator toggle enable/disable, arm boosts, and set floors. All
# reads/writes go through lfg_core.rarity (no new rarity logic); edits hit the
# live trait_rarity table that weighted_pick reads on every mint, so they take
# effect on the next mint with NO restart.
#
# Not wired into the Activity / Discord / lfg_service. No on-chain actions.
#
#   .venv/bin/python scripts/trait_dashboard.py [--network mainnet] [--port 8890] [--host 127.0.0.1]
#
# Reach it over an SSH tunnel:  ssh -L 8890:localhost:8890 <server>  then open
# http://localhost:8890.  Design: docs/superpowers/specs/2026-07-13-rarity-admin-dashboard-design.md

import os
import sqlite3
import sys

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from lfg_core import config, rarity  # noqa: E402
from lfg_core.db_path import app_db_path  # noqa: E402

_LAYER_EXTS = (".png", ".gif", ".mp4")


def resolve_image(body: str, category: str, value: str) -> str | None:
    """Local path of a trait layer file, or None. Mirrors LocalLayerStore's
    local resolution (the body dir then shared/, png→gif→mp4). Kept sync on
    purpose so the read path and the /img handler stay non-async. A body of
    '*' (legacy/ungendered rows) scans every concrete body dir."""
    base = config.LAYERS_DIR
    if body == "*":
        try:
            roots = [d for d in os.listdir(base) if os.path.isdir(os.path.join(base, d))]
        except OSError:
            roots = []
    else:
        roots = [body]
    for root in [*roots, "shared"]:
        for ext in _LAYER_EXTS:
            path = os.path.join(base, root, category, value + ext)
            if os.path.isfile(path):
                return path
    return None


def fetch_rows(
    network: str,
    *,
    db_path: str | None = None,
    body: str | None = None,
    category: str | None = None,
    q: str | None = None,
    status: str = "all",
    now=None,
) -> dict:
    """Read every trait_rarity row for `network` and compute share / effective
    weight / boost status with the engine's own functions, so the numbers equal
    what weighted_pick uses. Returns {network, rows, bodies, categories} with
    the given server-side filters applied."""
    now = now or rarity.utcnow()
    conn = sqlite3.connect(db_path or app_db_path(network))
    try:
        rarity.ensure_schema(conn)
        raw = conn.execute(
            """SELECT body, category, trait, live_count, floor_weight,
                      boost_initial, boost_step_hours, boost_started_at, enabled
               FROM trait_rarity WHERE network=?""",
            (network,),
        ).fetchall()
    finally:
        conn.close()

    # Per-(body, category) totals: the denominator and population size the
    # picker uses (matches get_odds / weighted_pick).
    totals: dict[tuple[str, str], int] = {}
    pops: dict[tuple[str, str], int] = {}
    for r in raw:
        key = (r[0], r[1])
        totals[key] = totals.get(key, 0) + r[3]
        pops[key] = pops.get(key, 0) + 1

    rows: list[dict] = []
    bodies: set[str] = set()
    categories: set[str] = set()
    for b, cat, trait, count, floor, bi, bs, bsa, enabled in raw:
        bodies.add(b)
        categories.add(cat)
        key = (b, cat)
        total = totals[key]
        rows.append(
            {
                "body": b,
                "category": cat,
                "trait": trait,
                "live_count": count,
                "share": (count / total * 100) if total else 0.0,
                "weight": rarity.effective_weight(
                    count, total, floor, bi, bs, bsa, now, population_size=pops[key]
                ),
                "enabled": bool(enabled),
                "boost_status": rarity.boost_status(bi, bs, bsa, now),
                "floor_weight": floor,
                "boost_initial": bi,
                "boost_step_hours": bs,
                "boost_started_at": bsa,
                "has_image": resolve_image(b, cat, trait) is not None,
            }
        )

    def keep(row: dict) -> bool:
        if body is not None and row["body"] != body:
            return False
        if category is not None and row["category"] != category:
            return False
        if q and q.lower() not in row["trait"].lower():
            return False
        st = status or "all"
        if st == "enabled" and not row["enabled"]:
            return False
        if st == "disabled" and row["enabled"]:
            return False
        if st == "boosted" and row["boost_status"] in ("—", "finished"):
            return False
        if st == "problems" and not (
            not row["enabled"] or row["live_count"] == 0 or not row["has_image"]
        ):
            return False
        return True

    rows = [r for r in rows if keep(r)]
    rows.sort(key=lambda r: (r["body"], r["category"], -r["weight"], r["trait"]))
    return {
        "network": network,
        "rows": rows,
        "bodies": sorted(bodies),
        "categories": sorted(categories),
    }
