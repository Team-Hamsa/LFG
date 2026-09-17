import os
import re

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


def test_mine_closet_hides_sell_button_for_none_value():
    """(#516 D5) a harvested-but-empty slot ("None") sits in the Closet as a
    loose asset like any other, but there is nothing to sell — no button."""
    js = _read("webapp/client/app.js")
    body = js[
        js.index("function renderMineGroups(data)") : js.index("function renderBidGroups(data)")
    ]
    assert "noAction: a.value === 'None'" in body


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
