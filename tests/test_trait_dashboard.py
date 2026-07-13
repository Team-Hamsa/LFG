import os

# Env-guard preamble: set before any lfg_core.config import so module-level
# constants freeze with valid values even when this file is collected before
# webapp/test_smoke.py (see tests/test_event_endpoints.py).
os.environ.setdefault("XUMM_API_KEY", "test")
os.environ.setdefault("XUMM_API_SECRET", "test")
os.environ.setdefault("SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2")
os.environ.setdefault("TOKEN_ISSUER_ADDRESS", "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
os.environ.setdefault("TOKEN_CURRENCY_HEX", "4C46474F00000000000000000000000000000000")
os.environ.setdefault("BUNNY_CDN_ACCESS_KEY", "test")
os.environ.setdefault("BUNNY_CDN_STORAGE_ZONE", "test")
os.environ.setdefault("LAYER_SOURCE", "local")
os.environ.setdefault("BUNNY_PULL_ZONE", "nft.pullzone.example")

import asyncio
import sqlite3
import struct
import zlib

from lfg_core import rarity

# --- helpers ---------------------------------------------------------------


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _seed(db_path, network, rows):
    """rows: list of (body, category, trait, live_count, enabled)."""
    conn = sqlite3.connect(db_path)
    rarity.ensure_schema(conn)
    for body, cat, trait, count, enabled in rows:
        conn.execute(
            """INSERT INTO trait_rarity (network, body, category, trait,
               live_count, floor_weight, enabled) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (network, body, cat, trait, count, 0.005, enabled),
        )
    conn.commit()
    conn.close()


def _png_1x1(path):
    """Write a minimal valid 1x1 PNG (no external deps)."""

    def chunk(tag, data):
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
    raw = b"\x00\xff\x00\x00\xff"  # one filtered RGBA pixel
    idat = zlib.compress(raw)
    with open(path, "wb") as f:
        f.write(sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b""))


# --- Task 1: fetch_rows ----------------------------------------------------


def test_fetch_rows_computes_share_weight_and_status(tmp_path):
    from scripts import trait_dashboard as td

    db = str(tmp_path / "m.db")
    _seed(
        db,
        "mainnet",
        [
            ("ape", "Eyes", "Laser", 3, 1),
            ("ape", "Eyes", "Star", 1, 1),
            ("ape", "Eyes", "Off", 0, 0),  # disabled, zero-count
        ],
    )
    out = td.fetch_rows("mainnet", db_path=db)
    rows = {r["trait"]: r for r in out["rows"]}
    assert rows["Laser"]["live_count"] == 3
    assert round(rows["Laser"]["share"], 1) == 75.0  # 3 / 4
    assert rows["Laser"]["weight"] > rows["Star"]["weight"]  # proportional
    assert rows["Off"]["enabled"] is False
    assert out["bodies"] == ["ape"] and out["categories"] == ["Eyes"]


def test_fetch_rows_filters(tmp_path):
    from scripts import trait_dashboard as td

    db = str(tmp_path / "m.db")
    _seed(
        db,
        "mainnet",
        [
            ("ape", "Eyes", "Laser", 3, 1),
            ("ape", "Eyes", "Star", 1, 1),
            ("male", "Hat", "Crown", 2, 1),
            ("ape", "Eyes", "Off", 0, 0),
        ],
    )
    assert {r["trait"] for r in td.fetch_rows("mainnet", db_path=db, q="la")["rows"]} == {"Laser"}
    assert {r["trait"] for r in td.fetch_rows("mainnet", db_path=db, body="male")["rows"]} == {
        "Crown"
    }
    assert {
        r["trait"] for r in td.fetch_rows("mainnet", db_path=db, status="disabled")["rows"]
    } == {"Off"}
    assert "Off" in {
        r["trait"] for r in td.fetch_rows("mainnet", db_path=db, status="problems")["rows"]
    }


# --- Task 2: /api/traits + index -------------------------------------------


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
            assert data["bodies"] == ["ape"]

    _run(body())


# --- Task 3: toggle + audit ------------------------------------------------


def test_toggle_flips_enabled_and_audits(tmp_path, monkeypatch):
    from scripts import trait_dashboard as td

    db = str(tmp_path / "m.db")
    _seed(db, "mainnet", [("ape", "Eyes", "Laser", 3, 1)])
    monkeypatch.setattr(td, "app_db_path", lambda net: db)
    monkeypatch.chdir(tmp_path)  # keep layer resolution hermetic
    audit_log = tmp_path / "reports" / "audit.log"
    monkeypatch.setattr(td, "AUDIT_LOG", str(audit_log))

    async def body():
        from aiohttp.test_utils import TestClient, TestServer

        async with TestClient(TestServer(td.create_app())) as c:
            r = await c.post(
                "/api/toggle",
                json={
                    "network": "mainnet",
                    "body": "ape",
                    "category": "Eyes",
                    "trait": "Laser",
                    "enabled": False,
                },
            )
            assert r.status == 200
            assert (await r.json())["enabled"] is False

    _run(body())
    assert td.fetch_rows("mainnet", db_path=db)["rows"][0]["enabled"] is False
    log = audit_log.read_text()
    assert log.count("Laser") == 1


def test_index_serves_html():
    from scripts import trait_dashboard as td

    async def body():
        from aiohttp.test_utils import TestClient, TestServer

        async with TestClient(TestServer(td.create_app())) as c:
            r = await c.get("/")
            assert r.status == 200
            assert "Trait Dashboard" in await r.text()

    _run(body())


# --- Task 4: boost + floor -------------------------------------------------


def test_boost_arms_dormant(tmp_path, monkeypatch):
    from scripts import trait_dashboard as td

    db = str(tmp_path / "m.db")
    _seed(db, "mainnet", [("ape", "Eyes", "Laser", 3, 1)])
    monkeypatch.setattr(td, "app_db_path", lambda net: db)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(td, "AUDIT_LOG", str(tmp_path / "reports" / "audit.log"))

    async def body():
        from aiohttp.test_utils import TestClient, TestServer

        async with TestClient(TestServer(td.create_app())) as c:
            r = await c.post(
                "/api/boost",
                json={
                    "network": "mainnet",
                    "body": "ape",
                    "category": "Eyes",
                    "trait": "Laser",
                    "initial": 5,
                    "step_hours": 24,
                },
            )
            assert r.status == 200
            data = await r.json()
            assert data["boost_status"] == "dormant"
            assert data["boost_initial"] == 5

    _run(body())


def test_floor_per_trait_then_global(tmp_path, monkeypatch):
    from scripts import trait_dashboard as td

    db = str(tmp_path / "m.db")
    _seed(db, "mainnet", [("ape", "Eyes", "Laser", 3, 1), ("ape", "Eyes", "Star", 1, 1)])
    monkeypatch.setattr(td, "app_db_path", lambda net: db)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(td, "AUDIT_LOG", str(tmp_path / "reports" / "audit.log"))

    async def body():
        from aiohttp.test_utils import TestClient, TestServer

        async with TestClient(TestServer(td.create_app())) as c:
            r = await c.post(
                "/api/floor",
                json={
                    "network": "mainnet",
                    "body": "ape",
                    "category": "Eyes",
                    "trait": "Laser",
                    "floor": 0.02,
                },
            )
            assert r.status == 200
            assert (await r.json())["floor_weight"] == 0.02
            r2 = await c.post(
                "/api/floor", json={"network": "mainnet", "trait": None, "floor": 0.01}
            )
            assert r2.status == 200

    _run(body())
    rows = {r["trait"]: r for r in td.fetch_rows("mainnet", db_path=db)["rows"]}
    # global floor overwrote every row for the network, incl. the per-trait 0.02
    assert rows["Laser"]["floor_weight"] == 0.01
    assert rows["Star"]["floor_weight"] == 0.01


# --- Task 5: validation ----------------------------------------------------


def test_validation_errors(tmp_path, monkeypatch):
    from scripts import trait_dashboard as td

    db = str(tmp_path / "m.db")
    _seed(db, "mainnet", [("ape", "Eyes", "Laser", 3, 1)])
    monkeypatch.setattr(td, "app_db_path", lambda net: db)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(td, "AUDIT_LOG", str(tmp_path / "reports" / "audit.log"))

    async def body():
        from aiohttp.test_utils import TestClient, TestServer

        async with TestClient(TestServer(td.create_app())) as c:
            # floor out of range -> 400
            r = await c.post(
                "/api/floor",
                json={
                    "network": "mainnet",
                    "body": "ape",
                    "category": "Eyes",
                    "trait": "Laser",
                    "floor": 5,
                },
            )
            assert r.status == 400
            # boost on an unknown trait -> 404
            r = await c.post(
                "/api/boost",
                json={
                    "network": "mainnet",
                    "body": "ape",
                    "category": "Eyes",
                    "trait": "Nope",
                    "initial": 5,
                    "step_hours": 24,
                },
            )
            assert r.status == 404
            # toggle missing the trait field -> 400
            r = await c.post(
                "/api/toggle",
                json={"network": "mainnet", "body": "ape", "category": "Eyes", "enabled": False},
            )
            assert r.status == 400

    _run(body())


def test_rejects_malformed_json_types(tmp_path, monkeypatch):
    """Review follow-up: type-mismatched fields must 400, not coerce into a
    bad SQLite bind (500) or silently truncate step_hours."""
    from scripts import trait_dashboard as td

    db = str(tmp_path / "m.db")
    _seed(db, "mainnet", [("ape", "Eyes", "Laser", 3, 1)])
    monkeypatch.setattr(td, "app_db_path", lambda net: db)
    monkeypatch.setattr(td, "AUDIT_LOG", str(tmp_path / "reports" / "audit.log"))

    async def body():
        from aiohttp.test_utils import TestClient, TestServer

        async with TestClient(TestServer(td.create_app())) as c:
            # array where a string field is expected
            r = await c.post(
                "/api/toggle",
                json={
                    "network": "mainnet",
                    "body": ["ape"],
                    "category": "Eyes",
                    "trait": "Laser",
                    "enabled": False,
                },
            )
            assert r.status == 400
            # fractional step_hours must not be silently truncated
            r = await c.post(
                "/api/boost",
                json={
                    "network": "mainnet",
                    "body": "ape",
                    "category": "Eyes",
                    "trait": "Laser",
                    "initial": 5,
                    "step_hours": 1.9,
                },
            )
            assert r.status == 400
            # numeric string floor
            r = await c.post(
                "/api/floor",
                json={
                    "network": "mainnet",
                    "body": "ape",
                    "category": "Eyes",
                    "trait": "Laser",
                    "floor": "0.5",
                },
            )
            assert r.status == 400

    _run(body())


# --- Task 6: /img + /api/sync ----------------------------------------------


def test_img_serves_and_sync_inserts(tmp_path, monkeypatch):
    from lfg_core import config
    from scripts import trait_dashboard as td

    layers = tmp_path / "layers"
    (layers / "male" / "Eyes").mkdir(parents=True)
    _png_1x1(str(layers / "male" / "Eyes" / "Laser.png"))
    monkeypatch.setattr(config, "LAYERS_DIR", str(layers))
    db = str(tmp_path / "m.db")
    _seed(db, "mainnet", [])  # empty rarity table
    monkeypatch.setattr(td, "app_db_path", lambda net: db)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(td, "AUDIT_LOG", str(tmp_path / "reports" / "audit.log"))

    async def body():
        from aiohttp.test_utils import TestClient, TestServer

        async with TestClient(TestServer(td.create_app())) as c:
            r = await c.get("/img?body=male&category=Eyes&value=Laser")
            assert r.status == 200
            assert (await r.read())[:8] == b"\x89PNG\r\n\x1a\n"
            miss = await c.get("/img?body=male&category=Eyes&value=Missing")
            assert miss.status == 404
            sync = await c.post("/api/sync", json={"network": "mainnet"})
            assert sync.status == 200
            assert (await sync.json())["inserted"] >= 1

    _run(body())
    rows = {r["trait"] for r in td.fetch_rows("mainnet", db_path=db)["rows"]}
    assert "Laser" in rows


def test_img_rejects_path_traversal(tmp_path, monkeypatch):
    """Review follow-up: query components must not escape LAYERS_DIR."""
    from lfg_core import config
    from scripts import trait_dashboard as td

    layers = tmp_path / "layers"
    (layers / "male" / "Eyes").mkdir(parents=True)
    _png_1x1(str(layers / "male" / "Eyes" / "Laser.png"))
    _png_1x1(str(tmp_path / "secret.png"))  # image-extension file OUTSIDE the tree
    monkeypatch.setattr(config, "LAYERS_DIR", str(layers))

    async def body():
        from aiohttp.test_utils import TestClient, TestServer

        async with TestClient(TestServer(td.create_app())) as c:
            assert (await c.get("/img?body=male&category=Eyes&value=Laser")).status == 200
            # traversal via category must 404, never serve ../secret
            bad = await c.get("/img?body=male&category=..%2F..&value=secret")
            assert bad.status == 404
            # a bare ".." component is also refused
            bad2 = await c.get("/img?body=..&category=Eyes&value=Laser")
            assert bad2.status == 404

    _run(body())
    # the confinement is in resolve_image itself, so the data layer agrees
    assert td.resolve_image("male", "../..", "secret") is None


# --- Task 7: UI markers ----------------------------------------------------


def test_index_has_ui_hooks():
    from scripts import trait_dashboard as td

    async def body():
        from aiohttp.test_utils import TestClient, TestServer

        async with TestClient(TestServer(td.create_app("testnet"))) as c:
            r = await c.get("/")
            html = await r.text()
            for hook in (
                'id="grid"',
                'id="list"',
                'id="search"',
                'id="network"',
                "Sync from layers",
            ):
                assert hook in html, hook
            # default network injected into the page
            assert "testnet" in html

    _run(body())


# --- Task 8: routes / entrypoint -------------------------------------------


def test_all_routes_registered():
    from scripts import trait_dashboard as td

    app = td.create_app("testnet")
    paths = {r.resource.canonical for r in app.router.routes() if r.resource is not None}
    for p in ("/", "/api/traits", "/img", "/api/toggle", "/api/boost", "/api/floor", "/api/sync"):
        assert p in paths, p
