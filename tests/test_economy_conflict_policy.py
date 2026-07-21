"""Per-kind economy 409 policy (fire-and-forget harvests spec 2026-07-21).

Harvests stack per user (409 only on the same nft_id); every other economy op
keeps per-user exclusivity and is mutually exclusive with in-flight harvests.
"""

import os
from types import SimpleNamespace

os.environ.setdefault("BUNNY_PULL_ZONE", "nft.pullzone.example")
os.environ.setdefault("LAYER_SOURCE", "local")

from lfg_service.app import _economy_conflict  # noqa: E402

TERMINAL = {"done", "failed"}


def _sess(kind, nft_id=None, user="u1", platform="discord", state="running"):
    inner = (
        SimpleNamespace(character=SimpleNamespace(nft_id=nft_id)) if nft_id else SimpleNamespace()
    )
    return SimpleNamespace(discord_id=user, platform=platform, kind=kind, state=state, inner=inner)


def test_no_sessions_allows_everything():
    for kind in ("harvest", "equip", "assemble", "extract", "deposit"):
        assert _economy_conflict({}, TERMINAL, kind, "u1", "discord", {}) is None


def test_harvests_stack_on_different_nfts():
    sessions = {"a": _sess("harvest", nft_id="AAA")}
    assert (
        _economy_conflict(sessions, TERMINAL, "harvest", "u1", "discord", {"nft_id": "BBB"}) is None
    )


def test_same_nft_harvest_409s():
    sessions = {"a": _sess("harvest", nft_id="AAA")}
    err = _economy_conflict(sessions, TERMINAL, "harvest", "u1", "discord", {"nft_id": "AAA"})
    assert err == "that character is already being harvested"


def test_terminal_harvest_does_not_block():
    sessions = {"a": _sess("harvest", nft_id="AAA", state="done")}
    assert (
        _economy_conflict(sessions, TERMINAL, "harvest", "u1", "discord", {"nft_id": "AAA"}) is None
    )


def test_other_users_and_platforms_do_not_block():
    sessions = {
        "a": _sess("harvest", nft_id="AAA", user="u2"),
        "b": _sess("equip", user="u1", platform="telegram"),
    }
    assert (
        _economy_conflict(sessions, TERMINAL, "harvest", "u1", "discord", {"nft_id": "AAA"}) is None
    )
    assert _economy_conflict(sessions, TERMINAL, "equip", "u1", "discord", {}) is None


def test_non_harvest_blocks_non_harvest():
    sessions = {"a": _sess("equip")}
    err = _economy_conflict(sessions, TERMINAL, "assemble", "u1", "discord", {})
    assert err == "an economy action is already in progress"


def test_non_harvest_blocks_harvest():
    sessions = {"a": _sess("equip")}
    err = _economy_conflict(sessions, TERMINAL, "harvest", "u1", "discord", {"nft_id": "AAA"})
    assert err == "an economy action is already in progress"


def test_harvest_blocks_non_harvest():
    sessions = {"a": _sess("harvest", nft_id="AAA")}
    err = _economy_conflict(sessions, TERMINAL, "equip", "u1", "discord", {})
    assert err == "wait for your running harvests to finish first"
