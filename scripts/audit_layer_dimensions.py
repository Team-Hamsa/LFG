#!/usr/bin/env python
# scripts/audit_layer_dimensions.py
# Guardrail: every trait layer under layers/ must be exactly 1080x1080.
#
# The compose pipeline (lfg_core/swap_compose.py) does NO scaling — an
# undersized layer renders as a tiny sprite in the top-left corner of every
# mint/swap it appears in. That happened for real: the two Diamond body GIFs
# shipped at 600x600 on 2026-07-11 and poisoned every composition that used
# them until 2026-07-20. This scanner fails loudly before the next one ships.
#
# layers/ is gitignored, so this must scan the WORKING TREE (never a git
# diff). It runs as a pre-push hook and is a no-op when layers/ is absent
# (CI checkouts have no art).
#
# GIF/PNG dimensions come from Pillow (header-only read, cheap); MP4 from
# ffprobe. A file whose dimensions cannot be read at all is reported as a
# failure too — corrupt art should not slip through as "unscanned".
#
# Usage:
#   .venv/bin/python scripts/audit_layer_dimensions.py [--layers-dir DIR] [--skip-png]
# Exit codes: 0 = clean (or no layers dir), 1 = offenders found.

import argparse
import os
import subprocess
import sys

CANVAS = (1080, 1080)
ANIMATED_EXTS = {".gif", ".mp4"}
STATIC_EXTS = {".png"}


def _probe_mp4(path: str) -> tuple[int, int] | None:
    try:
        proc = subprocess.run(
            [
                "ffprobe",
                "-v",
                "quiet",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height",
                "-of",
                "csv=p=0",
                path,
            ],
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    parts = proc.stdout.decode().strip().split(",")
    try:
        return int(parts[0]), int(parts[1])
    except (IndexError, ValueError):
        return None


def _probe_image(path: str) -> tuple[int, int] | None:
    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(path) as im:
            return im.size
    except (UnidentifiedImageError, OSError):
        return None


def read_dimensions(path: str) -> tuple[int, int] | None:
    """Return (width, height) or None if unreadable."""
    if path.lower().endswith(".mp4"):
        return _probe_mp4(path)
    return _probe_image(path)


def scan(layers_dir: str, include_png: bool = True) -> list[tuple[str, str]]:
    """Scan layers_dir; return [(relpath, problem)] for every offender."""
    exts = ANIMATED_EXTS | (STATIC_EXTS if include_png else set())
    offenders: list[tuple[str, str]] = []
    for root, _dirs, files in os.walk(layers_dir):
        for name in sorted(files):
            ext = os.path.splitext(name)[1].lower()
            if ext not in exts:
                continue
            path = os.path.join(root, name)
            rel = os.path.relpath(path, layers_dir)
            dims = read_dimensions(path)
            if dims is None:
                offenders.append((rel, "unreadable (corrupt or ffprobe/Pillow failure)"))
            elif dims != CANVAS:
                offenders.append((rel, f"{dims[0]}x{dims[1]} (expected 1080x1080)"))
    return offenders


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--layers-dir",
        default=os.environ.get("LAYERS_DIR", "layers"),
        help="layer tree root (default: $LAYERS_DIR or ./layers)",
    )
    parser.add_argument(
        "--skip-png",
        action="store_true",
        help="only scan animated layers (.gif/.mp4), skip PNGs",
    )
    args = parser.parse_args()

    if not os.path.isdir(args.layers_dir):
        print(f"audit_layer_dimensions: no layers dir at {args.layers_dir!r} — nothing to scan")
        return 0

    offenders = scan(args.layers_dir, include_png=not args.skip_png)
    if not offenders:
        print(f"audit_layer_dimensions: OK — every layer under {args.layers_dir!r} is 1080x1080")
        return 0

    print(
        f"audit_layer_dimensions: FAIL — {len(offenders)} layer(s) are not 1080x1080.\n"
        "The compose pipeline does no scaling; these will render as tiny "
        "top-left sprites in every mint/swap. Fix animated layers with "
        "scripts/make_animated_layer.py.",
        file=sys.stderr,
    )
    for rel, problem in offenders:
        print(f"  {rel}: {problem}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
