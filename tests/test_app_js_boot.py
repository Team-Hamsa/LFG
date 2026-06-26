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
