# Standalone Rarity Admin Dashboard — Design (v1)

**Date:** 2026-07-13
**Status:** Approved (design)
**Related:** #39 (trait_config.yaml authoring — explicitly OUT of scope here), the
variable-rarity engine (`lfg_core/rarity.py`), the existing Discord `/admin`
rarity buttons, and the `scripts/rarity_admin.py` CLI.

## Problem

Rarity administration already exists as *capabilities* — `lfg_core/rarity.py`
exposes `get_odds` / `arm_boost` / `set_enabled` / `set_floor`, surfaced through
the `scripts/rarity_admin.py` CLI and three Discord `/admin` buttons (View Odds,
Boost Trait, Toggle Trait). What is missing is a **dashboard**: the Discord
buttons are *blind modals* — to disable a trait you must already know and type
its exact `body` / `category` / `trait` strings, and to read odds you run "View
Odds" for one body+category at a time. There is no at-a-glance view of every
trait — its art, live count, share, effective weight, boost state, enabled flag —
with the ability to act on it directly.

## Goal

A **standalone, local admin dashboard** — not bolted onto the Activity, Discord
bot, or `lfg_service` — that shows the trait art alongside its live rarity state
and lets an operator toggle traits on/off, arm/re-arm boosts, and set floors,
with changes taking effect on the next mint **without a restart**.

## Non-Goals (v1)

- **`trait_config.yaml` authoring** (exclusions / inclusions / cross-body
  affinity / z-order). That is issue #39's remaining scope. It caches forever
  in-process (needs a `pm2 restart` to apply) and needs a whole safety pipeline
  (round-trip YAML, validate-before-write, satisfiability gate). Revisit as v2.
- **Any on-chain action** (mint, burn, offer). The dashboard only mutates the
  local `trait_rarity` SQLite table. Burns stay in Discord `/admin`.
- **Public exposure.** The tool binds to loopback by default and is never placed
  behind the public Tailscale Funnel. It introduces no new web-facing auth
  surface (which is exactly why the deferred public admin panel in
  `2026-07-05-web-ui-rescope-design.md` §5 does not apply — that deferral was
  about burn buttons on the public host; this is a local ops tool like every
  other `scripts/*.py`).

## Architecture

### Shape

`scripts/trait_dashboard.py` — a standalone `aiohttp` application. `aiohttp` is
already a project dependency (it backs `lfg_service`); **no new dependencies**.
The script:

```
.venv/bin/python scripts/trait_dashboard.py [--network mainnet] [--port 8890] [--host 127.0.0.1]
```

- `--network` sets the **default** network shown on load (default `mainnet`);
  the UI can switch networks live.
- `--host` defaults to `127.0.0.1` (loopback). Reach it via
  `ssh -L 8890:localhost:8890 <server>` then open `http://localhost:8890`, or
  pass a tailnet IP for direct private access. **Never** `0.0.0.0` on a
  public interface.
- `--port` defaults to `8890` (distinct from the Activity's `8176`).

It serves one self-contained HTML page (inline CSS + JS, no build step, no
external assets) plus a small set of JSON endpoints and one image endpoint.

### Reuse — the engine is the single source of truth

All reads and writes go through the **existing** `lfg_core.rarity` functions —
the dashboard adds **zero** new rarity logic:

| Dashboard action        | `lfg_core.rarity` call                                        |
|-------------------------|---------------------------------------------------------------|
| read rows for a network | direct `SELECT` over `trait_rarity` (mirrors `get_odds`' own query — its 5-tuple folds `enabled` into `status` and omits `floor_weight`/`boost_*`, so the dashboard needs the raw columns) |
| compute weight/status   | `effective_weight(...)`, `boost_status(...)` — the exact functions `get_odds` uses, so numbers match the picker |
| toggle on/off           | `set_enabled(conn, body, category, trait, enabled, network=net)` |
| arm / re-arm boost      | `arm_boost(conn, body, category, trait, network=net, boost_initial=, boost_step_hours=)` |
| set floor               | `set_floor(conn, floor, network=net, body=, category=, trait=)` |
| sync new layer art      | `_ensure_rows(...)` for every (body, category, value) in the layer store |

Because `weighted_pick` reads `trait_rarity` live on every mint, edits apply on
the next mint with **no restart** — the key advantage over `trait_config.yaml`.

### Network resolution

`lfg_core/db_path.app_db_path(net)` resolves the app DB file: `lfg_nfts.db` for
mainnet, `lfg_nfts_<net>.db` otherwise. Each `trait_rarity` row *also* carries a
`network` column. So a network switch flips **both**: the connection opens
`app_db_path(net)` and every rarity call passes `network=net`. The dashboard
resolves the DB path per selected network on each request (it does **not** rely
on the process's frozen `config.DB_PATH`), so one running process serves both
networks correctly.

### Image resolution

Thumbnails are served by `GET /img` from the process-wide layer store
(`layer_store.get_layer_store()`, `LocalLayerStore` under `LAYER_SOURCE=local`):

- Concrete body (`ape`/`female`/`male`/`milady`/`skeleton`):
  `layer_store.resolve(body, category, value)` returns the real `.png`/`.gif`
  (checks the body dir then `shared/`). The rarity `category` name *is* the
  layer-tree trait-type directory name, so no mapping is required.
- `body == "*"` rows (legacy/ungendered): attempt `resolve` against each concrete
  body in turn; first hit wins.
- The reserved **`Body Type`** category (whose "traits" are body-class names that
  weight the body pick, not art files) and any unresolvable value: the endpoint
  returns a 404, and the client renders a labeled placeholder tile — flagged,
  never hidden.

## Data flow

### `GET /api/traits`

Query params: `network` (default from `--network`), optional `body`, `category`,
`q` (case-insensitive substring over trait name), `status`
(`all|enabled|disabled|boosted|problems`).

Returns JSON `{network, rows: [...], bodies: [...], categories: [...]}` where
each row is:

```json
{
  "body": "ape", "category": "Eyes", "trait": "Laser",
  "live_count": 142, "share": 8.1, "weight": 0.081,
  "enabled": true, "boost_status": "—",
  "floor_weight": 0.005,
  "boost_initial": null, "boost_step_hours": 24, "boost_started_at": null,
  "has_image": true
}
```

- All `trait_rarity` rows for the network are `SELECT`ed once, grouped in Python
  by `(body, category)`; per group `category_total = sum(live_count)` and
  `population_size = len(group)`. Each row's `share = live_count/total*100` and
  `weight = effective_weight(live_count, total, floor, boost_initial,
  boost_step_hours, boost_started_at, now, population_size=population)` —
  identical math to `get_odds` / `weighted_pick`, so the numbers equal what the
  picker uses. `boost_status` comes from `rarity.boost_status(...)`.
- `bodies` and `categories` are the distinct values present, for populating the
  filter dropdowns.
- `has_image` is a cheap `resolve(...) is not None` check so the grid can show a
  placeholder without a failed image request. (The `problems` status filter =
  rows that are disabled OR `live_count == 0` OR `has_image == false`.)

### Mutations

- `POST /api/toggle` `{network, body, category, trait, enabled: bool}` → `set_enabled`.
- `POST /api/boost` `{network, body, category, trait, initial: float, step_hours: int}` → `arm_boost`.
- `POST /api/floor` `{network, body, category, trait|null, floor: float}` →
  `set_floor` (null trait = global for the network).
- `POST /api/sync` `{network}` → scan the layer store and `_ensure_rows` a
  floor-weight row for every `(body, category, value)` not yet in `trait_rarity`
  (so newly-added art appears without waiting for a mint). Reuses the same
  layer-scan shape as `rarity_admin.py`'s `scan_layer_values`.

Each mutation:
1. Opens a fresh connection to `app_db_path(network)`, calls `ensure_schema`,
   performs the op, closes the connection.
2. Appends one line to an **audit log** `reports/trait_dashboard_audit.log`
   (gitignored): ISO-8601 timestamp, network, action, body/category/trait, and
   the change (`old → new` where knowable — e.g. `enabled: 1 → 0`).
3. Returns the **re-read** row(s) so the client refreshes in place.

Every mutation validates its inputs server-side (known action, numeric ranges:
`floor` in `[0, 1]`, `initial` in `[1, 100]`, `step_hours` >= 1) and returns
HTTP 400 with a JSON `{error}` on bad input; a `rarity` `ValueError` (e.g.
`arm_boost` on a missing row) becomes a 404 `{error}`.

## UI

Single page, inline CSS/JS. Header: title, **network selector**, **Grid ⇄ List**
view toggle, and a **Sync from layers** button. Below: a **search box** (`q`),
**Body** dropdown, **Category** dropdown, and **Status** chips
(All · Enabled · Disabled · Boosted · Problems).

- **Grid view** — thumbnail cards: art (or placeholder), trait name, `n` /
  share% / weight, boost badge, an on/off switch, and Boost / Floor affordances.
- **List view** — dense table: small thumbnail + Trait / Body / Category / n /
  share / weight / boost / on-off, with **click-to-sort** column headers.

Search + Body/Category/Status filtering happen **client-side** over the loaded
row set for instant response; changing the **network** re-fetches. After any
mutation the affected row(s) update from the server's response. Destructive-ish
actions (disable, boost, floor) show a small inline confirm before firing
(toggling back on needs no confirm).

## Testing

`tests/test_trait_dashboard.py`, using `aiohttp`'s test utilities (the pattern
already in `tests/test_event_endpoints.py`) and a temp SQLite DB seeded via
`rarity.ensure_schema` + a handful of `trait_rarity` rows. The test module copies
the standard env-guard preamble (`XUMM_*`, `SEED`, `TOKEN_*`, `BUNNY_*`,
`LAYER_SOURCE=local`, `BUNNY_PULL_ZONE`) so module-level config constants freeze
correctly under full-suite collection order.

Coverage:
- `GET /api/traits` returns the seeded rows with correct `share` / `weight` /
  `boost_status` / `enabled`, and the `bodies` / `categories` lists.
- `q`, `body`, `category`, `status` params narrow the result set (incl. the
  `problems` lens: disabled OR zero-count OR missing-image).
- `POST /api/toggle` flips `enabled` and the change is visible on re-read;
  likewise `POST /api/boost` (arms a dormant boost) and `POST /api/floor`
  (per-trait and global).
- The `network` param routes reads/writes to the correct DB file (seed two temp
  DBs, assert isolation).
- `arm_boost` on a nonexistent row → 404; out-of-range numeric input → 400.
- `GET /img` streams a real layer file from a temp `layers/` tree and returns
  404 for a missing value (placeholder path). `POST /api/sync` inserts
  floor-weight rows for layer values absent from `trait_rarity`.
- Every mutation appends an audit-log line.

## Boundaries recap

- Standalone `scripts/trait_dashboard.py`; no changes to `lfg_service`, the
  Discord bot, the Telegram surface, or the Activity client.
- No new runtime dependencies.
- Loopback-bound by default; no on-chain writes; local audit log; instant effect
  (no restart). `trait_config.yaml` authoring remains #39 / a possible v2.
