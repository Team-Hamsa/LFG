# tests/test_swap_preview_api.py
# Server side of the Trait Swapper's before/after preview: /api/nfts carries
# the stacking rules (it is the chooser's only data source — /api/economy is
# ECONOMY_ENABLED-gated, swaps are not), and /api/layer serves the ape
# structural art (Nose / Ape Mask) the compose pipeline injects, so the
# preview can draw — and 404-check — exactly what swap_compose will.
import asyncio
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from lfg_core import ape_face, config, layer_store, swap_meta, trait_config
from lfg_service import app as server


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture(autouse=True)
def _real_trait_config():
    trait_config.reset_config()
    yield
    trait_config.reset_config()


@pytest.fixture
def layers(tmp_path, monkeypatch):
    """A local layer tree with the ape structural art and a thumb tier."""
    base = tmp_path / "layers"
    (base / "ape").mkdir(parents=True)
    (base / "ape" / ape_face.NOSE_ASSET).write_bytes(b"nose-full")
    (base / "ape" / ape_face.MASK_ASSET).write_bytes(b"mask-full")
    (base / ".thumbs" / "ape").mkdir(parents=True)
    (base / ".thumbs" / "ape" / ape_face.NOSE_ASSET).write_bytes(b"nose-thumb")
    store = layer_store.LocalLayerStore(str(base))
    monkeypatch.setattr(layer_store, "get_layer_store", lambda: store)
    return base


def _layer(query):
    return _run(server.handle_layer(make_mocked_request("GET", f"/api/layer?{query}")))


def test_nose_asset_is_served_and_prefers_its_thumb(layers):
    resp = _layer("body=ape&asset=nose")
    assert isinstance(resp, web.FileResponse)
    assert str(resp._path) == str(layers / "ape" / ape_face.NOSE_ASSET)
    thumb = _layer("body=ape&asset=nose&thumb=1")
    assert str(thumb._path) == str(layers / ".thumbs" / "ape" / ape_face.NOSE_ASSET)


def test_mask_asset_falls_back_to_the_full_file_without_a_thumb(layers):
    resp = _layer("body=ape&asset=mask&thumb=1")
    assert str(resp._path) == str(layers / "ape" / ape_face.MASK_ASSET)


def test_missing_asset_is_an_image_typed_404(layers):
    (layers / "ape" / ape_face.NOSE_ASSET).unlink()
    resp = _layer("body=ape&asset=nose")
    assert resp.status == 404
    assert resp.content_type == "image/png"
    assert not resp.body


@pytest.mark.parametrize(
    "query",
    [
        "body=ape&asset=Nose.png",  # a filename, not a key
        "body=ape&asset=../../.env",
        "body=ape&asset=",
        "body=male&asset=nose",  # structural art is ape-only
        "body=&asset=mask",
    ],
)
def test_asset_form_accepts_only_the_closed_ape_map(layers, query):
    assert _layer(query).status == 400


def test_trait_form_is_unchanged(layers):
    assert _layer("body=ape&trait=Head").status == 400  # value still required


def _nfts_body(monkeypatch):
    async def _roster(wallet):
        return []

    async def _no_fee(wallet):
        return None

    monkeypatch.setattr(server, "_wallet_nfts", _roster)
    monkeypatch.setattr(server, "_swap_fee_quote", _no_fee)
    # require_wallet's dev-mode bypass supplies the wallet, the same shortcut
    # test_swap_cross_body_api's matrix test takes.
    monkeypatch.setattr(config, "WEBAPP_DEV_MODE", True)
    resp = _run(server.handle_nfts(make_mocked_request("GET", "/api/nfts")))
    assert resp.status == 200
    return json.loads(resp.body)


def test_nfts_serves_the_preview_rules_with_the_economy_off(monkeypatch):
    monkeypatch.setattr(config, "ECONOMY_ENABLED", False)
    body = _nfts_body(monkeypatch)
    preview = body["swap_preview"]
    assert preview == json.loads(json.dumps(server._swap_preview_config(trait_config.get_config())))
    assert preview["trait_order"] == swap_meta.TRAIT_ORDER
    assert preview["z_order"] == trait_config.get_config().z_table()
    # The matrix payload is unchanged by the helper extraction.
    assert body["swap_matrix"]["universal_layers"] == ["Accessory", "Back"]
