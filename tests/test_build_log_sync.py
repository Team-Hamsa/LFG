"""Tests for the build-log merged-PR changelog sync (scripts/build_log_sync.py)."""

import pytest

from scripts import build_log_sync

PRS = [
    {"number": 10, "title": "feat: first", "mergedAt": "2026-07-02T10:00:00Z"},
    {"number": 12, "title": "fix: second", "mergedAt": "2026-08-01T10:00:00Z"},
    {"number": 11, "title": "docs: same day later", "mergedAt": "2026-08-01T12:00:00Z"},
    {"number": 3, "title": "pre-sprint", "mergedAt": "2026-06-01T00:00:00Z"},
]


def test_render_groups_by_month_newest_first_and_drops_pre_sprint():
    lines = build_log_sync.render(PRS)
    headings = [line for line in lines if line.startswith("### ")]
    assert headings == ["### August 2026 — 2 merged", "### July 2026 — 1 merged"]
    assert lines[0].startswith("_3 pull requests merged since 2026-06-21")
    assert not any("#3]" in line for line in lines)


def test_render_orders_prs_within_month_newest_first():
    lines = build_log_sync.render(PRS)
    aug = [line for line in lines if line.startswith("- 2026-08")]
    assert "[#11]" in aug[0] and "[#12]" in aug[1]
    assert aug[0] == (
        "- 2026-08-01 · [#11](https://github.com/Team-Hamsa/LFG/pull/11) docs: same day later"
    )


def test_escape_title_neutralises_link_breakout():
    assert build_log_sync.escape_title("x](https://evil) <b>") == r"x\](https://evil) \<b\>"


def test_replace_block_is_idempotent_and_preserves_surroundings():
    doc = "\n".join(["head", build_log_sync.START_MARK, "stale", build_log_sync.END_MARK, "tail"])
    once = build_log_sync.replace_block(doc, ["- a"])
    twice = build_log_sync.replace_block(once, ["- a"])
    assert once == twice
    assert once.startswith("head\n") and once.endswith("\ntail")
    assert "stale" not in once


def test_replace_block_requires_markers():
    with pytest.raises(SystemExit):
        build_log_sync.replace_block("no markers", ["- a"])
