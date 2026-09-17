# #522: an economy flow full-overwrites the Closet token from the local mirror,
# so it must refuse — before anything is built or submitted — while that mirror
# is behind the chain. Here the mirror falls behind the way the listener lets it:
# it handles the Closet modify to version C but cannot read C's metadata, so the
# mirror keeps version B while the history archive records C. Overwriting from
# B would erase C's credit on-chain.
#
# Reuses each flow suite's own fakes (no network), like
# test_economy_char_indeterminate.py.

import asyncio
import dataclasses
import os
import sqlite3
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest

from lfg_core import closet_token as ct
from lfg_core import config, history_events, history_store, nft_listener
from lfg_core import economy_flow as ef
from lfg_core import economy_store as es
from tests import test_economy_flow_assemble as asm
from tests import test_economy_flow_deposit as dep
from tests import test_economy_flow_equip as eqp
from tests import test_economy_flow_extract as ext
from tests import test_economy_flow_harvest as hrv
from tests.closet_archive_helpers import archive_tx, closet_uri, closet_version_tx

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
)

CLOSET_ID = (
    "00100000" + history_events.issuer_account_hex(config.SWAP_ISSUER_ADDRESS) + "3E5B27B704943DBA"
)
VERSION_B = closet_uri("b")
VERSION_C = closet_uri("c")
# The credit version C added and the mirror at B does not have.
NEWER_CREDIT = ("Accessory", "Crown Jewel", 1)

# Every dep that builds or submits something on-chain.
_BUILD_OR_SUBMIT = (
    "blank_meta_fn",
    "char_compose_fn",
    "char_burn_fn",
    "char_mint_fn",
    "char_modify_fn",
    "closet_mint_fn",
    "closet_modify_fn",
    "closet_upload_fn",
    "trait_burn_fn",
    "trait_compose_fn",
    "trait_mint_fn",
    "trait_upload_fn",
)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@dataclass
class _Case:
    conn: Any
    contents: list[tuple[str, str, int]]  # mirror contents at version B
    session: Any
    runner: Callable[..., Any]
    deps: ef.EconomyDeps


def _harvest(tmp_path) -> _Case:
    conn, f = hrv._conn_with_genesis(), hrv._Fakes()
    es.set_closet_token(conn, "rUser", "CLOSET0", "00", status=ct.ACTIVE, offer_id=None)
    session = ef.HarvestSession(owner="rUser", character=hrv._char(mutable=True), burnable=False)
    return _Case(conn, [], session, ef.run_harvest, hrv._deps(conn, f, tmp_path))


def _equip(tmp_path) -> _Case:
    conn, f = eqp._conn_with_bucket(), eqp._Fakes()
    session = ef.EquipSession(owner="rUser", character=eqp._char(), changes=[("Head", "Crown")])
    return _Case(conn, [("Head", "Crown", 1)], session, ef.run_equip, eqp._deps(conn, f, tmp_path))


def _deposit(tmp_path) -> _Case:
    conn, f = sqlite3.connect(":memory:"), dep._F()
    dep._active_closet_with_trait_token(conn)
    f.owner_for["TRAIT9"] = "rUser"
    session = ef.DepositSession(owner="rUser", nft_id="TRAIT9")
    return _Case(conn, [], session, ef.run_deposit, dep._deps(conn, f, tmp_path))


def _assemble(tmp_path) -> _Case:
    conn, f = asm._conn_with_closet(), asm._Fakes()
    contents = [(s, "None", 1) for s in asm.NON_BODY] + [("Body", "Straight Blue", 1)]
    return _Case(conn, contents, asm._session(), ef.run_assemble, asm._deps(conn, f, tmp_path))


def _extract(tmp_path) -> _Case:
    conn, f = sqlite3.connect(":memory:"), ext._F()
    ext._active_closet_with_trait(conn)
    session = ef.ExtractSession(owner="rUser", slot="Hat", value="Cap")
    return _Case(conn, [("Hat", "Cap", 2)], session, ef.run_extract, ext._deps(conn, f, tmp_path))


_FLOWS = {
    "assemble": _assemble,
    "deposit": _deposit,
    "equip": _equip,
    "extract": _extract,
    "harvest": _harvest,
}


def _listener_handles(conn, archive_path: str, tx: dict, contents: list | None) -> None:
    """The listener's two duties for one streamed Closet version: apply it to
    the mirror (contents None = its metadata could not be read), then archive it."""

    async def fetch_token(nft_id):
        return {
            "nft_id": nft_id,
            "owner": "rUser",
            "taxon": config.CLOSET_TAXON,
            "uri_hex": VERSION_B,
            "issuer": config.SWAP_ISSUER_ADDRESS,
        }

    async def fetch_meta(uri_hex):
        return None if contents is None else ct.build_closet_metadata("rUser", contents, [])

    _run(
        nft_listener.apply_economy_tx(
            conn, tx, fetch_token_fn=fetch_token, fetch_meta_fn=fetch_meta, genesis=None
        )
    )
    hconn = history_store.init_history_db(archive_path)
    try:
        archive_tx(hconn, tx)
    finally:
        hconn.close()


def _recording(deps: ef.EconomyDeps, calls: list[str]) -> ef.EconomyDeps:
    for name in _BUILD_OR_SUBMIT:
        fn = getattr(deps, name)
        if fn is None:
            continue

        async def wrapped(*args, _fn=fn, _name=name):
            calls.append(_name)
            return await _fn(*args)

        setattr(deps, name, wrapped)
    return deps


def _contents(conn) -> dict[tuple[str, str], int]:
    return {(s, v): n for o, s, v, n in es.read_closet_assets(conn) if o == "rUser"}


@pytest.mark.parametrize("flow", sorted(_FLOWS))
def test_flow_refuses_full_overwrite_from_a_mirror_behind_chain(flow, tmp_path):
    case = _FLOWS[flow](tmp_path)
    archive = str(tmp_path / "history_testnet.db")
    _listener_handles(
        case.conn, archive, closet_version_tx("modify", CLOSET_ID, VERSION_B, 1000), case.contents
    )
    _listener_handles(
        case.conn, archive, closet_version_tx("modify", CLOSET_ID, VERSION_C, 1005), None
    )
    mirror_before = _contents(case.conn)
    calls: list[str] = []
    deps = _recording(dataclasses.replace(case.deps, history_db_path=archive), calls)

    _run(case.runner(case.session, deps))

    assert case.session.state == ef.FAILED
    assert case.session.error == ct.MIRROR_BEHIND_MESSAGE
    assert calls == []  # nothing composed, uploaded, burned, minted or modified
    assert _contents(case.conn) == mirror_before
    if flow == "equip":  # #316: the character is definitively unchanged
        assert case.session.resolution == "reverted"


@pytest.mark.parametrize("flow", sorted(_FLOWS))
def test_flow_overwrites_normally_when_the_mirror_holds_the_newest_version(flow, tmp_path):
    """Control for the refusal above: the same setup, with version C readable,
    completes and keeps C's credit."""
    case = _FLOWS[flow](tmp_path)
    archive = str(tmp_path / "history_testnet.db")
    _listener_handles(
        case.conn, archive, closet_version_tx("modify", CLOSET_ID, VERSION_B, 1000), case.contents
    )
    _listener_handles(
        case.conn,
        archive,
        closet_version_tx("modify", CLOSET_ID, VERSION_C, 1005),
        [*case.contents, NEWER_CREDIT],
    )
    calls: list[str] = []
    deps = _recording(dataclasses.replace(case.deps, history_db_path=archive), calls)

    _run(case.runner(case.session, deps))

    assert case.session.state == ef.DONE, case.session.error
    assert "closet_modify_fn" in calls
    assert _contents(case.conn)[NEWER_CREDIT[:2]] == 1


def test_real_economy_deps_check_the_economy_networks_archive():
    """Production wiring: the service and CLI flows get the stale-mirror guard."""
    import _economy_deps

    deps = _economy_deps.build_economy_deps(sqlite3.connect(":memory:"))

    assert deps.history_db_path == history_store.history_db_path(config.ECONOMY_NETWORK)
