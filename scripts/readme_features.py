"""Regenerate the README feature-flags table between the feature-flags markers.

Each row pairs a feature flag's *code default* — regexed out of
lfg_core/config.py, never imported (importing lfg_core requires runtime env) —
with a hand-maintained description and production-state note kept in FLAGS
below. Because the default is parsed from the source each run, a flipped
default can never leave the README claiming the old one; a flag whose default
can no longer be parsed fails the run loudly (SystemExit) — that's the drift
signal telling us the parser or the flag moved.

Run by the same CI that refreshes the badge row (hackathon-loc.yml); safe to
run locally from the repo root and idempotent (README is only rewritten when
the generated block changes).
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

README_PATH = Path("README.md")
CONFIG_PATH = Path("lfg_core/config.py")

START_MARK = "<!-- feature-flags:start -->"
END_MARK = "<!-- feature-flags:end -->"


@dataclass(frozen=True)
class FlagInfo:
    """Hand-maintained metadata for one flag (the default is parsed, not listed)."""

    description: str
    production: str


# Descriptions and production-state notes are hand-maintained; the "code
# default" column is parsed from lfg_core/config.py so it can never drift.
FLAGS: dict[str, FlagInfo] = {
    "ECONOMY_ENABLED": FlagInfo(
        "Dress-up trait economy — Closet, harvest/assemble/equip, trait tokens, Trait Shop",
        "`1` since 2026-07-21 ([#185](../../issues/185))",
    ),
    "MARKET_ENABLED": FlagInfo(
        "In-app NFT marketplace (list / browse / buy via Xaman)",
        "on (default)",
    ),
    "BULK_MINT_UI_ENABLED": FlagInfo(
        "Activity bulk-mint quantity stepper (server bulk endpoints stay live regardless)",
        "staging first; enable per stack",
    ),
    "X_ENABLED": FlagInfo(
        "X brand-account auto-poster (also requires all four OAuth creds)",
        "off — go-live is a pending ops step ([#41](../../issues/41))",
    ),
    "SHARE_CARD_RENDER_ENABLED": FlagInfo(
        "Branded share-card PNG for X cards (needs node + Playwright Chromium)",
        "off — raw art serves as the card image",
    ),
    "WEB_ALLOWED_ORIGINS": FlagInfo(
        "Standalone web surface CORS allowlist (empty = feature off)",
        "set to the GitHub Pages origins ([#240](../../issues/240))",
    ),
}


def parse_default(flag: str, config_text: str) -> str:
    """The flag's code default, regexed out of lfg_core/config.py.

    Recognizes the three idioms config.py uses for its feature flags:
    a named ``<FLAG>_DEFAULT = "..."`` constant, a literal
    ``env_flag("<FLAG>", "...")`` default, and a literal
    ``os.getenv("<FLAG>", "...")`` default. Raises SystemExit when none
    match — the drift signal that the flag moved or changed shape.
    """
    patterns = (
        rf'{re.escape(flag)}_DEFAULT\s*=\s*"([^"]*)"',
        rf'env_flag\(\s*"{re.escape(flag)}",\s*"([^"]*)"\s*\)',
        rf'os\.getenv\(\s*"{re.escape(flag)}",\s*"([^"]*)"\s*\)',
    )
    for pattern in patterns:
        match = re.search(pattern, config_text)
        if match:
            return match.group(1)
    raise SystemExit(f"default for {flag} not found in {CONFIG_PATH} — update readme_features.py")


def describe_default(flag: str, raw: str) -> str:
    """Human-readable rendering of a parsed default value."""
    if flag == "WEB_ALLOWED_ORIGINS":
        return "empty (off)" if raw == "" else f"`{raw}`"
    return f"`{raw}` (off)" if raw in ("", "0", "false", "False") else f"`{raw}` (on)"


def render(config_text: str) -> list[str]:
    """The generated block body: one table row per flag."""
    lines = [
        "**Feature flags** — the *code default* column below is generated from"
        " `lfg_core/config.py` by CI and cannot drift:",
        "",
        "| Flag | Code default | Feature | Production |",
        "|---|---|---|---|",
    ]
    for flag, info in FLAGS.items():
        default = describe_default(flag, parse_default(flag, config_text))
        lines.append(f"| `{flag}` | {default} | {info.description} | {info.production} |")
    return lines


def replace_block(readme: str, block_lines: list[str]) -> str:
    """Swap the marker-delimited region for the freshly generated table."""
    pattern = re.compile(re.escape(START_MARK) + r".*?" + re.escape(END_MARK), flags=re.DOTALL)
    if not pattern.search(readme):
        raise SystemExit("feature-flags markers not found in README.md")
    replacement = "\n".join([START_MARK, *block_lines, END_MARK])
    return pattern.sub(lambda _match: replacement, readme, count=1)


def main() -> int:
    readme = README_PATH.read_text()
    updated = replace_block(readme, render(CONFIG_PATH.read_text()))
    if updated != readme:
        README_PATH.write_text(updated)
        print("updated")
    else:
        print("already current")
    return 0


if __name__ == "__main__":
    sys.exit(main())
