"""Per-kind economy 409 policy (fire-and-forget harvests spec 2026-07-21).

Harvests stack per user (409 only on the same nft_id); every other economy op
keeps per-user exclusivity and is mutually exclusive with in-flight harvests.
"""

import os
from types import SimpleNamespace

os.environ.setdefault("BUNNY_PULL_ZONE", "nft.pullzone.example")
os.environ.setdefault("LAYER_SOURCE", "local")

from lfg_service.app import (  # noqa: E402
    _economy_conflict,
    _reserve_economy_slot,
    economy_sessions,
)

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


# --- _reserve_economy_slot: the check+reserve seam that closes the TOCTOU
# window (Greptile P1, PR #307). This is fully synchronous, so no `await`
# ever separates the conflict check from the reservation write — two
# concurrent requests processed on the same event loop can never both pass.


def _clear_economy_sessions():
    economy_sessions.clear()


def test_reserve_then_conflict_then_release_then_reserve_again():
    _clear_economy_sessions()
    try:
        # First reservation for a harvest on nft AAA succeeds.
        placeholder_id, conflict = _reserve_economy_slot(
            "harvest", "u1", "discord", {"nft_id": "AAA"}
        )
        assert conflict is None
        assert placeholder_id in economy_sessions

        # A second concurrent reservation for the SAME nft_id, while the
        # placeholder is still live, is refused -- this is the exact race
        # that used to slip through the old check-then-await-then-insert
        # ordering.
        placeholder_id2, conflict2 = _reserve_economy_slot(
            "harvest", "u1", "discord", {"nft_id": "AAA"}
        )
        assert conflict2 == "that character is already being harvested"
        assert placeholder_id2 == ""
        # The failed reservation must not have touched the sessions table.
        assert len(economy_sessions) == 1

        # Releasing the first placeholder (as the handler does on
        # start_coro failure, or after swapping in the real session) frees
        # the slot for a new reservation.
        economy_sessions.pop(placeholder_id, None)
        placeholder_id3, conflict3 = _reserve_economy_slot(
            "harvest", "u1", "discord", {"nft_id": "AAA"}
        )
        assert conflict3 is None
        assert placeholder_id3 in economy_sessions
    finally:
        _clear_economy_sessions()


def test_reserve_different_nfts_both_succeed():
    _clear_economy_sessions()
    try:
        id_a, conflict_a = _reserve_economy_slot("harvest", "u1", "discord", {"nft_id": "AAA"})
        id_b, conflict_b = _reserve_economy_slot("harvest", "u1", "discord", {"nft_id": "BBB"})
        assert conflict_a is None and conflict_b is None
        assert {id_a, id_b} <= set(economy_sessions)
    finally:
        _clear_economy_sessions()


def test_reserve_non_harvest_blocks_second_non_harvest():
    _clear_economy_sessions()
    try:
        placeholder_id, conflict = _reserve_economy_slot("equip", "u1", "discord", {})
        assert conflict is None
        _, conflict2 = _reserve_economy_slot("assemble", "u1", "discord", {})
        assert conflict2 == "an economy action is already in progress"
    finally:
        _clear_economy_sessions()
