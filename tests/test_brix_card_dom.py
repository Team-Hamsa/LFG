# tests/test_brix_card_dom.py
# Issue #48 PR-3: source-assertion guard for the home-screen BRIX card's
# HTML/JS wiring, mirroring tests/test_market_panel_dom.py (the webapp client
# has no JS execution harness for DOM code — only brix_pure.js's pure
# functions are executed, see tests/test_brix_pure_js.py).
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLIENT = os.path.join(ROOT, "webapp", "client")


def _read(name: str) -> str:
    with open(os.path.join(CLIENT, name), encoding="utf-8") as f:
        return f.read()


def test_index_has_the_brix_card_inside_the_home_panel():
    html = _read("index.html")
    assert 'id="brix-card"' in html
    for node in ("brix-headline", "brix-sub", "brix-claim-btn"):
        assert f'id="{node}"' in html
    # It belongs to the home screen, i.e. between #mint-panel and its close.
    home = html.split('id="mint-panel"', 1)[1].split("</section>", 1)[0]
    assert 'id="brix-card"' in home


def test_card_starts_hidden():
    """brixCardView decides visibility; an unarmed deployment must never
    flash a payout tile before the first fetch resolves."""
    html = _read("index.html")
    card = re.search(r'<div id="brix-card"[^>]*>', html)
    assert card and "hidden" in card.group(0)


def test_app_js_imports_brix_pure():
    js = _read("app.js")
    assert "from './brix_pure.js" in js  # ?v= cache-buster suffix allowed


def test_app_js_renders_from_the_pure_view():
    js = _read("app.js")
    assert "brixPure.brixCardView(" in js
    assert "brixPure.claimErrorView(" in js
    assert "brixPure.isClaimTerminal(" in js


def test_claim_is_confirmed_through_the_in_app_overlay():
    """Discord's sandboxed iframe makes window.confirm a silent no-op."""
    js = _read("app.js")
    claim = js.split("async function claimBrix(", 1)
    assert len(claim) == 2, "claimBrix() not found in app.js"
    body = claim[1][:3000]
    assert "confirmDialog(" in body
    assert "window.confirm(" not in body


def test_home_screen_loads_the_card():
    js = _read("app.js")
    home = js.split("function showMintHome()", 1)[1][:1200]
    assert "loadBrix(" in home


def test_claim_button_is_disabled_while_the_request_is_in_flight():
    """Double-clicking Claim must not fire two POSTs — the second would be
    refused claim_in_flight at best, and the UI would report a false error."""
    js = _read("app.js")
    body = js.split("async function claimBrix(", 1)[1][:3000]
    assert "disabled = true" in body


def test_non_retryable_claim_error_locks_the_button_across_reload():
    """Greptile P1 on PR #434: after claims_disabled / trustline_required the
    unconditional loadBrix() re-enabled the button. The lock label from
    claimErrorView must survive the reload and only clear on the next home
    landing."""
    js = _read("app.js")
    body = js.split("async function claimBrix(", 1)[1][:4000]
    assert "lockLabel" in body
    render = js.split("function renderBrixCard(", 1)[1][:1500]
    assert "brixLock" in render
    home = js.split("function showMintHome()", 1)[1][:1200]
    assert "brixLock = null" in home


def test_module_cache_busters_are_bumped_in_lockstep():
    """ES-module imports are cache keys: a stale app.js against a fresh
    index.html serves a client with no card at all (see the 2026-07-21
    mint_pure incident)."""
    html = _read("index.html")
    js = _read("app.js")
    app_v = re.search(r'src="app\.js\?v=(\d+)"', html)
    assert app_v, "index.html does not cache-bust app.js"
    # Ratchet: raise this floor with every app.js ?v= bump. A version below the
    # floor means index.html would serve a stale app.js to warm caches.
    assert int(app_v.group(1)) >= 67, "app.js?v= regressed below the shipped version"
    assert re.search(r"from './brix_pure\.js\?v=\d+'", js)
