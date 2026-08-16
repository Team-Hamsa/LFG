"""Tests for scripts/render_architecture_svg.py (README architecture diagram)."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from scripts import render_architecture_svg as ras


def test_discovers_flow_modules_from_disk(tmp_path: Path) -> None:
    (tmp_path / "mint_flow.py").write_text("")
    (tmp_path / "zzz_new_flow.py").write_text("")
    (tmp_path / "not_a_flow.txt").write_text("")
    (tmp_path / "helpers.py").write_text("")
    assert ras.discover_flow_modules(tmp_path) == ["mint_flow", "zzz_new_flow"]


def test_real_repo_flow_modules_present() -> None:
    mods = ras.discover_flow_modules()
    assert "mint_flow" in mods
    assert "shop_flow" in mods
    assert "bulk_mint_flow" in mods
    assert mods == sorted(mods)


def test_output_is_deterministic() -> None:
    mods = ras.discover_flow_modules()
    assert ras.build_svg(mods) == ras.build_svg(mods)


def test_output_is_well_formed_xml_and_contains_flows() -> None:
    mods = ras.discover_flow_modules()
    svg = ras.build_svg(mods)
    root = ET.fromstring(svg)
    assert root.tag.endswith("svg")
    for name in mods:
        assert name in svg
    for landmark in ("lfg_service", "lfg_core", "XRP Ledger", "Xaman", "BunnyCDN"):
        assert landmark in svg


def test_new_flow_module_appears_automatically(tmp_path: Path) -> None:
    (tmp_path / "mint_flow.py").write_text("")
    base = ras.build_svg(ras.discover_flow_modules(tmp_path))
    (tmp_path / "aaa_flow.py").write_text("")
    grown = ras.build_svg(ras.discover_flow_modules(tmp_path))
    assert "aaa_flow" not in base
    assert "aaa_flow" in grown


def test_main_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out = tmp_path / "architecture.svg"
    monkeypatch.setattr(ras, "SVG_PATH", out)
    assert ras.main() == 0
    first = out.read_text()
    mtime = out.stat().st_mtime_ns
    assert ras.main() == 0
    assert out.read_text() == first
    assert out.stat().st_mtime_ns == mtime  # not rewritten when unchanged


def test_checked_in_svg_matches_generator() -> None:
    """The committed asset must be regenerated whenever the generator changes."""
    svg = ras.build_svg(ras.discover_flow_modules())
    assert ras.SVG_PATH.read_text() == svg
