# Tests for scripts/_brand.py — the shared badge vocabulary. Stdlib only:
# this module must import cleanly on a bare CI runner with no .env.
import ast
import importlib
import os
import sys
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

brand = importlib.import_module("scripts._brand")

_BRAND_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "_brand.py"
)


def test_module_imports_only_stdlib():
    """_brand.py must import cleanly on a bare CI runner with no .env, so it
    can never pull in lfg_core's config (which requires secrets). Check what
    the module actually imports (AST) rather than grepping its source text —
    a substring search on prose false-positived on this before."""
    tree = ast.parse(open(_BRAND_PATH).read(), filename=_BRAND_PATH)
    top_level_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top_level_names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                top_level_names.add(node.module.split(".")[0])
    for name in top_level_names:
        assert name == "__future__" or name in sys.stdlib_module_names


def test_palette_contains_every_colour_constant():
    for name in (
        "INK",
        "SURFACE",
        "SURFACE_LIGHT",
        "LINE",
        "PAPER",
        "TEXT",
        "MUTED",
        "ORANGE",
        "RED",
        "BLUE",
        "YELLOW",
        "GREEN",
        "PURPLE",
    ):
        assert getattr(brand, name) in brand.PALETTE


def test_fmt_and_esc():
    assert brand.fmt(1943) == "1,943"
    assert brand.esc("a & b <c>") == "a &amp; b &lt;c&gt;"


def test_open_svg_escapes_the_aria_label():
    tag = brand.open_svg(728, 330, "a & b")
    assert 'width="728"' in tag
    assert 'role="img"' in tag
    assert "a &amp; b" in tag


def test_sticker_card_and_tiles_parse_as_svg():
    parts = [brand.open_svg(728, 330, "t")]
    parts += brand.sticker_card(718, 320)
    parts += brand.title_block(24, "title", "subtitle")
    parts += brand.stat_tiles(
        24.0, 72, 672.0, [("16", "wallets", brand.BLUE), ("1,943", "txs", brand.ORANGE)]
    )
    parts += brand.sparkline(24.0, 310, 672.0, [1, 0, 5], brand.ORANGE)
    parts.append("</svg>")
    root = ET.fromstring("\n".join(parts))
    assert root.attrib["height"] == "330"


def test_sparkline_skips_zero_values():
    bars = [
        p
        for p in brand.sparkline(0.0, 100, 100.0, [0, 0, 0], brand.ORANGE)
        if p.startswith("<rect")
    ]
    assert bars == []


def test_stat_tiles_empty_returns_empty_list():
    assert brand.stat_tiles(0.0, 0, 100.0, []) == []


def test_stat_tiles_single_tile_does_not_crash():
    parts = brand.stat_tiles(24.0, 72, 672.0, [("16", "wallets", brand.BLUE)])
    assert len(parts) == 4


def test_sparkline_empty_series_returns_only_baseline():
    parts = brand.sparkline(0.0, 100, 100.0, [], brand.ORANGE)
    assert len(parts) == 1
    assert parts[0].startswith("<line")
    assert not any(p.startswith("<rect") for p in parts)


def test_sparkline_single_element_series_produces_one_bar():
    bars = [p for p in brand.sparkline(0.0, 100, 100.0, [5], brand.ORANGE) if p.startswith("<rect")]
    assert len(bars) == 1


def test_sparkline_all_identical_nonzero_series_produces_full_height_bars():
    series = [3, 3, 3, 3]
    bars = [
        p
        for p in brand.sparkline(0.0, 100, 100.0, series, brand.ORANGE, max_bar_h=26)
        if p.startswith("<rect")
    ]
    assert len(bars) == len(series)
    for bar in bars:
        assert 'height="26.0"' in bar
