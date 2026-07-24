#!/usr/bin/env python
# scripts/convert_layers_to_webm.py
# Batch-convert every animated trait layer (.gif/.mp4) in a layers tree to a
# sibling VP9 .webm — far smaller than GIF at 1080 and the native format of
# the true animation sources (see PR #296, which teaches layer_store /
# swap_compose to read .webm).
#
# Safe by construction:
#   - Sources are NEVER deleted. Extension precedence in layer_store is
#     png > gif > webm > mp4, so a .webm written next to its .gif source is
#     inert until the .gif is removed after the .webm-support deploy.
#   - GIF sources (alpha traits) encode as yuva420p VP9 with -auto-alt-ref 0
#     (alpha dies without it) and are verified frame-by-frame: the output is
#     re-decoded with the FORCED libvpx-vp9 decoder (ffmpeg's native VP9
#     decoder silently drops the alpha side-channel) and every frame's
#     top-left corner must stay transparent — the same gate
#     make_animated_layer.py applies to GIF outputs.
#   - MP4 sources (the opaque shared/Background loops) encode as yuv420p,
#     keeping any audio track as Opus (swap_compose maps audio through).
#   - Outputs are scaled to the 1080x1080 layer canvas (lanczos); non-square
#     sources are refused rather than stretched.
#   - A layer shadowed by a same-stem .png is dead art and is skipped.
#   - Hidden dirs are skipped, chiefly the derived .thumbs/ preview tier
#     (512px, GIF-only by design — see lfg_core/layer_thumbs.py).
#
# Usage:
#   .venv/bin/python scripts/convert_layers_to_webm.py                    # convert layers/
#   .venv/bin/python scripts/convert_layers_to_webm.py --dry-run          # list the work
#   .venv/bin/python scripts/convert_layers_to_webm.py --layers-dir ../LFG-staging/layers
#   .venv/bin/python scripts/convert_layers_to_webm.py --only "Curved Diamond" --force

import argparse
import os
import shutil
import subprocess
import sys

CANVAS = 1080
DEFAULT_FPS = 20  # fallback when ffprobe reports a degenerate frame rate


SUBPROCESS_TIMEOUT = 600  # seconds; a 1080p VP9 layer encode finishes well inside this


def run(cmd: list[str]) -> subprocess.CompletedProcess[bytes]:
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=SUBPROCESS_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"{cmd[0]} timed out after {SUBPROCESS_TIMEOUT}s") from exc
    if proc.returncode != 0:
        raise RuntimeError(
            f"{cmd[0]} failed: {proc.stderr.decode(errors='replace').strip()[-500:]}"
        )
    return proc


def probe(path: str, entries: str, extra: list[str] | None = None) -> str:
    cmd = [
        "ffprobe",
        "-v",
        "quiet",
        *(extra or []),
        "-show_entries",
        entries,
        "-of",
        "csv=p=0",
        path,
    ]
    return run(cmd).stdout.decode().strip()


def probe_fps(path: str) -> int:
    out = probe(path, "stream=r_frame_rate", ["-select_streams", "v:0"])
    num, _, den = out.partition("/")
    try:
        num_val, den_val = int(num), int(den or "1")
    except ValueError:
        return DEFAULT_FPS
    if num_val <= 0 or den_val <= 0:
        return DEFAULT_FPS
    return max(1, round(num_val / den_val))


def probe_size(path: str) -> tuple[int, int]:
    out = probe(path, "stream=width,height", ["-select_streams", "v:0"])
    try:
        width, height = out.split(",")[:2]
        return int(width), int(height)
    except ValueError as exc:
        # A corrupt/streamless file probes empty — surface it as the per-file
        # RuntimeError the batch loop catches, not a batch-aborting ValueError.
        raise RuntimeError(f"no readable video stream (ffprobe returned {out!r})") from exc


def has_audio(path: str) -> bool:
    return bool(probe(path, "stream=codec_type", ["-select_streams", "a"]))


def opaque_corner_frames(path: str) -> list[int]:
    """Indices of frames whose top-left 10x10 corner is fully opaque.

    Decodes with the forced libvpx-vp9 decoder — ffmpeg's native VP9 decoder
    silently drops WebM's alpha side-channel, which would make every frame
    read as opaque and fail a perfectly good file (or worse, the inverse check
    pass a broken one)."""
    raw = run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-c:v",
            "libvpx-vp9",
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


def convert_one(src: str, dest: str, crf: int, fps: int | None) -> str:
    src_w, src_h = probe_size(src)
    if src_w != src_h:
        raise RuntimeError(f"{src_w}x{src_h} — layer sources must be square; refusing to stretch")
    fps = fps or probe_fps(src)
    alpha = src.lower().endswith(".gif")
    tmp_dest = dest + ".tmp.webm"
    cmd = [
        "ffmpeg",
        "-y",
        "-v",
        "error",
        "-i",
        src,
        "-vf",
        f"fps={fps},scale={CANVAS}:{CANVAS}:flags=lanczos",
        "-c:v",
        "libvpx-vp9",
        "-pix_fmt",
        "yuva420p" if alpha else "yuv420p",
        "-b:v",
        "0",
        "-crf",
        str(crf),
        "-row-mt",
        "1",
    ]
    if alpha:
        # -auto-alt-ref 0 is required for libvpx-vp9 alpha; without it the
        # encode either fails or the alpha plane is dropped.
        cmd += ["-auto-alt-ref", "0"]
        cmd += ["-an"]
    elif has_audio(src):
        cmd += ["-c:a", "libopus"]
    else:
        cmd += ["-an"]
    cmd += ["-f", "webm", tmp_dest]
    try:
        run(cmd)
        width, height = probe_size(tmp_dest)
        if (width, height) != (CANVAS, CANVAS):
            raise RuntimeError(f"output is {width}x{height}, expected {CANVAS}x{CANVAS}")
        if alpha:
            pix_fmt = probe(tmp_dest, "stream=pix_fmt", ["-select_streams", "v:0"])
            bad = opaque_corner_frames(tmp_dest)
            if bad:
                raise RuntimeError(
                    f"frames {bad[:5]}{'…' if len(bad) > 5 else ''} have a fully opaque "
                    f"corner (pix_fmt={pix_fmt}) — alpha was lost in the encode"
                )
        os.replace(tmp_dest, dest)
    finally:
        if os.path.exists(tmp_dest):
            os.unlink(tmp_dest)
    src_mb = os.path.getsize(src) / 1e6
    dst_mb = os.path.getsize(dest) / 1e6
    return (
        f"{CANVAS}x{CANVAS} @ {fps}fps, {src_mb:.1f}MB -> {dst_mb:.1f}MB"
        f"{', alpha OK' if alpha else ' (opaque)'}"
    )


def find_sources(layers_dir: str, only: str | None) -> list[str]:
    out = []
    for root, dirs, files in os.walk(layers_dir):
        # Skip hidden dirs, mirroring layer_thumbs.scan(). Chiefly .thumbs/ —
        # a derived 512px mirror that is deliberately GIF-only so previews
        # render in a plain <img> (WebM doesn't). Converting it would upscale
        # thumbnails to the 1080 layer canvas AND write a format that tier
        # must not serve; make_layer_thumbs.py regenerates it from the real
        # sources, .webm included.
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for f in sorted(files):
            if not f.lower().endswith((".gif", ".mp4")):
                continue
            if only and only.lower() not in f.lower():
                continue
            out.append(os.path.join(root, f))
    return sorted(out)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--layers-dir", default="layers", help="layers tree root (default layers)")
    parser.add_argument("--only", help="substring filter on the filename")
    parser.add_argument("--force", action="store_true", help="re-encode even if the .webm exists")
    parser.add_argument("--dry-run", action="store_true", help="list the work, convert nothing")
    parser.add_argument("--crf", type=int, default=30, help="libvpx-vp9 CRF (default 30)")
    parser.add_argument("--fps", type=int, help="override fps (default: probed per input)")
    args = parser.parse_args()

    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            parser.error(f"{tool} not found on PATH")
    if not os.path.isdir(args.layers_dir):
        parser.error(f"{args.layers_dir} is not a directory")

    converted = skipped = failed = 0
    saved_bytes = 0
    for src in find_sources(args.layers_dir, args.only):
        stem, _ = os.path.splitext(src)
        dest = stem + ".webm"
        rel = os.path.relpath(src, args.layers_dir)
        if os.path.exists(stem + ".png"):
            print(f"SKIP  {rel}: shadowed by same-stem .png (dead art)")
            skipped += 1
            continue
        if os.path.exists(dest) and not args.force:
            print(f"SKIP  {rel}: .webm already exists (--force to redo)")
            skipped += 1
            continue
        if src.lower().endswith(".mp4") and "background" not in os.path.dirname(src).lower():
            print(f"WARN  {rel}: .mp4 outside a Background dir — encoding opaque anyway")
        if args.dry_run:
            print(f"WOULD {rel} -> {os.path.relpath(dest, args.layers_dir)}")
            converted += 1
            continue
        try:
            detail = convert_one(src, dest, args.crf, args.fps)
        except (RuntimeError, OSError) as exc:
            print(f"FAIL  {rel}: {exc}")
            failed += 1
            continue
        saved_bytes += os.path.getsize(src) - os.path.getsize(dest)
        print(f"OK    {rel}: {detail}")
        converted += 1

    verb = "would convert" if args.dry_run else "converted"
    print(
        f"\n{verb} {converted}, skipped {skipped}, failed {failed}"
        + ("" if args.dry_run else f", net saving {saved_bytes / 1e6:.1f}MB")
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
