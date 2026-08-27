# /api/layer 404s must not carry a JSON body: the Builder's <img> tiles load
# them cross-origin as no-cors, and Firefox's Opaque Response Blocking logs a
# console error for every non-image opaque response — one per Closet tile the
# selected body can't wear (2026-08-27). An empty body typed as an image is
# ORB-transparent and still fires img.onerror → onMissing pruning.
import asyncio

from aiohttp.test_utils import make_mocked_request

import lfg_service.app as app
from lfg_core import swap_compose


def test_layer_404_has_no_json_body(monkeypatch):
    async def _none(*a, **k):
        return None

    monkeypatch.setattr(swap_compose, "resolve_layer", _none)
    req = make_mocked_request("GET", "/api/layer?body=skeleton&trait=Head&value=Fredora")
    loop = asyncio.new_event_loop()
    try:
        resp = loop.run_until_complete(app.handle_layer(req))
    finally:
        loop.close()
    assert resp.status == 404
    assert resp.content_type == "image/png"
    assert not resp.body
