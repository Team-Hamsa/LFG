import asyncio
import json
import sqlite3

from lfg_core import closet_token as ct
from lfg_core import config
from lfg_core import economy_flow as ef
from lfg_core import economy_store as es
from tests.economy_helpers import flaky_mirror_conn


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _F:
    """Fake EconomyDeps callables for deposit tests.

    owner_for: maps nft_id -> on-ledger owner address.
    info_none: when True, trait_info returns None (simulates lookup failure).
    issuer_override: when set, trait_info returns this issuer instead of the
        real SWAP_ISSUER_ADDRESS (simulates wrong-issuer rejection).
    fail_sync: when True, closet_modify returns None -> _sync_then_persist raises.
    """

    def __init__(
        self,
        *,
        fail_sync: bool = False,
        raise_sync: bool = False,
        info_none: bool = False,
        issuer_override: str | None = None,
    ) -> None:
        self.burns: list[tuple[str, str]] = []
        self.modifies = 0
        self.fail_sync = fail_sync
        self.raise_sync = raise_sync
        self.info_none = info_none
        self.issuer_override = issuer_override
        self.owner_for: dict[str, str] = {}

    async def trait_info(self, nft_id: str) -> dict | None:
        if self.info_none:
            return None
        issuer = (
            self.issuer_override if self.issuer_override is not None else config.SWAP_ISSUER_ADDRESS
        )
        return {
            "taxon": config.TRAIT_TAXON,
            "issuer": issuer,
            "owner": self.owner_for.get(nft_id),
        }

    async def trait_meta(self, nft_id: str) -> dict:
        return {"lfg_trait": {"slot": "Hat", "value": "Cap"}}

    async def trait_burn(self, nft_id: str, owner: str) -> str | None:
        self.burns.append((nft_id, owner))
        return "BURN"

    async def closet_upload(self, meta: dict) -> str:
        return "https://cdn/c.json"

    async def closet_modify(self, nft_id: str, owner: str, url: str) -> str | None:
        if self.raise_sync:
            raise RuntimeError("timeout after submit")
        if self.fail_sync:
            return None
        self.modifies += 1
        return "MOD"

    async def closet_offer(self, nft_id: str, owner: str) -> str:
        return "OFFER"

    async def closet_accept(self, offer_id: str) -> dict:
        return {"xumm_url": "x"}

    async def closet_owner(self, nft_id: str) -> str:
        return "rUser"

    async def trait_mint(self, url: str) -> str:
        return "TRAIT0"


def _deps(conn, f, tmp):
    return ef.EconomyDeps(
        conn=conn,
        closet_upload_fn=f.closet_upload,
        closet_mint_fn=f.trait_mint,
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
        trait_compose_fn=None,
        trait_upload_fn=None,
        trait_mint_fn=f.trait_mint,
        trait_burn_fn=f.trait_burn,
        trait_info_fn=f.trait_info,
        trait_meta_fn=f.trait_meta,
        records_dir=str(tmp),
    )


def _active_closet_setup(conn, owner="rUser"):
    """Init schema and give owner an active Closet (empty contents)."""
    es.init_economy_schema(conn)
    es.set_closet_token(conn, owner, "CLOSET", "AB", status=ct.ACTIVE, offer_id=None)
    es.set_closet_contents(conn, owner, [], [])


def _active_closet_with_trait_token(conn, nft_id="TRAIT9", owner="rUser"):
    """Active closet + a pre-existing trait_tokens row for TRAIT9 (Hat/Cap)."""
    _active_closet_setup(conn, owner)
    es.upsert_trait_token(conn, nft_id, owner, "Hat", "Cap")


def test_deposit_happy_path(tmp_path):
    conn = sqlite3.connect(":memory:")
    _active_closet_with_trait_token(conn)
    f = _F()
    f.owner_for["TRAIT9"] = "rUser"
    session = ef.DepositSession(owner="rUser", nft_id="TRAIT9")
    _run(ef.run_deposit(session, _deps(conn, f, tmp_path)))

    assert session.state == ef.DONE
    assert f.burns == [("TRAIT9", "rUser")]  # trait was burned
    # Closet credited with Hat/Cap
    assets = {(sl, v): n for o, sl, v, n in es.read_closet_assets(conn) if o == "rUser"}
    assert assets[("Hat", "Cap")] == 1
    # trait_tokens row removed
    assert es.read_trait_tokens(conn) == []


def test_deposit_rejected_without_active_closet(tmp_path):
    conn = sqlite3.connect(":memory:")
    es.init_economy_schema(conn)  # no closet token
    f = _F()
    f.owner_for["TRAIT9"] = "rUser"
    session = ef.DepositSession(owner="rUser", nft_id="TRAIT9")
    _run(ef.run_deposit(session, _deps(conn, f, tmp_path)))

    assert session.state == ef.FAILED
    assert f.burns == []


def test_deposit_rejects_foreign_token(tmp_path):
    """trait_info returns wrong taxon -> FAILED, no burn."""
    conn = sqlite3.connect(":memory:")
    _active_closet_setup(conn)
    f = _F()
    f.owner_for["TRAIT9"] = "rUser"

    # Override trait_info to return a different taxon
    async def bad_trait_info(nft_id):
        return {"taxon": 9999, "issuer": config.SWAP_ISSUER_ADDRESS, "owner": "rUser"}

    deps = _deps(conn, f, tmp_path)
    deps = ef.EconomyDeps(
        conn=conn,
        closet_upload_fn=f.closet_upload,
        closet_mint_fn=f.trait_mint,
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
        trait_info_fn=bad_trait_info,
        trait_meta_fn=f.trait_meta,
        records_dir=str(tmp_path),
    )
    session = ef.DepositSession(owner="rUser", nft_id="TRAIT9")
    _run(ef.run_deposit(session, deps))

    assert session.state == ef.FAILED
    assert f.burns == []


def test_deposit_fail_closed_when_owner_mismatch(tmp_path):
    """trait_info owner != depositor -> FAILED, no burn."""
    conn = sqlite3.connect(":memory:")
    _active_closet_setup(conn)
    f = _F()
    f.owner_for["TRAIT9"] = "rSomeoneElse"  # different owner

    session = ef.DepositSession(owner="rUser", nft_id="TRAIT9")
    _run(ef.run_deposit(session, _deps(conn, f, tmp_path)))

    assert session.state == ef.FAILED
    assert f.burns == []
    assert "own" in (session.error or "").lower()


def test_deposit_burn_then_credit_fails_journals(tmp_path):
    """Burn succeeds but Closet credit fails: FAILED, journal written."""
    conn = sqlite3.connect(":memory:")
    _active_closet_with_trait_token(conn)
    f = _F(fail_sync=True)
    f.owner_for["TRAIT9"] = "rUser"

    session = ef.DepositSession(owner="rUser", nft_id="TRAIT9")
    _run(ef.run_deposit(session, _deps(conn, f, tmp_path)))

    assert session.state == ef.FAILED
    assert f.burns == [("TRAIT9", "rUser")]  # burn DID happen
    # Closet not credited in DB
    assets = {(sl, v): n for o, sl, v, n in es.read_closet_assets(conn) if o == "rUser"}
    assert ("Hat", "Cap") not in assets

    # Journal written with deposited_pending_closet status + slot/value for recovery
    record_path = tmp_path / f"deposit-{session.id}.json"
    assert record_path.exists()
    record = json.loads(record_path.read_text())
    assert record["status"] == "deposited_pending_closet"
    assert record["slot"] == "Hat"
    assert record["value"] == "Cap"


# --- #107: phase-aware deposit branches ---


def test_deposit_mirror_failure_completes_pending_mirror(tmp_path):
    """Burn OK, Closet credit committed on-chain, only the DB mirror fails:
    the session ends DONE with complete_pending_mirror — NOT
    deposited_pending_closet, whose operator recipe (re-credit) would
    double-credit an already-credited Closet."""
    conn = sqlite3.connect(":memory:")
    _active_closet_with_trait_token(conn)
    f = _F()
    f.owner_for["TRAIT9"] = "rUser"
    session = ef.DepositSession(owner="rUser", nft_id="TRAIT9")
    _run(ef.run_deposit(session, _deps(flaky_mirror_conn(conn), f, tmp_path)))

    assert session.state == ef.DONE
    assert f.burns == [("TRAIT9", "rUser")]
    record = json.loads((tmp_path / f"deposit-{session.id}.json").read_text())
    assert record["status"] == "complete_pending_mirror"
    assert record["sync_tx_hash"] == "MOD"
    assert record["mirror_pending"] is True


def test_deposit_indeterminate_journals_and_fails(tmp_path):
    """closet_modify raises (credit outcome unknown): fail-closed — FAILED,
    journal deposit_sync_indeterminate with slot/value preserved so an admin
    can reconcile from chain before any re-credit."""
    conn = sqlite3.connect(":memory:")
    _active_closet_with_trait_token(conn)
    f = _F(raise_sync=True)
    f.owner_for["TRAIT9"] = "rUser"
    session = ef.DepositSession(owner="rUser", nft_id="TRAIT9")
    _run(ef.run_deposit(session, _deps(conn, f, tmp_path)))

    assert session.state == ef.FAILED
    assert f.burns == [("TRAIT9", "rUser")]  # the irreversible burn had happened
    record = json.loads((tmp_path / f"deposit-{session.id}.json").read_text())
    assert record["status"] == "deposit_sync_indeterminate"
    assert record["slot"] == "Hat"
    assert record["value"] == "Cap"
    # closet NOT credited locally
    assets = {(sl, v): n for o, sl, v, n in es.read_closet_assets(conn) if o == "rUser"}
    assert ("Hat", "Cap") not in assets


# ---------------------------------------------------------------------------
# Additional fail-closed tests (Task 4 fix additions)
# ---------------------------------------------------------------------------


def test_deposit_fail_closed_when_info_is_none(tmp_path):
    """trait_info returns None (lookup failure) -> FAILED, no burn (fail-closed)."""
    conn = sqlite3.connect(":memory:")
    _active_closet_setup(conn)
    # Seed a trait_tokens row so we can confirm it stays untouched
    es.upsert_trait_token(conn, "TRAIT9", "rUser", "Hat", "Cap")

    f = _F(info_none=True)  # trait_info will return None
    f.owner_for["TRAIT9"] = "rUser"

    session = ef.DepositSession(owner="rUser", nft_id="TRAIT9")
    _run(ef.run_deposit(session, _deps(conn, f, tmp_path)))

    assert session.state == ef.FAILED
    assert f.burns == []  # NO burn on indeterminate lookup — fail-closed
    # trait_tokens row unchanged
    assert ("TRAIT9", "rUser", "Hat", "Cap") in es.read_trait_tokens(conn)
    # closet not credited
    assets = {(sl, v): n for o, sl, v, n in es.read_closet_assets(conn) if o == "rUser"}
    assert ("Hat", "Cap") not in assets


def test_deposit_rejects_wrong_issuer(tmp_path):
    """trait_info taxon OK but issuer wrong -> FAILED, no burn (fail-closed)."""
    conn = sqlite3.connect(":memory:")
    _active_closet_setup(conn)

    f = _F(issuer_override="rNotOurIssuer")  # correct taxon, wrong issuer
    f.owner_for["TRAIT9"] = "rUser"

    session = ef.DepositSession(owner="rUser", nft_id="TRAIT9")
    _run(ef.run_deposit(session, _deps(conn, f, tmp_path)))

    assert session.state == ef.FAILED
    assert f.burns == []  # the taxon-OK-but-issuer-wrong branch refuses
