"""Tests for the README roadmap sync (scripts/readme_roadmap.py)."""

import pytest

from scripts import readme_roadmap

ISSUES = [
    {"number": 48, "title": "BRIX daily distribution", "state": "OPEN", "closedAt": None},
    {"number": 39, "title": "Admin tooling", "state": "OPEN", "closedAt": None},
    {
        "number": 335,
        "title": "remove unreachable helpers",
        "state": "CLOSED",
        "closedAt": "2026-08-16T05:00:00Z",
    },
    {
        "number": 334,
        "title": "readiness audit wallets",
        "state": "CLOSED",
        "closedAt": "2026-08-16T06:00:00Z",
    },
]


def test_render_orders_open_by_number_and_closed_by_recency() -> None:
    lines = readme_roadmap.render(ISSUES)
    assert lines[0] == "- [ ] [#39 — Admin tooling](../../issues/39)"
    assert lines[1] == "- [ ] [#48 — BRIX daily distribution](../../issues/48)"
    checked = [line for line in lines if line.startswith("- [x]")]
    assert checked == [
        "- [x] [#334 — readiness audit wallets](../../issues/334) (closed 2026-08-16)",
        "- [x] [#335 — remove unreachable helpers](../../issues/335) (closed 2026-08-16)",
    ]


def test_render_without_closed_issues_has_no_completed_header() -> None:
    lines = readme_roadmap.render([ISSUES[0]])
    assert not any("Recently completed" in line for line in lines)
    assert len(lines) == 1


def test_render_caps_recently_completed() -> None:
    closed = [
        {
            "number": n,
            "title": f"t{n}",
            "state": "CLOSED",
            "closedAt": f"2026-08-{n:02d}T00:00:00Z",
        }
        for n in range(1, 15)
    ]
    lines = readme_roadmap.render(closed)
    assert (
        sum(1 for line in lines if line.startswith("- [x]")) == readme_roadmap.RECENT_COMPLETED_CAP
    )
    # most recently closed first
    assert "#14" in next(line for line in lines if line.startswith("- [x]"))


def test_replace_block_is_idempotent() -> None:
    readme = "\n".join(
        ["head", readme_roadmap.START_MARK, "stale", readme_roadmap.END_MARK, "tail"]
    )
    once = readme_roadmap.replace_block(readme, ["- [ ] a"])
    twice = readme_roadmap.replace_block(once, ["- [ ] a"])
    assert once == twice
    assert "stale" not in once
    assert once.splitlines()[0] == "head"
    assert once.splitlines()[-1] == "tail"


def test_replace_block_requires_markers() -> None:
    with pytest.raises(SystemExit):
        readme_roadmap.replace_block("no markers", ["- [ ] a"])


def test_replace_block_handles_backslashes() -> None:
    readme = f"{readme_roadmap.START_MARK}\nx\n{readme_roadmap.END_MARK}"
    out = readme_roadmap.replace_block(readme, [r"a\1\g<0>b"])
    assert r"a\1\g<0>b" in out


def test_repo_readme_carries_markers() -> None:
    text = readme_roadmap.README_PATH.read_text()
    assert readme_roadmap.START_MARK in text
    assert readme_roadmap.END_MARK in text


def test_escape_title_neutralizes_link_breakout() -> None:
    hostile = "x](https://evil.example) [click"
    escaped = readme_roadmap.escape_title(hostile)
    # every bracket is backslash-escaped, so none can close the link label
    assert escaped == r"x\](https://evil.example) \[click"


def test_escape_title_collapses_newlines_and_escapes_html() -> None:
    assert readme_roadmap.escape_title("a\nb\r\n  c") == "a b c"
    assert readme_roadmap.escape_title("<img> `x` \\y") == r"\<img\> \`x\` \\y"


def test_bullet_uses_escaped_title() -> None:
    issue = {"number": 7, "title": "bad ] title", "state": "OPEN", "closedAt": None}
    assert readme_roadmap.bullet(issue, checked=False) == (
        r"- [ ] [#7 — bad \] title](../../issues/7)"
    )
