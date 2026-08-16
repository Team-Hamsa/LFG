"""Tests for the README feature-flags table generator (scripts/readme_features.py)."""

import pytest

from scripts import readme_features

CONFIG_TEXT = readme_features.CONFIG_PATH.read_text()


def test_every_flag_parses_from_real_config() -> None:
    """The drift signal: each listed flag's default must still be regexable."""
    for flag in readme_features.FLAGS:
        readme_features.parse_default(flag, CONFIG_TEXT)  # raises SystemExit on drift


def test_known_defaults_from_real_config() -> None:
    assert readme_features.parse_default("ECONOMY_ENABLED", CONFIG_TEXT) == "0"
    assert readme_features.parse_default("MARKET_ENABLED", CONFIG_TEXT) == "1"
    assert readme_features.parse_default("BULK_MINT_UI_ENABLED", CONFIG_TEXT) == "0"
    assert readme_features.parse_default("X_ENABLED", CONFIG_TEXT) == "0"
    assert readme_features.parse_default("SHARE_CARD_RENDER_ENABLED", CONFIG_TEXT) == "0"
    assert readme_features.parse_default("WEB_ALLOWED_ORIGINS", CONFIG_TEXT) == ""


def test_parse_default_missing_flag_fails_loudly() -> None:
    with pytest.raises(SystemExit):
        readme_features.parse_default("NO_SUCH_FLAG", CONFIG_TEXT)


def test_describe_default() -> None:
    assert readme_features.describe_default("MARKET_ENABLED", "1") == "`1` (on)"
    assert readme_features.describe_default("ECONOMY_ENABLED", "0") == "`0` (off)"
    assert readme_features.describe_default("WEB_ALLOWED_ORIGINS", "") == "empty (off)"
    assert readme_features.describe_default("WEB_ALLOWED_ORIGINS", "https://x") == "`https://x`"


def test_render_has_one_row_per_flag() -> None:
    lines = readme_features.render(CONFIG_TEXT)
    rows = [line for line in lines if line.startswith("| `")]
    assert len(rows) == len(readme_features.FLAGS)
    joined = "\n".join(lines)
    for flag in readme_features.FLAGS:
        assert f"`{flag}`" in joined
    assert "| `MARKET_ENABLED` | `1` (on) |" in joined


def test_replace_block_is_idempotent() -> None:
    readme = "\n".join(
        [
            "# Title",
            readme_features.START_MARK,
            "stale content",
            readme_features.END_MARK,
            "tail",
        ]
    )
    block = readme_features.render(CONFIG_TEXT)
    once = readme_features.replace_block(readme, block)
    twice = readme_features.replace_block(once, block)
    assert once == twice
    assert "stale content" not in once
    assert once.splitlines()[0] == "# Title"
    assert once.splitlines()[-1] == "tail"


def test_replace_block_requires_markers() -> None:
    with pytest.raises(SystemExit):
        readme_features.replace_block("no markers here", ["row"])


def test_repo_readme_block_is_current() -> None:
    """The checked-in README must already carry the freshly generated block."""
    readme = readme_features.README_PATH.read_text()
    assert readme_features.replace_block(readme, readme_features.render(CONFIG_TEXT)) == readme


def test_parse_default_ignores_comment_lines() -> None:
    config = '# X_ENABLED_DEFAULT = "1"  (old example)\nX_ENABLED_DEFAULT = "0"\n'
    assert readme_features.parse_default("X_ENABLED", config) == "0"


def test_parse_default_rejects_conflicting_values() -> None:
    config = 'X_ENABLED_DEFAULT = "1"\nfoo = os.getenv("X_ENABLED", "0")\n'
    with pytest.raises(SystemExit):
        readme_features.parse_default("X_ENABLED", config)


def test_parse_default_agreeing_duplicates_are_fine() -> None:
    config = 'X_ENABLED_DEFAULT = "0"\nfoo = os.getenv("X_ENABLED", "0")\n'
    assert readme_features.parse_default("X_ENABLED", config) == "0"
