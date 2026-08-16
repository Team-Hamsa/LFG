# tests/test_body_fix.py — #301 pure Body-value rewrite helper.
# Env-guard preamble (copy verbatim, see tests/test_seasons.py).
import os

os.environ.setdefault("XUMM_API_KEY", "test")
os.environ.setdefault("XUMM_API_SECRET", "test")
os.environ.setdefault("SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2")
os.environ.setdefault("TOKEN_ISSUER_ADDRESS", "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
os.environ.setdefault("TOKEN_CURRENCY_HEX", "4C46474F00000000000000000000000000000000")
os.environ.setdefault("BUNNY_CDN_ACCESS_KEY", "test")
os.environ.setdefault("BUNNY_CDN_STORAGE_ZONE", "test")
os.environ.setdefault("LAYER_SOURCE", "local")
os.environ.setdefault("BUNNY_PULL_ZONE", "nft.pullzone.example")

import copy  # noqa: E402

from lfg_core.body_fix import BAD, GOOD, rewrite_body_value  # noqa: E402


def _meta(body_val):
    return {
        "edition": 64,
        "image": "https://cdn/x.png",
        "video": "https://cdn/x.mp4",
        "burnCount": 1,
        "attributes": [
            {"trait_type": "Background", "value": "Moving Pink Clouds"},
            {"trait_type": "Body", "value": body_val},
            {"trait_type": "Head", "value": "Cap Black"},
        ],
    }


def test_rewrites_only_body_value():
    original = _meta(BAD)
    snapshot = copy.deepcopy(original)
    meta, changed = rewrite_body_value(original)
    assert changed is True
    bodies = [a["value"] for a in meta["attributes"] if a["trait_type"] == "Body"]
    assert bodies == [GOOD]
    # every non-Body field untouched
    assert meta["image"] == "https://cdn/x.png" and meta["video"] == "https://cdn/x.mp4"
    assert meta["burnCount"] == 1 and meta["edition"] == 64
    assert [a["value"] for a in meta["attributes"] if a["trait_type"] == "Background"] == [
        "Moving Pink Clouds"
    ]
    # the input dict is not mutated in place
    assert original == snapshot


def test_idempotent_when_already_good():
    meta, changed = rewrite_body_value(_meta(GOOD))
    assert changed is False
    bodies = [a["value"] for a in meta["attributes"] if a["trait_type"] == "Body"]
    assert bodies == [GOOD]


def test_no_body_attr_is_noop():
    _, changed = rewrite_body_value({"attributes": [{"trait_type": "Head", "value": "Cap Black"}]})
    assert changed is False


def test_missing_attributes_key_is_noop():
    _, changed = rewrite_body_value({"edition": 5})
    assert changed is False
