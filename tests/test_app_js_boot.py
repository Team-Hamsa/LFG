# tests/test_app_js_boot.py
# The webapp client is no-build vanilla JS (no JS test harness), so this guards
# the dual-mode boot (#89, Part A) by asserting the source contains the Telegram
# branch AND preserves the Discord path (regression). True end-to-end is a Part B
# verification once the public URL exists.
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLIENT = os.path.join(ROOT, "webapp", "client")


def _read(name: str) -> str:
    with open(os.path.join(CLIENT, name), encoding="utf-8") as f:
        return f.read()


def test_app_js_has_telegram_branch():
    src = _read("app.js")
    assert "window.Telegram" in src
    assert "insideTelegram" in src
    assert "/api/telegram/auth" in src
    assert "setupTelegram" in src


def test_app_js_preserves_discord_path():
    # Regression guard: the existing Discord boot must be untouched.
    src = _read("app.js")
    assert "insideDiscord" in src
    assert "setupDiscord" in src
    assert "/api/token" in src


def test_app_js_uses_tg_initdata_and_openlink():
    src = _read("app.js")
    assert "tg.initData" in src
    assert "tg.openLink" in src


def test_app_js_guards_layer_requests_against_incomplete_metadata():
    # #100: NFTs with empty body / "None" Body value must not issue layer fetches
    # (those 400 on empty params). Assert the guard helper exists and is used at
    # every layerSrc() call site, and that the renderRoster fallback no longer
    # unconditionally builds a layer URL from a possibly-empty body.
    src = _read("app.js")
    # The guard helper is defined and exercised.
    assert "function layerComplete(" in src
    assert src.count("layerComplete(") >= 4  # definition + 3 call-site guards
    # renderCanvas degrades to an "indexing" placeholder for empty body.
    assert "still indexing" in src
    # The old guaranteed-400 roster fallback (layerSrc(... 'None')) is gone.
    assert "|| 'None')" not in src


def test_app_js_has_closet_states():
    src = _read("app.js")
    assert "Create your Closet" in src
    assert "Finish claiming your Closet" in src
    assert "/api/closet" in src
    # reads the nested token path (not the flat .status that the brief had wrong)
    assert "closet.token" in src
    # post-harvest claim block is gone — "Claim your Closet" was the old title
    assert "👜 Claim your Closet" not in src


def test_app_js_has_extract_deposit():
    src = _read("app.js")
    assert "/api/extract" in src and "/api/deposit" in src
    assert "economyState.trait_tokens" in src or "trait_tokens" in src
    assert "Extract" in src and "Deposit" in src
    assert "renderTraitStrip" in src


def test_app_js_renders_animated_nfts_as_video():
    # #250: animated NFTs ship an .mp4 next to the PNG still — result/chooser
    # artwork must render as <video>, not the frozen poster frame.
    src = _read("app.js")
    # The single media helper (and its fixed-id-slot wrapper) exist.
    assert "function mediaEl(" in src
    assert "function setMedia(" in src
    # Inline-autoplay attributes (webview autoplay policies gate on them).
    for attr in ("'muted'", "'autoplay'", "'loop'", "'playsinline'"):
        assert f"setAttribute({attr}" in src
    # Wired in: swap results, mint hero (defensive until #249 lands), assemble.
    assert "r.video_url" in src
    assert "s.video_url" in src
    assert "final.video_url" in src
    # Roster grid stays stills but flags animated cards.
    assert "anim-badge" in src
    assert ".anim-badge" in _read("style.css")


def test_leaderboard_card_present():
    html = _read("index.html")
    assert 'id="leaderboard"' in html and 'data-cat="brix"' in html


def test_app_js_wires_leaderboard():
    js = _read("app.js")
    assert "/api/leaderboard" in js and "loadLeaderboard" in js


def test_telegram_webapp_js_vendored_same_origin():
    # Vendored same-origin (not hotlinked) per the spec.
    assert os.path.exists(os.path.join(CLIENT, "telegram-web-app.js"))
    html = _read("index.html")
    assert "telegram-web-app.js" in html
    # Must be loaded BEFORE app.js so window.Telegram is defined at boot.
    # (Match the app.js *module* script tag, not the "app.js" substring inside
    # the "telegram-web-app.js" filename.)
    assert html.index("telegram-web-app.js") < html.index('src="app.js')
    # Same-origin: not hotlinking the CDN.
    assert "telegram.org/js/telegram-web-app.js" not in html
