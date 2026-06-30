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

from lfg_core import ape_face, swap_meta
from lfg_core.ape_face import TOP_TRAITS
from lfg_core.swap_meta import TRAIT_ORDER


def _canonical(attributes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Canonical layer order; 'None'/empty values skipped (no layer file)."""
    return sorted(
        (a for a in attributes if a.get("value") and a["value"] != "None"),
        key=lambda a: TRAIT_ORDER.index(a["trait_type"]),
    )


def _float_tops(layers: list[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
    """Move TOP_TRAITS effect layers to the end (rendered on top)."""
    tops = [lyr for lyr in layers if {"trait_type": lyr[0], "value": lyr[1]} in TOP_TRAITS]
    rest = [lyr for lyr in layers if lyr not in tops]
    return rest + tops


async def missing_layers(attributes: list[dict[str, Any]], body: str, store: Any) -> list[str]:
    """Trait + ape-structural files the store can't provide — checked BEFORE
    any burn."""
    canonical = _canonical(attributes)
    resolved = await asyncio.gather(
        *(store.resolve(body, a["trait_type"], a["value"]) for a in canonical)
    )
    missing = [
        f"{body}/{a['trait_type']}/{a['value']}"
        for a, path in zip(canonical, resolved, strict=False)
        if not path
    ]
    if body == "ape":
        if await store.resolve_asset(f"{body}/{ape_face.NOSE_ASSET}") is None:
            missing.append(f"{body}/{ape_face.NOSE_ASSET}")
        body_value = swap_meta.get_attr(attributes, "Body") or ""
        if (
            body_value in ape_face.MASKED_BODY_VALUES
            and await store.resolve_asset(f"{body}/{ape_face.MASK_ASSET}") is None
        ):
            missing.append(f"{body}/{ape_face.MASK_ASSET}")
    return missing


async def compose_nft(
    attributes: list[dict[str, Any]],
    body: str,
    store: Any,
    output_basename: str,
    out_dir: str = "generated",
) -> tuple[str, bool]:
    """Resolve all trait layers through the store, apply the ape face rule
    (nose + melt-ape masking), float TOP effects, and overlay.
    Returns (output_path, is_video)."""
    canonical = _canonical(attributes)
    paths = await asyncio.gather(
        *(store.resolve(body, a["trait_type"], a["value"]) for a in canonical)
    )
    for a, path in zip(canonical, paths, strict=False):
        if not path:
            raise FileNotFoundError(f"Layer not found: {body}/{a['trait_type']}/{a['value']}")
    if not canonical:
        raise ValueError("No trait layers to compose")

    layers = [(a["trait_type"], a["value"], p) for a, p in zip(canonical, paths, strict=False)]
    body_value = swap_meta.get_attr(attributes, "Body") or ""
    layers = await ape_face.inject_and_mask(layers, body, body_value, store, out_dir)
    layers = _float_tops(layers)
    files = [p for _t, _v, p in layers]
    masked_temps = [p for p in files if p.endswith(".masked.png")]

    os.makedirs(out_dir, exist_ok=True)
    is_video = any(not f.endswith(".png") for f in files)
    ext = "mp4" if is_video else "png"
    output_path = os.path.join(out_dir, f"{output_basename}.{ext}")
    try:
        await asyncio.to_thread(_run_ffmpeg, files, output_path, is_video)
    finally:
        for tmp in masked_temps:
            if os.path.exists(tmp):
                os.remove(tmp)
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
