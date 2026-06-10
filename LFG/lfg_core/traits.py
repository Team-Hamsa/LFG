# lfg_core/traits.py
# Trait layer selection and ffmpeg image composition.
# select_random_attributes() works against the unified LayerStore (used by
# the webapp mint flow); the directory-based helpers below are kept for the
# classic bot's local trait_layers/ tree.

import os
import re
import random
import logging

import ffmpeg

from lfg_core.swap_meta import TRAIT_ORDER


async def select_random_attributes(store, gender: str = None):
    """Pick a random gender (unless given) and one random value per trait
    type from the unified layer store. Returns (gender, attributes) where
    attributes is a metadata-style [{trait_type, value}] list in layer order."""
    if gender is None:
        genders = await store.list_genders()
        if not genders:
            raise ValueError("Layer store has no gender directories")
        gender = random.choice(genders)
    attributes = []
    for trait_type in TRAIT_ORDER:
        values = await store.list_values(gender, trait_type)
        if values:
            attributes.append({"trait_type": trait_type,
                               "value": random.choice(values)})
    if not attributes:
        raise ValueError(f"No trait layers found for gender '{gender}'")
    return gender, attributes


def get_sorted_trait_layers(trait_layers_dir: str) -> list:
    """Return trait layer folder names in compositing order (numeric prefix)."""
    directories = [
        d for d in os.listdir(trait_layers_dir)
        if os.path.isdir(os.path.join(trait_layers_dir, d))
    ]

    has_numeric_prefix = any(re.match(r'^\d+', d) for d in directories)

    if has_numeric_prefix:
        def sort_key(folder_name):
            match = re.match(r'^(\d+)', folder_name)
            return int(match.group(1)) if match else float('inf')
        return sorted(directories, key=sort_key)

    TRAIT_ORDER = ["background", "body", "clothing", "mouth", "eyebrows",
                   "eyes", "hat:hair", "accessory"]
    return sorted(
        directories,
        key=lambda d: (TRAIT_ORDER.index(d.lower()) if d.lower() in TRAIT_ORDER else float('inf'), d)
    )


def format_trait_name(text: str) -> str:
    """Convert a trait filename stem to capitalized display format."""
    clean_text = re.sub(r'^\d+\s+', '', text).strip()
    return ' '.join(word.capitalize() for word in clean_text.split())


def select_random_traits(trait_layers_dir: str) -> dict:
    """Pick one random PNG per layer. Returns {layer_folder: filename}."""
    selected = {}
    for layer in get_sorted_trait_layers(trait_layers_dir):
        layer_dir = os.path.join(trait_layers_dir, layer)
        valid_files = [f for f in os.listdir(layer_dir)
                       if not f.startswith('.') and f.lower().endswith('.png')]
        if valid_files:
            selected[layer] = random.choice(valid_files)
    return selected


def compose_image(trait_layers_dir: str, selected_traits: dict, output_path: str) -> str:
    """Overlay the selected trait PNGs (in layer order) into output_path."""
    input_images = [
        os.path.join(trait_layers_dir, layer, filename)
        for layer, filename in selected_traits.items()
    ]
    if not input_images:
        raise ValueError("No trait images selected")

    try:
        stream = ffmpeg.input(input_images[0])
        for additional_image in input_images[1:]:
            stream = ffmpeg.overlay(stream, ffmpeg.input(additional_image))
        stream = ffmpeg.output(stream, output_path, vframes=1, update=1, loglevel='error')
        ffmpeg.run(stream, overwrite_output=True, capture_stdout=True, capture_stderr=True)
    except ffmpeg.Error as e:
        error_msg = e.stderr.decode() if e.stderr else str(e)
        logging.error(f"FFmpeg error: {error_msg}")
        raise Exception(f"Failed to generate composite image: {error_msg}")

    return output_path
