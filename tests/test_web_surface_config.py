# tests/test_web_surface_config.py
# Standalone web surface (spec 2026-07-16): WEB_ALLOWED_ORIGINS env parsing
# and the memos surface→platform mapping for platform="web".
import os

os.environ.setdefault("BUNNY_PULL_ZONE", "nft.pullzone.example")
os.environ.setdefault("LAYER_SOURCE", "local")

from lfg_core import memos


def test_web_allowed_origins_default_is_empty_tuple():
    from lfg_core import config

    assert isinstance(config.WEB_ALLOWED_ORIGINS, tuple)


def test_parse_allowed_origins_strips_and_drops_empties():
    from lfg_core.config import _parse_allowed_origins

    got = _parse_allowed_origins(" https://a.example ,, https://b.example ")
    assert got == ("https://a.example", "https://b.example")


def test_parse_allowed_origins_empty_string():
    from lfg_core.config import _parse_allowed_origins

    assert _parse_allowed_origins("") == ()


def test_memos_web_surface_maps_to_webapp():
    assert memos.platform_for_surface("web") == memos.PLATFORM_WEBAPP
