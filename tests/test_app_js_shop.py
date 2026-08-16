# tests/test_app_js_shop.py
# Trait Shop Activity UI (#217 Task 11): the webapp client is no-build vanilla
# JS with no in-browser JS test harness (see test_app_js_boot.py's own header
# for the same rationale), so this guards the Shop section the same way —
# source-assertion tests on webapp/client/app.js and index.html rather than
# executing the DOM. Mirrors the marketplace panel's structure/flow exactly
# (catalog grid -> buy click -> signing overlay -> poll -> toast) per the
# task brief's "reuse the market-buy overlay flow verbatim" requirement.
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLIENT = os.path.join(ROOT, "webapp", "client")


def _read(name: str) -> str:
    with open(os.path.join(CLIENT, name), encoding="utf-8") as f:
        return f.read()


def test_shop_panel_present_in_html():
    html = _read("index.html")
    assert 'data-tab="shop"' in html
    assert 'id="market-shop"' in html
    assert 'id="shop-grid"' in html
    assert 'id="shop-empty"' in html


def test_shop_catalog_renders_items():
    src = _read("app.js")
    assert "function loadShopCatalog(" in src
    assert "/api/shop/catalog" in src
    assert "function renderShopGrid(" in src
    # catalog rows carry slot/value/price_brix/image_url (T8's shop.catalog +
    # _trait_image_url); the grid must show name, slot-implied label, and price.
    assert "item.price_brix" in src
    assert "BRIX" in src
    assert "item.image_url" in src


def test_shop_buy_posts_and_shows_overlay():
    src = _read("app.js")
    assert "function openShopBuyFlow(" in src
    assert "/api/shop/buy" in src
    # Buy reuses the flow-panel/showFlow overlay exactly like the market
    # buy flow (QR + deep link + poll), not bespoke UI.
    assert "showPanel('flow-panel')" in src
    assert "function shopBuyRender(" in src
    assert "s.accept.xumm_url" in src
    assert "function pollShopFlow(" in src
    assert "/api/shop/buy/${sessionId}" in src


def test_shop_closet_required_reuses_market_prompt():
    src = _read("app.js")
    # The closet_required branch inside openShopBuyFlow must call the SAME
    # promptClosetRequired() the trait market buy flow uses, not a new prompt.
    assert "async function openShopBuyFlow(item)" in src
    shop_fn_start = src.index("async function openShopBuyFlow(item)")
    shop_fn_body = src[shop_fn_start : shop_fn_start + 1200]
    assert "closet_required" in shop_fn_body
    assert "promptClosetRequired()" in shop_fn_body


def test_shop_session_active_resumes_instead_of_erroring():
    src = _read("app.js")
    assert "session_active" in src
    assert "function resumeShopBuy(" in src
    shop_fn_start = src.index("async function openShopBuyFlow(item)")
    shop_fn_body = src[shop_fn_start : shop_fn_start + 1500]
    assert "e.body.session_id" in shop_fn_body
    assert "resumeShopBuy(e.body.session_id)" in shop_fn_body


def test_shop_uses_no_native_confirm():
    src = _read("app.js")
    # Established convention (Discord's sandboxed iframe swallows
    # window.confirm silently) — the Shop buy flow must use confirmDialog.
    shop_fn_start = src.index("async function openShopBuyFlow(item)")
    shop_fn_body = src[shop_fn_start : shop_fn_start + 400]
    assert "confirmDialog(" in shop_fn_body
    assert "window.confirm(" not in src


def test_shop_tab_wired_into_market_tab_switch():
    src = _read("app.js")
    assert "function switchMarketTab(tab)" in src
    switch_start = src.index("function switchMarketTab(tab)")
    switch_body = src[switch_start : switch_start + 500]
    assert "market-shop" in switch_body
    assert "loadShopCatalog()" in switch_body


def test_api_helper_exposes_error_body_for_session_resume():
    # api()'s thrown Error must carry the full JSON body (not just .message)
    # so 409 session_active's session_id is reachable by callers.
    src = _read("app.js")
    assert "err.body = data" in src
