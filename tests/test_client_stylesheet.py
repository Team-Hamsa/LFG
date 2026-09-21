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


def _specificity(selector: str) -> tuple[int, int, int]:
    """(ids, classes+attrs+pseudo-classes, elements+pseudo-elements) for one
    compound selector — enough for the flat, class-based selectors this
    stylesheet uses."""
    sel = re.sub(r"::[a-z-]+", " EL ", selector)
    sel = re.sub(r":(?:hover|active|focus[a-z-]*|disabled|not|where|is|open)\b", " CL ", sel)
    ids = len(re.findall(r"#[\w-]+", sel))
    classes = (
        len(re.findall(r"\.[\w-]+", sel)) + len(re.findall(r"\[[^\]]+\]", sel)) + sel.count("CL")
    )
    elements = len(re.findall(r"(?:^|[\s>+~])([a-z][\w-]*)", sel)) + sel.count("EL")
    return (ids, classes, elements)


def _rules(css: str):
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
        selectors, body = m.group(1), m.group(2)
        if selectors.strip().startswith("@"):
            continue
        for sel in selectors.split(","):
            yield sel.strip(), body


def test_no_container_rule_can_strip_the_trait_art_backdrop():
    """Standalone trait art (a transparent /api/layer PNG) is unreadable on the
    dark UI without .trait-art's backdrop — dark eyebrows simply vanish.

    Any rule like `.trait-chip img { background: ... }` is specificity (0,1,1)
    and, written with the `background` SHORTHAND, resets background-image — so
    it silently defeats the backdrop. That is how eyebrows stayed invisible in
    the Wanted book and the Dressing Room strip after #557 fixed them on
    cards, and how .mine-row reintroduced it in #583. The backdrop rules must
    therefore outrank every background-setting rule that can match an
    img/video.
    """
    css = _stylesheet()
    backdrop = [
        (sel, _specificity(sel))
        for sel, _ in _rules(css)
        if re.search(r"\.trait-art(-face)?\b", sel)
    ]
    assert backdrop, "no .trait-art rules found in the stylesheet"
    floor = min(spec for _, spec in backdrop)

    offenders = []
    for sel, body in _rules(css):
        if re.search(r"\.trait-art(-face)?\b", sel):
            continue
        if not re.search(r"(?:^|[\s>+~,])(?:img|video)\b", sel):
            continue
        if not re.search(r"(?<![\w-])background(?:-color|-image)?\s*:", body):
            continue
        if _specificity(sel) >= floor:
            offenders.append(f"{sel.strip()} {_specificity(sel)} >= .trait-art {floor}")
    assert not offenders, "these rules can strip the trait-art backdrop:\n  " + "\n  ".join(
        offenders
    )


def test_card_footer_action_carries_no_sticker_ring():
    """--sticker-sm draws a 2px --paper (white) ring. A card footer button sits
    flush inside .nft-card's `overflow: hidden`, so that ring is clipped by the
    card edge and reads as one or two stray white lines that differ card to
    card. The footer variant must cancel the shadow, not just the border."""
    css = _stylesheet()
    # The RESTING state is what matters: a :active-only cancel would still
    # leave the ring visible on every card at rest.
    resting = [
        body
        for sel, body in _rules(css)
        if re.fullmatch(r"\.nft-card\s+\.mine-action", sel.strip())
    ]
    assert resting, "no bare `.nft-card .mine-action` rule found"
    assert any(re.search(r"box-shadow\s*:\s*none", b) for b in resting), (
        "the card footer button must set box-shadow: none at rest, or the "
        "sticker ring shows as clipped white lines"
    )
