"""Regenerate the README badge row between the badges:start/end markers.

Static identity badges (mainnet, surfaces, PWA, ...) are emitted verbatim so
the whole row lives in one place; the data-driven ones are computed each run:

- tests        — live `def test_` count, same counter as the vitals dashboard
- CI           — shields.io workflow-status endpoint (self-updating server-side)
- license      — shields.io GitHub-license endpoint
- SourceTag    — parsed from the `SOURCE_TAG` default in lfg_core/config.py
                 (regexed, not imported, so no runtime env is needed)
- tagged txs   — total from metrics/sourcetag.json, the nightly pm2 snapshot;
                 the badge is simply omitted while the file is absent

Run by the same CI that refreshes assets/hackathon_loc.svg on every push to
main; safe to run locally from the repo root and idempotent (README is only
rewritten when the generated block changes).
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from scripts.readme_dashboard import count_tests

README_PATH = Path("README.md")
CONFIG_PATH = Path("lfg_core/config.py")
METRICS_PATH = Path("metrics/sourcetag.json")

START_MARK = "<!-- badges:start -->"
END_MARK = "<!-- badges:end -->"

REPO = "Team-Hamsa/LFG"

STATIC_BADGES = [
    (
        "https://img.shields.io/badge/mainnet-live-2ea043?style=flat-square",
        "Mainnet: live",
        None,
    ),
    (
        "https://img.shields.io/badge/web_app-live-D89030?style=flat-square",
        "Web app live at build.letseffinggo.com",
        "https://build.letseffinggo.com",
    ),
    (
        "https://img.shields.io/badge/XRPL-NFTs-3E8DE3?style=flat-square",
        "Built on the XRP Ledger",
        None,
    ),
    (
        "https://img.shields.io/badge/Xaman-signing-F76B1C?style=flat-square",
        "Signed in Xaman",
        None,
    ),
    (
        "https://img.shields.io/badge/surfaces-Discord%20%C2%B7%20Telegram%20%C2%B7%20Web-5865F2?style=flat-square",
        "Surfaces: Discord, Telegram, Web",
        None,
    ),
    (
        "https://img.shields.io/badge/X-share%20%E2%86%92%20mint-000000?style=flat-square&logo=x&logoColor=white",
        "Share on X — per-NFT cards funnel into the app",
        None,
    ),
    (
        "https://img.shields.io/badge/PWA-installable-6B4FBB?style=flat-square",
        "Installable PWA",
        None,
    ),
]


def source_tag() -> str:
    """The SOURCE_TAG default, regexed out of lfg_core/config.py."""
    match = re.search(
        r'SOURCE_TAG\s*=\s*int\(os\.getenv\("SOURCE_TAG",\s*"(\d+)"\)\)',
        CONFIG_PATH.read_text(),
    )
    if not match:
        raise SystemExit("SOURCE_TAG default not found in lfg_core/config.py")
    return match.group(1)


def tagged_tx_total() -> int | None:
    """total_tagged_txs from the nightly metrics snapshot.

    An absent file is expected before the first nightly snapshot lands and
    omits the badge; a file that exists but can't be parsed is a real problem
    and must fail the run rather than silently dropping the badge.
    """
    if not METRICS_PATH.exists():
        return None
    try:
        data = json.loads(METRICS_PATH.read_text())
    except (OSError, ValueError) as exc:
        raise SystemExit(f"{METRICS_PATH} exists but is unreadable: {exc}") from exc
    total = data.get("total_tagged_txs")
    if not isinstance(total, int):
        raise SystemExit(f"{METRICS_PATH} has no integer total_tagged_txs (got {total!r})")
    return total


def badge(url: str, alt: str, href: str | None = None) -> str:
    img = f'<img src="{url}" alt="{alt}">'
    return f'<a href="{href}">{img}</a>' if href else img


def build_badges(tests: int, tag: str, tagged_txs: int | None) -> list[str]:
    lines = [badge(url, alt, href) for url, alt, href in STATIC_BADGES]
    lines.append(
        badge(
            f"https://img.shields.io/badge/tests-{tests:,}".replace(",", "%2C")
            + "-2ea043?style=flat-square",
            f"{tests:,} tests",
        )
    )
    lines.append(
        badge(
            f"https://img.shields.io/github/actions/workflow/status/{REPO}/ci.yml"
            "?branch=main&style=flat-square&label=CI",
            "CI status on main",
            f"https://github.com/{REPO}/actions/workflows/ci.yml",
        )
    )
    lines.append(
        badge(
            f"https://img.shields.io/github/license/{REPO}?style=flat-square&color=blue",
            "MIT license",
        )
    )
    lines.append(
        badge(
            f"https://img.shields.io/badge/SourceTag-{tag}-8957E5?style=flat-square",
            f"XRPL SourceTag {tag}",
        )
    )
    if tagged_txs is not None:
        lines.append(
            badge(
                f"https://img.shields.io/badge/tagged_txs-{tagged_txs:,}".replace(",", "%2C")
                + "-3E8DE3?style=flat-square",
                f"{tagged_txs:,} SourceTag-tagged XRPL transactions",
            )
        )
    return lines


def replace_block(readme: str, block_lines: list[str]) -> str:
    """Swap the marker-delimited region for the freshly generated badge lines."""
    pattern = re.compile(re.escape(START_MARK) + r".*?" + re.escape(END_MARK), flags=re.DOTALL)
    if not pattern.search(readme):
        raise SystemExit("badges markers not found in README.md")
    replacement = "\n".join([START_MARK, *block_lines, END_MARK])
    return pattern.sub(lambda _match: replacement, readme, count=1)


def main() -> int:
    readme = README_PATH.read_text()
    updated = replace_block(readme, build_badges(count_tests(), source_tag(), tagged_tx_total()))
    if updated != readme:
        README_PATH.write_text(updated)
        print("updated")
    else:
        print("already current")
    return 0


if __name__ == "__main__":
    sys.exit(main())
