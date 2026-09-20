# tests/test_leaderboard_selector.py
# The webapp client is no-build vanilla JS (no JS test harness), so the
# two-tier leaderboard selector (spec: 2026-07-04-leaderboard-two-tier-
# selector-design.md) is guarded by source assertions, like test_app_js_boot.
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLIENT = os.path.join(ROOT, "webapp", "client")


def _read(name: str) -> str:
    with open(os.path.join(CLIENT, name), encoding="utf-8") as f:
        return f.read()


def test_index_has_category_row():
    html = _read("index.html")
    assert 'id="lb-cats"' in html
    for cat in ("users", "nfts", "brix"):
        assert f'data-cat="{cat}"' in html
    # Sub-board chips are JS-rendered from CATEGORIES; none hardcoded in HTML.
    assert "data-board=" not in html
    assert 'id="lb-boards"' in html


def test_app_js_categories_map_covers_all_8_boards():
    src = _read("app.js")
    m = re.search(r"const CATEGORIES\s*=\s*\{.*?\};", src, re.S)
    assert m, "CATEGORIES map missing from app.js"
    block = m.group(0)
    for board in (
        "users_nfts",
        "users_swaps",
        "users_builds",
        "nft_swaps",
        "nft_rarity",
        "brix_rich",
        "brix_lp",
        "brix_earned",
    ):
        assert board in block, f"{board} missing from CATEGORIES"
    for label in ("Holders", "Swappers", "Builders", "Swaps", "Rarest", "Richlist", "LP", "Earned"):
        assert re.search(rf"label:\s*['\"]{re.escape(label)}['\"]", block), f"label {label} missing"
    assert "Hot" not in block  # renamed to Swaps


def test_app_js_category_switch_behavior():
    src = _read("app.js")
    # Sub-row renders from the map; category click selects its first board
    # and reloads.
    assert "function renderLbBoards()" in src
    assert "CATEGORIES[lbState.cat][0].board" in src
    assert "cat: 'users'" in src  # default category in lbState


def test_users_nfts_chip_renames_to_minters_off_all_time():
    """The board key is one; the name is two. All Time reads the live index
    (a holdings census), every window counts mints — so the chip must say
    Holders only on All Time."""
    src = _read("app.js")
    block = re.search(r"const CATEGORIES\s*=\s*\{.*?\};", src, re.S).group(0)
    assert re.search(r"label:\s*'Holders',\s*windowLabel:\s*'Minters'", block)
    assert "function lbBoardLabel(entry)" in src
    assert "lbState.period !== 'all'" in src
    assert "btn.textContent = lbBoardLabel(entry)" in src


def test_loadleaderboard_rerenders_board_chips_so_the_rename_applies():
    """Re-highlighting alone would leave a stale chip name after a period
    change; the row is re-rendered instead (delegated handler survives)."""
    src = _read("app.js")
    body = re.search(r"async function loadLeaderboard\(\)\s*\{.*?\n\}", src, re.S).group(0)
    assert "renderLbBoards();" in body
    assert "highlightChips('lb-boards'" not in body


def _stylesheet() -> str:
    html = _read("index.html")
    name = re.search(r'href="(style\.v\d+\.css)"', html).group(1)
    return _read(name)


def test_leaderboard_list_scrolls_to_25_while_the_card_stays_put():
    """Top 10 in view; rows 11-25 reachable by scrolling the list only."""
    css = _stylesheet()
    block = re.search(r"\.lb-list\s*\{[^}]*\}", css, re.S).group(0)
    assert "overflow-y: auto" in block
    assert "max-height:" in block
    assert "overscroll-behavior: contain" in block  # never scroll-chain the page
    frame = re.search(r"\.leaderboard\s*\{[^}]*\}", css, re.S).group(0)
    assert "--lb-rows: 10" in frame
    # Uniform row height, or "10 rows" would mean two different heights on
    # the thumbnail boards vs the text-only ones.
    row = re.search(r"\.lb-row\s*\{[^}]*\}", css, re.S).group(0)
    assert "min-height: var(--lb-row-h)" in row


def test_leaderboard_list_is_a_reachable_scroll_region():
    html = _read("index.html")
    ol = re.search(r"<ol id=\"lb-list\"[^>]*>", html).group(0)
    assert 'tabindex="0"' in ol
    assert "aria-label=" in ol
