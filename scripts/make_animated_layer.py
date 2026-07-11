#!/usr/bin/env python
# scripts/make_animated_layer.py
# Convert an animated GIF into a compose-ready trait layer: resample to a
# uniform frame cadence with ffmpeg (preserving wall-clock timing, incl.
# variable frame delays / hold frames), decompose to RGBA frames,
# lanczos-scale to the layer canvas (1080x1080), and re-encode with gifski
# (per-frame palettes, alpha preserved). Used for the animated Irridescent
# bodies (2026-07-11); applies to any animated trait.
#
# Layer inputs MUST match the 1080x1080 canvas of the static layers (the
# compose pipeline does no scaling — an undersized layer renders small in the
# top-left corner) and MUST keep their alpha channel (an opaque background
# paints over every layer below it). This script guarantees both; it verifies
# the output's size and per-frame corner transparency before declaring
# success. Non-square sources are refused rather than stretched.
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

DEFAULT_FPS = 20  # fallback when ffprobe reports a degenerate frame rate


def run(cmd: list[str]) -> subprocess.CompletedProcess[bytes]:
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"{cmd[0]} failed: {proc.stderr.decode(errors='replace').strip()[-500:]}"
        )
    return proc


def probe(path: str, entries: str) -> str:
    return (
        run(["ffprobe", "-v", "quiet", "-show_entries", entries, "-of", "csv=p=0", path])
        .stdout.decode()
        .strip()
    )


def probe_fps(path: str) -> int:
    out = probe(path, "stream=r_frame_rate")
    num, _, den = out.partition("/")
    try:
        num_val, den_val = int(num), int(den or "1")
    except ValueError:
        return DEFAULT_FPS
    if num_val <= 0 or den_val <= 0:
        return DEFAULT_FPS
    return max(1, round(num_val / den_val))


def probe_size(path: str) -> tuple[int, int]:
    width, height = probe(path, "stream=width,height").split(",")[:2]
    return int(width), int(height)


def opaque_corner_frames(path: str) -> list[int]:
    """Indices of frames whose top-left 10x10 corner is fully opaque.

    Checks EVERY frame, not just the first — a source that starts transparent
    but flattens to an opaque background mid-animation would cover the layers
    below it during composition."""
    raw = run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            path,
            "-vf",
            "format=rgba,crop=10:10:0:0",
            "-f",
            "rawvideo",
            "-",
        ]
    ).stdout
    frame_bytes = 10 * 10 * 4
    bad = []
    for i in range(len(raw) // frame_bytes):
        alphas = raw[i * frame_bytes : (i + 1) * frame_bytes][3::4]
        if set(alphas) == {255}:
            bad.append(i)
    return bad


def convert(src: str, dest: str, size: int, quality: int, fps: int | None) -> None:
    src_w, src_h = probe_size(src)
    if src_w != src_h:
        raise RuntimeError(
            f"{src} is {src_w}x{src_h} — layer sources must be square; refusing to "
            "stretch. Re-export on a square canvas."
        )
    fps = fps or probe_fps(src)
    dest_dir = os.path.dirname(dest)
    if dest_dir:
        os.makedirs(dest_dir, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        # fps filter first: resamples variable-delay/hold-frame GIFs to a
        # uniform cadence while preserving wall-clock timing, so gifski's
        # constant --fps reproduces the original playback speed and pauses.
        run(
            [
                "ffmpeg",
                "-y",
                "-v",
                "error",
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
    width, height = probe_size(dest)
    if (width, height) != (size, size):
        raise RuntimeError(f"output is {width}x{height}, expected {size}x{size}")
    bad_frames = opaque_corner_frames(dest)
    if bad_frames:
        raise RuntimeError(
            f"output frames {bad_frames[:5]}{'…' if len(bad_frames) > 5 else ''} have a "
            "fully opaque corner — the source likely lost its alpha channel (e.g. "
            "exported via mp4). Re-export with transparency."
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
