#!/usr/bin/env python3
"""One-off, idempotent uploader for the BLANK-character silhouette art.

Uploads a 1080x1080 PNG to the CDN path ``blank/silhouette.png`` at the storage
zone root — the path ``config.BLANK_IMAGE_URL`` points at
(``https://<BUNNY_PULL_ZONE>/blank/silhouette.png``). Harvest sets a stripped
character's metadata ``image`` to that URL.

  python scripts/upload_blank_art.py --file path/to/silhouette.png

Idempotent: skips the upload when the remote object already exists with the same
byte length (pass --force to re-upload regardless).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

import aiohttp
from dotenv import load_dotenv

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)

load_dotenv()

CDN_PATH = "blank/silhouette.png"
_EXPECTED_SIZE = (1080, 1080)


def _verify_dimensions(path: str) -> bool:
    """Return True if `path` is a 1080x1080 image. Falls back to ffprobe when
    Pillow is absent; if neither is available, warns and returns True (skips
    verification rather than blocking the one-off upload)."""
    try:
        from PIL import Image
    except ImportError:
        import shutil
        import subprocess

        if not shutil.which("ffprobe"):
            print("WARNING: neither Pillow nor ffprobe available; skipping size check")
            return True
        out = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height",
                "-of",
                "csv=p=0:s=x",
                path,
            ],
            capture_output=True,
            text=True,
        )
        dims = out.stdout.strip()
        if dims != "x".join(str(n) for n in _EXPECTED_SIZE):
            print(f"ERROR: {path} is {dims!r}, expected 1080x1080")
            return False
        return True

    with Image.open(path) as im:
        if im.size != _EXPECTED_SIZE:
            print(f"ERROR: {path} is {im.size}, expected {_EXPECTED_SIZE}")
            return False
    return True


async def _remote_size(session: aiohttp.ClientSession, base: str, key: str) -> int | None:
    """Byte length of the existing remote object, or None if it doesn't exist."""
    async with session.get(base, headers={"AccessKey": key}) as r:
        if r.status != 200:
            return None
        return int(r.headers.get("Content-Length") or len(await r.read()))


async def _amain(args: argparse.Namespace) -> int:
    from lfg_core import config

    if not os.path.isfile(args.file):
        print(f"ERROR: no such file: {args.file}")
        return 2
    if not _verify_dimensions(args.file):
        return 2

    with open(args.file, "rb") as f:
        data = f.read()

    base = f"{config.BUNNY_CDN_BASE_URL}/{config.BUNNY_CDN_STORAGE_ZONE}/{CDN_PATH}"
    key = config.BUNNY_CDN_ACCESS_KEY
    async with aiohttp.ClientSession() as session:
        if not args.force:
            existing = await _remote_size(session, base, key)
            if existing == len(data):
                print(f"Up to date: {CDN_PATH} already {existing} bytes (use --force to re-upload)")
                return 0
        async with session.put(
            base, data=data, headers={"AccessKey": key, "Content-Type": "image/png"}
        ) as r:
            if r.status not in (200, 201):
                print(f"ERROR: upload failed ({r.status}) for {CDN_PATH}")
                return 1
    pull = f"https://{config.BUNNY_PULL_ZONE}/{CDN_PATH}" if config.BUNNY_PULL_ZONE else CDN_PATH
    print(f"Uploaded {len(data)} bytes -> {pull}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Upload the blank silhouette art to the CDN.")
    parser.add_argument("--file", required=True, help="path to a 1080x1080 silhouette PNG")
    parser.add_argument("--force", action="store_true", help="re-upload even if size matches")
    return asyncio.run(_amain(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
