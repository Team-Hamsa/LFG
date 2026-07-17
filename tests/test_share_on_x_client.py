# tests/test_share_on_x_client.py
# #41 T9: "Share on X" buttons in the no-build vanilla-JS Activity client
# (webapp/client/app.js). There is no JS execution harness in this repo for
# app.js (mirrors tests/test_economy_feature_flag.py's
# test_client_hides_dressup_when_economy_disabled: source-string assertions
# against the file are the established convention for this file, not a real
# browser/JS runtime) — these are source-string guards, not behavioral tests.
#
# No lfg_core/lfg_service import here, so the test-env-guard preamble
# (tests/test_seasons.py lines 1-18) doesn't apply — this file only reads a
# static asset off disk.
import os

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_APP_JS = os.path.join(_ROOT, "webapp", "client", "app.js")


def _read_app_js() -> str:
    with open(_APP_JS, encoding="utf-8") as f:
        return f.read()


def test_share_url_never_uses_location_origin():
    # Spec x-integration §6.1, verbatim: "location.origin must NOT be used
    # for the share URL" — inside the Activity the page is served from
    # Discord's *.discordsays.com sandbox proxy, not our public host; X's
    # crawler can't reach it and the intent would share a dead link.
    src = _read_app_js()
    assert "location.origin" not in src


def test_client_reads_public_share_base_url_from_config():
    src = _read_app_js()
    assert "public_share_base_url" in src


def test_client_reads_bithomp_base_url_from_config():
    src = _read_app_js()
    assert "bithomp_base_url" in src


def test_intent_url_uses_documented_x_domain():
    # recon-xapi.md A5: docs.x.com's canonical documented form is
    # https://x.com/intent/tweet (twitter.com 301s to it; x.com/intent/post
    # works but is undocumented) — emit the documented form.
    src = _read_app_js()
    assert "https://x.com/intent/tweet" in src
    assert "twitter.com/intent" not in src


def test_share_button_routes_through_openexternal_not_raw_window_open():
    # The SDK-aware openExternal() helper (already used for every other
    # outbound link in the sandboxed Activity iframe) must be what fires the
    # intent link — not a bare `window.open` call written fresh for this
    # feature. openExternal() itself still contains the one legitimate
    # `window.open` as its own non-SDK fallback (dev-mode / outside Discord).
    src = _read_app_js()
    intent_idx = src.index("https://x.com/intent/tweet")
    # Search a window around the intent-URL construction for the dispatch
    # call, rather than assuming exact adjacency.
    window = src[max(0, intent_idx - 400) : intent_idx + 800]
    assert "openExternal(" in window


def test_mint_and_swap_share_text_present():
    src = _read_app_js()
    assert "I just minted LFGO #" in src
    assert "I just swapped traits on" in src


def test_copy_link_fallback_present_without_native_dialogs():
    # navigator.clipboard with a visible readonly-input fallback; never
    # window.confirm/alert — both are silent no-ops inside the Discord
    # Activity iframe (repo memory: lfg-services-pm2.md).
    src = _read_app_js()
    assert "navigator.clipboard" in src
    assert "window.confirm(" not in src
    assert "window.alert(" not in src
