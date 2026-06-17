# lfg_core/swap_compose.py
# NFT composition from the unified layer store (shared by mint and swap).
# Layers resolve through a LayerStore (CDN-backed or local) and are overlaid
# with ffmpeg: PNG output when every layer is a PNG, otherwise MP4 (audio is
# carried over from the first video trait that has it).

import asyncio
import logging
import os
from typing import Any

import ffmpeg

from lfg_core.swap_meta import TRAIT_ORDER

# Traits that must render on top of everything else (e.g. laser eyes).
TOP_TRAITS = [
    {"trait_type": "Eyes", "value": "Wavy"},
    {"trait_type": "Mouth", "value": "Rainbow Puke"},
    {"trait_type": "Eyes", "value": "Laser Eyes"},
    {"trait_type": "Eyes", "value": "Laser"},
]


def _ordered_traits(attributes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Canonical layer order, with TOP_TRAITS moved to the end (on top).
    'None' values are skipped (no layer file)."""
    ordered = sorted(
        (a for a in attributes if a.get("value") and a["value"] != "None"),
        key=lambda a: TRAIT_ORDER.index(a["trait_type"]),
    )
    tops = [
        a for a in ordered if {"trait_type": a["trait_type"], "value": a["value"]} in TOP_TRAITS
    ]
    rest = [a for a in ordered if a not in tops]
    return rest + tops


async def missing_layers(attributes: list[dict[str, Any]], body: str, store: Any) -> list[str]:
    """Trait files the store can't provide — checked BEFORE any burn."""
    ordered = _ordered_traits(attributes)
    resolved = await asyncio.gather(
        *(store.resolve(body, a["trait_type"], a["value"]) for a in ordered)
    )
    return [
        f"{body}/{a['trait_type']}/{a['value']}"
        for a, path in zip(ordered, resolved, strict=False)
        if not path
    ]


async def compose_nft(
    attributes: list[dict[str, Any]],
    body: str,
    store: Any,
    output_basename: str,
    out_dir: str = "generated",
) -> tuple[str, bool]:
    """Resolve all trait layers through the store and overlay them.
    Returns (output_path, is_video)."""
    ordered = _ordered_traits(attributes)
    files = await asyncio.gather(
        *(store.resolve(body, a["trait_type"], a["value"]) for a in ordered)
    )
    for a, path in zip(ordered, files, strict=False):
        if not path:
            raise FileNotFoundError(f"Layer not found: {body}/{a['trait_type']}/{a['value']}")
    if not files:
        raise ValueError("No trait layers to compose")

    os.makedirs(out_dir, exist_ok=True)
    is_video = any(not f.endswith(".png") for f in files)
    ext = "mp4" if is_video else "png"
    output_path = os.path.join(out_dir, f"{output_basename}.{ext}")
    await asyncio.to_thread(_run_ffmpeg, files, output_path, is_video)
    logging.info(f"Composed NFT: {output_path}")
    return output_path, is_video


def _run_ffmpeg(files: list[str], output_path: str, is_video: bool) -> None:
    inputs = [ffmpeg.input(f) for f in files]
    stream = inputs[0]
    for inp in inputs[1:]:
        stream = stream.overlay(inp)

    audio = None
    if is_video:
        # Carry audio over from the first video trait that has it.
        for f, inp in zip(files, inputs, strict=False):
            if f.endswith(".mp4") and _has_audio(f):
                audio = inp.audio
                break
    kwargs = {} if is_video else {"vframes": 1, "update": 1}
    if audio is not None:
        ffmpeg.output(stream, audio, output_path, **kwargs).overwrite_output().run(quiet=True)
    else:
        ffmpeg.output(stream, output_path, **kwargs).overwrite_output().run(quiet=True)


def _has_audio(path: str) -> bool:
    try:
        info = ffmpeg.probe(path)
        return any(s.get("codec_type") == "audio" for s in info.get("streams", []))
    except Exception:
        return False


def extract_first_frame(video_path: str, image_path: str) -> str:
    """PNG thumbnail of a video NFT (used as the metadata image)."""
    ffmpeg.input(video_path).output(image_path, vframes=1).overwrite_output().run(quiet=True)
    return image_path


async def upload_output(
    output_path: str, is_video: bool, upload: Any, cdn_basename: str
) -> tuple[str, str | None]:
    """Upload a composed NFT (mp4 + png thumbnail for videos, png otherwise)
    via `upload(path_on_cdn, data, content_type) -> url`, cleaning up local
    files. Returns (image_url, video_url)."""
    video_url = None
    try:
        if is_video:
            with open(output_path, "rb") as f:
                video_url = await upload(f"{cdn_basename}.mp4", f.read(), "video/mp4")
            thumb = await asyncio.to_thread(
                extract_first_frame, output_path, os.path.splitext(output_path)[0] + ".png"
            )
            try:
                with open(thumb, "rb") as f:
                    image_url = await upload(f"{cdn_basename}.png", f.read(), "image/png")
            finally:
                if os.path.exists(thumb):
                    os.remove(thumb)
        else:
            with open(output_path, "rb") as f:
                image_url = await upload(f"{cdn_basename}.png", f.read(), "image/png")
    finally:
        if os.path.exists(output_path):
            os.remove(output_path)
    return image_url, video_url
