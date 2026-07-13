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

from aiohttp import web

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


# --- Mutations -------------------------------------------------------------

AUDIT_LOG = os.path.join("reports", "trait_dashboard_audit.log")


def audit(network: str, action: str, body: str, category: str, trait: str, detail: str) -> None:
    """Append one tab-separated line to the local audit log (reports/ is
    gitignored). Best-effort provenance for a local single-operator tool."""
    os.makedirs("reports", exist_ok=True)
    line = "\t".join(
        [rarity.utcnow().isoformat(), network, action, f"{body}/{category}/{trait}", detail]
    )
    with open(AUDIT_LOG, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def _one_row(network: str, body: str, category: str, trait: str, *, db_path: str) -> dict | None:
    for row in fetch_rows(network, db_path=db_path, body=body, category=category)["rows"]:
        if row["trait"] == trait:
            return row
    return None


def apply_toggle(
    network: str, body: str, category: str, trait: str, enabled: bool, *, db_path: str | None = None
) -> dict:
    """Enable/disable a trait via rarity.set_enabled; audit and return the
    re-read row. Raises ValueError if the (body, category, trait) row is
    absent (set_enabled would silently no-op otherwise)."""
    dbp = db_path or app_db_path(network)
    before = _one_row(network, body, category, trait, db_path=dbp)
    if before is None:
        raise ValueError(f"No trait_rarity row for {network}/{body}/{category}/{trait}")
    conn = sqlite3.connect(dbp)
    try:
        rarity.set_enabled(conn, body, category, trait, enabled, network=network)
    finally:
        conn.close()
    audit(network, "toggle", body, category, trait, f"enabled: {int(before['enabled'])} -> {int(enabled)}")
    row = _one_row(network, body, category, trait, db_path=dbp)
    assert row is not None
    return row


def apply_boost(
    network: str,
    body: str,
    category: str,
    trait: str,
    initial: float,
    step_hours: int,
    *,
    db_path: str | None = None,
) -> dict:
    """Arm (or re-arm) a dormant boost via rarity.arm_boost; audit and return
    the re-read row. arm_boost raises ValueError on a missing row."""
    dbp = db_path or app_db_path(network)
    conn = sqlite3.connect(dbp)
    try:
        rarity.arm_boost(
            conn, body, category, trait, network=network, boost_initial=initial, boost_step_hours=step_hours
        )
    finally:
        conn.close()
    audit(network, "boost", body, category, trait, f"boost -> {initial}x / {step_hours}h")
    row = _one_row(network, body, category, trait, db_path=dbp)
    assert row is not None
    return row


def apply_floor(
    network: str,
    body: str | None,
    category: str | None,
    trait: str | None,
    floor: float,
    *,
    db_path: str | None = None,
) -> dict:
    """Set floor_weight for one trait (body+category+trait given) or globally
    for the network (trait None). Audits; returns the re-read row for a
    per-trait set, or a scope summary for a global set."""
    dbp = db_path or app_db_path(network)
    conn = sqlite3.connect(dbp)
    try:
        rarity.set_floor(conn, floor, network=network, body=body, category=category, trait=trait)
    finally:
        conn.close()
    audit(network, "floor", body or "*", category or "*", trait or "*", f"floor -> {floor}")
    if trait is not None:
        row = _one_row(network, body or "", category or "", trait, db_path=dbp)
        if row is None:
            raise ValueError(f"No trait_rarity row for {network}/{body}/{category}/{trait}")
        return row
    return {"network": network, "scope": "global", "floor": floor}


# --- HTTP layer ------------------------------------------------------------

DEFAULT_NETWORK: web.AppKey[str] = web.AppKey("default_network", str)

# Full self-contained UI is injected in a later task; the stub keeps the marker
# the index test asserts on.
INDEX_HTML = "<!doctype html><title>Trait Dashboard</title><h1>Trait Dashboard</h1>"


async def handle_index(request: web.Request) -> web.Response:
    return web.Response(text=INDEX_HTML, content_type="text/html")


async def handle_traits(request: web.Request) -> web.Response:
    net = request.query.get("network") or request.app[DEFAULT_NETWORK]
    data = fetch_rows(
        net,
        body=request.query.get("body") or None,
        category=request.query.get("category") or None,
        q=request.query.get("q") or None,
        status=request.query.get("status") or "all",
    )
    return web.json_response(data)


async def handle_toggle(request: web.Request) -> web.Response:
    data = await request.json()
    row = apply_toggle(
        data["network"], data["body"], data["category"], data["trait"], bool(data["enabled"])
    )
    return web.json_response(row)


async def handle_boost(request: web.Request) -> web.Response:
    data = await request.json()
    row = apply_boost(
        data["network"],
        data["body"],
        data["category"],
        data["trait"],
        float(data["initial"]),
        int(data["step_hours"]),
    )
    return web.json_response(row)


async def handle_floor(request: web.Request) -> web.Response:
    data = await request.json()
    row = apply_floor(
        data["network"],
        data.get("body"),
        data.get("category"),
        data.get("trait"),
        float(data["floor"]),
    )
    return web.json_response(row)


def create_app(default_network: str = "mainnet") -> web.Application:
    app = web.Application()
    app[DEFAULT_NETWORK] = default_network
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/traits", handle_traits)
    app.router.add_post("/api/toggle", handle_toggle)
    app.router.add_post("/api/boost", handle_boost)
    app.router.add_post("/api/floor", handle_floor)
    return app
