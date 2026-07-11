#!/usr/bin/env python
# scripts/make_animated_layer.py
# Convert an animated GIF into a compose-ready trait layer: decompose to RGBA
# frames with ffmpeg, lanczos-scale to the layer canvas (1080x1080), and
# re-encode with gifski (per-frame palettes, alpha preserved). Used for the
# animated Irridescent bodies (2026-07-11); applies to any animated trait.
#
# Layer inputs MUST match the 1080x1080 canvas of the static layers (the
# compose pipeline does no scaling — an undersized layer renders small in the
# top-left corner) and MUST keep their alpha channel (an opaque background
# paints over every layer below it). This script guarantees both; it verifies
# the output's size and corner transparency before declaring success.
#
# Requires ffmpeg/ffprobe on PATH and gifski (https://gif.ski — prebuilt
# binary lives at ~/.local/bin/gifski on the deploy box). gifski silently
# halves output resolution unless -W/-H are passed explicitly; this script
# always passes them.
#
# Usage:
#   .venv/bin/python scripts/make_animated_layer.py "in.gif" -o "layers/female/Body/Curved Irridescent.gif"
#   .venv/bin/python scripts/make_animated_layer.py "in.gif" ...   # writes <stem>.1080.gif next to input

import argparse
import os
import shutil
import subprocess
import sys
import tempfile


def run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed: {proc.stderr.strip()[-500:]}")


def probe_fps(path: str) -> int:
    out = subprocess.run(
        [
            "ffprobe",
            "-v",
            "quiet",
            "-show_entries",
            "stream=r_frame_rate",
            "-of",
            "csv=p=0",
            path,
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    num, _, den = out.partition("/")
    return max(1, round(int(num) / int(den or "1")))


def probe_size(path: str) -> tuple[int, int]:
    out = subprocess.run(
        [
            "ffprobe",
            "-v",
            "quiet",
            "-show_entries",
            "stream=width,height",
            "-of",
            "csv=p=0",
            path,
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    width, height = out.split(",")[:2]
    return int(width), int(height)


def corner_alpha_values(path: str) -> set[int]:
    """Alpha bytes of the top-left 10x10 of the first frame."""
    with tempfile.TemporaryDirectory() as tmp:
        frame = os.path.join(tmp, "probe.png")
        run(["ffmpeg", "-y", "-v", "error", "-i", path, "-frames:v", "1", "-pix_fmt", "rgba", frame])
        raw = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-i",
                frame,
                "-vf",
                "format=rgba,crop=10:10:0:0",
                "-f",
                "rawvideo",
                "-",
            ],
            capture_output=True,
            check=True,
        ).stdout
    return set(raw[3::4])


def convert(src: str, dest: str, size: int, quality: int, fps: int | None) -> None:
    fps = fps or probe_fps(src)
    with tempfile.TemporaryDirectory() as tmp:
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
                "-pix_fmt",
                "rgba",
                os.path.join(tmp, "f%05d.png"),
            ]
        )
        frames = sorted(
            os.path.join(tmp, f) for f in os.listdir(tmp) if f.endswith(".png")
        )
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
    width, height = probe_size(dest)
    if (width, height) != (size, size):
        raise RuntimeError(f"output is {width}x{height}, expected {size}x{size}")
    if corner_alpha_values(dest) == {255}:
        raise RuntimeError(
            "output corner is fully opaque — the source likely lost its alpha "
            "channel (e.g. exported via mp4). Re-export with transparency."
        )
    print(f"{dest}: {size}x{size} @ {fps}fps, {os.path.getsize(dest) / 1e6:.1f}MB, alpha OK")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="+", help="source animated GIF(s)")
    parser.add_argument("-o", "--output", help="output path (single input only)")
    parser.add_argument("--size", type=int, default=1080, help="canvas size (default 1080)")
    parser.add_argument("--quality", type=int, default=90, help="gifski quality (default 90)")
    parser.add_argument("--fps", type=int, help="override fps (default: probed from input)")
    args = parser.parse_args()

    if args.output and len(args.inputs) > 1:
        parser.error("-o only works with a single input")
    for tool in ("ffmpeg", "ffprobe", "gifski"):
        if shutil.which(tool) is None:
            parser.error(f"{tool} not found on PATH (gifski: https://gif.ski)")

    for src in args.inputs:
        stem, _ = os.path.splitext(src)
        dest = args.output or f"{stem}.{args.size}.gif"
        convert(src, dest, args.size, args.quality, args.fps)
    return 0


if __name__ == "__main__":
    sys.exit(main())
