# Tests for scripts/render_sourcetag_svg.py — pure renderer, no lfg_core import.
import importlib
import json
import os
import sys
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

brand = importlib.import_module("scripts._brand")
rs = importlib.import_module("scripts.render_sourcetag_svg")

DATA = {
    "source_tag": 2606160021,
    "network": "mainnet",
    "total_tagged_txs": 1943,
    "unique_wallets": 16,
    "by_type": {
        "NFTokenMint": 700,
        "NFTokenCreateOffer": 692,
        "NFTokenAcceptOffer": 311,
        "NFTokenModify": 89,
        "NFTokenBurn": 77,
        "Payment": 64,
        "NFTokenCancelOffer": 2,
    },
    "daily": [
        {"date": "2026-07-20", "count": 12},
        {"date": "2026-07-21", "count": 0},
        {"date": "2026-07-22", "count": 30},
    ],
    "excluded": ["rHU8nu9zSnCpkL3gShG4aGawHzaRVfmKwQ"],
    "first_tagged_tx": "2026-07-20",
    "archive_max_close_time": "2026-07-22T03:20:11+00:00",
    "as_of": "2026-07-22T00:20:00+00:00",
}


def test_output_is_wellformed_xml_and_728_wide():
    root = ET.fromstring(rs.build_svg(DATA))
    assert root.attrib["width"] == "728"
    assert root.attrib["role"] == "img"
    assert "16" in root.attrib["aria-label"]
    assert "1,943" in root.attrib["aria-label"]


def test_headline_numbers_are_rendered_with_thousands_separators():
    svg = rs.build_svg(DATA)
    assert ">1,943<" in svg
    assert ">16<" in svg
    assert "2606160021" in svg


def test_uses_only_brand_palette_colours():
    import re

    svg = rs.build_svg(DATA)
    for colour in set(re.findall(r"#[0-9A-Fa-f]{6}", svg)):
        assert colour in brand.PALETTE, f"non-brand colour {colour}"


def test_renderer_does_not_redeclare_the_palette():
    """The palette lives in scripts/_brand.py; a second copy would drift."""
    src = open(
        os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "scripts",
            "render_sourcetag_svg.py",
        )
    ).read()
    assert "#0A0A0A" not in src and "#D89030" not in src


def test_all_content_stays_inside_the_card():
    """Guards the geometry: a taller breakdown or sparkline must not overflow."""
    svg = rs.build_svg(DATA)
    root = ET.fromstring(svg)
    # sticker_card's drop-shadow rect sits at y=8 with the same height as the
    # card body at y=2, so it always bottoms out 6px lower than the card
    # itself by design (see scripts/_brand.py, also true of dashboard.svg) —
    # the real "stays inside the badge" boundary includes that shadow.
    card_bottom = 8 + 320
    for el in root:
        y = el.attrib.get("y") or el.attrib.get("y1")
        if y is None:
            continue
        bottom = float(y) + float(el.attrib.get("height", 0))
        assert bottom <= card_bottom, f"{el.tag} at y={y} overflows the card"
    assert int(root.attrib["height"]) >= card_bottom


def test_zero_activity_renders_without_crashing():
    empty = dict(DATA, total_tagged_txs=0, unique_wallets=0, by_type={}, daily=[])
    root = ET.fromstring(rs.build_svg(empty))
    assert root.attrib["width"] == "728"


def test_main_writes_only_when_changed(tmp_path, monkeypatch):
    src = tmp_path / "sourcetag.json"
    src.write_text(json.dumps(DATA))
    dest = tmp_path / "sourcetag.svg"
    monkeypatch.setattr(rs, "JSON_PATH", src)
    monkeypatch.setattr(rs, "SVG_PATH", dest)

    assert rs.main() == 0
    first = dest.stat().st_mtime_ns
    assert rs.main() == 0
    assert dest.stat().st_mtime_ns == first  # idempotent, no rewrite


def test_main_fails_loudly_when_json_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(rs, "JSON_PATH", tmp_path / "absent.json")
    monkeypatch.setattr(rs, "SVG_PATH", tmp_path / "out.svg")
    assert rs.main() != 0


def test_breakdown_rows_are_descending_and_match_input_counts():
    svg = rs.build_svg(DATA)
    # DATA's by_type sorted descending: mint 700, offer 692, accept 311,
    # modify 89, burn 77, then Payment 64 + NFTokenCancelOffer 2 fold into
    # one "+2 more" row (66) since there are 7 types (> the 6-row cap).
    assert ">700<" in svg
    assert ">692<" in svg
    assert ">311<" in svg
    mint_idx = svg.index(">mint<")
    offer_idx = svg.index(">offer<")
    assert mint_idx < offer_idx


def test_breakdown_bar_widths_are_non_increasing():
    svg = rs.build_svg(DATA)
    root = ET.fromstring(svg)
    # Breakdown bars all share the same x (area_x + label_w = 24 + 58 = 82)
    # and height (9), which distinguishes them from the card/shadow/sparkline
    # rects that share this <rect> tag.
    bar_widths = [
        float(el.attrib["width"])
        for el in root.iter()
        if el.tag.endswith("rect")
        and el.attrib.get("x") == "82.0"
        and el.attrib.get("height") == "9"
    ]
    assert len(bar_widths) == 6  # 5 real rows + the folded "+2 more" row
    assert bar_widths == sorted(bar_widths, reverse=True)


def test_unknown_type_name_falls_back_to_lowercased_name():
    data = dict(
        DATA,
        by_type={"SomeNewTxType": 5},
    )
    svg = rs.build_svg(data)
    assert ">somenewtxtype<" in svg


def test_all_zero_counts_render_without_zerodivisionerror():
    data = dict(DATA, by_type={"NFTokenMint": 0, "Payment": 0})
    root = ET.fromstring(rs.build_svg(data))
    assert root.attrib["width"] == "728"


def test_overflow_beyond_six_types_is_visibly_represented():
    by_type = {
        "NFTokenMint": 100,
        "NFTokenCreateOffer": 90,
        "NFTokenAcceptOffer": 80,
        "NFTokenModify": 70,
        "NFTokenBurn": 60,
        "Payment": 50,
        "TrustSet": 40,
        "NFTokenCancelOffer": 30,
    }
    svg = rs.build_svg(dict(DATA, by_type=by_type))
    # 8 types, top 5 shown individually (mint/offer/accept/modify/burn) + 1
    # folded "+N more" row carrying the combined remainder
    # (Payment 50 + TrustSet 40 + NFTokenCancelOffer 30 = 120).
    assert ">+3 more<" in svg
    assert ">120<" in svg


def test_string_typed_counts_are_coerced_to_int():
    data = dict(DATA, by_type={"NFTokenMint": "700", "Payment": "64"})
    svg = rs.build_svg(data)
    assert ">700<" in svg
    assert ">64<" in svg


def test_main_fails_cleanly_on_structurally_malformed_json(tmp_path, monkeypatch):
    src = tmp_path / "sourcetag.json"
    src.write_text(json.dumps({}))
    dest = tmp_path / "sourcetag.svg"
    monkeypatch.setattr(rs, "JSON_PATH", src)
    monkeypatch.setattr(rs, "SVG_PATH", dest)

    assert rs.main() != 0
    assert not dest.exists()
