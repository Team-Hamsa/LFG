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
# GIF/PNG dimensions come from Pillow with a full per-frame decode (a
# truncated file can still report header dimensions); MP4/WebM from ffprobe.
# Unreadable/corrupt files are reported as failures too. Results are cached
# per (size, mtime) so unchanged art is never re-decoded.
#
# Usage:
#   .venv/bin/python scripts/audit_layer_dimensions.py [--layers-dir DIR] [--skip-png]
# Exit codes: 0 = clean (or no layers dir), 1 = offenders found.

import argparse
import json
import os
import subprocess
import sys
from typing import Any

CANVAS = (1080, 1080)
ANIMATED_EXTS = {".gif", ".mp4", ".webm"}
# Extensions probed with ffprobe rather than Pillow.
VIDEO_EXTS = {".mp4", ".webm"}
STATIC_EXTS = {".png"}

# Full-decode validation of the whole tree takes ~2 minutes, so results are
# cached per file keyed on (size, mtime_ns) — only new/changed art is probed.
CACHE_VERSION = 1


def _load_cache(path: str) -> dict[str, Any]:
    try:
        with open(path) as fh:
            cache = json.load(fh)
        if cache.get("version") == CACHE_VERSION:
            files: dict[str, Any] = cache.get("files", {})
            return files
    except (OSError, ValueError):
        pass
    return {}


def _save_cache(path: str, files: dict[str, Any]) -> None:
    try:
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump({"version": CACHE_VERSION, "files": files}, fh)
        os.replace(tmp, path)
    except OSError:
        pass  # cache is an optimization only — never fail the audit over it


def _probe_video(path: str) -> tuple[int, int] | None:
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
    # Some containers emit one row per stream despite -select_streams; take the first.
    parts = proc.stdout.decode().strip().splitlines()[0].split(",") if proc.stdout.strip() else []
    try:
        return int(parts[0]), int(parts[1])
    except (IndexError, ValueError):
        return None


def _probe_image(path: str) -> tuple[int, int] | None:
    from PIL import Image, ImageSequence, UnidentifiedImageError

    try:
        with Image.open(path) as im:
            size = im.size
            # Image.open only parses the header — a truncated file can still
            # report dimensions. Decode every frame so corrupt payloads fail.
            for frame in ImageSequence.Iterator(im):
                frame.load()
            return size
    except (UnidentifiedImageError, OSError):
        return None


def read_dimensions(path: str) -> tuple[int, int] | None:
    """Return (width, height) or None if unreadable."""
    if os.path.splitext(path)[1].lower() in VIDEO_EXTS:
        return _probe_video(path)
    return _probe_image(path)


def scan(
    layers_dir: str, include_png: bool = True, cache_path: str | None = None
) -> list[tuple[str, str]]:
    """Scan layers_dir; return [(relpath, problem)] for every offender."""
    exts = ANIMATED_EXTS | (STATIC_EXTS if include_png else set())
    cache = _load_cache(cache_path) if cache_path else {}
    fresh: dict[str, Any] = {}
    offenders: list[tuple[str, str]] = []
    for root, dirs, files in os.walk(layers_dir):
        # Prune hidden dirs in place: they hold derived caches (e.g. .thumbs/,
        # 512x512 preview art), never trait layers that reach the compositor.
        dirs[:] = [d for d in sorted(dirs) if not d.startswith(".")]
        for name in sorted(files):
            ext = os.path.splitext(name)[1].lower()
            if ext not in exts:
                continue
            path = os.path.join(root, name)
            rel = os.path.relpath(path, layers_dir)
            st = os.stat(path)
            stamp = [st.st_size, st.st_mtime_ns]
            cached = cache.get(rel)
            if cached and cached.get("stamp") == stamp:
                dims = tuple(cached["dims"]) if cached["dims"] else None
            else:
                dims = read_dimensions(path)
            fresh[rel] = {"stamp": stamp, "dims": list(dims) if dims else None}
            if dims is None:
                offenders.append((rel, "unreadable (corrupt or ffprobe/Pillow failure)"))
            elif dims != CANVAS:
                offenders.append((rel, f"{dims[0]}x{dims[1]} (expected 1080x1080)"))
    if cache_path:
        _save_cache(cache_path, fresh)
    return offenders


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--layers-dir",
        default=os.environ.get("LAYERS_DIR", "layers"),
        help="layer tree root (default: $LAYERS_DIR or ./layers)",
    )
    parser.add_argument(
        "--cache",
        default=os.environ.get("LAYER_DIM_CACHE", ".layer_dimensions_cache.json"),
        help="result cache path (default: .layer_dimensions_cache.json; '' disables)",
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

    offenders = scan(args.layers_dir, include_png=not args.skip_png, cache_path=args.cache or None)
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
