import json
import os
import re
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(ROOT, rel)) as fh:
        return fh.read()


def _stylesheet():
    html = _read("webapp/client/index.html")
    return _read("webapp/client/" + re.search(r'href="(style\.v\d+\.css)"', html).group(1))


def test_markup_has_closet_market_surfaces():
    html = _read("webapp/client/index.html")
    for element_id in (
        "market-book",
        "closet-book-asks",
        "closet-book-asks-empty",
        "closet-book-bids",
        "closet-book-bids-empty",
        "closet-bid-new-btn",
        "closet-bid-form-panel",
        "closet-bid-slot",
        "closet-bid-value",
        "closet-bid-price",
        "closet-bid-note",
        "closet-bid-confirm-btn",
        "closet-bid-cancel-btn",
        "mine-closet-orders-section",
        "mine-closet-orders",
        "mine-closet-incoming-section",
        "mine-closet-incoming",
        "mine-closet-fills-section",
        "mine-closet-fills",
    ):
        assert f'id="{element_id}"' in html, element_id
    assert 'data-tab="book"' in html


def test_app_js_wires_closet_market():
    js = _read("webapp/client/app.js")
    assert re.search(r"import \* as closetPure from './closet_market_pure\.js\?v=\d+';", js)
    assert "closet_bid: (id) => `/api/closet/bid/${id}`" in js
    assert "closet_fill: (id) => `/api/closet/fill/${id}`" in js
    assert "cfg.closet_market_enabled === true" in js
    for fn in (
        "function closetBidRender(s)",
        "function closetFillRender(s)",
        "async function loadClosetBook()",
        "async function loadClosetMine()",
        "async function openClosetBidForm(",
        "async function placeClosetBid(",
        "async function postClosetAsk(",
        "async function buyBestClosetAsk(",
        "async function fillClosetBid(",
        "async function cancelClosetOrder(",
        "async function viewClosetFill(",
        "function applyClosetMarketVisibility(",
    ):
        assert fn in js, fn
    assert (
        "'/api/deposit'" in js and "Deposit & fill" not in js
    )  # label comes from closetPure, not hard-coded
    assert "closetPure.bookBadge(row.book)" in js


def test_badge_is_styled():
    assert ".market-card-bid" in _stylesheet()


def test_bid_render_only_celebrates_a_filled_bid():
    """(#443 final review I1) `matched` renders as pending now; the done branch
    distinguishes a live (open) bid from a filled one."""
    js = _read("webapp/client/app.js")
    body = js[js.index("function closetBidRender(s)") : js.index("function closetFillRender(s)")]
    assert "Your bid was filled — the trait is in your Closet." in body
    assert "Bid matched" not in body


def test_closet_market_dom_lookups_are_null_guarded():
    """PR #502 C5: a cached older index.html can pair with this app.js (Discord).
    A missing Closet Market id must not throw inside main() and skip the rest
    of config / auth / handler setup."""
    js = _read("webapp/client/app.js")
    body = js[
        js.index("function applyClosetMarketVisibility(") : js.index("function closetBidRender(s)")
    ]
    assert "el(id).hidden" not in body
    assert "el('market-book').hidden" not in body
    assert "el('market-book').hidden" not in js  # switchMarketTab too
    for element_id in (
        "closet-bid-new-btn",
        "closet-bid-confirm-btn",
        "closet-bid-cancel-btn",
        "closet-bid-price",
    ):
        assert f"el('{element_id}').on" not in js, element_id
    assert "if (closetBidNote) closetBidNote.textContent" in js


def test_closet_ask_errors_are_surfaced():
    """Staging 2026-09-16: a 409 trustline_required from POST /api/closet/ask
    was an unhandled rejection — the list modal closed and nothing happened."""
    js = _read("webapp/client/app.js")
    body = js[
        js.index("async function postClosetAsk(") : js.index("async function loadClosetMine()")
    ]
    assert "e.body.code === 'trustline_required'" in body
    assert "startBrixTrustline(" in body
    assert "el('market-list-confirm-btn').onclick = submitListForm;" not in js
    assert "submitListForm().catch((e) => showError(e.message))" in js


def test_every_shown_panel_is_registered():
    """Staging 2026-09-16: showPanel('closet-bid-form-panel') hid every panel in
    ALL_PANELS but the form itself was not in the list, so Place a bid left a
    blank screen. Every literal showPanel target must be registered."""
    js = _read("webapp/client/app.js")
    registry = js[
        js.index("const ALL_PANELS = [") : js.index("];", js.index("const ALL_PANELS = ["))
    ]
    registered = set(re.findall(r"'([a-z0-9-]+)'", registry))
    shown = set(re.findall(r"showPanel\('([a-z0-9-]+)'\)", js))
    assert shown, "no showPanel targets found"
    assert shown <= registered, sorted(shown - registered)


_NODE = shutil.which("node")


def _run_post_closet_ask(scenario: str) -> dict:
    """Execute app.js's real postClosetAsk under Node with its collaborators
    stubbed, returning what the stubs observed."""
    if _NODE is None:
        pytest.skip("node is not installed on this host")
    js = _read("webapp/client/app.js")
    start = js.index("async function postClosetAsk(")
    src = js[start : js.index("\nasync function ", start + 1)]
    script = (
        src
        + """
const seen = { calls: [], errors: [], trustline: 0, panels: [] };
let reject = SCENARIO;
async function api(path, opts) {
  seen.calls.push([path, opts.body]);
  if (reject) {
    const e = new Error(reject === 'trustline' ? 'a BRIX trustline is required' : 'that trait is not available');
    e.status = 409;
    e.body = { code: reject === 'trustline' ? 'trustline_required' : 'not_available' };
    reject = null;
    throw e;
  }
  return { order: {}, fill: null };
}
let onSet = null;
function startBrixTrustline(opts) { seen.trustline += 1; onSet = opts.onSet; }
function showError(msg) { seen.errors.push(msg); }
function showPanel(id) { seen.panels.push(id); }
function switchMarketTab() {}
(async () => {
  const item = { slot: 'Hat', value: 'Cap' };
  // Exactly the list-confirm wiring: rejections surface via showError.
  await postClosetAsk(item, '12.5').catch((e) => showError(e.message));
  if (onSet) await onSet();
  console.log(JSON.stringify(seen));
})();
""".replace("SCENARIO", json.dumps(scenario))
    )
    proc = subprocess.run(
        [_NODE, "--input-type=module"], input=script, capture_output=True, text=True, timeout=15
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_closet_ask_trustline_required_sets_line_then_reposts_same_ask():
    seen = _run_post_closet_ask("trustline")
    assert seen["trustline"] == 1
    assert seen["errors"] == []
    body = json.dumps({"slot": "Hat", "value": "Cap", "price_brix": "12.5"}, separators=(",", ":"))
    assert seen["calls"] == [["/api/closet/ask", body], ["/api/closet/ask", body]]
    assert seen["panels"] == ["market-panel"]


def test_closet_ask_other_refusal_reaches_show_error():
    seen = _run_post_closet_ask("not_available")
    assert seen["trustline"] == 0
    assert seen["errors"] == ["that trait is not available"]
    assert len(seen["calls"]) == 1
