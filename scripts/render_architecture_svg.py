"""Regenerate the README architecture diagram (assets/architecture.svg).

A brand-kit block diagram of the system: the four client surfaces plus the
X funnel path flowing into the shared lfg_service backend, lfg_core beneath
it (with the session-flow modules discovered live from lfg_core/*_flow.py,
so a new flow module appears in the diagram automatically), the listener
process group with its per-network SQLite stores, and the external systems
(XRPL/Clio, Xaman, BunnyCDN + IPFS).

Stdlib only, no app-domain imports (runs on a bare CI runner with no .env,
same posture as scripts/readme_dashboard.py). Deterministic — no timestamps,
no randomness; the flow-module list is sorted — and idempotent: the SVG is
only rewritten when its content changes.
"""

from __future__ import annotations

import sys
from pathlib import Path

from scripts._brand import (
    BLUE,
    FONT,
    GREEN,
    LINE,
    MUTED,
    ORANGE,
    PURPLE,
    RED,
    SURFACE,
    SURFACE_LIGHT,
    TEXT,
    YELLOW,
    esc,
    open_svg,
    sticker_card,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SVG_PATH = REPO_ROOT / "assets" / "architecture.svg"
FLOW_GLOB_DIR = REPO_ROOT / "lfg_core"

W = 900
H = 600
PAD = 29  # left/right inset used by the sibling badges
AREA_W = W - 2 * PAD  # 842


def discover_flow_modules(core_dir: Path = FLOW_GLOB_DIR) -> list[str]:
    """Session-flow module stems from lfg_core/*_flow.py, sorted for determinism."""
    return sorted(p.stem for p in core_dir.glob("*_flow.py"))


def text_w(s: str, size: float, bold: bool = False) -> float:
    """Generous width estimate so labels can never overflow their boxes."""
    return len(s) * size * (0.68 if bold else 0.62)


def box(
    x: float,
    y: float,
    w: float,
    h: float,
    accent: str,
    title: str,
    sub: str,
    title_size: float = 15,
    sub_size: float = 11,
) -> list[str]:
    """A brand card: accent bar, bold title, muted subtitle. Asserts fit."""
    inner = w - 28  # accent bar + padding
    assert text_w(title, title_size, bold=True) <= inner, f"title overflows: {title!r}"
    assert text_w(sub, sub_size) <= inner, f"subtitle overflows: {sub!r}"
    ty = y + h / 2 - 4
    return [
        f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" rx="12" '
        f'fill="{SURFACE}" stroke="#FFFFFF" stroke-width="2"/>',
        f'<rect x="{x + 12:.1f}" y="{y + 13:.1f}" width="6" height="{h - 26:.1f}" '
        f'rx="3" fill="{accent}"/>',
        f'<text x="{x + 28:.1f}" y="{ty:.1f}" font-family="{FONT}" '
        f'font-size="{title_size}" font-weight="800" fill="{TEXT}">{esc(title)}</text>',
        f'<text x="{x + 28:.1f}" y="{ty + 17:.1f}" font-family="{FONT}" '
        f'font-size="{sub_size}" fill="{MUTED}">{esc(sub)}</text>',
    ]


def arrow_down(x: float, y1: float, y2: float) -> list[str]:
    return [
        f'<path d="M{x:.1f},{y1:.1f} L{x:.1f},{y2:.1f}" fill="none" '
        f'stroke="{MUTED}" stroke-width="2" stroke-linecap="round"/>',
        f'<polygon points="{x - 5:.1f},{y2 - 8:.1f} {x + 5:.1f},{y2 - 8:.1f} '
        f'{x:.1f},{y2:.1f}" fill="{MUTED}"/>',
    ]


def elbow_down(x1: float, y1: float, x2: float, y2: float, ymid: float) -> list[str]:
    """Down from (x1,y1), across at ymid, down into (x2,y2) with an arrowhead."""
    return [
        f'<path d="M{x1:.1f},{y1:.1f} L{x1:.1f},{ymid:.1f} L{x2:.1f},{ymid:.1f} '
        f'L{x2:.1f},{y2:.1f}" fill="none" stroke="{MUTED}" stroke-width="2" '
        f'stroke-linecap="round" stroke-linejoin="round"/>',
        f'<polygon points="{x2 - 5:.1f},{y2 - 8:.1f} {x2 + 5:.1f},{y2 - 8:.1f} '
        f'{x2:.1f},{y2:.1f}" fill="{MUTED}"/>',
    ]


def chip(x: float, y: float, w: float, h: float, label: str, size: int = 11) -> list[str]:
    return [
        f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" '
        f'rx="{h / 2:.1f}" fill="{SURFACE_LIGHT}" stroke="{LINE}" stroke-width="1"/>',
        f'<text x="{x + w / 2:.1f}" y="{y + h / 2 + 4:.1f}" text-anchor="middle" '
        f'font-family="{FONT}" font-size="{size}" fill="{TEXT}">{esc(label)}</text>',
    ]


def count_chip_rows(flow_modules: list[str], avail_w: float, chip_gap: float, size: int) -> int:
    """How many wrapped rows the flow chips need inside the lfg_core card."""
    rows, used = 1, 0.0
    for name in flow_modules:
        cw = text_w(name, size) + 22
        if used and used + cw > avail_w:
            rows += 1
            used = 0.0
        used += cw + chip_gap
    return rows


def build_svg(flow_modules: list[str]) -> str:
    label = (
        "LFG system architecture: four client surfaces (Discord bot, Discord "
        "Activity, Telegram bot + Mini App, and the web app at "
        "build.letseffinggo.com) plus the X funnel path all call the shared "
        "lfg_service backend, which runs the session state machines over "
        "lfg_core (" + ", ".join(flow_modules) + "). A separate listener "
        "process group streams the Clio transaction feed into per-network "
        "SQLite stores. External systems: XRP Ledger (Clio + rippled), Xaman "
        "(XUMM) signing, and BunnyCDN + IPFS hosting."
    )
    # The lfg_core card grows with the discovered flow modules; everything
    # below it (externals row, footer, canvas) shifts by the same delta, so a
    # new flow module expands the diagram instead of overflowing it.
    chip_h, chip_gap, chip_size = 24, 8, 11
    core_x, core_w = PAD, 560
    chip_avail_w = core_w - 20.0 - 16.0
    chip_rows = count_chip_rows(flow_modules, chip_avail_w, chip_gap, chip_size)
    core_h = max(128, 56 + chip_rows * chip_h + (chip_rows - 1) * chip_gap + 16)
    delta = core_h - 128
    h_total = H + delta

    parts: list[str] = [open_svg(W, h_total, label)]
    parts += sticker_card(W - 16, h_total - 16)

    # Header
    parts.append(
        f'<text x="{PAD}" y="38" font-family="{FONT}" font-size="22" font-weight="900">'
        f'<tspan fill="{YELLOW}">LFG</tspan><tspan fill="{TEXT}"> system architecture'
        "</tspan></text>"
    )
    parts.append(
        f'<text x="{W - PAD}" y="36" text-anchor="end" font-family="{FONT}" '
        f'font-size="12" fill="{MUTED}">one backend &#183; every surface &#183; '
        "no custody</text>"
    )

    # Row 1: four client surfaces + the X funnel path
    surfaces = [
        ("Discord Bot", "slash commands", GREEN),
        ("Discord Activity", "embedded client", PURPLE),
        ("Telegram", "bot + Mini App", YELLOW),
        ("Web App", "build.letseffinggo.com", ORANGE),
        ("X funnel", "share cards → PWA", BLUE),
    ]
    row1_y, row1_h, gap = 60, 62, 8
    sw = (AREA_W - gap * (len(surfaces) - 1)) / len(surfaces)  # 158.8
    centers: list[float] = []
    for i, (title, sub, accent) in enumerate(surfaces):
        sx = PAD + i * (sw + gap)
        centers.append(sx + sw / 2)
        parts += box(sx, row1_y, sw, row1_h, accent, title, sub, title_size=12, sub_size=9.5)

    # Row 2: lfg_service
    svc_x, svc_w = 170, 560
    svc_y, svc_h = 176, 62
    row1_bot = row1_y + row1_h
    for cx in centers:
        tx = min(max(cx, svc_x + 40), svc_x + svc_w - 40)
        parts += elbow_down(cx, row1_bot, tx, svc_y, (row1_bot + svc_y) / 2)
    parts += box(
        svc_x,
        svc_y,
        svc_w,
        svc_h,
        BLUE,
        "lfg_service",
        "aiohttp REST/WS hub · auth + identity · session state machines",
        title_size=17,
        sub_size=12,
    )

    # Row 3: lfg_core with dynamic flow chips, listener + SQLite on the right
    core_y = 286
    parts += arrow_down(svc_x + svc_w / 2, svc_y + svc_h, core_y)
    parts.append(
        f'<rect x="{core_x}" y="{core_y}" width="{core_w}" height="{core_h}" rx="12" '
        f'fill="{SURFACE}" stroke="#FFFFFF" stroke-width="2"/>'
    )
    parts.append(
        f'<rect x="{core_x + 12}" y="{core_y + 13}" width="6" height="30" rx="3" fill="{ORANGE}"/>'
    )
    parts.append(
        f'<text x="{core_x + 28}" y="{core_y + 26}" font-family="{FONT}" font-size="17" '
        f'font-weight="800" fill="{TEXT}">lfg_core</text>'
    )
    parts.append(
        f'<text x="{core_x + 28}" y="{core_y + 43}" font-family="{FONT}" font-size="11" '
        f'fill="{MUTED}">shared domain library · XRPL + Xaman ops · '
        "trait engine · rarity</text>"
    )
    # Flow-module chips, wrapped inside the card (sized above to always fit).
    cx0 = core_x + 20.0
    cx = cx0
    cy = core_y + 56.0
    for name in flow_modules:
        cw = text_w(name, chip_size) + 22
        if cx + cw > core_x + core_w - 16:
            cx = cx0
            cy += chip_h + chip_gap
        assert cy + chip_h <= core_y + core_h - 10, "flow chips overflow lfg_core box"
        parts += chip(cx, cy, cw, chip_h, name, size=chip_size)
        cx += cw + chip_gap

    # Right column: listener process group + per-network SQLite stores
    side_x, side_w = 613, W - PAD - 613  # 258
    lst_y, lst_h = 286, 56
    db_y, db_h = 358, 56
    parts += box(
        side_x,
        lst_y,
        side_w,
        lst_h,
        RED,
        "Listener processes",
        "Clio tx stream → live indexes",
        title_size=13,
        sub_size=10,
    )
    parts += box(
        side_x,
        db_y,
        side_w,
        db_h,
        GREEN,
        "Per-network SQLite",
        "lfg_nfts · onchain_* · history_*",
        title_size=13,
        sub_size=10,
    )
    parts += arrow_down(side_x + side_w / 2, lst_y + lst_h, db_y)
    # lfg_core reads the stores the listeners keep fresh.
    parts.append(
        f'<path d="M{core_x + core_w},{db_y + db_h / 2:.1f} L{side_x},{db_y + db_h / 2:.1f}" '
        f'fill="none" stroke="{MUTED}" stroke-width="2" stroke-linecap="round" '
        'stroke-dasharray="4 4"/>'
    )

    # Row 4: external systems
    externals = [
        ("XRP Ledger", "Clio + rippled · NFToken txs", RED),
        ("Xaman (XUMM)", "QR + push signing · no custody", ORANGE),
        ("BunnyCDN + IPFS", "image + metadata hosting", BLUE),
    ]
    ext_y, ext_h, ext_gap = 470 + delta, 66, 16
    ew = (AREA_W - ext_gap * (len(externals) - 1)) / len(externals)  # 270
    core_bot = core_y + core_h
    for i, (title, sub, accent) in enumerate(externals):
        ex = PAD + i * (ew + ext_gap)
        ecx = ex + ew / 2
        src_x = min(max(ecx, core_x + 40), core_x + core_w - 40)
        parts += elbow_down(src_x, core_bot, ecx, ext_y, (core_bot + ext_y) / 2 + 14)
        parts += box(ex, ext_y, ew, ext_h, accent, title, sub, title_size=15, sub_size=10)

    # Footer
    parts.append(
        f'<text x="{W / 2}" y="{h_total - 22}" text-anchor="middle" font-family="{FONT}" '
        f'font-size="11.5" fill="{MUTED}">every XRPL transaction carries SourceTag '
        "2606160021 + provenance memos — all signing happens in the user’s "
        "Xaman wallet</text>"
    )
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def main() -> int:
    svg = build_svg(discover_flow_modules())
    changed = not SVG_PATH.exists() or SVG_PATH.read_text() != svg
    if changed:
        SVG_PATH.write_text(svg)
        print(f"wrote {SVG_PATH}")
    else:
        print(f"{SVG_PATH} unchanged")
    return 0


if __name__ == "__main__":
    sys.exit(main())
