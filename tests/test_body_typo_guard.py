# tests/test_body_typo_guard.py — #301 recurrence guard: reject a single-r
# "Iridescent *" Body art file re-entering the layer pool, plus a resolution
# regression documenting why the typo value fails swaps.
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

import asyncio  # noqa: E402
import pathlib  # noqa: E402

from lfg_core import swap_compose  # noqa: E402
from lfg_core.body_fix import BAD, GOOD  # noqa: E402
from scripts.validate_trait_config import find_typo_body_files  # noqa: E402


def _mk(path: pathlib.Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")


def test_guard_flags_single_r_body_file(tmp_path):
    layers = tmp_path / "layers"
    _mk(layers / "skeleton" / "Body" / f"{GOOD}.webm")
    _mk(layers / "skeleton" / "Body" / f"{BAD}.webm")
    flagged = find_typo_body_files(str(layers))
    assert len(flagged) == 1
    assert flagged[0].endswith(f"{BAD}.webm")


def test_guard_passes_double_r_only(tmp_path):
    layers = tmp_path / "layers"
    _mk(layers / "skeleton" / "Body" / f"{GOOD}.webm")
    _mk(layers / "male" / "Body" / "Gold.png")
    assert find_typo_body_files(str(layers)) == []


def test_guard_ignores_non_body_dirs(tmp_path):
    layers = tmp_path / "layers"
    _mk(layers / "skeleton" / "Head" / f"{BAD}.png")
    assert find_typo_body_files(str(layers)) == []


class _FakeStore:
    """Store exposing only the double-r skeleton Body asset."""

    async def resolve(self, body, trait_type, value):
        if (body, trait_type, value) == ("skeleton", "Body", GOOD):
            return "layers/skeleton/Body/Irridescent Skeleton.webm"
        return None

    async def list_bodies(self):
        return ["skeleton"]


def _missing(value):
    attrs = [{"trait_type": "Body", "value": value}]
    # new_event_loop (not asyncio.run) — see repo convention note above.
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(swap_compose.missing_layers(attrs, "skeleton", _FakeStore()))
    finally:
        loop.close()


def test_resolution_regression_good_resolves_bad_gaps():
    assert _missing(GOOD) == []
    gaps = _missing(BAD)
    assert gaps and any("Body" in g for g in gaps)
