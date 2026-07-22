# Tests for scripts/_brand.py — the shared badge vocabulary. Stdlib only:
# this module must import cleanly on a bare CI runner with no .env.
import importlib
import os
import sys
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

brand = importlib.import_module("scripts._brand")


def test_module_has_no_lfg_core_dependency():
    src = open(
        os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "_brand.py"
        )
    ).read()
    assert "lfg_core" not in src
    assert "dotenv" not in src


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
