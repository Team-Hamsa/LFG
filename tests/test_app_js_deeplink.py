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
    assert "app.js?v=79" in html


# ---------------------------------------------------------------------------
# #447 — a borrowed (one-shot) Joey pairing must never become the session
# pairing. The link panel pairs a SECOND wallet purely to prove ownership; if
# wc.js adopted it, every later signTx and the next reload's restore would
# target the linked wallet while the LFG session still belongs to the
# signed-in one. No decision here is pure (it is session + storage state), so
# it is guarded with source assertions like the rest of this module.
# ---------------------------------------------------------------------------


def test_fresh_pairing_is_borrowed_not_adopted():
    src = _read("wc.js")
    assert "function borrow(" in src
    # the fresh arm returns borrow(), the reuse arm keeps adopt()
    assert "return fresh ? borrow(session) : adopt(session);" in src
    borrow_fn = src[src.index("function borrow(") :][:300]
    # borrow must not write module state or storage
    assert "storeTopic" not in borrow_fn
    assert "wallet =" not in borrow_fn


def test_sign_tx_accepts_an_explicit_topic_override():
    src = _read("wc.js")
    fn = src[src.index("export async function signTx(") :]
    fn = fn[: fn.index("\n}")]
    assert "topic: requestTopic" in fn  # caller-supplied pairing
    assert "const t = requestTopic || topic;" in fn  # defaults to the session's


def test_release_never_tears_down_the_session_pairing():
    src = _read("wc.js")
    fn = src[src.index("export async function release(") :]
    fn = fn[: fn.index("\n}")]
    assert "borrowedTopic === topic" in fn  # hard guard on the primary pairing


def test_link_joey_signs_with_the_borrowed_topic_and_releases_it():
    src = _read("app.js")
    fn = src[src.index("async function startLinkJoey(") :]
    fn = fn[: fn.index("\n}\n")]
    assert "fresh: true" in fn
    assert "topic: borrowedTopic" in fn  # the proof is signed by the PROVING wallet
    # released on every exit — success, decline or crash
    assert "} finally {" in fn
    assert "release(borrowedTopic)" in fn
