# tests/test_economy_owner_lock.py
# Per-owner Closet read-modify-write serialization (#180). The Closet token is a
# full-overwrite record, so two flows for one owner that interleave read -> sync
# -> mirror lose an update. These tests drive REAL economy_flow ops concurrently
# with an instrumented closet_modify that (a) records the peak number of flows
# simultaneously inside the on-chain modify and (b) forces a scheduler yield, so
# an unserialized interleave WOULD be observed. The different-owners case is the
# control: it proves the instrumentation can see overlap (peak 2), which makes
# the same-owner peak of 1 a real assertion, not a coincidence.
#
# Env-guard preamble: importing lfg_core.config freezes its constants (e.g.
# LAYER_SOURCE) at import time; set the same defaults test_smoke.py uses so
# collection order can't strand them. (Copy the block verbatim from
# tests/test_server_identity_wiring.py — same keys/values.)
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
import sqlite3  # noqa: E402

from lfg_core import closet_token as ct  # noqa: E402
from lfg_core import config  # noqa: E402
from lfg_core import economy_flow as ef  # noqa: E402
from lfg_core import economy_store as es  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _DepositFake:
    """Deposit deps for N owners sharing one connection. `closet_modify` counts
    how many flows are inside the on-chain modify at once (its `await sleep`
    forces a yield, so a racing flow interleaves here if nothing serializes it).
    Each trait token maps to a distinct (slot, value) so a lost update is visible
    as a missing credit."""

    def __init__(self) -> None:
        self.active = 0
        self.peak = 0
        self.burns: list[tuple[str, str]] = []
        self.owner_for: dict[str, str] = {}  # trait nft_id -> on-ledger owner
        self.meta_for: dict[str, tuple[str, str]] = {}  # trait nft_id -> (slot, value)
        self.closet_owner_of: dict[str, str] = {}  # closet nft_id -> owner

    async def trait_info(self, nft_id: str) -> dict:
        return {
            "taxon": config.TRAIT_TAXON,
            "issuer": config.SWAP_ISSUER_ADDRESS,
            "owner": self.owner_for.get(nft_id),
        }

    async def trait_meta(self, nft_id: str) -> dict:
        slot, value = self.meta_for[nft_id]
        return {"lfg_trait": {"slot": slot, "value": value}}

    async def trait_burn(self, nft_id: str, owner: str) -> str:
        self.burns.append((nft_id, owner))
        return "BURN"

    async def closet_upload(self, meta: dict) -> str:
        return "https://cdn/c.json"

    async def closet_modify(self, nft_id: str, owner: str, url: str) -> str:
        self.active += 1
        self.peak = max(self.peak, self.active)
        await asyncio.sleep(0.02)  # yield: an unserialized racer interleaves here
        self.active -= 1
        return "MOD"

    async def closet_offer(self, nft_id: str, owner: str) -> str:
        return "OFFER"

    async def closet_accept(self, offer_id: str) -> dict:
        return {"xumm_url": "x"}

    async def closet_owner(self, nft_id: str) -> str | None:
        return self.closet_owner_of.get(nft_id)


def _deposit_deps(conn, f, tmp):
    return ef.EconomyDeps(
        conn=conn,
        closet_upload_fn=f.closet_upload,
        closet_mint_fn=None,
        closet_offer_fn=f.closet_offer,
        closet_accept_fn=f.closet_accept,
        closet_modify_fn=f.closet_modify,
        char_compose_fn=None,
        char_mint_fn=None,
        char_modify_fn=None,
        char_burn_fn=None,
        char_offer_fn=f.closet_offer,
        char_accept_fn=f.closet_accept,
        closet_owner_fn=f.closet_owner,
        trait_burn_fn=f.trait_burn,
        trait_info_fn=f.trait_info,
        trait_meta_fn=f.trait_meta,
        records_dir=str(tmp),
    )


def _setup_owner(conn, f, owner, closet_nft, trait_nft, slot, value):
    es.set_closet_token(conn, owner, closet_nft, "AB", status=ct.ACTIVE, offer_id=None)
    es.set_closet_contents(conn, owner, [], [])
    es.upsert_trait_token(conn, trait_nft, owner, slot, value)
    f.owner_for[trait_nft] = owner
    f.meta_for[trait_nft] = (slot, value)
    f.closet_owner_of[closet_nft] = owner


def test_same_owner_deposits_serialize_no_lost_credit(tmp_path):
    """Two concurrent deposits into ONE owner's Closet must serialize: never two
    flows inside the modify at once, and BOTH credits survive (no full-overwrite
    lost update)."""
    conn = sqlite3.connect(":memory:")
    es.init_economy_schema(conn)
    f = _DepositFake()
    # Same owner, same Closet, two different trait tokens -> two distinct credits.
    es.set_closet_token(conn, "rUser", "CLOSET1", "AB", status=ct.ACTIVE, offer_id=None)
    es.set_closet_contents(conn, "rUser", [], [])
    f.closet_owner_of["CLOSET1"] = "rUser"
    for trait_nft, (slot, value) in (("TRAITA", ("Hat", "Cap")), ("TRAITB", ("Eyes", "Blue"))):
        es.upsert_trait_token(conn, trait_nft, "rUser", slot, value)
        f.owner_for[trait_nft] = "rUser"
        f.meta_for[trait_nft] = (slot, value)

    async def both():
        s1 = ef.DepositSession(owner="rUser", nft_id="TRAITA")
        s2 = ef.DepositSession(owner="rUser", nft_id="TRAITB")
        deps = _deposit_deps(conn, f, tmp_path)
        await asyncio.gather(ef.run_deposit(s1, deps), ef.run_deposit(s2, deps))
        return s1, s2

    s1, s2 = _run(both())

    assert s1.state == ef.DONE and s2.state == ef.DONE
    assert f.peak == 1, f"flows overlapped inside the Closet modify (peak={f.peak})"
    # Both credits landed — neither overwrote the other.
    assets = {(sl, v): n for o, sl, v, n in es.read_closet_assets(conn) if o == "rUser"}
    assert assets.get(("Hat", "Cap")) == 1
    assert assets.get(("Eyes", "Blue")) == 1


def test_different_owners_run_concurrently(tmp_path):
    """Control case: two owners have independent locks, so their deposits DO
    overlap inside the modify (peak 2). This proves the instrumentation can see
    an interleave — making the same-owner peak of 1 a meaningful assertion."""
    conn = sqlite3.connect(":memory:")
    es.init_economy_schema(conn)
    f = _DepositFake()
    _setup_owner(conn, f, "rAlice", "CLOSET_A", "TRAITA", "Hat", "Cap")
    _setup_owner(conn, f, "rBob", "CLOSET_B", "TRAITB", "Eyes", "Blue")

    async def both():
        s1 = ef.DepositSession(owner="rAlice", nft_id="TRAITA")
        s2 = ef.DepositSession(owner="rBob", nft_id="TRAITB")
        deps = _deposit_deps(conn, f, tmp_path)
        await asyncio.gather(ef.run_deposit(s1, deps), ef.run_deposit(s2, deps))
        return s1, s2

    s1, s2 = _run(both())

    assert s1.state == ef.DONE and s2.state == ef.DONE
    assert f.peak == 2, f"different owners should not serialize (peak={f.peak})"


class _EnsureFake:
    """ensure_closet deps whose `mint` counts calls and yields, so a concurrent
    double-tap that isn't serialized would mint twice."""

    def __init__(self) -> None:
        self.minted = 0

    async def upload(self, meta: dict) -> str:
        return "https://cdn/c.json"

    async def mint(self, url: str) -> str:
        self.minted += 1
        await asyncio.sleep(0.02)  # yield: a racing ensure_closet mints here too
        return f"CLOSET{self.minted}"

    async def offer(self, nft_id: str, owner: str) -> str:
        return "OFFER"

    async def accept(self, offer_id: str) -> dict:
        return {"xumm_url": "x"}


def test_ensure_closet_concurrent_double_tap_mints_once(tmp_path):
    """Two concurrent ensure_closet for one owner (double-tap / register race)
    must mint exactly one Closet: the loser sees the winner's recorded token."""
    conn = sqlite3.connect(":memory:")
    es.init_economy_schema(conn)
    f = _EnsureFake()

    async def both():
        return await asyncio.gather(
            ct.ensure_closet(
                conn,
                "rUser",
                upload_fn=f.upload,
                mint_fn=f.mint,
                offer_fn=f.offer,
                accept_payload_fn=f.accept,
            ),
            ct.ensure_closet(
                conn,
                "rUser",
                upload_fn=f.upload,
                mint_fn=f.mint,
                offer_fn=f.offer,
                accept_payload_fn=f.accept,
            ),
        )

    ref1, ref2 = _run(both())

    assert f.minted == 1, f"double-tap minted {f.minted} Closets"
    # Exactly one Closet is recorded, and both callers see the same token id.
    assert es.get_closet_record(conn, "rUser") is not None
    assert ref1.nft_id == ref2.nft_id == "CLOSET1"
    # Exactly one caller reports having minted it.
    assert [ref1.minted, ref2.minted].count(True) == 1
