# #493: an INDETERMINATE character / trait-token submit (xrpl_ops raised
# IndeterminateResultError — e.g. s1.ripple.com `tooBusy` mid-burst) must NOT
# collapse into the generic `failed` journal. Every such step journals a
# DISTINCT non-terminal `<op>_<step>_indeterminate` status carrying the pending
# tx hash, runs NO on-chain compensation, and writes NO supply_changes row for
# the step whose outcome is unknown. In the 2026-09-13 mainnet incident every
# one of these txs had actually landed, so a `failed` journal that nothing ever
# revisits silently lost the user's assets.
#
# Reuses each flow suite's own fakes (no network) and overrides only the step
# under test to raise.

import asyncio
import json
import sqlite3

from lfg_core import closet_token as ct
from lfg_core import economy_flow as ef
from lfg_core import economy_store as es
from lfg_core import xrpl_ops
from tests import test_economy_flow_assemble as asm
from tests import test_economy_flow_deposit as dep
from tests import test_economy_flow_equip as eqp
from tests import test_economy_flow_extract as ext
from tests import test_economy_flow_harvest as hrv

NON_BODY = hrv.NON_BODY


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _indeterminate(tx_hash: str | None) -> xrpl_ops.IndeterminateResultError:
    return xrpl_ops.IndeterminateResultError(
        "NFTokenModify: on-ledger outcome unknown after submit raised (tooBusy)",
        tx_hash=tx_hash,
    )


def _record(tmp_path, op: str, session_id: str) -> dict:
    return json.loads((tmp_path / f"{op}-{session_id}.json").read_text())


# --- IndeterminateResultError carries the signed hash --------------------------


def test_indeterminate_error_constructor_is_backward_compatible():
    e = xrpl_ops.IndeterminateResultError("outcome unknown")
    assert str(e) == "outcome unknown"
    assert e.tx_hash is None
    assert xrpl_ops.IndeterminateResultError("x", tx_hash="H").tx_hash == "H"


def test_submit_and_confirm_indeterminate_carries_signed_hash(monkeypatch):
    monkeypatch.setattr(
        xrpl_ops, "autofill_and_sign", lambda tx, client, wallet, **k: _SignedStub("SIGNEDHASH")
    )

    def submit_boom(tx, client, wallet, **k):
        raise RuntimeError("tooBusy")

    class _Resp:
        result = {"error": "txnNotFound"}

    monkeypatch.setattr(xrpl_ops, "submit_and_wait", submit_boom)
    monkeypatch.setattr(xrpl_ops.JsonRpcClient, "request", lambda self, req: _Resp())
    try:
        _run(xrpl_ops.burn_nft("NFTID", owner="rOwner"))
    except xrpl_ops.IndeterminateResultError as e:
        assert e.tx_hash == "SIGNEDHASH"
    else:  # pragma: no cover - the assertion is the raise
        raise AssertionError("expected IndeterminateResultError")


class _SignedStub:
    def __init__(self, tx_hash: str) -> None:
        self._hash = tx_hash

    def get_hash(self) -> str:
        return self._hash


# --- Harvest -------------------------------------------------------------------


def _harvest_setup(fakes):
    conn = hrv._conn_with_genesis()
    es.set_closet_token(conn, "rUser", "CLOSET0", "00", status=ct.ACTIVE, offer_id=None)
    return conn, fakes


def test_harvest_mutable_modify_indeterminate(tmp_path):
    """#3730: the blank modify raised indeterminate (it had landed). No revert,
    no Closet write, distinct journal with the pending hash + moved_assets."""

    class F(hrv._Fakes):
        async def char_modify(self, nft_id, owner, url):
            self.char_modifies.append((nft_id, owner, url))
            raise _indeterminate("PENDMOD")

    conn, f = _harvest_setup(F())
    s = ef.HarvestSession(owner="rUser", character=hrv._char(mutable=True), burnable=False)
    _run(ef.run_harvest(s, hrv._deps(conn, f, tmp_path)))

    assert s.state == ef.FAILED
    assert "unknown" in (s.error or "") and s.id in (s.error or "")
    assert len(f.char_modifies) == 1  # NO modify-back
    assert f.bucket_modifies == []  # Closet never touched
    assert hrv._all_slot_assets(conn) == {}
    assert es.read_supply_changes(conn) == []
    rec = _record(tmp_path, "harvest", s.id)
    assert rec["status"] == "harvest_modify_indeterminate"
    assert rec["pending_tx_hash"] == "PENDMOD"
    assert len(rec["moved_assets"]) == len(NON_BODY) + 1
    assert rec["modify_hash"] is None


def test_harvest_legacy_burn_indeterminate(tmp_path):
    """#2475: the legacy burn raised indeterminate (it had landed). No remint, no
    supply row for the unknown burn, Closet untouched."""

    class F(hrv._Fakes):
        async def char_burn(self, nft_id, owner):
            self.burns.append((nft_id, owner))
            raise _indeterminate("PENDBURN")

    conn, f = _harvest_setup(F())
    s = ef.HarvestSession(owner="rUser", character=hrv._char(mutable=False), burnable=True)
    _run(ef.run_harvest(s, hrv._deps(conn, f, tmp_path)))

    assert s.state == ef.FAILED
    assert f.mints == []  # never reminted against an unknown burn
    assert es.read_supply_changes(conn) == []
    assert hrv._all_slot_assets(conn) == {}
    rec = _record(tmp_path, "harvest", s.id)
    assert rec["status"] == "harvest_burn_indeterminate"
    assert rec["pending_tx_hash"] == "PENDBURN"
    assert rec["legacy_upgrade"] is True
    assert rec["burn_hash"] is None


def test_harvest_legacy_remint_indeterminate_keeps_burn_row_only(tmp_path):
    """#2783: burn committed, the blank remint raised indeterminate (it had
    landed). The definitive burn's -1 row stays; NO +1 mint row, no offer, no
    Closet credit."""

    class F(hrv._Fakes):
        async def char_mint(self, url):
            self.mints.append(url)
            raise _indeterminate("PENDMINT")

    conn, f = _harvest_setup(F())
    s = ef.HarvestSession(owner="rUser", character=hrv._char(mutable=False), burnable=True)
    _run(ef.run_harvest(s, hrv._deps(conn, f, tmp_path)))

    assert s.state == ef.FAILED
    assert f.burns == [("NFT7", "rUser")]
    assert f.offers == []
    assert [c["kind"] for c in es.read_supply_changes(conn)] == ["burn"]
    assert hrv._all_slot_assets(conn) == {}
    rec = _record(tmp_path, "harvest", s.id)
    assert rec["status"] == "harvest_remint_indeterminate"
    assert rec["pending_tx_hash"] == "PENDMINT"
    assert rec["burn_hash"] == "BURNHASH"
    assert rec["new_nft_id"] is None


def test_harvest_mutable_revert_indeterminate(tmp_path):
    """Closet deposit definitively failed, then the modify-BACK raised
    indeterminate: the character may be blank OR restored — journal distinctly,
    never `failed`/`harvested_pending_closet`."""

    class F(hrv._Fakes):
        async def char_modify(self, nft_id, owner, url):
            self.char_modifies.append((nft_id, owner, url))
            if len(self.char_modifies) == 1:
                return "MODIFYHASH"
            raise _indeterminate("PENDREVERT")

    conn = hrv._conn_with_genesis()
    es.set_closet_token(conn, "rUser", "CLOSET0", "00", status=ct.ACTIVE, offer_id=None)
    f = F(fail_closet_modify=True)
    char = hrv._char(mutable=True, uri_hex=b"ipfs://old".hex())
    s = ef.HarvestSession(owner="rUser", character=char, burnable=False)
    _run(ef.run_harvest(s, hrv._deps(conn, f, tmp_path)))

    assert s.state == ef.FAILED
    assert len(f.char_modifies) == 2
    rec = _record(tmp_path, "harvest", s.id)
    assert rec["status"] == "harvest_revert_indeterminate"
    assert rec["pending_tx_hash"] == "PENDREVERT"
    assert rec["modify_hash"] == "MODIFYHASH"


def test_harvest_closet_sync_indeterminate_records_pending_hash(tmp_path):
    """The Closet-side indeterminate already fails closed; it now also journals
    the pending Closet modify hash when xrpl_ops knew it."""

    class F(hrv._Fakes):
        async def closet_modify(self, nft_id, owner, url):
            try:
                raise _indeterminate("PENDCLOSET")
            except xrpl_ops.IndeterminateResultError as e:  # _economy_deps mapping
                raise ct.ClosetIndeterminateError(str(e)) from e

    conn, f = _harvest_setup(F())
    s = ef.HarvestSession(owner="rUser", character=hrv._char(mutable=True), burnable=False)
    _run(ef.run_harvest(s, hrv._deps(conn, f, tmp_path)))

    rec = _record(tmp_path, "harvest", s.id)
    assert rec["status"] == "harvest_sync_indeterminate"
    assert rec["pending_tx_hash"] == "PENDCLOSET"


# --- Assemble ------------------------------------------------------------------


def test_assemble_modify_indeterminate(tmp_path):
    class F(asm._Fakes):
        async def char_modify(self, nft_id, owner, url):
            self.char_modifies.append((nft_id, owner, url))
            raise _indeterminate("PENDDRESS")

    conn, f = asm._conn_with_closet(), F()
    before = asm._all_slot_assets(conn)
    s = asm._session()
    _run(ef.run_assemble(s, asm._deps(conn, f, tmp_path)))

    assert s.state == ef.FAILED
    assert len(f.char_modifies) == 1  # no modify-back
    assert f.bucket_modifies == 0  # Closet never debited
    assert asm._all_slot_assets(conn) == before
    rec = _record(tmp_path, "assemble", s.id)
    assert rec["status"] == "assemble_modify_indeterminate"
    assert rec["pending_tx_hash"] == "PENDDRESS"


def test_assemble_revert_indeterminate(tmp_path):
    class F(asm._Fakes):
        async def char_modify(self, nft_id, owner, url):
            self.char_modifies.append((nft_id, owner, url))
            if len(self.char_modifies) == 1:
                return "MODIFYHASH"
            raise _indeterminate("PENDREVERT")

    conn, f = asm._conn_with_closet(), F(fail_closet_modify=True)
    s = asm._session(asm._blank_char(uri_hex=b"https://cdn/blank.json".hex()))
    _run(ef.run_assemble(s, asm._deps(conn, f, tmp_path)))

    assert s.state == ef.FAILED
    assert len(f.char_modifies) == 2
    rec = _record(tmp_path, "assemble", s.id)
    assert rec["status"] == "assemble_revert_indeterminate"
    assert rec["pending_tx_hash"] == "PENDREVERT"
    assert rec["modify_hash"] == "MODIFYHASH"


# --- Equip ---------------------------------------------------------------------


def test_equip_modify_indeterminate(tmp_path):
    class F(eqp._Fakes):
        async def char_modify(self, nft_id, owner, url):
            self.char_modifies.append((nft_id, owner, url))
            raise _indeterminate("PENDEQUIP")

    conn, f = eqp._conn_with_bucket(), F()
    s = ef.EquipSession(owner="rUser", character=eqp._char(), changes=[("Head", "Crown")])
    _run(ef.run_equip(s, eqp._deps(conn, f, tmp_path)))

    assert s.state == ef.FAILED
    assert s.resolution == "uncertain"  # client must not redraw / invite re-save
    assert len(f.char_modifies) == 1
    assets = {(slot, v): n for o, slot, v, n in es.read_closet_assets(conn)}
    assert assets == {("Head", "Crown"): 1}
    rec = _record(tmp_path, "equip", s.id)
    assert rec["status"] == "equip_modify_indeterminate"
    assert rec["pending_tx_hash"] == "PENDEQUIP"
    assert rec["resolution"] == "uncertain"


def test_equip_revert_indeterminate(tmp_path):
    class F(eqp._Fakes):
        async def char_modify(self, nft_id, owner, url):
            self.char_modifies.append((nft_id, owner, url))
            if len(self.char_modifies) == 1:
                return "MODH"
            raise _indeterminate("PENDREVERT")

    conn, f = eqp._conn_with_bucket(), F(fail_closet_modify=True)
    s = ef.EquipSession(owner="rUser", character=eqp._char(), changes=[("Head", "Crown")])
    _run(ef.run_equip(s, eqp._deps(conn, f, tmp_path)))

    assert s.state == ef.FAILED
    assert s.resolution == "uncertain"
    rec = _record(tmp_path, "equip", s.id)
    assert rec["status"] == "equip_revert_indeterminate"
    assert rec["pending_tx_hash"] == "PENDREVERT"


# --- Extract -------------------------------------------------------------------


def test_extract_mint_indeterminate(tmp_path):
    class F(ext._F):
        async def trait_mint(self, url):
            raise _indeterminate("PENDTRAITMINT")

    conn, f = sqlite3.connect(":memory:"), F()
    ext._active_closet_with_trait(conn)
    s = ef.ExtractSession(owner="rUser", slot="Hat", value="Cap")
    _run(ef.run_extract(s, ext._deps(conn, f, tmp_path)))

    assert s.state == ef.FAILED
    assert f.burns == []  # no burn-back of a token we can't name
    assert f.modifies == 0  # Closet never decremented
    assert es.read_trait_tokens(conn) == []
    assets = {(sl, v): n for o, sl, v, n in es.read_closet_assets(conn) if o == "rUser"}
    assert assets[("Hat", "Cap")] == 2
    rec = _record(tmp_path, "extract", s.id)
    assert rec["status"] == "extract_mint_indeterminate"
    assert rec["pending_tx_hash"] == "PENDTRAITMINT"


def test_extract_burn_back_indeterminate(tmp_path):
    class F(ext._F):
        async def trait_burn(self, nft_id, owner):
            self.burns.append((nft_id, owner))
            raise _indeterminate("PENDBURNBACK")

    conn, f = sqlite3.connect(":memory:"), F(fail_sync=True)
    ext._active_closet_with_trait(conn)
    s = ef.ExtractSession(owner="rUser", slot="Hat", value="Cap")
    _run(ef.run_extract(s, ext._deps(conn, f, tmp_path)))

    assert s.state == ef.FAILED
    assert f.burns == [("TRAIT0", "")]
    assert s.nft_id == "TRAIT0"  # kept — the token may still exist
    rec = _record(tmp_path, "extract", s.id)
    assert rec["status"] == "extract_revert_indeterminate"
    assert rec["pending_tx_hash"] == "PENDBURNBACK"
    assert rec["nft_id"] == "TRAIT0"


# --- Deposit -------------------------------------------------------------------


def test_deposit_burn_indeterminate(tmp_path):
    """f23aa6de: the trait burn raised indeterminate (it had landed). No Closet
    credit (it could double-credit if the burn didn't land... or be owed if it
    did — reconcile from chain), trait_tokens row left for the listener."""

    class F(dep._F):
        async def trait_burn(self, nft_id, owner):
            self.burns.append((nft_id, owner))
            raise _indeterminate("PENDDEPBURN")

    conn, f = sqlite3.connect(":memory:"), F()
    dep._active_closet_with_trait_token(conn)
    f.owner_for["TRAIT9"] = "rUser"
    s = ef.DepositSession(owner="rUser", nft_id="TRAIT9")
    _run(ef.run_deposit(s, dep._deps(conn, f, tmp_path)))

    assert s.state == ef.FAILED
    assert f.modifies == 0
    assert es.read_trait_tokens(conn) == [("TRAIT9", "rUser", "Hat", "Cap")]
    assets = {(sl, v): n for o, sl, v, n in es.read_closet_assets(conn) if o == "rUser"}
    assert assets.get(("Hat", "Cap"), 0) == 0
    rec = _record(tmp_path, "deposit", s.id)
    assert rec["status"] == "deposit_burn_indeterminate"
    assert rec["pending_tx_hash"] == "PENDDEPBURN"
    assert (rec["slot"], rec["value"]) == ("Hat", "Cap")
    assert rec["burn_hash"] is None
