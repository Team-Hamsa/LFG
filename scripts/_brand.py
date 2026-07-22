"""Shared LFG brand vocabulary for the README badge renderers.

Palette and drawing primitives used by scripts/readme_dashboard.py and
scripts/render_sourcetag_svg.py, so the badges cannot drift apart. Stdlib
only, and deliberately free of any app-domain imports: these run on a bare
CI runner with no .env. Source of truth for the colours is
webapp/client/style.css.
"""

from __future__ import annotations

INK = "#0A0A0A"
SURFACE = "#181818"
SURFACE_LIGHT = "#202020"  # subtle tile fill, one step up from the card
LINE = "#2C2C2C"
PAPER = "#FFFFFF"
TEXT = "#F5F4F1"
MUTED = "#9C9A94"
ORANGE = "#D89030"
RED = "#D84830"
BLUE = "#4890C0"
YELLOW = "#F0D848"
GREEN = "#3DA35D"
PURPLE = "#601878"
FONT = "-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"

PALETTE = frozenset(
    {
        INK,
        SURFACE,
        SURFACE_LIGHT,
        LINE,
        PAPER,
        TEXT,
        MUTED,
        ORANGE,
        RED,
        BLUE,
        YELLOW,
        GREEN,
        PURPLE,
    }
)


def fmt(n: int) -> str:
    return f"{n:,}"


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def open_svg(w: int, h: int, label: str) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
        f'viewBox="0 0 {w} {h}" role="img" aria-label="{esc(label)}">'
    )


def sticker_card(card_w: int, card_h: int) -> list[str]:
    """Hard offset shadow, then the card with its paper ring. The dark fill is
    what lets the badge read on both GitHub light and dark themes."""
    return [
        f'<rect x="8" y="8" width="{card_w}" height="{card_h}" rx="18" fill="{INK}"/>',
        f'<rect x="2" y="2" width="{card_w}" height="{card_h}" rx="18" '
        f'fill="{SURFACE}" stroke="{PAPER}" stroke-width="3"/>',
    ]


def title_block(pad: int, title: str, subtitle: str) -> list[str]:
    return [
        f'<text x="{pad}" y="34" font-family="{FONT}" font-size="19" '
        f'font-weight="700" fill="{TEXT}">{esc(title)}</text>',
        f'<text x="{pad}" y="56" font-family="{FONT}" font-size="13" '
        f'fill="{MUTED}">{esc(subtitle)}</text>',
    ]


def stat_tiles(x: float, y: int, area_w: float, tiles: list[tuple[str, str, str]]) -> list[str]:
    """Evenly spaced tiles: big brand-coloured number over a muted label."""
    parts: list[str] = []
    tile_h, gap = 60, 16
    tile_w = (area_w - gap * (len(tiles) - 1)) / len(tiles)
    for i, (value, label, color) in enumerate(tiles):
        tx = x + i * (tile_w + gap)
        parts.append(
            f'<rect x="{tx:.1f}" y="{y}" width="{tile_w:.1f}" height="{tile_h}" '
            f'rx="12" fill="{SURFACE_LIGHT}" stroke="{LINE}" stroke-width="1"/>'
        )
        parts.append(
            f'<rect x="{tx + 16:.1f}" y="{y + 12}" width="30" height="5" rx="2.5" fill="{color}"/>'
        )
        parts.append(
            f'<text x="{tx + 16:.1f}" y="{y + 42}" font-family="{FONT}" '
            f'font-size="26" font-weight="800" fill="{color}">{value}</text>'
        )
        parts.append(
            f'<text x="{tx + 16:.1f}" y="{y + 55}" font-family="{FONT}" '
            f'font-size="11" fill="{MUTED}">{esc(label)}</text>'
        )
    return parts


def sparkline(
    x: float,
    base_y: int,
    area_w: float,
    series: list[int],
    colour: str = ORANGE,
    max_bar_h: int = 26,
) -> list[str]:
    """A baseline with one thin bar per value; zero-height days are skipped."""
    parts = [
        f'<line x1="{x:.1f}" y1="{base_y}" x2="{x + area_w:.1f}" '
        f'y2="{base_y}" stroke="{LINE}" stroke-width="1"/>'
    ]
    if not series:
        return parts
    peak = max(max(series), 1)
    slot = area_w / len(series)
    bar_w = slot * 0.55
    for i, count in enumerate(series):
        if count <= 0:
            continue
        bar_h = max(max_bar_h * count / peak, 2.0)
        bx = x + i * slot + (slot - bar_w) / 2
        parts.append(
            f'<rect x="{bx:.1f}" y="{base_y - bar_h:.1f}" width="{bar_w:.1f}" '
            f'height="{bar_h:.1f}" rx="2" fill="{colour}"/>'
        )
    return parts
