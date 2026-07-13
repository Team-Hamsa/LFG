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

import json
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


def _list_subdirs(dirname: str) -> list[str]:
    path = os.path.join(config.LAYERS_DIR, dirname)
    if not os.path.isdir(path):
        return []
    return [d for d in os.listdir(path) if os.path.isdir(os.path.join(path, d)) and not d.startswith(".")]


def _list_values(dirname: str, trait_type: str) -> set[str]:
    path = os.path.join(config.LAYERS_DIR, dirname, trait_type)
    if not os.path.isdir(path):
        return set()
    out = set()
    for name in os.listdir(path):
        stem, ext = os.path.splitext(name)
        if ext.lower() in _LAYER_EXTS and not name.startswith("."):
            out.add(stem)
    return out


def scan_layer_tree() -> dict[str, dict[str, list[str]]]:
    """{body: {trait_type: [values]}} over the local layer tree, mirroring
    LocalLayerStore semantics: each body's values union in shared/ (which is
    available to every body at mint time). Sync on purpose."""
    base = config.LAYERS_DIR
    out: dict[str, dict[str, list[str]]] = {}
    if not os.path.isdir(base):
        return out
    for body in _list_subdirs(""):
        if body == "shared":
            continue
        trait_types = set(_list_subdirs(body)) | set(_list_subdirs("shared"))
        cats: dict[str, list[str]] = {}
        for trait_type in trait_types:
            values = _list_values(body, trait_type) | _list_values("shared", trait_type)
            if values:
                cats[trait_type] = sorted(values)
        if cats:
            out[body] = cats
    return out


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


def sync_layers(network: str, *, db_path: str | None = None) -> int:
    """Insert a floor-weight trait_rarity row for every (body, category, value)
    in the local layer tree not already tracked, so newly-added art shows up
    without waiting for a mint. Returns the number of rows inserted."""
    dbp = db_path or app_db_path(network)
    now = rarity.utcnow()
    inserted = 0
    conn = sqlite3.connect(dbp)
    try:
        rarity.ensure_schema(conn)
        existing = {
            (b, c, t)
            for b, c, t in conn.execute(
                "SELECT body, category, trait FROM trait_rarity WHERE network=?", (network,)
            )
        }
        for body, cats in scan_layer_tree().items():
            for category, values in cats.items():
                fresh = [v for v in values if (body, category, v) not in existing]
                if fresh:
                    rarity._ensure_rows(conn, network, body, category, fresh, now)
                    inserted += len(fresh)
    finally:
        conn.close()
    if inserted:
        audit(network, "sync", "*", "*", "*", f"inserted {inserted} floor rows")
    return inserted


# --- HTTP layer ------------------------------------------------------------

DEFAULT_NETWORK: web.AppKey[str] = web.AppKey("default_network", str)


class _BadInput(Exception):
    """Client input error → HTTP 400."""


async def _json(request: web.Request) -> dict:
    try:
        data = await request.json()
    except (json.JSONDecodeError, ValueError) as e:
        raise _BadInput("invalid JSON body") from e
    if not isinstance(data, dict):
        raise _BadInput("body must be a JSON object")
    return data


def _require(data: dict, *keys: str) -> None:
    for key in keys:
        if data.get(key) is None:
            raise _BadInput(f"missing field: {key}")


def _num(data: dict, key: str, lo: float, hi: float) -> float:
    try:
        val = float(data[key])
    except (KeyError, TypeError, ValueError) as e:
        raise _BadInput(f"invalid {key}") from e
    if not (lo <= val <= hi):
        raise _BadInput(f"{key} out of range [{lo}, {hi}]")
    return val


@web.middleware
async def _error_mw(request: web.Request, handler) -> web.StreamResponse:
    try:
        return await handler(request)
    except _BadInput as e:
        return web.json_response({"error": str(e)}, status=400)
    except ValueError as e:
        # rarity.set_enabled/arm_boost & apply_* raise ValueError for a missing
        # (body, category, trait) row.
        return web.json_response({"error": str(e)}, status=404)

# Self-contained page: inline CSS + JS, no build step, no external assets.
# `__DEFAULT_NETWORK__` is substituted per request in handle_index.
INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Trait Dashboard</title>
<style>
  :root { color-scheme: light dark; --bg:#fff; --fg:#1a1a1a; --mut:#777; --line:#ddd;
          --card:#fafafa; --accent:#2d6cdf; --off:#c0392b; --chk:#e8f0fe; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#16181c; --fg:#e6e6e6; --mut:#8b93a1; --line:#2c313a;
            --card:#1e2127; --accent:#5b8def; --off:#e06c5b; --chk:#233047; } }
  * { box-sizing:border-box; }
  body { margin:0; font:14px/1.4 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
         background:var(--bg); color:var(--fg); }
  header { display:flex; align-items:center; gap:14px; flex-wrap:wrap;
           padding:12px 16px; border-bottom:1px solid var(--line); position:sticky; top:0;
           background:var(--bg); z-index:5; }
  header h1 { font-size:16px; margin:0; margin-right:auto; }
  .filters { display:flex; align-items:center; gap:10px; flex-wrap:wrap; padding:10px 16px;
             border-bottom:1px solid var(--line); }
  select, input, button { font:inherit; color:var(--fg); background:var(--bg);
           border:1px solid var(--line); border-radius:6px; padding:5px 9px; }
  button { cursor:pointer; }
  button.active { background:var(--accent); color:#fff; border-color:var(--accent); }
  #search { min-width:200px; }
  .chip { border-radius:14px; }
  .chip.active { background:var(--accent); color:#fff; border-color:var(--accent); }
  #count { color:var(--mut); font-size:12px; font-weight:400; }
  .grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(150px,1fr));
          gap:12px; padding:16px; }
  .card { border:1px solid var(--line); border-radius:10px; background:var(--card);
          padding:8px; display:flex; flex-direction:column; gap:4px; }
  .card.off { opacity:.5; }
  .thumb { width:100%; aspect-ratio:1; object-fit:contain; border-radius:6px;
           background:conic-gradient(#0002 25%,#0000 0 50%,#0002 0 75%,#0000 0) 0 0/16px 16px; }
  .thumb.sm { width:34px; height:34px; }
  .thumb.ph { display:flex; align-items:center; justify-content:center; color:var(--mut);
              font-size:11px; border:1px dashed var(--line); }
  .name { font-weight:600; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .sub, .meta { color:var(--mut); font-size:12px; }
  .badge { font-size:11px; color:var(--accent); min-height:14px; }
  .sw { display:flex; align-items:center; gap:6px; font-size:12px; }
  .acts { display:flex; gap:6px; margin-top:2px; }
  .acts button { flex:1; padding:3px 0; font-size:12px; }
  table.list { width:100%; border-collapse:collapse; }
  table.list th, table.list td { padding:6px 8px; border-bottom:1px solid var(--line);
           text-align:left; white-space:nowrap; }
  table.list th[data-sort] { cursor:pointer; user-select:none; }
  table.list tr.off { opacity:.5; }
  td.nm, .nm { font-weight:600; }
  .hidden { display:none; }
</style>
</head>
<body>
<header>
  <h1>Trait Dashboard <span id="count"></span></h1>
  <select id="network" title="network">
    <option value="mainnet">mainnet</option>
    <option value="testnet">testnet</option>
  </select>
  <span>
    <button id="view-grid" class="active">&#9638; Grid</button>
    <button id="view-list">&#9776; List</button>
  </span>
  <button id="sync" title="Insert floor rows for newly-added layer art">Sync from layers</button>
</header>
<div class="filters">
  <input id="search" placeholder="Search trait…" autocomplete="off">
  <select id="f-body"><option value="">All bodies</option></select>
  <select id="f-category"><option value="">All categories</option></select>
  <span id="status-chips">
    <button class="chip active" data-status="all">All</button>
    <button class="chip" data-status="enabled">Enabled</button>
    <button class="chip" data-status="disabled">Disabled</button>
    <button class="chip" data-status="boosted">Boosted</button>
    <button class="chip" data-status="problems">Problems</button>
  </span>
</div>
<div id="grid" class="grid"></div>
<table id="list" class="list hidden">
  <thead><tr>
    <th></th>
    <th data-sort="trait">Trait</th>
    <th data-sort="body">Body</th>
    <th data-sort="category">Category</th>
    <th data-sort="live_count">n</th>
    <th data-sort="share">Share</th>
    <th data-sort="weight">Weight</th>
    <th data-sort="boost_status">Boost</th>
    <th>On</th>
    <th></th>
  </tr></thead>
  <tbody></tbody>
</table>
<script>
const DEFAULT_NETWORK = "__DEFAULT_NETWORK__";
const $ = (id) => document.getElementById(id);
const $net=$("network"), $search=$("search"), $fbody=$("f-body"), $fcat=$("f-category"),
      $grid=$("grid"), $list=$("list"), $ltbody=$list.querySelector("tbody"),
      $count=$("count"), $sync=$("sync"), $vgrid=$("view-grid"), $vlist=$("view-list");

let allRows=[], view="grid", activeStatus="all", sortKey=null, sortDir=1;
$net.value = DEFAULT_NETWORK === "testnet" ? "testnet" : "mainnet";
let network = $net.value;

function el(tag, props={}, kids=[]) {
  const e=document.createElement(tag);
  for (const [k,v] of Object.entries(props)) {
    if (k==="class") e.className=v;
    else if (k==="text") e.textContent=v;
    else if (k.startsWith("on") && typeof v==="function") e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v);
  }
  for (const kid of kids) e.append(kid);
  return e;
}
const imgUrl = (r) => "/img?body="+encodeURIComponent(r.body)+"&category="+
      encodeURIComponent(r.category)+"&value="+encodeURIComponent(r.trait);
const badge = (r) => (r.boost_status==="—" ? "" : r.boost_status);

function thumb(r, cls) {
  const img=el("img",{class:"thumb"+(cls||""), loading:"lazy", alt:r.trait, src:imgUrl(r)});
  img.addEventListener("error", () =>
    img.replaceWith(el("div",{class:"thumb"+(cls||"")+" ph", text:(cls?"—":"no art")})));
  return img;
}
function toggleBox(r) {
  const chk=el("input",{type:"checkbox"});
  chk.checked=r.enabled;
  chk.addEventListener("change", () => doToggle(r, chk.checked));
  return chk;
}
function actions(r) {
  return [el("button",{text:"Boost", onclick:()=>doBoost(r)}),
          el("button",{text:"Floor", onclick:()=>doFloor(r)})];
}
function card(r) {
  return el("div",{class:"card"+(r.enabled?"":" off")},[
    thumb(r,""),
    el("div",{class:"name", title:r.trait, text:r.trait}),
    el("div",{class:"sub", text:r.body+" · "+r.category}),
    el("div",{class:"meta", text:"n="+r.live_count+" · "+r.share.toFixed(1)+"% · w"+r.weight.toFixed(3)}),
    el("div",{class:"badge", text:badge(r)}),
    el("label",{class:"sw"},[toggleBox(r), el("span",{text:r.enabled?"on":"off"})]),
    el("div",{class:"acts"}, actions(r)),
  ]);
}
function listRow(r) {
  return el("tr",{class:r.enabled?"":"off"},[
    el("td",{},[thumb(r," sm")]),
    el("td",{class:"nm", text:r.trait}),
    el("td",{text:r.body}),
    el("td",{text:r.category}),
    el("td",{text:String(r.live_count)}),
    el("td",{text:r.share.toFixed(1)+"%"}),
    el("td",{text:r.weight.toFixed(3)}),
    el("td",{text:badge(r)||"—"}),
    el("td",{},[toggleBox(r)]),
    el("td",{}, actions(r)),
  ]);
}
function filtered() {
  const q=$search.value.trim().toLowerCase(), b=$fbody.value, c=$fcat.value, st=activeStatus;
  return allRows.filter(r => {
    if (b && r.body!==b) return false;
    if (c && r.category!==c) return false;
    if (q && !r.trait.toLowerCase().includes(q)) return false;
    if (st==="enabled" && !r.enabled) return false;
    if (st==="disabled" && r.enabled) return false;
    if (st==="boosted" && (r.boost_status==="—"||r.boost_status==="finished")) return false;
    if (st==="problems" && !(!r.enabled || r.live_count===0 || !r.has_image)) return false;
    return true;
  });
}
function sortRows(rows) {
  if (!sortKey) return rows;
  return [...rows].sort((a,b) => {
    const x=a[sortKey], y=b[sortKey];
    if (typeof x==="string") return x.localeCompare(y)*sortDir;
    return ((x||0)-(y||0))*sortDir;
  });
}
function render() {
  const rows=filtered();
  $count.textContent = "("+rows.length+")";
  if (view==="grid") $grid.replaceChildren(...rows.map(card));
  else $ltbody.replaceChildren(...sortRows(rows).map(listRow));
}
function fillSelect(sel, values, allLabel) {
  const cur=sel.value;
  sel.replaceChildren(el("option",{value:"", text:allLabel}));
  for (const v of values) sel.append(el("option",{value:v, text:v}));
  if ([...sel.options].some(o=>o.value===cur)) sel.value=cur;
}
async function load() {
  try {
    const r=await fetch("/api/traits?network="+encodeURIComponent(network));
    const data=await r.json();
    allRows=data.rows||[];
    fillSelect($fbody, data.bodies||[], "All bodies");
    fillSelect($fcat, data.categories||[], "All categories");
    render();
  } catch (e) { alert("Load failed: "+e); }
}
async function post(path, bodyObj) {
  const r=await fetch(path,{method:"POST", headers:{"Content-Type":"application/json"},
                            body:JSON.stringify(bodyObj)});
  if (!r.ok) { const e=await r.json().catch(()=>({})); alert("Error: "+(e.error||r.status)); return null; }
  return r.json();
}
function replaceRow(u) {
  const i=allRows.findIndex(x=>x.body===u.body&&x.category===u.category&&x.trait===u.trait);
  if (i>=0) allRows[i]=u;
  render();
}
async function doToggle(r, enabled) {
  if (!enabled && !confirm("Disable "+r.body+"/"+r.category+"/"+r.trait+"?")) { render(); return; }
  const u=await post("/api/toggle",{network, body:r.body, category:r.category, trait:r.trait, enabled});
  if (u) replaceRow(u); else render();
}
async function doBoost(r) {
  const initial=parseFloat(prompt("Boost multiplier (e.g. 7):", r.boost_initial||7));
  if (!initial) return;
  const step=parseInt(prompt("Step hours (decays -1x per window):", r.boost_step_hours||24),10);
  if (!step) return;
  if (!confirm("Arm "+initial+"x boost on "+r.trait+"?")) return;
  const u=await post("/api/boost",{network, body:r.body, category:r.category, trait:r.trait, initial, step_hours:step});
  if (u) replaceRow(u);
}
async function doFloor(r) {
  const floor=parseFloat(prompt("Floor weight (0-1):", r.floor_weight));
  if (isNaN(floor)) return;
  if (!confirm("Set floor "+floor+" on "+r.trait+"?")) return;
  const u=await post("/api/floor",{network, body:r.body, category:r.category, trait:r.trait, floor});
  if (u) replaceRow(u);
}
async function doSync() {
  if (!confirm("Insert floor rows for any newly-added layer art on "+network+"?")) return;
  const u=await post("/api/sync",{network});
  if (u) { alert("Inserted "+u.inserted+" rows"); load(); }
}
function setView(v) {
  view=v;
  $grid.classList.toggle("hidden", v!=="grid");
  $list.classList.toggle("hidden", v!=="list");
  $vgrid.classList.toggle("active", v==="grid");
  $vlist.classList.toggle("active", v==="list");
  render();
}
$search.addEventListener("input", render);
$fbody.addEventListener("change", render);
$fcat.addEventListener("change", render);
$net.addEventListener("change", () => { network=$net.value; load(); });
$vgrid.addEventListener("click", () => setView("grid"));
$vlist.addEventListener("click", () => setView("list"));
$sync.addEventListener("click", doSync);
document.querySelectorAll("#status-chips .chip").forEach(ch =>
  ch.addEventListener("click", () => {
    activeStatus=ch.dataset.status;
    document.querySelectorAll("#status-chips .chip").forEach(x=>x.classList.toggle("active", x===ch));
    render();
  }));
document.querySelectorAll("#list th[data-sort]").forEach(th =>
  th.addEventListener("click", () => {
    const k=th.dataset.sort;
    if (sortKey===k) sortDir=-sortDir; else { sortKey=k; sortDir=1; }
    render();
  }));
load();
</script>
</body>
</html>"""


async def handle_index(request: web.Request) -> web.Response:
    html = INDEX_HTML.replace("__DEFAULT_NETWORK__", request.app[DEFAULT_NETWORK])
    return web.Response(text=html, content_type="text/html")


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
    data = await _json(request)
    _require(data, "network", "body", "category", "trait")
    if not isinstance(data.get("enabled"), bool):
        raise _BadInput("enabled must be a boolean")
    row = apply_toggle(
        data["network"], data["body"], data["category"], data["trait"], data["enabled"]
    )
    return web.json_response(row)


async def handle_boost(request: web.Request) -> web.Response:
    data = await _json(request)
    _require(data, "network", "body", "category", "trait")
    initial = _num(data, "initial", 1, 100)
    step_hours = _num(data, "step_hours", 1, 100000)
    row = apply_boost(
        data["network"], data["body"], data["category"], data["trait"], initial, int(step_hours)
    )
    return web.json_response(row)


async def handle_floor(request: web.Request) -> web.Response:
    data = await _json(request)
    _require(data, "network")
    floor = _num(data, "floor", 0, 1)
    trait = data.get("trait")
    if trait is not None:  # per-trait floor needs the full key
        _require(data, "body", "category")
    row = apply_floor(data["network"], data.get("body"), data.get("category"), trait, floor)
    return web.json_response(row)


async def handle_sync(request: web.Request) -> web.Response:
    data = await _json(request)
    _require(data, "network")
    inserted = sync_layers(data["network"])
    return web.json_response({"inserted": inserted})


async def handle_img(request: web.Request) -> web.StreamResponse:
    path = resolve_image(
        request.query.get("body", ""),
        request.query.get("category", ""),
        request.query.get("value", ""),
    )
    if not path:
        return web.json_response({"error": "not found"}, status=404)
    return web.FileResponse(path)


def create_app(default_network: str = "mainnet") -> web.Application:
    app = web.Application(middlewares=[_error_mw])
    app[DEFAULT_NETWORK] = default_network
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/traits", handle_traits)
    app.router.add_get("/img", handle_img)
    app.router.add_post("/api/toggle", handle_toggle)
    app.router.add_post("/api/boost", handle_boost)
    app.router.add_post("/api/floor", handle_floor)
    app.router.add_post("/api/sync", handle_sync)
    return app


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Standalone rarity admin dashboard (local-only)")
    parser.add_argument(
        "--network", default=config.XRPL_NETWORK, help="default network shown on load"
    )
    parser.add_argument(
        "--host", default="127.0.0.1", help="bind host (loopback by default; reach via SSH tunnel)"
    )
    parser.add_argument("--port", type=int, default=8890, help="bind port (default 8890)")
    args = parser.parse_args()
    print(f"Trait Dashboard → http://{args.host}:{args.port}  (default network: {args.network})")
    web.run_app(create_app(args.network), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
