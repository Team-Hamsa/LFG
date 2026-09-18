# tests/test_market_panel_dom.py
# Task 10 (#44): source-assertion guard for the marketplace panel's HTML/JS
# wiring, mirroring test_app_js_boot.py / test_leaderboard_selector.py.
# market_pure.js's pure functions are executed in test_market_pure_js.py;
# the listing detail overlay (openListingDetail) also runs here under Node
# against a stub DOM — see _run_open_listing_detail.
import json
import os
import re
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLIENT = os.path.join(ROOT, "webapp", "client")
_NODE = shutil.which("node")


def _read(name: str) -> str:
    with open(os.path.join(CLIENT, name), encoding="utf-8") as f:
        return f.read()


def _stylesheet() -> str:
    """The app stylesheet's contents, resolved from index.html rather than a
    hardcoded name: the file carries its cache version in the FILENAME
    (Discord's desktop asset cache keys on path and ignores `?v=` query
    strings), so the name changes whenever the CSS must bust that cache."""
    html = _read("index.html")
    m = re.search(r'<link rel="stylesheet" href="(style[^"]*\.css)[^"]*"', html)
    assert m, "index.html has no app stylesheet <link>"
    return _read(m.group(1))


def test_index_has_market_panel_and_nav_entry():
    html = _read("index.html")
    assert 'id="market-panel"' in html
    assert 'id="market-btn"' in html  # nav entry alongside mint/swap/swapper
    assert 'id="market-tabs"' in html and 'data-tab="browse"' in html and 'data-tab="mine"' in html
    assert (
        'id="market-kind"' in html
        and 'data-kind="character"' in html
        and 'data-kind="trait"' in html
    )
    assert 'id="market-list-form-panel"' in html


def test_index_has_mine_groups():
    html = _read("index.html")
    for group_id in ("mine-listings", "mine-characters", "mine-traits", "mine-closet"):
        assert f'id="{group_id}"' in html


def test_app_js_imports_market_pure():
    js = _read("app.js")
    assert "from './market_pure.js" in js  # ?v= cache-buster suffix allowed


def test_app_js_has_single_market_flow_driver():
    js = _read("app.js")
    assert "async function marketFlow(kind, startPath, body, render)" in js
    # Reused by all four ops (spec §Q8), not one-off per-op QR/poll code.
    for call in [
        "marketFlow('buy', '/api/market/buy'",
        "marketFlow('cancel', '/api/market/cancel'",
        "marketFlow('list', '/api/market/list'",
        "'trait_list', '/api/market/trait/list'",
    ]:
        assert call in js, f"missing marketFlow call: {call}"


def test_app_js_never_uses_window_confirm():
    # Discord's sandboxed iframe makes native window.confirm a silent no-op;
    # every confirmation must route through the existing confirmDialog overlay.
    js = _read("app.js")
    assert "window.confirm(" not in js
    assert "confirmDialog(" in js
    # New marketplace confirmations specifically use the overlay.
    assert js.count("confirmDialog(") >= 3  # buy, cancel, list-form (at least)


def test_app_js_royalty_disclosure_and_closet_prompt_wired():
    js = _read("app.js")
    assert "marketPure.royaltyDisclosure(" in js
    # #133: buy-flow royalty math goes through the no-throw seam.
    assert "marketPure.safeComputeRoyalty(" in js
    assert "marketPure.CLOSET_REQUIRED_MESSAGE" in js
    assert "promptClosetRequired" in js
    assert "added to your Closet" in js


def test_app_js_trait_wizard_step_labels_used():
    js = _read("app.js")
    assert "marketPure.traitWizardStepLabel(" in js


def test_no_70_percent_or_30_percent_fee_copy_anywhere_in_client():
    # Global constraint: fee copy is ALWAYS "7% / seller nets 93%". Scoped to
    # the copy-bearing files only — style.css legitimately contains unrelated
    # "70%" values (an animation keyframe offset, a skeleton-loader width)
    # that have nothing to do with the royalty split.
    for name in ("app.js", "index.html", "market_pure.js"):
        src = _read(name)
        assert "70%" not in src, f"{name} contains the corrected 70% myth"
        assert "30%" not in src, f"{name} contains the corrected 30% myth"


def test_market_pure_js_says_93_and_7_percent():
    src = _read("market_pure.js")
    assert "93%" in src and "7%" in src


def test_mock_market_module_exists_and_wired_into_service(monkeypatch):
    # Task 10 requires a dev-mode mock for the market endpoints (unlike the
    # rest of #44's tasks, the real handlers had no WEBAPP_DEV_MODE branch
    # before this task — see webapp/test_market_dev_mode.py for behavior).
    import importlib

    mock_market = importlib.import_module("webapp.mock_market")
    assert hasattr(mock_market, "INSTANCE")
    app_src_path = os.path.join(ROOT, "lfg_service", "app.py")
    with open(app_src_path, encoding="utf-8") as f:
        app_src = f.read()
    assert "mock_market" in app_src
    assert "config.WEBAPP_DEV_MODE" in app_src


def test_app_js_buy_flow_surfaces_bad_price_instead_of_throwing():
    # #133: computeRoyalty throws on a malformed listing price; openBuyFlow
    # must route that through the no-throw seam + showError, never raw
    # computeRoyalty (whose rejection the card onclick would swallow).
    js = _read("app.js")
    assert "safeComputeRoyalty(" in js
    assert "marketPure.computeRoyalty(" not in js


def test_app_js_market_click_seams_catch_async_throws():
    # #133: no marketplace card/chip click may fail silently — every async
    # handler fired from a grid card or Mine chip routes rejections to
    # showError at the onclick seam.
    js = _read("app.js")
    assert "openBuyFlow(row).catch((e) => showError(e.message))" in js
    assert (
        "Promise.resolve().then(() => onAction(entry.payload)).catch((e) => showError(e.message))"
        in js
    )


def test_external_buy_now_wiring():
    # #426: an external row with a server-computed clearing price gets a
    # primary "Buy now" action (the plain bid flow at clearing_xrp), the fee
    # line, a demoted deep link, and an honest fill watch after the bid lands.
    html = _read("index.html")
    js = _read("app.js")
    assert 'id="listing-detail-external"' in html
    assert 'id="listing-detail-fee"' in html
    assert "marketPure.buyNowLabel(vm)" in js
    assert "marketPure.externalFeeNote(vm)" in js
    assert "price_xrp: vm.clearingXrp" in js
    assert "marketPure.externalFillCopy(marketplace" in js
    assert "MARKET_STATUS_PATH.bid(sessionId)" in js
    assert "'/api/market/bid'" in js


def test_own_external_listing_offers_no_buy_now():
    # A seller opening their OWN xrp.cafe-listed NFT (e.g. via "My listings
    # only") got an enabled Buy now: a bid on their own NFT, which the server
    # refuses (400) only after the confirm dialog. The own-listing check must
    # run before both external branches. The marketplace link stays so the
    # seller can manage the listing there; the fee note goes (nothing to pay).
    js = _read("app.js")
    start = js.index("async function openListingDetail(")
    body = js[start : js.index("\n}\n", start)]
    own = body.index("marketPure.isOwnListing(vm, me && me.wallet)")
    assert own < body.index("buyExternalNow(row, vm)"), "own check must precede Buy now"
    assert own < body.index("`Buy on ${vm.marketplace} ↗`"), "own check must precede link-out"
    own_branch = body[own : body.index("} else if", own)]
    assert "action.textContent = 'Your listing';" in own_branch
    assert "action.disabled = true;" in own_branch
    assert "showMarketplaceLink();" in own_branch
    assert "feeNote" not in own_branch
    # The link is the gated "View on <marketplace> ↗" secondary, shared with
    # the Buy-now branch.
    link = body[body.index("const showMarketplaceLink = () => {") :]
    link = link[: link.index("\n  };\n")]
    assert "if (!vm.externalUrl) return;" in link
    assert "extLink.textContent = `View on ${vm.marketplace} ↗`;" in link
    assert "extLink.hidden = false;" in link


def _run_open_listing_detail(cases: list) -> list:
    """Execute app.js's real openListingDetail under Node against the real pure
    modules and a stub DOM, once per [row, signed-in wallet] case. Returns what
    the overlay shows, plus what pressing its primary action (when enabled) and
    its marketplace link (when shown) called."""
    if _NODE is None:
        pytest.skip("node is not installed on this host")
    js = _read("app.js")
    start = js.index("async function openListingDetail(")
    src = js[start : js.index("\n}\n", start) + 3]
    script = (
        "import * as marketPure from './webapp/client/market_pure.js';\n"
        "import * as closetPure from './webapp/client/closet_market_pure.js';\n"
        "import * as mediaPure from './webapp/client/media_pure.js';\n"
        + src
        + """
const calls = [];
let nodes = {};
function node() {
  return {
    hidden: false, textContent: '', disabled: false, onclick: null, value: '', placeholder: '',
    classList: { toggle() {} }, querySelector: node, replaceChildren() {}, appendChild() {},
    focus() {},
  };
}
function el(id) { return (nodes[id] ||= node()); }
function setMedia(id) { return el(id); }
function marketRowImgSrc() { return ''; }
const BLANK_IMG = '';
globalThis.document = { activeElement: null, createElement: node };
// A direct window.open bypasses the Discord SDK / Telegram openers.
globalThis.window = { open: (url, target, features) => calls.push(['window.open', url, features]) };
function openExternal(url, features) { calls.push(['openExternal', url, features]); }
async function buyExternalNow(row, vm) { calls.push(['buyExternalNow', vm.clearingXrp]); }
async function openBuyFlow(row) { calls.push(['openBuyFlow', row.offer_index]); }
function closeListingDetail() {}
function renderListingOffers() {}
function renderListingHistory() {}
async function api() { return {}; }
function showError(msg) { calls.push(['showError', msg]); }
async function placeBid() {}
async function placeClosetBid() {}
let me = null;
let closetMarketEnabled = false;
let activeListingId = null;
let lastListingTrigger = null;
(async () => {
  const out = [];
  for (const [row, wallet] of CASES) {
    nodes = {};
    calls.length = 0;
    me = wallet ? { wallet } : null;
    await openListingDetail(row);
    const action = el('listing-detail-action');
    const link = el('listing-detail-external');
    const fee = el('listing-detail-fee');
    const seen = {
      action: action.textContent,
      disabled: action.disabled,
      link: link.hidden ? null : link.textContent,
      fee: !fee.hidden,
      bid: !el('listing-detail-bid').hidden,
    };
    if (action.onclick && !action.disabled) action.onclick();
    if (!link.hidden) link.onclick();
    await new Promise((resolve) => setTimeout(resolve, 0));
    out.push({ ...seen, calls: [...calls] });
  }
  console.log(JSON.stringify(out));
})();
""".replace("CASES", json.dumps(cases))
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


_CAFE_URL = "https://xrp.cafe/nft/00AA"
# A Buy-now external row: xrp.cafe's fee rate is measured, so the server sent
# the clearing price (#426).
_CAFE_ROW = {
    "kind": "character",
    "nft_id": "00AA",
    "nft_number": 42,
    "seller": "rSeller",
    "offer_index": "OFF1",
    "amount_xrp": "4",
    "amount_drops": "4000000",
    "source": "external",
    "buyable": False,
    "marketplace": "xrp.cafe",
    "external_url": _CAFE_URL,
    "broker_rate": 0.01589,
    "clearing_xrp": "4.064587",
    "clearing_drops": "4064587",
}
# Same listing on a broker whose fee is unmeasured: no clearing price.
_UNMEASURED_ROW = {
    **_CAFE_ROW,
    "broker_rate": None,
    "clearing_xrp": None,
    "clearing_drops": None,
}
_IN_APP_ROW = {
    "kind": "character",
    "nft_id": "00BB",
    "nft_number": 7,
    "seller": "rSeller",
    "offer_index": "OFF2",
    "amount_xrp": "10",
    "amount_drops": "10000000",
}


def test_listing_detail_actions_for_own_and_external_rows():
    # Executes the overlay: your own listing is never buyable (external or
    # not), and every marketplace link opens through openExternal (Discord
    # SDK / Telegram bridge; noopener in a plain browser), never window.open.
    own_cafe, other_cafe, own_unmeasured, other_unmeasured, own_in_app = _run_open_listing_detail(
        [
            [_CAFE_ROW, "rSeller"],
            [_CAFE_ROW, "rBuyer"],
            [_UNMEASURED_ROW, "rSeller"],
            [_UNMEASURED_ROW, "rBuyer"],
            [_IN_APP_ROW, "rSeller"],
        ]
    )
    cafe_link = ["openExternal", _CAFE_URL, "noopener"]
    # The bug: this was an enabled "Buy now" whose bid the server refuses.
    assert own_cafe == {
        "action": "Your listing",
        "disabled": True,
        "link": "View on xrp.cafe ↗",
        "fee": False,
        "bid": False,
        "calls": [cafe_link],
    }
    assert other_cafe == {
        "action": "Buy now — 4.07 XRP via xrp.cafe",
        "disabled": False,
        "link": "View on xrp.cafe ↗",
        "fee": True,
        "bid": True,
        "calls": [["buyExternalNow", "4.064587"], cafe_link],
    }
    assert own_unmeasured == {
        "action": "Your listing",
        "disabled": True,
        "link": "View on xrp.cafe ↗",
        "fee": False,
        "bid": False,
        "calls": [cafe_link],
    }
    assert other_unmeasured == {
        "action": "Buy on xrp.cafe ↗",
        "disabled": False,
        "link": None,
        "fee": False,
        "bid": True,
        "calls": [cafe_link],
    }
    assert own_in_app == {
        "action": "Your listing",
        "disabled": True,
        "link": None,
        "fee": False,
        "bid": False,
        "calls": [],
    }


def test_external_listing_wiring():
    # #131: external (brokered) listings — toggle in the filter bar, distinct
    # disabled card treatment, no in-app buy path for external rows.
    html = _read("index.html")
    assert 'id="market-include-external"' in html
    js = _read("app.js")
    assert "el('market-include-external')?.checked" in js
    assert "market-card-external" in js
    assert "marketPure.externalLabel(vm)" in js
    # External cards never enter openBuyFlow; they link out (or explain).
    assert "vm.externalUrl" in js
    css = _stylesheet()
    assert ".nft-card.market-card-external" in css


def test_buy_now_external_cards_render_standard():
    # A Buy-now external row (measured broker fee) is a standard card: the
    # faded treatment + "Listed on" badge are gated on externalLook(vm), and
    # the toggle unchecked still fetches those rows ('supported' mode) so
    # it hides only the externals we can't buy here.
    js = _read("app.js")
    grid = js[js.index("function renderMarketGrid") : js.index("function closeListingDetail")]
    assert "if (marketPure.externalLook(vm)) {" in grid
    assert "card.classList.add('market-card-external')" in grid
    assert "? true : 'supported'" in js


def test_browse_ux_wiring():
    # #203: pagination, rarity sort, "listed by me", and the listing detail
    # overlay replacing card-click-straight-to-buy.
    html = _read("index.html")
    assert 'value="rarity_desc"' in html
    assert 'id="market-mine-only"' in html
    assert 'id="market-load-more"' in html
    for el_id in (
        "listing-overlay",
        "listing-detail-img",
        "listing-detail-title",
        "listing-detail-attrs",
        "listing-detail-history",
        "listing-detail-action",
        "listing-detail-close",
    ):
        assert f'id="{el_id}"' in html
    js = _read("app.js")
    assert "openListingDetail(row)" in js
    assert "loadMarketBrowse({ append: true })" in js
    assert "el('market-mine-only')?.checked" in js
    assert "marketPure.rarityLabel(vm)" in js
    assert "/api/market/history?" in js
    css = _stylesheet()
    assert ".listing-detail" in css
    assert ".market-card-rarity" in css


def test_bids_wiring():
    # #283: bids UI — Place-bid in the detail overlay, My bids / incoming
    # bids groups in Mine, bid + bid_accept flow routing.
    html = _read("index.html")
    for el_id in (
        "mine-bids",
        "mine-incoming-bids",
        "listing-detail-bids",
        "listing-bid-form",
        "listing-bid-price",
        "listing-bid-confirm",
        "listing-detail-bid",
    ):
        assert f'id="{el_id}"' in html
    js = _read("app.js")
    assert "bid: (id) => `/api/market/bid/${id}`" in js
    assert "bid_accept: (id) => `/api/market/bid/accept/${id}`" in js
    assert "function marketBidRender" in js
    assert "function marketBidAcceptRender" in js
    assert "'/api/market/bids/mine'" in js
    assert "renderChipList(el('mine-incoming-bids')" in js
    # Bids are character-only and never offered on the viewer's own listing.
    assert "vm.kind === 'character' && (!me || !me.wallet || me.wallet !== vm.seller)" in js


def test_shop_filter_wiring():
    # #217 follow-up: Shop slot chips + live search + sort, all client-side.
    html = _read("index.html")
    assert 'id="shop-slot-chips"' in html
    assert 'id="shop-search"' in html
    assert 'id="shop-sort"' in html
    js = _read("app.js")
    assert "marketPure.filterShopItems(shopState.items, shopState)" in js
    assert "marketPure.shopSlotCounts(shopState.items)" in js
    assert "el('shop-search').oninput" in js
    assert "el('shop-sort').onchange" in js


def test_grouped_trait_cards_wiring():
    # #481: trait browse asks the server to collapse duplicate (slot, value)
    # listings; the card shows a count badge and the detail overlay lists the
    # individual offers so a buyer can still pick a specific one.
    js = _read("app.js")
    assert "group: isTrait" in js
    assert "market-card-count" in js
    assert "function renderListingOffers(" in js
    html = _read("index.html")
    assert 'id="listing-detail-offers"' in html
    css = _stylesheet()
    assert ".market-card-count" in css
    assert ".listing-offers" in css


def test_grouped_trait_count_badge_survives_card_rebuild():
    # The card body is rebuilt with card.replaceChildren(img, name); a badge
    # appended BEFORE that call is silently dropped (CodeRabbit on #482).
    js = _read("app.js")
    start = js.index("function renderMarketGrid(")
    end = js.index("\n}\n", start)
    body = js[start:end]
    rebuild = body.index("card.replaceChildren(img, name)")
    badge = body.index("cnt.className = 'market-card-count'")
    assert badge > rebuild, "count badge must be appended after replaceChildren"


def _enclosing_preludes(css: str, idx: int) -> list[str]:
    """Preludes of every block enclosing css[idx], innermost first — e.g.
    [".listing-detail-body", "@container listing-detail (min-width: 600px)"].
    Expects comments already stripped."""
    out, depth = [], 0
    for i in range(idx - 1, -1, -1):
        if css[i] == "}":
            depth += 1
        elif css[i] == "{":
            if depth:
                depth -= 1
            else:
                start = max(css.rfind(ch, 0, i) for ch in ";{}") + 1
                out.append(" ".join(css[start:i].split()))
    return out


def test_trait_listing_detail_is_compact():
    # A trait listing is three short lines and one Buy button; the 760px
    # side-by-side dialog is sized for a character's ~9 attribute chips and
    # three-button footer, so a trait opened into a mostly empty box.
    js = _read("app.js")
    start = js.index("async function openListingDetail(")
    body = js[start : js.index("\n}\n", start)]
    assert "classList.toggle('listing-detail-compact', vm.kind === 'trait')" in body
    css = re.sub(r"/\*.*?\*/", "", _stylesheet(), flags=re.S)
    full = re.search(r"\.listing-detail\s*\{[^}]*?width:\s*min\((\d+)px", css)
    compact = re.search(r"\.listing-detail-compact\s*\{[^}]*?width:\s*min\((\d+)px", css)
    assert full and compact and int(compact.group(1)) < int(full.group(1))
    # Art-beside-details keys off the dialog's own width, not the viewport:
    # a viewport query would split the narrow trait card into two cramped
    # columns on every wide screen.
    assert re.search(
        r"\.listing-detail\s*\{[^}]*container:\s*listing-detail\s*/\s*inline-size", css
    )
    grids = list(re.finditer(r"\.listing-detail-body\s*\{[^}]*display:\s*grid", css))
    assert grids
    for m in grids:
        preludes = _enclosing_preludes(css, m.end() - 1)
        assert any(p.startswith("@container listing-detail") for p in preludes), preludes


def _direct_parent_ids(html: str) -> dict:
    """Map each element id to the id of its direct parent element."""
    from html.parser import HTMLParser

    void = {"area", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "wbr"}

    class Parents(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.stack: list = []
            self.parents: dict = {}

        def handle_starttag(self, tag, attrs):
            own = dict(attrs).get("id")
            if own:
                self.parents[own] = self.stack[-1][1] if self.stack else None
            if tag not in void:
                self.stack.append((tag, own))

        def handle_endtag(self, tag):
            while self.stack:
                if self.stack.pop()[0] == tag:
                    break

    p = Parents()
    p.feed(html)
    return p.parents


def test_standalone_card_inputs_are_capped_not_full_bleed():
    """Discord desktop 2026-09-18: at >=760px the app widens to 880px, and the
    global `input[type="text"] { width: 100% }` stretched the lone price box on
    Place a bid and List for sale to ~790px. A text input sitting directly in a
    card is capped instead."""
    rule = re.search(r'\.card > input\[type="text"\]\s*\{([^}]*)\}', _stylesheet())
    assert rule, 'no `.card > input[type="text"]` rule'
    cap = re.search(r"max-width:\s*(\d+)px", rule.group(1))
    assert cap and int(cap.group(1)) <= 320
    # The amount reads centered, like the rest of the card.
    assert re.search(r"text-align:\s*center", rule.group(1))
    # The cap only reaches inputs that are direct children of their card.
    parents = _direct_parent_ids(_read("index.html"))
    assert parents["closet-bid-price"] == "closet-bid-form-panel"
    assert parents["market-list-price"] == "market-list-form-panel"
