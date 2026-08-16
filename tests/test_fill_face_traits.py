# Legacy ape face auto-roll (#168): None face slots are filled through the
# rarity engine the first time an ape goes through the swapper.
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DISCORD_BOT_TOKEN", "x")
os.environ.setdefault("XUMM_API_KEY", "x")
os.environ.setdefault("XUMM_API_SECRET", "x")
os.environ.setdefault("BUNNY_CDN_ACCESS_KEY", "x")
os.environ.setdefault("BUNNY_CDN_STORAGE_ZONE", "x")
os.environ.setdefault("SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2")
os.environ.setdefault("TOKEN_ISSUER_ADDRESS", "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
os.environ.setdefault("TOKEN_CURRENCY_HEX", "4C46474F00000000000000000000000000000000")
os.environ.setdefault("BUNNY_PULL_ZONE", "nft.pullzone.example")
os.environ.setdefault("LAYER_SOURCE", "local")

import asyncio  # noqa: E402
import random  # noqa: E402
import sqlite3  # noqa: E402

import pytest  # noqa: E402

from lfg_core import traits  # noqa: E402
from lfg_core.swap_meta import normalize_attributes  # noqa: E402


class FakeStore:
    """list_values-only store; values per (body, trait_type)."""

    def __init__(self, values):
        self.values = values

    async def list_values(self, body, trait_type):
        return self.values.get((body, trait_type), [])


FACE_VALUES = {
    ("ape", "Eyes"): ["None", "Wide Eyes", "Laser Eyes"],
    ("ape", "Eyebrows"): ["None", "Angry"],
    ("ape", "Mouth"): ["None", "Grin", "Cigar"],
}


def _conn():
    # weighted_pick/ensure_schema create trait_rarity (and tolerate a
    # missing LFG table) on a bare in-memory connection — same pattern
    # used by tests/test_rarity.py's own conn fixture.
    return sqlite3.connect(":memory:")


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _faceless_ape():
    return normalize_attributes(
        [{"trait_type": "Body", "value": "Xray"}, {"trait_type": "Accessory", "value": "Bible"}]
    )


def _get(attrs, t):
    return next(a["value"] for a in attrs if a["trait_type"] == t)


def test_fills_all_none_face_slots_for_ape():
    attrs = _faceless_ape()
    rolled = _run(
        traits.fill_missing_face_traits(
            FakeStore(FACE_VALUES), "ape", attrs, conn=_conn(), rng=random.Random(1)
        )
    )
    assert rolled is True
    for slot in ("Eyes", "Eyebrows", "Mouth"):
        assert _get(attrs, slot) not in ("None", "", None)
    # Non-face slots untouched.
    assert _get(attrs, "Accessory") == "Bible"


def test_never_rolls_the_none_value():
    attrs = _faceless_ape()
    for seed in range(20):
        a = [dict(x) for x in attrs]
        _run(
            traits.fill_missing_face_traits(
                FakeStore(FACE_VALUES), "ape", a, conn=_conn(), rng=random.Random(seed)
            )
        )
        for slot in ("Eyes", "Eyebrows", "Mouth"):
            assert _get(a, slot) != "None"


def test_non_ape_body_is_untouched():
    attrs = _faceless_ape()
    rolled = _run(
        traits.fill_missing_face_traits(
            FakeStore(FACE_VALUES), "skeleton", attrs, conn=_conn(), rng=random.Random(1)
        )
    )
    assert rolled is False
    for slot in ("Eyes", "Eyebrows", "Mouth"):
        assert _get(attrs, slot) == "None"


def test_existing_real_face_value_is_preserved():
    attrs = _faceless_ape()
    for a in attrs:
        if a["trait_type"] == "Eyes":
            a["value"] = "Wide Eyes"
    _run(
        traits.fill_missing_face_traits(
            FakeStore(FACE_VALUES), "ape", attrs, conn=_conn(), rng=random.Random(1)
        )
    )
    assert _get(attrs, "Eyes") == "Wide Eyes"
    assert _get(attrs, "Mouth") != "None"


def test_deterministic_under_seeded_rng():
    a1, a2 = _faceless_ape(), _faceless_ape()
    _run(
        traits.fill_missing_face_traits(
            FakeStore(FACE_VALUES), "ape", a1, conn=_conn(), rng=random.Random(7)
        )
    )
    _run(
        traits.fill_missing_face_traits(
            FakeStore(FACE_VALUES), "ape", a2, conn=_conn(), rng=random.Random(7)
        )
    )
    assert a1 == a2


def test_no_candidates_for_a_slot_is_skipped_not_error():
    # Store has no Eyebrows values at all (layer absent): slot stays None,
    # the others still roll — mirrors select_random_attributes's "no raw
    # values -> skip layer" behavior.
    values = {k: v for k, v in FACE_VALUES.items() if k != ("ape", "Eyebrows")}
    attrs = _faceless_ape()
    rolled = _run(
        traits.fill_missing_face_traits(
            FakeStore(values), "ape", attrs, conn=_conn(), rng=random.Random(1)
        )
    )
    assert rolled is True
    assert _get(attrs, "Eyebrows") == "None"
    assert _get(attrs, "Eyes") != "None"


def test_over_constrained_slot_raises(monkeypatch):
    # Candidates exist but config rules eliminate them all -> fail loud,
    # same contract as select_random_attributes.
    from lfg_core import trait_config

    cfg = trait_config.get_config()
    monkeypatch.setattr(
        type(cfg), "value_allowed", lambda self, body, t, v: t not in ("Eyes", "Eyebrows", "Mouth")
    )
    attrs = _faceless_ape()
    with pytest.raises(ValueError, match="no legal"):
        _run(
            traits.fill_missing_face_traits(
                FakeStore(FACE_VALUES), "ape", attrs, conn=_conn(), rng=random.Random(1)
            )
        )
