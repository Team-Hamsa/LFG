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
        # #583: the Mine tab's Closet surfaces are the five intent groups —
        # orders split by side into Selling/Buying, incoming bids and stuck
        # fills into Needs you, settled fills into History.
        "mine-group-needsYou",
        "mine-body-needsYou",
        "mine-group-selling",
        "mine-body-selling",
        "mine-group-buying",
        "mine-body-buying",
        "mine-group-history",
        "mine-body-history",
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
        "function renderMine(",
        "async function openClosetBidForm(",
        "async function placeClosetBid(",
        "async function postClosetAsk(",
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


def test_bid_form_selects_hug_their_text_and_center():
    # The Slot/Trait dropdowns size to their choice and sit centered under
    # their labels — not full-bleed like the price input below them.
    css = re.sub(r"/\*.*?\*/", "", _stylesheet(), flags=re.S)
    rule = re.search(r"#closet-bid-form-panel select\s*\{([^}]*)\}", css)
    assert rule, "no #closet-bid-form-panel select rule"
    body = rule.group(1)
    assert "100%" not in body
    assert re.search(r"width:\s*fit-content", body)
    assert re.search(r"margin:\s*\S+\s+auto\b", body)


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


def test_mine_closet_hides_sell_button_for_none_value():
    """(#516 D5) a harvested-but-empty slot ("None") sits in the Closet as a
    loose asset like any other, but there is nothing to sell — no button.
    Since #583 the decision is mine_pure's (`action: null`, `dim: true`) and
    both Mine renderers skip the button when an item carries no action."""
    pure = _read("webapp/client/mine_pure.js")
    assert "const isNone = a.value === 'None'" in pure
    assert "action: isNone ? null :" in pure
    js = _read("webapp/client/app.js")
    for renderer in ("function renderMineCard(item)", "function renderMineRow(item)"):
        body = js[js.index(renderer) : js.index("\n}\n", js.index(renderer))]
        assert "if (item.action) parts.push(mineActionButton(" in body, renderer


def test_render_chip_list_skips_the_action_button_when_no_action():
    js = _read("webapp/client/app.js")
    body = js[js.index("function renderChipList(") : js.index("function mineTraitImgSrc(")]
    assert "entry.noAction" in body


def test_market_flow_surfaces_wallet_unsupported_error():
    """(#516 D4) bid-create and ask-buy both start through marketFlow; a 409
    wallet_unsupported must not be swallowed by a code-specific branch that
    only recognizes closet_required/trustline_required — its exact server
    text has to reach the flow panel."""
    js = _read("webapp/client/app.js")
    body = js[js.index("async function marketFlow(") : js.index("function marketListRender(")]
    assert "wallet_unsupported" in body


def test_closet_ask_errors_are_surfaced():
    """Staging 2026-09-16: a 409 trustline_required from POST /api/closet/ask
    was an unhandled rejection — the list modal closed and nothing happened."""
    js = _read("webapp/client/app.js")
    body = js[
        js.index("async function postClosetAsk(") : js.index("async function cancelClosetOrder(")
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


# --- Browse > Traits lists Closet asks beside the NFT trait listings ---


def test_wanted_tab_leaves_closet_listings_to_browse():
    """Closet asks are bought from Browse now; Wanted is bids only."""
    html = _read("webapp/client/index.html")
    assert 'id="closet-book-asks"' not in html
    assert 'id="closet-book-asks-empty"' not in html
    js = _read("webapp/client/app.js")
    assert "closet-book-asks" not in js
    assert "buyBestClosetAsk" not in js


def test_listing_detail_tracks_offers_by_listing_key():
    """A Closet ask has no offer_index, so the offer list and the overlay's
    request id key on marketPure.listingKey instead."""
    js = _read("webapp/client/app.js")
    offers = js[
        js.index("function renderListingOffers(") : js.index("async function openListingDetail(")
    ]
    assert "marketPure.listingKey(o) === activeKey" in offers
    assert "o.offer_index === activeOfferIndex" not in offers
    detail = js[
        js.index("async function openListingDetail(") : js.index("async function loadMarketBrowse(")
    ]
    assert "const requestId = vm.key;" in detail
    assert "renderListingOffers(offers, vm.key, offers, offersTotal)" in detail


def _run_open_buy_flow(rows: list) -> dict:
    """Execute app.js's real openBuyFlow under Node against the real pure
    modules, with the flow/dialog collaborators stubbed."""
    if _NODE is None:
        pytest.skip("node is not installed on this host")
    js = _read("webapp/client/app.js")
    start = js.index("async function openBuyFlow(")
    src = js[start : js.index("\nasync function ", start + 1)]
    script = (
        "import * as marketPure from './webapp/client/market_pure.js';\n"
        "import * as closetPure from './webapp/client/closet_market_pure.js';\n"
        "import * as minePure from './webapp/client/mine_pure.js';\n"
        + src
        + """
const seen = { confirms: [], flows: [], errors: [] };
async function confirmDialog(opts) { seen.confirms.push(opts.text); return true; }
function closetFillRender() {}
function marketBuyRender() { return function nftBuyRender() {}; }
async function marketFlow(kind, path, body, render) {
  seen.flows.push([kind, path, body, render === closetFillRender ? 'closetFillRender' : render.name]);
}
function showError(msg) { seen.errors.push(msg); }
(async () => {
  for (const row of ROWS) await openBuyFlow(row);
  console.log(JSON.stringify(seen));
})();
""".replace("ROWS", json.dumps(rows))
    )
    proc = subprocess.run(
        [_NODE, "--input-type=module"],
        input=script,
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=15,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_browse_buy_routes_a_closet_ask_to_the_closet_buy():
    closet_row = {
        "kind": "trait",
        "slot": "Hat",
        "value": "Cap",
        "amount_brix": "4",
        "source": "closet",
        "order_id": "o/1",
        "offer_index": None,
        "nft_id": None,
    }
    nft_row = {
        "kind": "trait",
        "slot": "Hat",
        "value": "Cap",
        "amount_brix": "5",
        "offer_index": "AB",
    }
    seen = _run_open_buy_flow([closet_row, nft_row])
    assert seen["errors"] == []
    assert seen["flows"] == [
        ["closet_fill", "/api/closet/ask/o%2F1/buy", {}, "closetFillRender"],
        ["buy", "/api/market/buy", {"offer_index": "AB"}, "nftBuyRender"],
    ]
    assert seen["confirms"][0] == (
        "4 BRIX — it goes straight into your Closet. Not enough BRIX? You'll pay in XRP instead."
    )


def test_listing_detail_offers_no_buy_on_your_own_listing():
    """Greptile on #550: Browse now shows your own Closet asks, and the server
    refuses a buy of your own listing, so the action must not offer Buy."""
    js = _read("webapp/client/app.js")
    detail = js[
        js.index("async function openListingDetail(") : js.index("async function loadMarketBrowse(")
    ]
    own = detail.index("marketPure.isOwnListing(vm, me && me.wallet)")
    assert "action.textContent = 'Your listing';" in detail[own:]
    assert own < detail.index("action.textContent = `Buy — ${vm.priceLabel}`;")


_API_BASE = "https://api.test"
_CHICKEN = "/api/layer?body=female&trait=Clothing&value=Chicken%20Suit%20Iridescent&thumb=1"
_CROWN = "/api/layer?body=shared&trait=Head&value=Crown&thumb=1"
_LASER = "/api/layer?body=male&trait=Eyes&value=Laser&thumb=1"


def _top_level_fn(js: str, signature: str) -> str:
    start = js.index(signature)
    return js[start : js.index("\n}\n", start) + 3]


def _run_closet_loaders(responses: dict) -> dict:
    """Execute app.js's real loadClosetBook (the Wanted book) and its real
    mineImgSrc over mine_pure's Mine groups, with the real traitLayerSrc /
    mineTraitImgSrc, under Node; returns each surface's image sources. No
    economy state is loaded, so any image has to come from the server's
    image_url, not a body guess."""
    if _NODE is None:
        pytest.skip("node is not installed on this host")
    js = _read("webapp/client/app.js")
    fns = "\n".join(
        _top_level_fn(js, sig)
        for sig in (
            "function traitLayerSrc(",
            "function mineTraitImgSrc(",
            "function mineImgSrc(",
            "async function loadClosetBook(",
        )
    )
    script = (
        "import * as closetPure from './webapp/client/closet_market_pure.js';\n"
        "import * as minePure from './webapp/client/mine_pure.js';\n"
        f"const API_BASE = {json.dumps(_API_BASE)};\n"
        f"const RESPONSES = {json.dumps(responses)};\n"
        """
let economyState = null;
let closetMarketEnabled = true;
const me = { wallet: 'rMe' };
const seen = {};
const THUMB_W = 256;
async function api(path) { return RESPONSES[path]; }
function el(id) { return { id }; }
function imgUrl(u) { return u; }
function renderChipList(container, empty, entries) { seen[container.id] = entries.map((e) => e.imgSrc); }
function openClosetBidForm() {}
function cancelClosetOrder() {}
function fillClosetBid() {}
function viewClosetFill() {}
"""
        + fns
        + """
await loadClosetBook();
// #583: the Mine tab's three Closet surfaces are groups of mine_pure items,
// and their art reaches the DOM through mineImgSrc.
const built = minePure.buildMine({
  mine: {}, bids: {}, closet: RESPONSES['/api/closet/orders/mine'],
  wallet: 'rMe', closetMarketEnabled: true,
});
seen['mine-selling'] = built.selling.map(mineImgSrc);
seen['mine-needsYou'] = built.needsYou.map(mineImgSrc);
seen['mine-history'] = built.history.map(mineImgSrc);
console.log(JSON.stringify(seen));
"""
    )
    proc = subprocess.run(
        [_NODE, "--input-type=module"],
        input=script,
        capture_output=True,
        text=True,
        timeout=15,
        cwd=ROOT,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_closet_market_chips_render_the_server_trait_image():
    """Prod 2026-09-18: the Wanted row for Clothing: Chicken Suit Iridescent
    rendered an empty tile — both Closet Market loaders passed null for the
    image, so mineTraitImgSrc could only guess the art under the active
    character's body. They must use the server's disk-verified image_url."""
    seen = _run_closet_loaders(
        {
            "/api/closet/book": {
                "rows": [
                    {
                        "slot": "Clothing",
                        "value": "Chicken Suit Iridescent",
                        "best_ask_brix": None,
                        "ask_count": 0,
                        "best_bid_brix": "1000",
                        "bid_count": 1,
                        "image_url": _CHICKEN,
                    },
                    {
                        "slot": "Head",
                        "value": "Crown",
                        "best_ask_brix": "12",
                        "ask_count": 1,
                        "best_bid_brix": None,
                        "bid_count": 0,
                        "image_url": _CROWN,
                    },
                ]
            },
            "/api/closet/orders/mine": {
                "orders": [
                    {
                        "id": "O1",
                        "side": "ask",
                        "slot": "Eyes",
                        "value": "Laser",
                        "price_brix": "5",
                        "state": "open",
                        "image_url": _LASER,
                    }
                ],
                "bids_on_my_traits": [
                    {
                        "id": "B1",
                        "slot": "Head",
                        "value": "Crown",
                        "price_brix": "10",
                        "source": "closet",
                        "nft_id": None,
                        "image_url": _CROWN,
                    }
                ],
                "fills": [
                    {
                        "id": "F1",
                        "seller": "rMe",
                        "buyer": "rYou",
                        "slot": "Clothing",
                        "value": "Chicken Suit Iridescent",
                        "price_brix": "1000",
                        "state": "mirrored",
                        "image_url": _CHICKEN,
                    }
                ],
            },
        }
    )
    assert seen == {
        "closet-book-bids": [_API_BASE + _CHICKEN],
        "mine-selling": [_API_BASE + _LASER],
        "mine-needsYou": [_API_BASE + _CROWN],
        "mine-history": [_API_BASE + _CHICKEN],
    }
