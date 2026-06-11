# lfg_core/traits.py
# Random trait selection from the unified layer store (used by the webapp
# mint flow). The classic bot's directory-based helpers live in main.py.

import random

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
