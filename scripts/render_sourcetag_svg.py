"""Render metrics/sourcetag.json into the assets/sourcetag.svg README badge.

Pure renderer: reads one JSON file, writes one SVG. It touches no database and
imports nothing from lfg_core, so it runs on a bare CI runner. Idempotent —
the SVG is rewritten only when its content changes. Run by the same workflow
that refreshes assets/hackathon_loc.svg and assets/dashboard.svg.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from scripts._brand import (
    BLUE,
    FONT,
    GREEN,
    MUTED,
    ORANGE,
    PURPLE,
    RED,
    TEXT,
    YELLOW,
    esc,
    fmt,
    open_svg,
    sparkline,
    stat_tiles,
    sticker_card,
    title_block,
)

JSON_PATH = Path("metrics/sourcetag.json")
SVG_PATH = Path("assets/sourcetag.svg")

# Long XRPL type names are unreadable at 11px; these are the badge labels.
TYPE_LABELS = {
    "NFTokenMint": "mint",
    "NFTokenCreateOffer": "offer",
    "NFTokenAcceptOffer": "accept",
    "NFTokenModify": "modify",
    "NFTokenBurn": "burn",
    "NFTokenCancelOffer": "cancel",
    "Payment": "payment",
    "TrustSet": "trustset",
}
BAR_COLORS = [ORANGE, BLUE, RED, GREEN, YELLOW, PURPLE]


def build_svg(data: dict[str, Any]) -> str:
    """Sticker-style badge: two stat tiles, a type breakdown, a daily sparkline."""
    wallets = int(data["unique_wallets"])
    total = int(data["total_tagged_txs"])
    tag = data["source_tag"]
    by_type = data.get("by_type") or {}
    series = [int(d["count"]) for d in (data.get("daily") or [])]

    # Geometry: title block ends ~y=132, stat tiles 72..132, breakdown rows
    # 158..254 (6 × 16), sparkline caption at 276, chart baseline at 310 with
    # 26px of headroom (bar tops ≥ 284). Card spans y=2..322 — everything must
    # stay inside it.
    w, h = 728, 330
    card_w, card_h = 718, 320
    pad = 24
    area_x, area_w = float(pad), 672.0

    label = (
        f"XRPL source tag {tag}: {fmt(total)} tagged transactions "
        f"from {fmt(wallets)} unique wallets"
    )

    parts = [open_svg(w, h, label)]
    parts += sticker_card(card_w, card_h)
    parts += title_block(
        pad, f"XRPL source tag · {tag}", "live on-ledger volume · auto-updated daily"
    )
    parts += stat_tiles(
        area_x,
        72,
        area_w,
        [
            (fmt(wallets), "unique wallets", BLUE),
            (fmt(total), "tagged transactions", ORANGE),
        ],
    )

    # Type breakdown: one horizontal bar per tx type, longest first.
    row_y = 158
    all_ordered = sorted(
        ((name, int(count)) for name, count in by_type.items()),
        key=lambda kv: (-kv[1], kv[0]),
    )
    summary_row: tuple[str, int] | None = None
    if len(all_ordered) > 6:
        # Keep the top 5 rows and fold everything else into one visible
        # "+N more" summary row (summed count) rather than silently dropping
        # types past the 6-row layout cap. The summary is a footnote, not a
        # category: it never draws a bar and never feeds `peak` (its summed
        # count can exceed every real row's count and would otherwise lie
        # about which type dominates — see the regression this fixes).
        ordered = list(all_ordered[:5])
        rest = all_ordered[5:]
        rest_count = sum(count for _, count in rest)
        summary_row = (f"+{len(rest)} more", rest_count)
    else:
        ordered = all_ordered
    if ordered or summary_row is not None:
        peak = max((count for _, count in ordered), default=0) or 1
        label_w, count_w = 58.0, 44.0
        track = area_w - label_w - count_w
        for i, (type_name, count) in enumerate(ordered):
            ry = row_y + i * 16
            color = BAR_COLORS[i % len(BAR_COLORS)]
            label_text = TYPE_LABELS.get(type_name, type_name.lower())
            parts.append(
                f'<text x="{area_x:.1f}" y="{ry + 8}" font-family="{FONT}" '
                f'font-size="11" fill="{MUTED}">'
                f"{esc(label_text)}</text>"
            )
            bar_w = max(track * count / peak, 2.0)
            parts.append(
                f'<rect x="{area_x + label_w:.1f}" y="{ry:.1f}" width="{bar_w:.1f}" '
                f'height="9" rx="4.5" fill="{color}"/>'
            )
            parts.append(
                f'<text x="{area_x + label_w + track + 8:.1f}" y="{ry + 8}" '
                f'font-family="{FONT}" font-size="11" fill="{TEXT}">{fmt(count)}</text>'
            )
        if summary_row is not None:
            # No bar: a summary footnote must not visually compete with the
            # real rows it stands in for. Label and count both render MUTED
            # to read as a footnote, not a category.
            ry = row_y + len(ordered) * 16
            label_text, count = summary_row
            parts.append(
                f'<text x="{area_x:.1f}" y="{ry + 8}" font-family="{FONT}" '
                f'font-size="11" fill="{MUTED}">'
                f"{esc(label_text)}</text>"
            )
            parts.append(
                f'<text x="{area_x + label_w + track + 8:.1f}" y="{ry + 8}" '
                f'font-family="{FONT}" font-size="11" fill="{MUTED}">{fmt(count)}</text>'
            )

    # Daily sparkline along the bottom.
    parts.append(
        f'<text x="{pad}" y="276" font-family="{FONT}" font-size="12" '
        f'fill="{MUTED}">tagged tx / day</text>'
    )
    parts += sparkline(area_x, 310, area_w, series, ORANGE)

    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def main() -> int:
    if not JSON_PATH.exists():
        print(f"missing {JSON_PATH}", file=sys.stderr)
        return 2
    try:
        data = json.loads(JSON_PATH.read_text())
    except json.JSONDecodeError as exc:
        print(f"malformed {JSON_PATH}: {exc}", file=sys.stderr)
        return 2

    try:
        svg = build_svg(data)
    except (KeyError, TypeError, ValueError) as exc:
        print(f"malformed {JSON_PATH}: {exc}", file=sys.stderr)
        return 2
    changed = not SVG_PATH.exists() or SVG_PATH.read_text() != svg
    if changed:
        SVG_PATH.parent.mkdir(parents=True, exist_ok=True)
        SVG_PATH.write_text(svg)
    print("updated" if changed else "already current")
    return 0


if __name__ == "__main__":
    sys.exit(main())
