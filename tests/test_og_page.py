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

from aiohttp.test_utils import make_mocked_request  # noqa: E402

from lfg_core import nft_index  # noqa: E402
from lfg_service import app as server  # noqa: E402


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


def _req(number):
    request = make_mocked_request("GET", f"/nft/{number}")
    request.match_info["number"] = str(number)
    return request


def test_nft_card_route_registered():
    app = server.create_app()
    method_paths = {(r.method, getattr(r.resource, "canonical", "")) for r in app.router.routes()}
    assert ("GET", "/nft/{number}") in method_paths


def test_nft_card_known_edition_renders_meta_tags(tmp_path, monkeypatch):
    monkeypatch.setattr(server.config, "PUBLIC_SHARE_BASE_URL", "")
    _seed_onchain(tmp_path, monkeypatch, [_onchain("AAA", 42, image="https://cdn.example/42.png")])
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
    assert 'property="og:title" content="LFGO #42"' in body
    assert "<title>LFGO #42</title>" in body
    # og:description carries the trait summary (fixed slot order, no rarity dep)
    assert "Background: Nebula" in body
    assert "Body: Ape" in body
    # Visible link out to bithomp (mainnet host per config.IS_TESTNET default)
    assert "bithomp.com/en/nft/AAA" in body


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
