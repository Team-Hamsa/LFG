"""Tests for the README badge-row generator (scripts/readme_badges.py)."""

from pathlib import Path

import pytest

from scripts import readme_badges


def test_source_tag_matches_config_default() -> None:
    assert readme_badges.source_tag() == "2606160021"


def test_build_badges_includes_dynamic_entries() -> None:
    lines = readme_badges.build_badges(2935, "2606160021", 1234)
    joined = "\n".join(lines)
    assert "tests-2%2C935" in joined
    assert "actions/workflow/status/Team-Hamsa/LFG/ci.yml" in joined
    assert "github/license/Team-Hamsa/LFG" in joined
    assert "SourceTag-2606160021" in joined
    assert "tagged_txs-1%2C234" in joined
    # every static badge survives too
    assert len(lines) == len(readme_badges.STATIC_BADGES) + 5


def test_build_badges_omits_tagged_txs_when_metrics_absent() -> None:
    lines = readme_badges.build_badges(10, "2606160021", None)
    assert not any("tagged_txs" in line for line in lines)
    assert len(lines) == len(readme_badges.STATIC_BADGES) + 4


def test_replace_block_is_idempotent() -> None:
    readme = "\n".join(
        [
            "# Title",
            readme_badges.START_MARK,
            "stale content",
            readme_badges.END_MARK,
            "tail",
        ]
    )
    once = readme_badges.replace_block(readme, ["<img a>", "<img b>"])
    twice = readme_badges.replace_block(once, ["<img a>", "<img b>"])
    assert once == twice
    assert "stale content" not in once
    assert once.splitlines()[1] == readme_badges.START_MARK
    assert once.splitlines()[4] == readme_badges.END_MARK
    assert once.splitlines()[-1] == "tail"


def test_replace_block_requires_markers() -> None:
    with pytest.raises(SystemExit):
        readme_badges.replace_block("no markers here", ["<img>"])


def test_tagged_tx_total_absent_file_omits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(readme_badges, "METRICS_PATH", tmp_path / "missing.json")
    assert readme_badges.tagged_tx_total() is None


def test_tagged_tx_total_malformed_file_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad = tmp_path / "sourcetag.json"
    bad.write_text("{not json")
    monkeypatch.setattr(readme_badges, "METRICS_PATH", bad)
    with pytest.raises(SystemExit):
        readme_badges.tagged_tx_total()


def test_tagged_tx_total_non_integer_total_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad = tmp_path / "sourcetag.json"
    bad.write_text('{"total_tagged_txs": "1961"}')
    monkeypatch.setattr(readme_badges, "METRICS_PATH", bad)
    with pytest.raises(SystemExit):
        readme_badges.tagged_tx_total()


def test_tagged_tx_total_boolean_total_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # JSON true/false parse as Python bools, which are ints — must be rejected
    bad = tmp_path / "sourcetag.json"
    bad.write_text('{"total_tagged_txs": true}')
    monkeypatch.setattr(readme_badges, "METRICS_PATH", bad)
    with pytest.raises(SystemExit):
        readme_badges.tagged_tx_total()


def test_replace_block_handles_backslashes_in_content() -> None:
    # re.sub replacement escaping must not mangle literal backslashes/groups
    readme = f"{readme_badges.START_MARK}\nx\n{readme_badges.END_MARK}"
    out = readme_badges.replace_block(readme, [r"a\1\g<0>b"])
    assert r"a\1\g<0>b" in out


def test_repo_readme_carries_markers() -> None:
    text = readme_badges.README_PATH.read_text()
    assert readme_badges.START_MARK in text
    assert readme_badges.END_MARK in text
