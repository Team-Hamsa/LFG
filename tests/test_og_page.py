# tests/test_og_page.py
# Env-guard preamble: importing lfg_service.app freezes lfg_core.config
# constants (e.g. IMG_PROXY_ALLOWED_BASES, LAYER_SOURCE) at import time; set
# the same defaults test_smoke.py / test_server_identity_wiring.py use so
# collection order can't strand them. (Copy the block verbatim from
# tests/test_server_identity_wiring.py — same keys/values.)
import os

os.environ.setdefault("XUMM_API_KEY", "test")
os.environ.setdefault("XUMM_API_SECRET", "test")
os.environ.setdefault("SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2")
os.environ.setdefault("TOKEN_ISSUER_ADDRESS", "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
os.environ.setdefault("TOKEN_CURRENCY_HEX", "4C46474F00000000000000000000000000000000")
os.environ.setdefault("BUNNY_CDN_ACCESS_KEY", "test")
os.environ.setdefault("BUNNY_CDN_STORAGE_ZONE", "test")
os.environ.setdefault("LAYER_SOURCE", "local")
os.environ.setdefault("BUNNY_PULL_ZONE", "nft.pullzone.example")

import asyncio  # noqa: E402
from html import escape  # noqa: E402

import pytest  # noqa: E402
from aiohttp.test_utils import make_mocked_request  # noqa: E402

from lfg_core import nft_index  # noqa: E402
from lfg_service import app as server  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_app_db(tmp_path, monkeypatch):
    """Every test in this module exercises handle_nft_card, which (since the
    #41 share-click-logging change) calls share_clicks.record_click against
    db_path.app_db_path() on every live-card render. Without this redirect,
    running the suite writes rows into the REAL lfg_nfts*.db on disk. Also
    pins SHARE_FORWARD_URL to "" by default so the suite doesn't depend on
    whatever a box's .env happens to set; tests that want it set monkeypatch
    it explicitly afterward (autouse fixtures run first, so the later
    monkeypatch.setattr in the test body wins)."""
    db = str(tmp_path / "app_clicks.db")
    monkeypatch.setattr(server.db_path, "app_db_path", lambda network=None: db)
    monkeypatch.setattr(server.config, "SHARE_FORWARD_URL", "")
    monkeypatch.setattr(server.config, "SHARE_CARD_RENDER_ENABLED", False)
    return db


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _onchain(nft_id, number, burned=False, image="https://cdn.example/img.png", attrs=None):
    return nft_index.OnchainNft(
        nft_id=nft_id,
        nft_number=number,
        owner="rOwner",
        is_burned=burned,
        mutable=True,
        uri_hex="6868",
        body="male",
        attributes=attrs if attrs is not None else [{"trait_type": "Body", "value": "Straight"}],
        image=image,
        ledger_index=100,
    )


def _seed_onchain(tmp_path, monkeypatch, records):
    db_path = str(tmp_path / "onchain.db")
    monkeypatch.setattr(server.nft_index, "index_db_path", lambda network: db_path)
    conn = nft_index.init_db(db_path)
    for rec in records:
        nft_index.upsert(conn, rec)
    conn.close()


def _req(number, query="", headers=None):
    request = make_mocked_request(
        "GET", f"/nft/{number}{('?' + query) if query else ''}", headers=headers or {}
    )
    request.match_info["number"] = str(number)
    return request


def test_nft_card_route_registered():
    app = server.create_app()
    method_paths = {(r.method, getattr(r.resource, "canonical", "")) for r in app.router.routes()}
    assert ("GET", "/nft/{number}") in method_paths


def test_nft_card_known_edition_renders_meta_tags(tmp_path, monkeypatch):
    monkeypatch.setattr(server.config, "PUBLIC_SHARE_BASE_URL", "")
    # attrs=[] so the trait summary exercises the LFG-row FALLBACK (the index
    # is preferred when it has attributes — see the stale-LFG-row test below).
    _seed_onchain(
        tmp_path, monkeypatch, [_onchain("AAA", 42, image="https://cdn.example/42.png", attrs=[])]
    )
    monkeypatch.setattr(
        server,
        "get_nft_data",
        lambda n: {
            "nft_number": 42,
            "nft_id": "AAA",
            "image_url": "https://cdn.example/42.png",
            "traits": {
                "background": "Nebula",
                "back": None,
                "body": "Ape",
                "clothing": "Hoodie",
                "eyes": None,
                "eyebrows": None,
                "mouth": None,
                "hat": None,
                "accessory": None,
            },
        },
    )

    resp = _run(server.handle_nft_card(_req(42)))

    assert resp.status == 200
    assert resp.content_type == "text/html"
    body = resp.text
    assert 'name="twitter:card" content="summary_large_image"' in body
    assert 'name="twitter:image" content="https://cdn.example/42.png"' in body
    assert 'property="og:image" content="https://cdn.example/42.png"' in body
    assert 'property="og:title" content="LFG #42 · Tap to start building"' in body
    assert "<title>LFG #42 · Tap to start building</title>" in body
    # og:description carries the trait summary (fixed slot order, no rarity dep)
    assert "Background: Nebula" in body
    assert "Body: Ape" in body
    # Visible link out to bithomp — substring is host-agnostic (mainnet vs.
    # testnet's "test." prefix both contain it) so this doesn't pin
    # config.IS_TESTNET, which conftest.py may default either way.
    assert "bithomp.com/en/nft/AAA" in body


def test_nft_card_skips_non_dict_attribute_entries(tmp_path, monkeypatch):
    """attributes_json is externally-sourced NFT metadata (IPFS/CDN) — a
    non-dict element (string/null/...) must be skipped, not crash the
    PUBLIC card endpoint with a 500 (#41 review fix)."""
    monkeypatch.setattr(server.config, "PUBLIC_SHARE_BASE_URL", "")
    _seed_onchain(
        tmp_path,
        monkeypatch,
        [
            _onchain(
                "MALFORMED",
                61,
                image="https://cdn.example/61.png",
                attrs=["not-a-dict", None, {"trait_type": "Body", "value": "Ape"}],
            )
        ],
    )
    monkeypatch.setattr(
        server,
        "get_nft_data",
        lambda n: {
            "nft_number": 61,
            "nft_id": "MALFORMED",
            "image_url": "https://cdn.example/61.png",
            "traits": {},
        },
    )

    resp = _run(server.handle_nft_card(_req(61)))

    assert resp.status == 200
    body = resp.text
    assert "Body: Ape" in body


def test_nft_card_prefers_onchain_index_over_stale_lfg_row(tmp_path, monkeypatch):
    """#41 fix wave: swaps NEVER update the LFG table, while the listener
    keeps the on-chain index fresh (NFTokenModify + burn-remint). A post-swap
    share card built LFG-row-first showed pre-swap art/traits and a bithomp
    link to the BURNED old token — the index must win for image_url, nft_id,
    AND traits; the LFG row is only the fallback."""
    monkeypatch.setattr(server.config, "PUBLIC_SHARE_BASE_URL", "")
    _seed_onchain(
        tmp_path,
        monkeypatch,
        [
            _onchain(
                "NEWID",
                42,
                image="https://cdn.example/42-postswap.png",
                attrs=[{"trait_type": "Eyes", "value": "Laser"}],
            )
        ],
    )
    monkeypatch.setattr(
        server,
        "get_nft_data",
        lambda n: {
            "nft_number": 42,
            "nft_id": "OLDID",  # the burned pre-swap token
            "image_url": "https://cdn.example/42-preswap.png",
            "traits": {"eyes": "Round"},
        },
    )

    resp = _run(server.handle_nft_card(_req(42)))

    assert resp.status == 200
    body = resp.text
    # Image: on-chain index wins over the stale LFG row.
    assert 'name="twitter:image" content="https://cdn.example/42-postswap.png"' in body
    assert "42-preswap.png" not in body
    # Traits: on-chain attributes win.
    assert "Eyes: Laser" in body
    assert "Round" not in body
    # bithomp link: must target the LIVE token, never the burned one.
    assert "bithomp.com/en/nft/NEWID" in body
    assert "OLDID" not in body


def test_nft_card_lfg_row_fallback_when_index_lacks_image_and_traits(tmp_path, monkeypatch):
    """Editions whose index record carries no image / attributes (e.g. an
    unreadable-metadata backfill row) still render from the LFG row."""
    monkeypatch.setattr(server.config, "PUBLIC_SHARE_BASE_URL", "")
    _seed_onchain(tmp_path, monkeypatch, [_onchain("FFF", 43, image="", attrs=[])])
    monkeypatch.setattr(
        server,
        "get_nft_data",
        lambda n: {
            "nft_number": 43,
            "nft_id": "FFF",
            "image_url": "https://cdn.example/43.png",
            "traits": {"background": "Nebula"},
        },
    )

    resp = _run(server.handle_nft_card(_req(43)))

    assert resp.status == 200
    body = resp.text
    assert 'name="twitter:image" content="https://cdn.example/43.png"' in body
    assert "Background: Nebula" in body
    assert "bithomp.com/en/nft/FFF" in body


def test_nft_card_falls_back_to_http_lfg_image_when_onchain_is_ipfs(tmp_path, monkeypatch):
    """#41 fix: legacy mainnet editions carry ipfs:// URIs in their on-chain
    metadata. X's crawler can't fetch ipfs://, so an onchain-image-first card
    must skip the unfetchable ipfs:// value and fall back to the LFG row's
    http(s) image_url rather than emitting a dead twitter:image."""
    monkeypatch.setattr(server.config, "PUBLIC_SHARE_BASE_URL", "")
    _seed_onchain(
        tmp_path,
        monkeypatch,
        [_onchain("GGG", 44, image="ipfs://bafybeigdyrzt/44.png", attrs=[])],
    )
    monkeypatch.setattr(
        server,
        "get_nft_data",
        lambda n: {
            "nft_number": 44,
            "nft_id": "GGG",
            "image_url": "https://cdn.example/44.png",
            "traits": {},
        },
    )

    resp = _run(server.handle_nft_card(_req(44)))

    assert resp.status == 200
    body = resp.text
    assert 'name="twitter:image" content="https://cdn.example/44.png"' in body
    assert "ipfs://" not in body


def test_nft_card_prefers_onchain_http_image_over_lfg_row(tmp_path, monkeypatch):
    """Pins existing behavior: when the on-chain image IS a fetchable
    http(s) URL, it still wins over the LFG row (stale-swap correctness)."""
    monkeypatch.setattr(server.config, "PUBLIC_SHARE_BASE_URL", "")
    _seed_onchain(
        tmp_path,
        monkeypatch,
        [_onchain("HHH", 45, image="https://cdn.example/45-postswap.png", attrs=[])],
    )
    monkeypatch.setattr(
        server,
        "get_nft_data",
        lambda n: {
            "nft_number": 45,
            "nft_id": "HHH",
            "image_url": "https://cdn.example/45-preswap.png",
            "traits": {},
        },
    )

    resp = _run(server.handle_nft_card(_req(45)))

    assert resp.status == 200
    body = resp.text
    assert 'name="twitter:image" content="https://cdn.example/45-postswap.png"' in body
    assert "45-preswap.png" not in body


def test_nft_card_omits_image_tags_when_neither_source_is_http(tmp_path, monkeypatch):
    """Both onchain and LFG-row images non-http(s) (or absent) -> the image
    tags are omitted entirely (the existing no-image code path), and the
    page still renders 200 rather than emitting an unfetchable URL."""
    monkeypatch.setattr(server.config, "PUBLIC_SHARE_BASE_URL", "")
    _seed_onchain(
        tmp_path,
        monkeypatch,
        [_onchain("III", 46, image="ipfs://bafybeigdyrzt/46.png", attrs=[])],
    )
    monkeypatch.setattr(
        server,
        "get_nft_data",
        lambda n: {
            "nft_number": 46,
            "nft_id": "III",
            "image_url": None,
            "traits": {},
        },
    )

    resp = _run(server.handle_nft_card(_req(46)))

    assert resp.status == 200
    body = resp.text
    assert "twitter:image" not in body
    assert "og:image" not in body
    assert "ipfs://" not in body


def test_nft_card_escapes_image_url(tmp_path, monkeypatch):
    monkeypatch.setattr(server.config, "PUBLIC_SHARE_BASE_URL", "")
    dirty_url = 'https://cdn.example/42.png?x="quote"&y=amp'
    _seed_onchain(tmp_path, monkeypatch, [_onchain("BBB", 42, image=dirty_url)])
    monkeypatch.setattr(
        server,
        "get_nft_data",
        lambda n: {
            "nft_number": 42,
            "nft_id": "BBB",
            "image_url": dirty_url,
            "traits": {},
        },
    )

    resp = _run(server.handle_nft_card(_req(42)))

    assert resp.status == 200
    body = resp.text
    # The raw URL (unescaped quote/ampersand) must never appear verbatim...
    assert dirty_url not in body
    # ...only its html.escape(quote=True) form.
    assert escape(dirty_url, quote=True) in body


def test_nft_card_unknown_number_returns_404(tmp_path, monkeypatch):
    _seed_onchain(tmp_path, monkeypatch, [])
    monkeypatch.setattr(server, "get_nft_data", lambda n: None)

    resp = _run(server.handle_nft_card(_req(9999)))

    assert resp.status == 404


def test_nft_card_burned_edition_returns_404(tmp_path, monkeypatch):
    # A dress-up Harvest burn never touches the LFG table, so the stale LFG
    # row is still returned here — the on-chain index (is_burned=1) is what
    # must drive the 404, not row-presence in the LFG table (#41 §6.2).
    _seed_onchain(tmp_path, monkeypatch, [_onchain("CCC", 7, burned=True)])
    monkeypatch.setattr(
        server,
        "get_nft_data",
        lambda n: {
            "nft_number": 7,
            "nft_id": "CCC",
            "image_url": "https://cdn.example/stale.png",
            "traits": {},
        },
    )

    resp = _run(server.handle_nft_card(_req(7)))

    assert resp.status == 404


def test_nft_card_no_nft_id_returns_404(tmp_path, monkeypatch):
    # A never-minted draft LFG row (nft_id IS NULL) with no corresponding
    # on-chain token at all.
    _seed_onchain(tmp_path, monkeypatch, [])
    monkeypatch.setattr(
        server,
        "get_nft_data",
        lambda n: {"nft_number": 8, "nft_id": None, "image_url": None, "traits": {}},
    )

    resp = _run(server.handle_nft_card(_req(8)))

    assert resp.status == 404


def test_nft_card_omits_og_url_when_base_unset(tmp_path, monkeypatch):
    monkeypatch.setattr(server.config, "PUBLIC_SHARE_BASE_URL", "")
    _seed_onchain(tmp_path, monkeypatch, [_onchain("DDD", 55)])
    monkeypatch.setattr(
        server,
        "get_nft_data",
        lambda n: {
            "nft_number": 55,
            "nft_id": "DDD",
            "image_url": "https://cdn.example/55.png",
            "traits": {},
        },
    )

    resp = _run(server.handle_nft_card(_req(55)))

    body = resp.text
    assert 'property="og:url"' not in body
    assert 'rel="canonical"' not in body


def test_nft_card_includes_og_url_when_base_set(tmp_path, monkeypatch):
    monkeypatch.setattr(server.config, "PUBLIC_SHARE_BASE_URL", "https://share.example/lfg")
    _seed_onchain(tmp_path, monkeypatch, [_onchain("EEE", 55)])
    monkeypatch.setattr(
        server,
        "get_nft_data",
        lambda n: {
            "nft_number": 55,
            "nft_id": "EEE",
            "image_url": "https://cdn.example/55.png",
            "traits": {},
        },
    )

    resp = _run(server.handle_nft_card(_req(55)))

    body = resp.text
    assert 'property="og:url" content="https://share.example/lfg/nft/55"' in body
    assert 'rel="canonical" href="https://share.example/lfg/nft/55"' in body


_REF = "rrrrrrrrrrrrrrrrrrrrrhoLvTp"  # valid classic address (ACCOUNT_ZERO)


def _seed_basic(tmp_path, monkeypatch, number=42):
    _seed_onchain(tmp_path, monkeypatch, [_onchain("AAA", number)])
    monkeypatch.setattr(server, "get_nft_data", lambda n: None)


def test_forward_unset_keeps_legacy_body(tmp_path, monkeypatch):
    monkeypatch.setattr(server.config, "SHARE_FORWARD_URL", "")
    _seed_basic(tmp_path, monkeypatch)
    body = _run(server.handle_nft_card(_req(42))).text
    assert "location.replace" not in body
    assert "<h1>LFG #42 · Tap to start building</h1>" in body


def test_forward_set_injects_js_redirect_and_keeps_meta(tmp_path, monkeypatch):
    monkeypatch.setattr(server.config, "SHARE_FORWARD_URL", "https://build.example")
    _seed_basic(tmp_path, monkeypatch)
    resp = _run(server.handle_nft_card(_req(42)))
    assert resp.status == 200  # no HTTP redirect, ever
    body = resp.text
    # Meta tags untouched — the crawler contract.
    assert 'name="twitter:card" content="summary_large_image"' in body
    assert 'name="twitter:image" content="https://cdn.example/img.png"' in body
    # JS-only forward + visible fallback link, Bithomp retained.
    assert 'location.replace("https:\\/\\/build.example")' in body
    assert 'href="https://build.example"' in body
    assert "View on Bithomp" in body
    assert "http-equiv" not in body  # no meta-refresh


def test_forward_appends_valid_ref_and_logs_click(tmp_path, monkeypatch, _isolate_app_db):
    monkeypatch.setattr(server.config, "SHARE_FORWARD_URL", "https://build.example")
    monkeypatch.setattr(server.config, "PUBLIC_SHARE_BASE_URL", "https://share.example")
    _seed_basic(tmp_path, monkeypatch)
    db = _isolate_app_db
    body = _run(
        server.handle_nft_card(_req(42, query=f"ref={_REF}", headers={"User-Agent": "Mozilla/5.0"}))
    ).text
    assert f'location.replace("https:\\/\\/build.example?ref={_REF}")' in body
    # og:url / canonical stay ref-less so X dedupes card variants.
    assert 'property="og:url" content="https://share.example/nft/42"' in body
    assert 'rel="canonical" href="https://share.example/nft/42"' in body
    import sqlite3

    row = (
        sqlite3.connect(db)
        .execute("SELECT nft_number, ref_wallet, is_bot FROM share_clicks")
        .fetchone()
    )
    assert row == (42, _REF, 0)


def test_forward_url_with_query_uses_ampersand(tmp_path, monkeypatch, _isolate_app_db):
    # A SHARE_FORWARD_URL that already carries a query string must get ref
    # joined with & (not a second ?), or the URL is malformed for most parsers.
    monkeypatch.setattr(server.config, "SHARE_FORWARD_URL", "https://build.example?utm_source=x")
    _seed_basic(tmp_path, monkeypatch)
    body = _run(
        server.handle_nft_card(_req(42, query=f"ref={_REF}", headers={"User-Agent": "Mozilla/5.0"}))
    ).text
    assert f'location.replace("https:\\/\\/build.example?utm_source=x&ref={_REF}")' in body


def test_invalid_ref_ignored_not_echoed(tmp_path, monkeypatch, _isolate_app_db):
    monkeypatch.setattr(server.config, "SHARE_FORWARD_URL", "https://build.example")
    _seed_basic(tmp_path, monkeypatch)
    db = _isolate_app_db
    evil = '"><script>alert(1)</script>'
    body = _run(server.handle_nft_card(_req(42, query="ref=" + escape(evil)))).text
    assert "alert(1)" not in body
    assert 'location.replace("https:\\/\\/build.example")' in body  # no ref appended
    import sqlite3

    (ref,) = sqlite3.connect(db).execute("SELECT ref_wallet FROM share_clicks").fetchone()
    assert ref is None


def test_bot_user_agent_flagged(tmp_path, monkeypatch, _isolate_app_db):
    monkeypatch.setattr(server.config, "SHARE_FORWARD_URL", "https://build.example")
    _seed_basic(tmp_path, monkeypatch)
    db = _isolate_app_db
    _run(server.handle_nft_card(_req(42, headers={"User-Agent": "Twitterbot/1.0"})))
    import sqlite3

    (is_bot,) = sqlite3.connect(db).execute("SELECT is_bot FROM share_clicks").fetchone()
    assert is_bot == 1


def test_click_log_failure_never_breaks_card(tmp_path, monkeypatch):
    monkeypatch.setattr(server.config, "SHARE_FORWARD_URL", "https://build.example")
    _seed_basic(tmp_path, monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(server.share_clicks, "record_click", boom)
    resp = _run(server.handle_nft_card(_req(42)))
    assert resp.status == 200


def test_unknown_edition_404_does_not_log_click(tmp_path, monkeypatch, _isolate_app_db):
    """A 404 card (unknown edition) must never write a share_clicks row —
    record_click only runs on the live-card render path."""
    monkeypatch.setattr(server.config, "SHARE_FORWARD_URL", "https://build.example")
    _seed_onchain(tmp_path, monkeypatch, [])
    monkeypatch.setattr(server, "get_nft_data", lambda n: None)

    resp = _run(server.handle_nft_card(_req(9999)))

    assert resp.status == 404
    import sqlite3

    conn = sqlite3.connect(_isolate_app_db)
    table_exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='share_clicks'"
    ).fetchone()
    assert (
        table_exists is None or conn.execute("SELECT COUNT(*) FROM share_clicks").fetchone()[0] == 0
    )


def test_card_png_route_registered():
    app = server.create_app()
    method_paths = {(r.method, getattr(r.resource, "canonical", "")) for r in app.router.routes()}
    assert ("GET", "/nft/{number}/card.png") in method_paths


def _card_req(number):
    request = make_mocked_request("GET", f"/nft/{number}/card.png")
    request.match_info["number"] = str(number)
    return request


def test_card_png_unknown_edition_404(tmp_path, monkeypatch):
    _seed_onchain(tmp_path, monkeypatch, [])
    monkeypatch.setattr(server, "get_nft_data", lambda n: None)
    resp = _run(server.handle_nft_card_png(_card_req(9999)))
    assert resp.status == 404


def test_card_png_cache_hit_serves_without_render(tmp_path, monkeypatch):
    _seed_basic(tmp_path, monkeypatch)
    card_dir = tmp_path / "cards"
    monkeypatch.setattr(server, "_SHARE_CARD_DIR", str(card_dir))
    cached = server._share_card_path(42, "https://cdn.example/img.png")
    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_bytes(b"\x89PNG-cached")

    def no_render(*a, **k):
        raise AssertionError("render must not run on cache hit")

    monkeypatch.setattr(server, "_render_share_card", no_render)
    resp = _run(server.handle_nft_card_png(_card_req(42)))
    assert resp.status == 200
    assert resp.body == b"\x89PNG-cached"
    assert resp.content_type == "image/png"


def test_card_png_cache_key_changes_with_art(tmp_path, monkeypatch):
    a = server._share_card_path(42, "https://cdn.example/img.png")
    b = server._share_card_path(42, "https://cdn.example/img-v2.png")
    assert a != b
    assert a.name.startswith("42-") and b.name.startswith("42-")


def test_card_png_miss_renders_and_caches(tmp_path, monkeypatch):
    _seed_basic(tmp_path, monkeypatch)
    card_dir = tmp_path / "cards"
    monkeypatch.setattr(server, "_SHARE_CARD_DIR", str(card_dir))
    monkeypatch.setattr(server.config, "IMG_PROXY_ALLOWED_BASES", ("https://cdn.example",))

    async def fake_fetch(url):
        assert url == "https://cdn.example/img.png"
        return b"rawart", "image/png"

    async def fake_render(number, art_path, out_path):
        out_path.write_bytes(b"\x89PNG-rendered")

    monkeypatch.setattr(server, "_fetch_cdn", fake_fetch)
    monkeypatch.setattr(server, "_render_share_card", fake_render)
    resp = _run(server.handle_nft_card_png(_card_req(42)))
    assert resp.status == 200
    assert resp.body == b"\x89PNG-rendered"
    assert server._share_card_path(42, "https://cdn.example/img.png").exists()


def test_card_png_render_failure_302_to_raw_art(tmp_path, monkeypatch):
    _seed_basic(tmp_path, monkeypatch)
    card_dir = tmp_path / "cards"
    monkeypatch.setattr(server, "_SHARE_CARD_DIR", str(card_dir))
    monkeypatch.setattr(server.config, "IMG_PROXY_ALLOWED_BASES", ("https://cdn.example",))

    async def fake_fetch(url):
        return b"rawart", "image/png"

    async def broken_render(number, art_path, out_path):
        raise RuntimeError("chromium missing")

    monkeypatch.setattr(server, "_fetch_cdn", fake_fetch)
    monkeypatch.setattr(server, "_render_share_card", broken_render)
    resp = _run(server.handle_nft_card_png(_card_req(42)))
    assert resp.status == 302
    assert resp.headers["Location"] == "https://cdn.example/img.png"


def test_card_png_disallowed_art_url_302(tmp_path, monkeypatch):
    _seed_onchain(tmp_path, monkeypatch, [_onchain("AAA", 42, image="https://evil.example/x.png")])
    monkeypatch.setattr(server, "get_nft_data", lambda n: None)
    resp = _run(server.handle_nft_card_png(_card_req(42)))
    assert resp.status == 302
    assert resp.headers["Location"] == "https://evil.example/x.png"


def test_card_png_falls_back_to_http_lfg_image_when_onchain_is_ipfs(tmp_path, monkeypatch):
    """06ffa0b fix mirrored into the PNG endpoint: an ipfs:// on-chain image
    must not be used as the redirect/cache-key target when a fetchable
    http(s) LFG-row image_url is available."""
    card_dir = tmp_path / "cards"
    monkeypatch.setattr(server, "_SHARE_CARD_DIR", str(card_dir))
    monkeypatch.setattr(server.config, "IMG_PROXY_ALLOWED_BASES", ("https://cdn.example",))
    _seed_onchain(
        tmp_path,
        monkeypatch,
        [_onchain("AAA", 42, image="ipfs://bafybeigdyrzt/42.png", attrs=[])],
    )
    monkeypatch.setattr(
        server,
        "get_nft_data",
        lambda n: {
            "nft_number": 42,
            "nft_id": "AAA",
            "image_url": "https://cdn.example/img.png",
            "traits": {},
        },
    )
    cached = server._share_card_path(42, "https://cdn.example/img.png")
    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_bytes(b"\x89PNG-cached")

    resp = _run(server.handle_nft_card_png(_card_req(42)))

    assert resp.status == 200
    assert resp.body == b"\x89PNG-cached"


def test_card_png_ipfs_only_edition_404s(tmp_path, monkeypatch):
    """Neither the on-chain image nor the LFG-row fallback is fetchable ->
    404, never a 302 Location pointing at an ipfs:// URI."""
    _seed_onchain(
        tmp_path,
        monkeypatch,
        [_onchain("AAA", 42, image="ipfs://bafybeigdyrzt/42.png", attrs=[])],
    )
    monkeypatch.setattr(
        server,
        "get_nft_data",
        lambda n: {"nft_number": 42, "nft_id": "AAA", "image_url": None, "traits": {}},
    )

    resp = _run(server.handle_nft_card_png(_card_req(42)))

    assert resp.status == 404


def test_card_page_omits_image_tags_when_enabled_and_ipfs_only(tmp_path, monkeypatch):
    """06ffa0b + card-switch interaction: with rendering enabled and a public
    base configured, an ipfs-only edition (no fetchable art at all) must
    still omit the image tags entirely rather than advertising a card.png
    URL that would 404 -- the card-URL switch must not override the
    no-fetchable-image omission."""
    monkeypatch.setattr(server.config, "SHARE_CARD_RENDER_ENABLED", True)
    monkeypatch.setattr(server.config, "PUBLIC_SHARE_BASE_URL", "https://share.example")
    _seed_onchain(
        tmp_path,
        monkeypatch,
        [_onchain("AAA", 46, image="ipfs://bafybeigdyrzt/46.png", attrs=[])],
    )
    monkeypatch.setattr(
        server,
        "get_nft_data",
        lambda n: {"nft_number": 46, "nft_id": "AAA", "image_url": None, "traits": {}},
    )

    body = _run(server.handle_nft_card(_req(46))).text

    assert "twitter:image" not in body
    assert "og:image" not in body
    assert "card.png" not in body
    assert "ipfs://" not in body


def test_card_page_image_tags_switch_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setattr(server.config, "SHARE_CARD_RENDER_ENABLED", True)
    monkeypatch.setattr(server.config, "PUBLIC_SHARE_BASE_URL", "https://share.example")
    _seed_basic(tmp_path, monkeypatch)
    body = _run(server.handle_nft_card(_req(42))).text
    assert 'name="twitter:image" content="https://share.example/nft/42/card.png"' in body
    assert 'property="og:image" content="https://share.example/nft/42/card.png"' in body
    assert "cdn.example/img.png" not in body.split("</head>")[0]


def test_card_page_image_tags_raw_when_disabled_or_no_base(tmp_path, monkeypatch):
    monkeypatch.setattr(server.config, "SHARE_CARD_RENDER_ENABLED", False)
    monkeypatch.setattr(server.config, "PUBLIC_SHARE_BASE_URL", "https://share.example")
    _seed_basic(tmp_path, monkeypatch)
    body = _run(server.handle_nft_card(_req(42))).text
    assert 'name="twitter:image" content="https://cdn.example/img.png"' in body
    # enabled but no public base -> still raw art (can't build an absolute card URL)
    monkeypatch.setattr(server.config, "SHARE_CARD_RENDER_ENABLED", True)
    monkeypatch.setattr(server.config, "PUBLIC_SHARE_BASE_URL", "")
    body = _run(server.handle_nft_card(_req(42))).text
    assert 'name="twitter:image" content="https://cdn.example/img.png"' in body


def test_render_env_strips_pm2_node_ipc_vars(monkeypatch):
    monkeypatch.setenv("NODE_CHANNEL_FD", "3")
    monkeypatch.setenv("NODE_CHANNEL_SERIALIZATION_MODE", "json")
    monkeypatch.setenv("PATH", "/usr/bin")
    env = server._render_env()
    assert "NODE_CHANNEL_FD" not in env
    assert "NODE_CHANNEL_SERIALIZATION_MODE" not in env
    assert env["PATH"] == "/usr/bin"
