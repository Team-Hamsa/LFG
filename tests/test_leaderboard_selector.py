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
