#!/usr/bin/env python
# scripts/make_layer_thumbs.py
# Build/refresh the layer thumbnail tier: layers/.thumbs/ mirrors layers/ with
# every asset downscaled to 512x512 — PNG sources stay PNG (ffmpeg lanczos),
# animated sources (.gif/.webm/.mp4) become GIF (RGBA frames -> gifski) so
# previews render in a plain <img> everywhere (Discord's webview can't play
# WebM/MP4 in <img>, which broke the trait shop and Activity tiles).
#
# Idempotent: only thumbs that are missing or older than their source (mtime)
# are rebuilt; thumbs whose source is gone are pruned. `--check` builds
# nothing and exits 1 if any thumb is stale/missing (audit/CI-friendly).
#
# Requires ffmpeg/ffprobe on PATH; gifski too when any animated source is
# stale (prebuilt binary at ~/.local/bin/gifski on the deploy box). .webm
# inputs are decoded with libvpx-vp9 explicitly — ffmpeg's native VP9 decoder
# silently drops the alpha side-channel.
#
# Usage:
#   .venv/bin/python scripts/make_layer_thumbs.py                # LAYERS_DIR
#   .venv/bin/python scripts/make_layer_thumbs.py --layers-dir ~/LFG/layers
#   .venv/bin/python scripts/make_layer_thumbs.py --check

import argparse
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lfg_core import config, layer_thumbs  # noqa: E402

ANIMATED_EXTS = {".gif", ".webm", ".mp4"}


def run(cmd: list[str]) -> subprocess.CompletedProcess[bytes]:
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"{cmd[0]} failed: {proc.stderr.decode(errors='replace').strip()[-500:]}"
        )
    return proc


def probe_fps(path: str) -> int:
    out = (
        run(
            [
                "ffprobe",
                "-v",
                "quiet",
                "-show_entries",
                "stream=r_frame_rate",
                "-of",
                "csv=p=0",
                path,
            ]
        )
        .stdout.decode()
        .strip()
    )
    num, _, den = out.partition("/")
    try:
        fps = int(num) / int(den or "1")
    except (ValueError, ZeroDivisionError):
        return 20
    return max(1, round(fps)) if fps > 0 else 20


def _decode_args(src: str) -> list[str]:
    # Force the libvpx decoder for WebM: ffmpeg's native VP9 decoder silently
    # drops the alpha side-channel, flattening the layer onto black.
    if src.lower().endswith(".webm"):
        return ["-c:v", "libvpx-vp9"]
    return []


def build_png_thumb(src: str, dest: str, size: int) -> None:
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-i",
            src,
            "-vf",
            f"scale={size}:{size}:flags=lanczos",
            "-frames:v",
            "1",
            dest,
        ]
    )


def build_gif_thumb(src: str, dest: str, size: int, quality: int) -> None:
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    fps = probe_fps(src)
    with tempfile.TemporaryDirectory() as tmp:
        run(
            [
                "ffmpeg",
                "-y",
                "-v",
                "error",
                *_decode_args(src),
                "-i",
                src,
                "-vf",
                f"fps={fps},scale={size}:{size}:flags=lanczos",
                "-pix_fmt",
                "rgba",
                os.path.join(tmp, "f%05d.png"),
            ]
        )
        frames = sorted(os.path.join(tmp, f) for f in os.listdir(tmp) if f.endswith(".png"))
        if not frames:
            raise RuntimeError(f"no frames decoded from {src}")
        run(
            [
                "gifski",
                "--fps",
                str(fps),
                "--quality",
                str(quality),
                "-W",
                str(size),
                "-H",
                str(size),
                "-o",
                dest,
                *frames,
            ]
        )


def build_thumb(src: str, dest: str, size: int, quality: int) -> None:
    if os.path.splitext(src)[1].lower() in ANIMATED_EXTS:
        build_gif_thumb(src, dest, size, quality)
    else:
        build_png_thumb(src, dest, size)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--layers-dir", default=config.LAYERS_DIR, help="layer tree root (default: LAYERS_DIR)"
    )
    parser.add_argument(
        "--size", type=int, default=layer_thumbs.THUMB_SIZE, help="thumb canvas (default 512)"
    )
    parser.add_argument("--quality", type=int, default=80, help="gifski quality (default 80)")
    parser.add_argument(
        "--check",
        action="store_true",
        help="build nothing; exit 1 if any thumb is missing/stale or orphaned",
    )
    args = parser.parse_args()

    base = os.path.abspath(os.path.expanduser(args.layers_dir))
    if not os.path.isdir(base):
        parser.error(f"not a directory: {base}")
    stale, orphans = layer_thumbs.scan(base)

    if args.check:
        for src, _ in stale:
            print(f"stale/missing thumb: {os.path.relpath(src, base)}")
        for t in orphans:
            print(f"orphan thumb: {os.path.relpath(t, base)}")
        print(f"{len(stale)} stale, {len(orphans)} orphans")
        return 1 if stale or orphans else 0

    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            parser.error(f"{tool} not found on PATH")
    if any(os.path.splitext(s)[1].lower() in ANIMATED_EXTS for s, _ in stale):
        if shutil.which("gifski") is None:
            parser.error("gifski not found on PATH (https://gif.ski)")

    failed = 0
    for i, (src, dest) in enumerate(stale, 1):
        try:
            build_thumb(src, dest, args.size, args.quality)
            print(f"[{i}/{len(stale)}] {os.path.relpath(dest, base)}")
        except RuntimeError as e:
            failed += 1
            print(f"[{i}/{len(stale)}] FAILED {os.path.relpath(src, base)}: {e}")
    for t in orphans:
        os.remove(t)
        print(f"pruned {os.path.relpath(t, base)}")
    print(f"done: {len(stale) - failed} built, {failed} failed, {len(orphans)} pruned")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
