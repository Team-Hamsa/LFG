# Rarity Admin Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship `scripts/trait_dashboard.py` — a standalone, loopback-bound aiohttp dashboard over the existing `lfg_core.rarity` engine that shows every trait's art + live odds and lets an operator toggle enable/disable, arm boosts, and set floors, with instant effect and no restart.

**Architecture:** One standalone aiohttp app. A thin data layer wraps the existing `lfg_core.rarity` functions (no new rarity logic) and resolves the per-network DB via `lfg_core.db_path.app_db_path`. HTTP handlers validate input, call the data layer, append an audit line, and return re-read rows. One embedded self-contained HTML page (inline CSS/JS) renders grid + list views with client-side search/filter. Images stream from `lfg_core.layer_store`.

**Tech Stack:** Python 3, aiohttp (already a dep), sqlite3, `lfg_core.rarity` / `lfg_core.db_path` / `lfg_core.layer_store` / `lfg_core.config`. No new dependencies.

## Global Constraints

- **No new runtime dependencies.** aiohttp only.
- **Reuse the engine.** All rarity reads/writes go through `lfg_core.rarity` (`effective_weight`, `boost_status`, `set_enabled`, `arm_boost`, `set_floor`, `_ensure_rows`). The dashboard adds zero rarity math.
- **Network correctness.** Every request resolves its DB via `app_db_path(network)` and passes `network=<net>` to rarity calls. Never rely on the process's frozen `config.DB_PATH`.
- **Loopback default.** `--host` defaults to `127.0.0.1`. No on-chain actions.
- **Audit every mutation** to `reports/trait_dashboard_audit.log` (dir gitignored; create with `os.makedirs("reports", exist_ok=True)`).
- **Test env-guard preamble.** `tests/test_trait_dashboard.py` must set, before importing anything that pulls `lfg_core.config`: `XUMM_API_KEY`, `XUMM_API_SECRET`, `SEED`, `TOKEN_ISSUER_ADDRESS`, `TOKEN_CURRENCY_HEX`, `BUNNY_CDN_ACCESS_KEY`, `BUNNY_CDN_STORAGE_ZONE`, `LAYER_SOURCE=local`, `BUNNY_PULL_ZONE` (copy the block from `tests/test_event_endpoints.py:1-14`).
- **mypy clean.** The project runs mypy in the pre-push gate; `scripts/` is currently excluded (`pyproject.toml` `exclude = [..., "^scripts/"]`), so `scripts/trait_dashboard.py` is not type-gated, but the test file IS. Keep test types clean.

## File Structure

- Create: `scripts/trait_dashboard.py` — data layer + aiohttp handlers + embedded HTML + argparse `main()`.
- Create: `tests/test_trait_dashboard.py` — endpoint + data-layer tests (aiohttp test utils).
- Modify: `CLAUDE.md` — one short subsection documenting the tool (run command, ssh-tunnel reach, scope).
- No `.gitignore` change (`reports/` already ignored).

### `scripts/trait_dashboard.py` public shape (Produces, for later tasks/tests)

```python
DB_FOR = app_db_path  # via lfg_core.db_path

def fetch_rows(network: str, *, db_path: str | None = None,
               body: str | None = None, category: str | None = None,
               q: str | None = None, status: str = "all",
               now: datetime | None = None) -> dict:
    """Return {"network", "rows": [row...], "bodies": [...], "categories": [...]}.
    Each row: body, category, trait, live_count, share, weight, enabled,
    boost_status, floor_weight, boost_initial, boost_step_hours,
    boost_started_at, has_image. Filters applied server-side."""

def apply_toggle(network, body, category, trait, enabled, *, db_path=None) -> dict   # re-read row
def apply_boost(network, body, category, trait, initial, step_hours, *, db_path=None) -> dict
def apply_floor(network, body, category, trait, floor, *, db_path=None) -> dict  # trait None = global
def sync_layers(network, *, db_path=None) -> int   # number of rows inserted
def resolve_image(body, category, value) -> str | None   # local file path or None
def audit(network, action, body, category, trait, detail) -> None

def create_app(default_network: str = "mainnet") -> web.Application
def main() -> None   # argparse --network/--port/--host, web.run_app
```

Handlers (routes registered in `create_app`):
`GET /` (index HTML) · `GET /api/traits` · `POST /api/toggle` · `POST /api/boost` · `POST /api/floor` · `POST /api/sync` · `GET /img`.

---

### Task 1: Row fetching (`fetch_rows`) — the core read

**Files:**
- Create: `scripts/trait_dashboard.py` (data-layer portion)
- Test: `tests/test_trait_dashboard.py`

**Interfaces:**
- Consumes: `lfg_core.rarity.ensure_schema`, `effective_weight`, `boost_status`; `lfg_core.db_path.app_db_path`; `lfg_core.layer_store.get_layer_store().resolve`.
- Produces: `fetch_rows(network, *, db_path, body, category, q, status, now)` returning the `{network, rows, bodies, categories}` dict described above.

**Logic:** open `sqlite3.connect(db_path or app_db_path(network))`; `ensure_schema`; `SELECT body, category, trait, live_count, floor_weight, boost_initial, boost_step_hours, boost_started_at, enabled FROM trait_rarity WHERE network=?`. Group rows by `(body, category)`; per group `total = sum(live_count)`, `population = len(group)`. Per row: `share = live_count/total*100 if total else 0`; `weight = effective_weight(live_count, total, floor_weight, boost_initial, boost_step_hours, boost_started_at, now, population_size=population)`; `boost_status = rarity.boost_status(boost_initial, boost_step_hours, boost_started_at, now)`; `has_image = resolve_image(body, category, trait) is not None`. Apply filters: `body`/`category` exact; `q` case-insensitive substring on `trait`; `status`: `enabled`→enabled==1, `disabled`→enabled==0, `boosted`→boost_status not in ("—","finished"), `problems`→(enabled==0 or live_count==0 or not has_image). `bodies`/`categories` = sorted distinct over the unfiltered network set.

- [ ] **Step 1: Write the failing test**

```python
def test_fetch_rows_computes_share_weight_and_status(tmp_path):
    from scripts import trait_dashboard as td
    db = str(tmp_path / "m.db")
    _seed(db, "mainnet", [
        ("ape", "Eyes", "Laser", 3, 1),
        ("ape", "Eyes", "Star", 1, 1),
        ("ape", "Eyes", "Off", 0, 0),      # disabled, zero-count
    ])
    out = td.fetch_rows("mainnet", db_path=db)
    rows = {r["trait"]: r for r in out["rows"]}
    assert rows["Laser"]["live_count"] == 3
    assert round(rows["Laser"]["share"], 1) == 75.0          # 3/4
    assert rows["Laser"]["weight"] > rows["Star"]["weight"]  # proportional
    assert rows["Off"]["enabled"] is False
    assert out["bodies"] == ["ape"] and out["categories"] == ["Eyes"]
```

with a `_seed(db, network, rows)` helper that `rarity.ensure_schema`s and inserts `trait_rarity` rows (`enabled` from the tuple's last field, floor `0.005`).

- [ ] **Step 2: Run test to verify it fails** — `pytest tests/test_trait_dashboard.py::test_fetch_rows_computes_share_weight_and_status -v` → FAIL (module/attr missing).
- [ ] **Step 3: Implement `resolve_image`, `fetch_rows`, module imports** (data layer only; no HTTP yet).
- [ ] **Step 4: Run test to verify it passes.**
- [ ] **Step 5: Write the filter test**

```python
def test_fetch_rows_filters(tmp_path):
    from scripts import trait_dashboard as td
    db = str(tmp_path / "m.db")
    _seed(db, "mainnet", [
        ("ape", "Eyes", "Laser", 3, 1), ("ape", "Eyes", "Star", 1, 1),
        ("male", "Hat", "Crown", 2, 1), ("ape", "Eyes", "Off", 0, 0),
    ])
    assert {r["trait"] for r in td.fetch_rows("mainnet", db_path=db, q="la")["rows"]} == {"Laser"}
    assert {r["trait"] for r in td.fetch_rows("mainnet", db_path=db, body="male")["rows"]} == {"Crown"}
    assert {r["trait"] for r in td.fetch_rows("mainnet", db_path=db, status="disabled")["rows"]} == {"Off"}
    assert "Off" in {r["trait"] for r in td.fetch_rows("mainnet", db_path=db, status="problems")["rows"]}
```

- [ ] **Step 6: Run → fail → implement filters → pass.**
- [ ] **Step 7: Commit** — `git add scripts/trait_dashboard.py tests/test_trait_dashboard.py && git commit -m "feat(dashboard): rarity row fetch + filtering"`

---

### Task 2: `GET /api/traits` endpoint + index route

**Files:** Modify `scripts/trait_dashboard.py` (add `create_app`, `handle_traits`, `handle_index`, embedded HTML constant); Test `tests/test_trait_dashboard.py`.

**Interfaces:**
- Consumes: `fetch_rows` (Task 1).
- Produces: `create_app(default_network="mainnet") -> web.Application` with routes `GET /` and `GET /api/traits`. `handle_traits` reads `network` (default `app["default_network"]`) + optional `body`/`category`/`q`/`status` from `request.query`, calls `fetch_rows`, returns `web.json_response`.

- [ ] **Step 1: Write the failing test** (aiohttp TestClient, mirroring `tests/test_event_endpoints.py`'s loop pattern):

```python
def test_api_traits_returns_rows(tmp_path, monkeypatch):
    from scripts import trait_dashboard as td
    db = str(tmp_path / "m.db")
    _seed(db, "mainnet", [("ape", "Eyes", "Laser", 3, 1)])
    monkeypatch.setattr(td, "app_db_path", lambda net: db)
    async def body():
        from aiohttp.test_utils import TestClient, TestServer
        async with TestClient(TestServer(td.create_app())) as c:
            r = await c.get("/api/traits?network=mainnet")
            assert r.status == 200
            data = await r.json()
            assert data["rows"][0]["trait"] == "Laser"
    _run(body)   # new_event_loop helper as in test_event_endpoints
```

- [ ] **Step 2: Run → fail.**
- [ ] **Step 3: Implement `create_app` + `handle_traits` + a minimal `handle_index`** returning `web.Response(text=INDEX_HTML, content_type="text/html")` where `INDEX_HTML` for now is a stub containing `"Trait Dashboard"` (full UI lands in Task 6). Register routes.
- [ ] **Step 4: Run → pass.**
- [ ] **Step 5: Index smoke test** — `GET /` returns 200 and body contains `"Trait Dashboard"`. Run → implement (already) → pass.
- [ ] **Step 6: Commit** — `feat(dashboard): /api/traits + index route`

---

### Task 3: Toggle mutation + audit log

**Files:** Modify `scripts/trait_dashboard.py` (add `audit`, `apply_toggle`, `handle_toggle`); Test.

**Interfaces:**
- Consumes: `lfg_core.rarity.set_enabled`, `fetch_rows` (to re-read the one row).
- Produces: `apply_toggle(network, body, category, trait, enabled, *, db_path) -> dict` (the re-read row); `audit(network, action, body, category, trait, detail)`; `POST /api/toggle` handler validating JSON body `{network, body, category, trait, enabled}`.

**Logic:** `apply_toggle` reads current `enabled`, calls `rarity.set_enabled(...)`, appends audit `enabled: <old> -> <new>`, returns the single re-read row via `fetch_rows(...)` filtered to that trait. `audit` opens `reports/trait_dashboard_audit.log` in append mode (`makedirs` first) and writes `<iso-ts>\t<network>\t<action>\t<body>/<category>/<trait>\t<detail>\n` (timestamp from `rarity.utcnow().isoformat()`).

- [ ] **Step 1: Write the failing test**

```python
def test_toggle_flips_enabled_and_audits(tmp_path, monkeypatch):
    from scripts import trait_dashboard as td
    db = str(tmp_path / "m.db")
    _seed(db, "mainnet", [("ape", "Eyes", "Laser", 3, 1)])
    monkeypatch.setattr(td, "app_db_path", lambda net: db)
    monkeypatch.chdir(tmp_path)   # so reports/ lands under tmp
    async def body():
        from aiohttp.test_utils import TestClient, TestServer
        async with TestClient(TestServer(td.create_app())) as c:
            r = await c.post("/api/toggle", json={"network": "mainnet", "body": "ape",
                "category": "Eyes", "trait": "Laser", "enabled": False})
            assert r.status == 200 and (await r.json())["enabled"] is False
    _run(body)
    assert td.fetch_rows("mainnet", db_path=db)["rows"][0]["enabled"] is False
    assert (tmp_path / "reports" / "trait_dashboard_audit.log").read_text().count("Laser") == 1
```

- [ ] **Step 2: Run → fail.**  **Step 3: Implement `audit`, `apply_toggle`, `handle_toggle`, register route.**  **Step 4: Run → pass.**
- [ ] **Step 5: Commit** — `feat(dashboard): toggle enable/disable + audit log`

---

### Task 4: Boost + floor mutations

**Files:** Modify `scripts/trait_dashboard.py` (`apply_boost`, `apply_floor`, `handle_boost`, `handle_floor`); Test.

**Interfaces:**
- Consumes: `lfg_core.rarity.arm_boost`, `set_floor`.
- Produces: `apply_boost(network, body, category, trait, initial, step_hours, *, db_path)`; `apply_floor(network, body, category, trait, floor, *, db_path)` (trait `None` → global); `POST /api/boost`, `POST /api/floor`.

- [ ] **Step 1: Write the failing tests** — boost arms a dormant boost (`boost_status` becomes `"dormant"`, `boost_initial` set); per-trait floor sets `floor_weight`; global floor (`trait: null`) sets floor on every row for the network. Assert each via `fetch_rows` re-read and one audit line apiece.
- [ ] **Step 2: Run → fail.**  **Step 3: Implement both apply fns + handlers + routes.**  **Step 4: Run → pass.**
- [ ] **Step 5: Commit** — `feat(dashboard): arm boost + set floor`

---

### Task 5: Input validation (400 / 404)

**Files:** Modify handlers to validate; Test.

**Logic:** a shared `_require(body_json, *keys)` and numeric-range checks: `floor` in `[0, 1]`; `initial` in `[1, 100]`; `step_hours` >= 1; `enabled` a bool. Missing key / out-of-range / non-JSON → `web.json_response({"error": ...}, status=400)`. A `rarity.ValueError` (e.g. `arm_boost`/`apply_toggle` on a nonexistent row) → catch and return `status=404`.

- [ ] **Step 1: Write failing tests** — `POST /api/floor` with `floor=5` → 400; `POST /api/boost` on unknown trait → 404; `POST /api/toggle` missing `trait` → 400.
- [ ] **Step 2: Run → fail.**  **Step 3: Implement validation + error mapping.**  **Step 4: Run → pass.**
- [ ] **Step 5: Commit** — `feat(dashboard): server-side input validation`

---

### Task 6: `GET /img` + `POST /api/sync`

**Files:** Modify `scripts/trait_dashboard.py` (`handle_img`, `sync_layers`, `handle_sync`); Test.

**Interfaces:**
- Consumes: `lfg_core.layer_store.get_layer_store()` (`.resolve`, `.list_bodies`, `.list_trait_types`, `.list_values`), `lfg_core.rarity._ensure_rows`, `lfg_core.rarity.utcnow`.
- Produces: `GET /img?body=&category=&value=` → `web.FileResponse(path)` (content-type inferred) or 404; `sync_layers(network, *, db_path) -> int`; `POST /api/sync` `{network}` → `{"inserted": n}`.

**`sync_layers` logic:** open conn to `app_db_path(network)`; `ensure_schema`; for each body in `list_bodies()`, each trait_type in `list_trait_types(body)`, `values = list_values(body, trait_type)`; count rows not already present, then `_ensure_rows(conn, network, body, trait_type, values, utcnow())`; return inserted count.

- [ ] **Step 1: Write failing tests** — build a temp `layers/` tree (`male/Eyes/Laser.png` with a 1×1 PNG) and point `config.LAYERS_DIR`/the store at it (monkeypatch `td.get_layer_store` to a `LocalLayerStore(tmp_layers)`); `GET /img?body=male&category=Eyes&value=Laser` → 200 image bytes; missing value → 404; `sync_layers("mainnet", db_path=db)` inserts a `("male","Eyes","Laser")` row absent before.
- [ ] **Step 2: Run → fail.**  **Step 3: Implement `handle_img`, `sync_layers`, `handle_sync`, routes.**  **Step 4: Run → pass.**
- [ ] **Step 5: Commit** — `feat(dashboard): image serving + sync-from-layers`

---

### Task 7: Full embedded UI (grid + list + filters)

**Files:** Modify `scripts/trait_dashboard.py` — replace the `INDEX_HTML` stub with the complete self-contained page (inline `<style>` + `<script type="module">` or plain script).

**UI behavior (vanilla JS, no build):**
- On load: read network from a `<select>` (default injected from `app["default_network"]`), `fetch('/api/traits?network='+net)`, store `allRows`, render.
- Header: network `<select>`, Grid/List toggle buttons, "Sync from layers" button (POST `/api/sync`, then re-fetch).
- Controls: search `<input>` (`q`), Body `<select>`, Category `<select>` (populated from `bodies`/`categories`), Status chips. All filtering is client-side over `allRows` for instant response; changing network re-fetches.
- Grid: cards with `<img src="/img?body=..&category=..&value=..">` (onerror → placeholder), name, `n`/share/weight, boost badge, an on/off `<input type=checkbox>`, and Boost / Floor buttons that open a tiny inline prompt and POST. Disable/boost/floor confirm first (`confirm()` is fine here — this is a real browser over SSH, not the Discord sandboxed iframe).
- List: `<table>` with sortable headers (click cycles asc/desc), small thumbnail cell, inline on/off + boost/floor.
- After a successful mutation, replace the row in `allRows` from the JSON response and re-render.

**Testing:** JS is not unit-tested here (no browser on the server). The server contract it depends on is already covered by Tasks 1–6. Add one guard test that `GET /` returns 200 and the HTML contains the key hooks: `id="grid"`, `id="list"`, `id="search"`, `id="network"`, and the string `Sync from layers` — so an accidental template break is caught.

- [ ] **Step 1: Write the failing HTML-markers test** (asserts the five hooks above in `GET /` body).
- [ ] **Step 2: Run → fail** (stub lacks them).
- [ ] **Step 3: Implement the full `INDEX_HTML`.**
- [ ] **Step 4: Run → pass.**
- [ ] **Step 5: Manual smoke (documented, not automated):** `./.venv/bin/python scripts/trait_dashboard.py --network testnet --port 8890` then `curl -s localhost:8890/api/traits?network=testnet | head`; open over SSH tunnel and eyeball grid/list/search/toggle. Record result in the PR.
- [ ] **Step 6: Commit** — `feat(dashboard): full grid/list UI with search + filters`

---

### Task 8: `main()` (argparse) + CLAUDE.md docs

**Files:** Modify `scripts/trait_dashboard.py` (`main`, `if __name__ == "__main__"`), `CLAUDE.md`.

**`main` logic:** `argparse` `--network` (default `config.XRPL_NETWORK`), `--port` (default `8890`), `--host` (default `127.0.0.1`); `sys.path` bootstrap (`REPO_ROOT` insert, matching `scripts/rarity_admin.py:18`); `load_dotenv()`; `web.run_app(create_app(args.network), host=args.host, port=args.port)`.

- [ ] **Step 1: Write a smoke test** that `main`'s parser builds and `create_app("testnet")` yields an app whose router has `/api/traits`, `/api/toggle`, `/img` (assert route paths, like `test_event_endpoints.py::test_event_routes_registered`).
- [ ] **Step 2: Run → fail → implement `main` + route-presence → pass.**
- [ ] **Step 3: Add a `### Rarity admin dashboard` subsection to `CLAUDE.md`** under the rarity/scripts area: run command, `ssh -L` reach, loopback-only + no-on-chain + instant-effect notes, and that `trait_config.yaml` authoring is #39/out of scope.
- [ ] **Step 4: Commit** — `feat(dashboard): CLI entrypoint + docs`

---

## Final gate (before PR)

- [ ] `ruff check scripts/trait_dashboard.py tests/test_trait_dashboard.py` and `ruff format` — clean.
- [ ] `mypy tests/test_trait_dashboard.py` (scripts/ is mypy-excluded) — clean.
- [ ] `pytest tests/test_trait_dashboard.py -v` — all green; then a full `pytest` run to confirm no full-suite-order regression (env-guard preamble).
- [ ] Manual smoke recorded (Task 7 Step 5).
- [ ] Open a **draft** PR (`gh pr create --draft`), body links this spec + plan and notes it's the rarity half distinct from #39's `trait_config.yaml` half. Wait for Greptile + CodeRabbit; resolve/address findings before merge.

## Self-Review

- **Spec coverage:** grid+list ✓ (T7), search/body/category/status filters ✓ (T1/T7), toggle ✓ (T3), boost ✓ (T4), floor ✓ (T4), network selector ✓ (T1/T2/T7), images+placeholder ✓ (T6), sync ✓ (T6), audit log ✓ (T3), validation ✓ (T5), loopback/CLI ✓ (T8), no new deps ✓, no on-chain ✓.
- **Placeholders:** none — every task has concrete test code or a concrete assertion target.
- **Type/name consistency:** `fetch_rows`/`apply_toggle`/`apply_boost`/`apply_floor`/`sync_layers`/`resolve_image`/`audit`/`create_app`/`main` used identically across tasks; `app_db_path` monkeypatch seam consistent in tests.
