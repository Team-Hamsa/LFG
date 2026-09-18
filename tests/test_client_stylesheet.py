"""Contracts of the app stylesheet itself (the one index.html links).

A url() that points at a missing file fails silently in the browser — the
select chevron would just vanish — so a missing asset fails here instead.
"""

import os
import re
from urllib.parse import unquote, urlsplit

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLIENT = os.path.join(ROOT, "webapp", "client")


def _stylesheet_path() -> str:
    with open(os.path.join(CLIENT, "index.html")) as fh:
        html = fh.read()
    m = re.search(r'<link rel="stylesheet" href="(style[^"]*\.css)[^"]*"', html)
    assert m, "index.html has no app stylesheet <link>"
    return os.path.join(CLIENT, m.group(1))


def _stylesheet() -> str:
    with open(_stylesheet_path()) as fh:
        return re.sub(r"/\*.*?\*/", "", fh.read(), flags=re.S)


def _missing_assets(css: str, base_dir: str) -> list[str]:
    urls = re.findall(r"url\(\s*['\"]?([^'\")]+?)['\"]?\s*\)", css)
    local = [u for u in urls if not re.match(r"(data:|[a-z]+://|//|#)", u)]
    # Resolve the path only: a ?v= cache-buster or #fragment isn't on disk.
    return [
        u for u in local if not os.path.isfile(os.path.join(base_dir, unquote(urlsplit(u).path)))
    ]


def test_every_stylesheet_url_ships_with_the_client():
    path = _stylesheet_path()
    assert _missing_assets(_stylesheet(), os.path.dirname(path)) == []


def test_missing_asset_is_reported():
    css = (
        "a { background: url(assets/nope.svg?v=1); }"
        " b { mask: url('data:image/png;base64,AA'); }"
        # A cache-buster query or an SVG #fragment isn't part of the filename.
        " c { background: url(assets/select-chevron.svg?v=2); }"
        ' d { mask: url("assets/select-chevron.svg#down"); }'
    )
    assert _missing_assets(css, CLIENT) == ["assets/nope.svg?v=1"]


def test_every_select_gets_the_brand_treatment():
    css = _stylesheet()
    # A bare `select` rule (not scoped to one panel) drops the UA's white box...
    assert re.search(r"(?:^|\})\s*select\s*\{[^}]*appearance:\s*none", css)
    # ...and Chromium's customizable select draws the open list too.
    assert "@supports (appearance: base-select)" in css
    assert "::picker(select)" in css
