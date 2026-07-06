# tests/test_market_panel_dom.py
# Task 10 (#44): source-assertion guard for the marketplace panel's HTML/JS
# wiring, mirroring test_app_js_boot.py / test_leaderboard_selector.py (the
# webapp client has no JS execution harness for DOM code — only
# market_pure.js's pure functions are executed, see test_market_pure_js.py).
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLIENT = os.path.join(ROOT, "webapp", "client")


def _read(name: str) -> str:
    with open(os.path.join(CLIENT, name), encoding="utf-8") as f:
        return f.read()


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
    assert "from './market_pure.js'" in js


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
    assert "marketPure.computeRoyalty(" in js
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
