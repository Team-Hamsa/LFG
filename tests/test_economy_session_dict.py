# tests/test_economy_session_dict.py
# #316: economy_session_dict("equip", s) must expose the machine-readable
# `resolution` outcome discriminator, getattr-safe for fakes/old journals
# that predate the field.
import os

os.environ.setdefault("BUNNY_PULL_ZONE", "https://example.test")
os.environ.setdefault("LAYER_SOURCE", "local")

from webapp import economy_api  # noqa: E402


class _Fake:
    id = "e1"
    state = "failed"
    error = "outcome unknown"
    displaced = {"Head": "Crown"}
    resolution = "uncertain"


def test_equip_dict_carries_resolution():
    d = economy_api.economy_session_dict("equip", _Fake())
    assert d["resolution"] == "uncertain"
    assert d["displaced"] == [{"slot": "Head", "value": "Crown"}]


def test_equip_dict_resolution_defaults_none_when_absent():
    class _Old:  # predates the field / a mock fake
        id = "e2"
        state = "done"
        error = None
        displaced: dict[str, str] = {}

    d = economy_api.economy_session_dict("equip", _Old())
    assert d["resolution"] is None
