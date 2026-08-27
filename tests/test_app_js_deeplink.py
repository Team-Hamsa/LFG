# tests/test_app_js_deeplink.py
# #142 — mobile-primary Xaman deep-link delivery in the Activity client.
# The webapp client is no-build vanilla JS with no in-browser test harness
# (see test_app_js_boot.py), so this guards the wiring with source assertions
# on webapp/client/app.js and index.html; the decision logic itself lives in
# signdelivery_pure.js and is executed under Node by
# tests/test_signdelivery_pure_js.py.
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLIENT = os.path.join(ROOT, "webapp", "client")


def _read(name: str) -> str:
    with open(os.path.join(CLIENT, name), encoding="utf-8") as f:
        return f.read()


def test_pure_module_imported_with_cache_buster():
    src = _read("app.js")
    assert re.search(r"import \* as signDeliveryPure from './signdelivery_pure\.js\?v=\d+'", src)


def test_mobile_detection_uses_coarse_pointer_media_query():
    src = _read("app.js")
    assert "function isCoarsePointer(" in src
    assert "pointer: coarse" in src


def test_auto_open_is_deduped_per_payload_link():
    src = _read("app.js")
    assert "function maybeAutoOpen(" in src
    # dedup via the pure helper, opened through the sandboxed-iframe-safe path
    assert "shouldAutoOpen(" in src
    fn = src[src.index("function maybeAutoOpen(") :][:400]
    assert "openExternal(" in fn


def test_show_flow_routes_through_apply_sign_delivery():
    src = _read("app.js")
    assert "function applySignDelivery(" in src
    fn_start = src.index("function showFlow(")
    fn_body = src[fn_start : fn_start + 2500]
    assert "applySignDelivery(" in fn_body
    # showFlow accepts the per-payload push state so 'sent' can collapse the QR
    assert re.search(r"function showFlow\(\{[^)]*\bpush\b", src)


def test_flow_qr_has_show_qr_disclosure():
    html = _read("index.html")
    assert 'id="flow-qr-toggle"' in html
    assert 'id="flow-qr"' in html


def test_register_signin_routes_through_apply_sign_delivery():
    src = _read("app.js")
    fn_start = src.index("function renderSignin(")
    fn_body = src[fn_start : fn_start + 800]
    assert "applySignDelivery(" in fn_body
    assert 'id="register-qr-toggle"' in _read("index.html")


def test_dynamic_sign_panels_route_through_apply_sign_delivery():
    src = _read("app.js")
    # swap fee + swap accept + the two claim trays each build their own QR
    # <img> + "Open in Xaman" button; all must route delivery decisions
    # through the same helper (uniform mobile behavior).
    assert src.count("applySignDelivery(") >= 6


def test_cache_busters_bumped():
    html = _read("index.html")
    assert "app.js?v=78" in html
