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

from lfg_core import ape_face, swap_meta, trait_config


def _canonical(attributes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Canonical layer order (z-order from trait_config: layer z, or a
    per-value z_override — this is what floats effect traits like Wavy Eyes
    to render on top of everything else); 'None'/empty values skipped (no
    layer file)."""
    non_empty = [a for a in attributes if a.get("value") and a["value"] != "None"]
    return trait_config.get_config().sort_attributes(non_empty)


async def resolve_layer(store: Any, cfg: Any, body: str, trait_type: str, value: str) -> str | None:
    """Own dir first, then layers/shared/ (both via store.resolve — shared
    values are body-independent and bypass matrix gating, short-circuiting
    before the foreign loop); else any matrix-permitted foreign dir
    (cross-body swaps render the source body's asset). Affinity narrower
    than the matrix wins.

    NOTE: the foreign-body loop is FIRST-MATCH in list_bodies() order
    (alphabetical). That's only deterministic-by-construction because of a
    config invariant: universal-layer art (Accessory/Back) is body-independent,
    and every non-universal cross-body pair currently permits exactly ONE
    foreign body per (body, layer) — so at most one foreign dir can matter.
    If PR-5's layers/shared/ move (or a future swap_matrix change) breaks
    that invariant, first-match silently picks the alphabetically-first
    body's art; revisit the ordering here before relying on it."""
    path: str | None = await store.resolve(body, trait_type, value)
    if path:
        return path
    for foreign in await store.list_bodies():
        if foreign == body or not cfg.swap_allowed(body, foreign, trait_type):
            continue
        if not cfg.value_allowed(foreign, trait_type, value):
            continue
        path = await store.resolve(foreign, trait_type, value)
        if path:
            return path
    return None


async def missing_layers(attributes: list[dict[str, Any]], body: str, store: Any) -> list[str]:
    """Trait + ape-structural files the store can't provide — checked BEFORE
    any burn."""
    canonical = _canonical(attributes)
    cfg = trait_config.get_config()
    resolved = await asyncio.gather(
        *(resolve_layer(store, cfg, body, a["trait_type"], a["value"]) for a in canonical)
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
    (nose + melt-ape masking), and overlay in order determined by trait_config
    z-values (including effect layers on top). Returns (output_path, is_video)."""
    canonical = _canonical(attributes)
    cfg = trait_config.get_config()
    paths = await asyncio.gather(
        *(resolve_layer(store, cfg, body, a["trait_type"], a["value"]) for a in canonical)
    )
    layers: list[tuple[str, str, str]] = []
    for a, path in zip(canonical, paths, strict=False):
        if not path:
            raise FileNotFoundError(f"Layer not found: {body}/{a['trait_type']}/{a['value']}")
        layers.append((a["trait_type"], a["value"], path))
    if not canonical:
        raise ValueError("No trait layers to compose")

    body_value = swap_meta.get_attr(attributes, "Body") or ""
    layers = await ape_face.inject_and_mask(layers, body, body_value, store, out_dir)
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
    output_path: str, is_video: bool, upload: Any, cdn_basename: str, keep_still: str | None = None
) -> tuple[str, str | None]:
    """Upload a composed NFT (mp4 + png thumbnail for videos, png otherwise)
    via `upload(path_on_cdn, data, content_type) -> url`, cleaning up local
    files. Returns (image_url, video_url).

    With `keep_still` set, the PNG still (the poster frame for videos) is
    moved there instead of deleted — the swap/mint flows stage it for the
    local image archive (#163) and promote/discard it once the on-chain
    outcome is known."""
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
                _stash_or_remove(thumb, keep_still)
        else:
            with open(output_path, "rb") as f:
                image_url = await upload(f"{cdn_basename}.png", f.read(), "image/png")
    finally:
        if is_video:
            if os.path.exists(output_path):
                os.remove(output_path)
        else:
            _stash_or_remove(output_path, keep_still)
    return image_url, video_url


def _stash_or_remove(path: str, keep_still: str | None) -> None:
    if not os.path.exists(path):
        return
    if keep_still:
        try:
            os.makedirs(os.path.dirname(keep_still), exist_ok=True)
            os.replace(path, keep_still)
            return
        except OSError:
            logging.exception(f"Staging still {path} -> {keep_still} failed")
    try:
        os.remove(path)
    except OSError:
        # Local cleanup must never turn a successful CDN upload into a
        # failed mint/swap.
        logging.exception(f"Removing composed still {path} failed")
