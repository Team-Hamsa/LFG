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


def test_index_has_the_brix_claim_button_in_the_home_action_row():
    """The drip is one action button alongside Mint/Build/Swap/Trade — not a
    section of its own (user call, 2026-08-24)."""
    html = _read("index.html")
    assert 'id="brix-claim-btn"' in html
    home = html.split('id="mint-panel"', 1)[1].split("</section>", 1)[0]
    actions = home.split('class="actions"', 1)[1].split('id="leaderboard"', 1)[0]
    assert 'id="brix-claim-btn"' in actions
    # ...in the same row as the other action buttons.
    assert actions.index('id="brix-claim-btn"') > actions.index('id="market-btn"')
    btn = re.search(r'<button id="brix-claim-btn"[^>]*>', html)
    assert btn and 'class="secondary"' in btn.group(0)


def test_button_starts_hidden():
    """brixCardView decides visibility; an unarmed deployment must never
    flash a payout button before the first fetch resolves."""
    html = _read("index.html")
    btn = re.search(r'<button id="brix-claim-btn"[^>]*>', html)
    assert btn and "hidden" in btn.group(0)


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


def test_transient_poll_errors_keep_polling_then_refresh():
    """Greptile P1 on PR #434: a transient /api/brix/claim/{id} failure used
    to end the poll silently, stranding the button on "Claiming…" until the
    next home landing. The catch must reschedule within the same tick budget,
    and an exhausted budget must re-read status rather than freeze."""
    js = _read("app.js")
    body = js.split("function pollBrixClaim(", 1)[1][:2500]
    catch = body.split("} catch (e) {", 1)[1].split(
        "if (gen !== brixPollGen) return;\n    if (brixPure", 1
    )[0]
    assert "setTimeout(tick, BRIX_POLL_MS)" in catch
    assert "loadBrix({ poll: false })" in catch
    # Both budget-exhaustion paths refresh WITHOUT restarting the poll —
    # otherwise loadBrix() sees the same open claim and resets the tick
    # counter, making the bound meaningless (Greptile P1, second round).
    assert body.count("if (ticks >= BRIX_POLL_MAX) { loadBrix({ poll: false }); return; }") == 2
    load = js.split("async function loadBrix(", 1)[1][:1200]
    # poll:false returns before the poll/stopBrixPoll branch — a late-resolving
    # exhausted refresh must not cancel a poll a newer landing started (CodeRabbit).
    assert load.index("if (!poll) return view;") < load.index("else stopBrixPoll();")
    # ...and a poll-less refresh whose generation moved while fetching is
    # dropped before render, so it can't repaint "Claiming…" over a newer
    # poll's terminal render (Greptile, third round).
    assert load.index(
        "if (loadGen !== brixLoadGen || pollGen !== brixPollGen) return null;"
    ) < load.index("renderBrixCard(view)")


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
    assert int(app_v.group(1)) >= 76, "app.js?v= regressed below the shipped version"
    brix_v = re.search(r"from './brix_pure\.js\?v=(\d+)'", js)
    assert brix_v, "app.js does not cache-bust the brix_pure.js import"
    # Same ratchet: raise with every brix_pure.js ?v= bump.
    assert int(brix_v.group(1)) >= 6, "brix_pure.js?v= regressed below the shipped version"


# --- BRIX trustline flow (#441) -----------------------------------------------


def test_index_has_a_trustline_panel_shaped_like_the_signin_panel():
    html = _read("index.html")
    assert 'id="trustline-panel"' in html
    for part in (
        "trustline-sub",
        "trustline-spinner",
        "trustline-qr",
        "trustline-link-btn",
        "trustline-qr-toggle",
        "trustline-retry-btn",
        "trustline-back-btn",
    ):
        assert f'id="{part}"' in html, part


def test_claim_button_routes_to_the_trustset_flow_when_a_line_is_needed():
    """The trustline_required lock is actionable: the button stays ENABLED
    under its lock label and the click starts the TrustSet flow instead of
    another doomed claim POST."""
    js = _read("app.js")
    render = js.split("function renderBrixCard(", 1)[1][:1500]
    assert "brixTrustlineNeeded" in render
    claim = js.split("async function claimBrix(", 1)[1][:600]
    assert "startBrixTrustline" in claim
    assert "'/api/brix/trustline'" in js
    assert "/api/brix/trustline/${" in js
    assert "applySignDelivery" in js.split("function renderTrustline(", 1)[1][:800]


def test_trait_buy_trustline_required_reuses_the_flow():
    js = _read("app.js")
    body = js.split("async function marketFlow(", 1)[1][:1500]
    assert "trustline_required" in body
    assert "startBrixTrustline" in body


def test_home_landing_clears_the_trustline_arm():
    js = _read("app.js")
    home = js.split("function showMintHome()", 1)[1][:1200]
    assert "brixTrustlineNeeded = false" in home


def test_trustline_panel_is_registered_with_show_panel():
    # showPanel() only un-hides ids in ALL_PANELS; an unregistered panel keeps
    # its shipped `hidden` attribute forever (CodeRabbit on #442).
    src = open(os.path.join(CLIENT, "app.js")).read()
    m = re.search(r"const ALL_PANELS = \[(.*?)\];", src, re.S)
    assert m and "'trustline-panel'" in m.group(1)


def test_trait_buy_reissues_after_trustline_set():
    src = open(os.path.join(CLIENT, "app.js")).read()
    assert "onSet: () => marketFlow(kind, startPath, body, render)" in src
    assert "(trustlineOnSet || trustlineBack || showMintHome)()" in src


def test_trustline_poll_rechecks_staleness_after_the_await():
    # A late status response must not fire the captured onSet continuation
    # over whatever panel the user moved to (Greptile on #442).
    src = open(os.path.join(CLIENT, "app.js")).read()
    start = src.index("function pollTrustline")
    body = src[start : src.index("\n}\n", start)]
    await_pos = body.index("s = await api(")
    assert body.find("if (stale()) return;", await_pos) >= 0, "no stale check after the await"
    assert body.find("if (stale()) return;") < await_pos, "no stale check before the await"
    assert "gen !== trustlinePollGen" in body
