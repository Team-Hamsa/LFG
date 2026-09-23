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


def test_replace_block_handles_backslashes_in_content() -> None:
    # re.sub replacement escaping must not mangle literal backslashes/groups
    readme = f"{readme_badges.START_MARK}\nx\n{readme_badges.END_MARK}"
    out = readme_badges.replace_block(readme, [r"a\1\g<0>b"])
    assert r"a\1\g<0>b" in out


def test_main_writes_the_frozen_hackathon_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """tests / tagged_txs are frozen as submitted, whatever the live counts are.

    Runs from an empty directory with no git repo and no metrics snapshot, so
    any live counting would fail or drop the badge.
    """
    config_path = readme_badges.CONFIG_PATH.resolve()
    readme = tmp_path / "README.md"
    readme.write_text(f"{readme_badges.START_MARK}\nstale\n{readme_badges.END_MARK}\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(readme_badges, "README_PATH", readme)
    monkeypatch.setattr(readme_badges, "CONFIG_PATH", config_path)

    assert readme_badges.main() == 0
    text = readme.read_text()
    assert "tests-5%2C397" in text
    assert "tagged_txs-12%2C141" in text
    assert readme_badges.FROZEN_TESTS == 5397
    assert readme_badges.FROZEN_TAGGED_TXS == 12141


def test_repo_readme_carries_markers() -> None:
    text = readme_badges.README_PATH.read_text()
    assert readme_badges.START_MARK in text
    assert readme_badges.END_MARK in text
