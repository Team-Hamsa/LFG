"""The Make Waves hackathon stats and graphics are frozen as submitted.

Make Waves closed 2026-09-21. Every hackathon stat the repo shows is pinned
to what it showed at submission: commit 48656477 (2026-09-21 08:17 UTC), the
last refresh after the pitch deck landed. That covers the LoC bar, the
repo-vitals dashboard, the SourceTag badge and its metrics snapshot, the
tests / tagged-txs badges, and the merged-PR changelog in the build log.

Nothing regenerates them any more: the LoC / dashboard / SourceTag steps left
the README sync workflow, build-log-sync.yml was deleted, and the nightly
lfg-sourcetag push was unregistered along with sourcetag_metrics' `--push`.
These tests pin the exact bytes, so anything that regenerates one by accident
(a generator run from the repo root, a revived workflow or pm2 entry) fails
the gate instead of quietly rewriting what the judges saw.

To change a frozen artifact on purpose, update its pin here in the same
commit and say why in the message.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

FROZEN_FILES = {
    "assets/hackathon_loc.svg": "7fa02b402f652b4129aabdfabf3b7a0dd89cb6022ac5b763275c5c76e63d1a3a",
    "assets/dashboard.svg": "6dd6b896f8a3abfa35513b3bfb4f01a14f960ee161441748d1dedf56347e41f7",
    "assets/sourcetag.svg": "6bb28606c1453959660ca760ddd01b0f8687171a088533778510d8794ebf3e50",
    "metrics/sourcetag.json": "0b121a1dfda4cacc50dfe08cddb3e9057b4621c20a80a5eceb29eaa77c994559",
}

# (file, start marker, end marker) -> sha256 of the block, markers included.
FROZEN_BLOCKS = {
    (
        "README.md",
        "<!-- hackathon-loc:start -->",
        "<!-- hackathon-loc:end -->",
    ): "f9d3f20c5605a550812e2dd0dd6b1072617465400616e59301b494c5d0c6c2aa",
    (
        "docs/HACKATHON.md",
        "<!-- changelog:start -->",
        "<!-- changelog:end -->",
    ): "e3eb27f13ec946ee854b9c88f33e315abebfd50c8c02ef9b5dd81ab9b7d085f8",
}

FROZEN_BADGES = [
    '<img src="https://img.shields.io/badge/tests-5%2C397-2ea043?style=flat-square"'
    ' alt="5,397 tests">',
    '<img src="https://img.shields.io/badge/tagged_txs-12%2C141-3E8DE3?style=flat-square"'
    ' alt="12,141 SourceTag-tagged XRPL transactions">',
]

# Generators whose output is frozen. None may run from CI or pm2 again.
RETIRED_GENERATORS = [
    "hackathon_loc",
    "readme_dashboard",
    "render_sourcetag_svg",
    "build_log_sync",
    "sourcetag_metrics",
]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _block(text: str, start: str, end: str) -> str:
    assert text.count(start) == 1, f"expected exactly one {start}"
    i = text.index(start)
    j = text.index(end, i) + len(end)
    return text[i:j]


@pytest.mark.parametrize("path", sorted(FROZEN_FILES))
def test_frozen_file_is_byte_identical(path: str) -> None:
    assert _sha256((ROOT / path).read_bytes()) == FROZEN_FILES[path], (
        f"{path} changed. Hackathon stats are frozen as submitted; restore it with "
        f"`git restore --source=48656477 -- {path}`."
    )


@pytest.mark.parametrize("key", sorted(FROZEN_BLOCKS), ids=lambda k: f"{k[0]}:{k[1]}")
def test_frozen_block_is_unchanged(key: tuple[str, str, str]) -> None:
    path, start, end = key
    block = _block((ROOT / path).read_text(), start, end)
    assert _sha256(block.encode()) == FROZEN_BLOCKS[key], (
        f"the {start} block in {path} changed; it is frozen as submitted"
    )


@pytest.mark.parametrize("line", FROZEN_BADGES)
def test_frozen_badge_is_in_readme(line: str) -> None:
    lines = (ROOT / "README.md").read_text().splitlines()
    assert lines.count(line) == 1


def test_no_workflow_or_pm2_app_runs_a_retired_generator() -> None:
    automation = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
    automation += sorted(ROOT.glob("ecosystem*.config.js"))
    assert automation, "found no workflows or ecosystem files to check"
    offenders = [
        f"{path.relative_to(ROOT)}: {name}"
        for path in automation
        for name in RETIRED_GENERATORS
        if name in path.read_text()
    ]
    assert offenders == []
