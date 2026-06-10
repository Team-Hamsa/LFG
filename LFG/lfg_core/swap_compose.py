# lfg_core/swap_compose.py
# Recompose a swapped NFT from the gender-specific layer directories
# (<layers_dir>/<gender>/<TraitType>/<Value>.png|.gif|.mp4) with ffmpeg.
# Ported from Trait-Swapper/helpers.py makeNft(), using only ffmpeg-python
# (audio is mapped directly from the video trait instead of via moviepy).

import os
import logging

import ffmpeg

from lfg_core.swap_meta import TRAIT_ORDER

# Traits that must render on top of everything else (e.g. laser eyes).
TOP_TRAITS = [
    {"trait_type": "Eyes", "value": "Wavy"},
    {"trait_type": "Mouth", "value": "Rainbow Puke"},
    {"trait_type": "Eyes", "value": "Laser Eyes"},
    {"trait_type": "Eyes", "value": "Laser"},
]

_EXTENSIONS = (".png", ".gif", ".mp4")


def _layer_file(layers_dir: str, gender: str, trait_type: str, value: str):
    base = os.path.join(layers_dir, gender, trait_type, value)
    for ext in _EXTENSIONS:
        if os.path.isfile(base + ext):
            return base + ext
    return None


def _ordered_traits(attributes: list) -> list:
    """Canonical layer order, with TOP_TRAITS moved to the end (on top).
    'None' values are skipped (no layer file)."""
    ordered = sorted(
        (a for a in attributes if a.get("value") and a["value"] != "None"),
        key=lambda a: TRAIT_ORDER.index(a["trait_type"]))
    tops = [a for a in ordered
            if {"trait_type": a["trait_type"], "value": a["value"]} in TOP_TRAITS]
    rest = [a for a in ordered if a not in tops]
    return rest + tops


def missing_layers(attributes: list, gender: str, layers_dir: str) -> list:
    """Trait files that don't exist on disk — checked BEFORE any burn."""
    missing = []
    for a in _ordered_traits(attributes):
        if not _layer_file(layers_dir, gender, a["trait_type"], a["value"]):
            missing.append(f"{gender}/{a['trait_type']}/{a['value']}")
    return missing


def compose_swapped_nft(attributes: list, gender: str, nft_number: int,
                        burn_count: int, layers_dir: str, out_dir: str = "generated"):
    """Overlay all trait layers; returns (output_path, is_video).
    Output is PNG when every layer is a PNG, otherwise MP4."""
    os.makedirs(out_dir, exist_ok=True)
    files = []
    for a in _ordered_traits(attributes):
        path = _layer_file(layers_dir, gender, a["trait_type"], a["value"])
        if not path:
            raise FileNotFoundError(
                f"Layer not found: {gender}/{a['trait_type']}/{a['value']}")
        files.append(path)
    if not files:
        raise ValueError("No trait layers to compose")

    is_video = any(not f.endswith(".png") for f in files)
    ext = "mp4" if is_video else "png"
    output_path = os.path.join(out_dir, f"{nft_number}_{burn_count}.{ext}")

    inputs = [ffmpeg.input(f) for f in files]
    stream = inputs[0]
    for inp in inputs[1:]:
        stream = stream.overlay(inp)

    out_kwargs = {}
    audio = None
    if is_video:
        # Carry audio over from the first video trait that has it.
        for f, inp in zip(files, inputs):
            if f.endswith(".mp4") and _has_audio(f):
                audio = inp.audio
                break
    if audio is not None:
        ffmpeg.output(stream, audio, output_path, **out_kwargs)\
              .overwrite_output().run(quiet=True)
    else:
        ffmpeg.output(stream, output_path, **out_kwargs)\
              .overwrite_output().run(quiet=True)
    logging.info(f"Composed swap NFT: {output_path}")
    return output_path, is_video


def _has_audio(path: str) -> bool:
    try:
        info = ffmpeg.probe(path)
        return any(s.get("codec_type") == "audio" for s in info.get("streams", []))
    except Exception:
        return False


def extract_first_frame(video_path: str, image_path: str) -> str:
    """PNG thumbnail of a video NFT (used as the metadata image)."""
    ffmpeg.input(video_path).output(image_path, vframes=1)\
          .overwrite_output().run(quiet=True)
    return image_path
