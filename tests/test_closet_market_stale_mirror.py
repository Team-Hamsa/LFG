# #530: the Closet Market settlement's mirror step (`app._closet_market_mirror`,
# run by `closet_market_flow._mirror` once a fill is PAID) full-overwrites each
# side's Closet token from `closet_assets`. Like the economy flows since #529, it
# must refuse before anything is uploaded or submitted while that owner's mirror
# may lack a version that is already on-chain, or the overwrite erases it (#522):
#
#   * behind  — the ledger history archive holds a newer version of the Closet
#               than the mirror (`closet_token.ensure_mirror_current`);
#   * pending — a committed Closet modify's mirror write failed (the #184
#               `mirror_pending` flag). While the owner has an unmirrored fill
#               the listener skips the contents rebuild (#443) but still advances
#               the mirror's URI and ledger stamp, so the archive check alone
#               reads those stale contents as current.
#
# A refusal is a wait, not a failure: the side stays unmirrored, no attempt is
# counted, and the settlement sweep retries it until the mirror catches up.

import dataclasses
import logging
from dataclasses import dataclass, field
from typing import Any

import pytest

from lfg_core import closet_market_flow as cmf
from lfg_core import closet_market_store as cms
from lfg_core import closet_token as ct
from lfg_core import economy_flow as ef
from lfg_core import economy_store as es
from lfg_service import app as server
from tests import test_closet_market_api as api_tests
from tests.closet_archive_helpers import closet_archive, closet_uri
from tests.closet_market_helpers import (
    BUYER,
    SELLER,
    Fakes,
    conn,
    deps,
    fill,
    make_db,
    open_bid,
    run,
)
from tests.test_closet_market_ops import audit

closet_env = api_tests.closet_env  # re-export the fixture (a direct import trips ruff F811)

SELLER_CLOSET, BUYER_CLOSET = f"C-{SELLER}", f"C-{BUYER}"
SELLER_A = closet_uri("seller-a")
BUYER_B, BUYER_C = closet_uri("buyer-b"), closet_uri("buyer-c")
# The buyer's Closet as the archive knows it: current at B, or already at C.
CURRENT = [("modify", BUYER_B, 1000)]
BEHIND = [("modify", BUYER_B, 1000), ("modify", BUYER_C, 1005)]
# The credit the newer on-chain version holds and the stale mirror lacks.
NEWER_CREDIT = ("Accessory", "Crown Jewel", 1)


@dataclass
class _Chain:
    """The settlement's Closet CDN upload and NFTokenModify, recorded."""

    uploads: list[dict[str, Any]] = field(default_factory=list)
    modifies: list[tuple[str, str, str]] = field(default_factory=list)
    ledger: int = 2000

    async def upload(self, meta):
        self.uploads.append(meta)
        return f"https://cdn/closets/settled-{len(self.uploads)}.json"

    async def modify(self, nft_id, owner, url):
        self.modifies.append((nft_id, owner, url))
        self.ledger += 1
        return ct.ModifyReceipt(tx_hash=f"MOD{len(self.modifies)}", ledger_index=self.ledger)

    def touched(self) -> list[str]:
        """Owners whose Closet metadata was uploaded or whose token was modified."""
        uploaded = {m["name"].split()[-1] for m in self.uploads}
        return sorted(uploaded | {owner for _, owner, _ in self.modifies})

    def assets_uploaded_for(self, owner):
        return [m["lfg_closet"]["assets"] for m in self.uploads if m["name"].endswith(owner)]


def _settlement_deps(c, chain, archive, records_dir):
    """What `economy_api.build_settlement_deps` hands `_closet_market_mirror`:
    recorded Closet ops, and the history archive the stale-mirror check reads."""
    return ef.EconomyDeps(
        conn=c,
        closet_upload_fn=chain.upload,
        closet_mint_fn=None,
        closet_offer_fn=None,
        closet_accept_fn=None,
        closet_modify_fn=chain.modify,
        char_compose_fn=None,
        char_mint_fn=None,
        char_modify_fn=None,
        char_burn_fn=None,
        char_offer_fn=None,
        char_accept_fn=None,
        records_dir=records_dir,
        history_db_path=archive,
    )


def _setup(tmp_path, monkeypatch, buyer_versions):
    """A seller holding the traded unit and a buyer, each with an active Closet
    whose mirror is dated to the version the archive also holds."""
    path = str(tmp_path / "onchain.db")
    make_db(path)
    c = conn(path)
    es.set_closet_token(
        c, SELLER, SELLER_CLOSET, SELLER_A, status=ct.ACTIVE, applied_ledger_index=900
    )
    es.set_closet_token(
        c, BUYER, BUYER_CLOSET, BUYER_B, status=ct.ACTIVE, applied_ledger_index=1000
    )
    c.close()
    closet_archive(tmp_path, SELLER_CLOSET, [("modify", SELLER_A, 900)])
    archive = closet_archive(tmp_path, BUYER_CLOSET, buyer_versions)
    chain = _Chain()
    monkeypatch.setattr(
        server.economy_api,
        "build_settlement_deps",
        lambda c: _settlement_deps(c, chain, archive, str(tmp_path / "records")),
    )
    return path, chain, archive


def _stale_buyer(tmp_path, monkeypatch, kind):
    """The buyer's mirror in one of the two states a full overwrite must refuse;
    returns (path, chain, the refusal's ClosetError code)."""
    if kind == "behind":
        path, chain, _ = _setup(tmp_path, monkeypatch, BEHIND)
        return path, chain, ct.CLOSET_MIRROR_BEHIND
    path, chain, _ = _setup(tmp_path, monkeypatch, CURRENT)
    c = conn(path)
    es.set_mirror_pending(c, BUYER, True)
    c.close()
    return path, chain, ct.CLOSET_MIRROR_PENDING


def _holder_fill(path, f):
    bid = open_bid(path, f, price="10")
    c = conn(path)
    got = cms.fill_bid(c, bid["id"], SELLER, fee_bps=0, platform=None)
    c.close()
    return got


def _real_mirror_deps(path, f, tmp_path):
    """The flow deps (fakes for the escrow and payments) with the REAL mirror step."""
    return dataclasses.replace(deps(path, f, tmp_path), mirror_fn=server._closet_market_mirror)


def _waiting_warnings(caplog, fill_id, code):
    return [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and fill_id in r.getMessage() and code in r.getMessage()
    ]


# --- app._closet_market_mirror ------------------------------------------------


def test_mirror_refuses_an_owner_whose_mirror_is_behind_the_chain(tmp_path, monkeypatch):
    path, chain, _ = _setup(tmp_path, monkeypatch, BEHIND)
    c = conn(path)

    with pytest.raises(ct.ClosetError) as refused:
        run(server._closet_market_mirror(c, BUYER))

    assert chain.uploads == [] and chain.modifies == []
    assert type(refused.value) is ct.ClosetError  # plain: nothing was committed
    assert refused.value.code == ct.CLOSET_MIRROR_BEHIND
    assert es.get_closet_record(c, BUYER)[1] == BUYER_B  # the mirror is untouched
    c.close()


def test_mirror_refuses_an_owner_with_a_pending_mirror_write(tmp_path, monkeypatch):
    """The archive check alone passes here: the mirror is dated to the newest
    archived version. That is exactly the state the listener leaves when a flow's
    committed modify lost its mirror write while the owner had an unmirrored fill."""
    path, chain, archive = _setup(tmp_path, monkeypatch, CURRENT)
    c = conn(path)
    es.set_mirror_pending(c, BUYER, True)
    ct.ensure_mirror_current(c, BUYER, history_db_path=archive)  # does not refuse

    with pytest.raises(ct.ClosetError) as refused:
        run(server._closet_market_mirror(c, BUYER))

    assert chain.uploads == [] and chain.modifies == []
    assert type(refused.value) is ct.ClosetError
    assert refused.value.code == ct.CLOSET_MIRROR_PENDING
    c.close()


def test_mirror_overwrites_a_current_mirror(tmp_path, monkeypatch):
    path, chain, _ = _setup(tmp_path, monkeypatch, CURRENT)
    c = conn(path)
    es.set_closet_contents(c, BUYER, [("Head", "Crown", 1)], [])

    run(server._closet_market_mirror(c, BUYER))

    assert [(nft_id, owner) for nft_id, owner, _ in chain.modifies] == [(BUYER_CLOSET, BUYER)]
    assert chain.assets_uploaded_for(BUYER) == [[{"slot": "Head", "value": "Crown", "count": 1}]]
    assert es.get_closet_applied_ledger(c, BUYER) == chain.ledger  # the new version is dated
    c.close()


# --- a fill through settle_fill (and the sweep) ----------------------------------


@pytest.mark.parametrize("kind", ["behind", "pending"])
def test_settlement_waits_on_a_stale_mirror_then_completes_once_it_catches_up(
    kind, tmp_path, monkeypatch, caplog
):
    path, chain, code = _stale_buyer(tmp_path, monkeypatch, kind)
    f = Fakes()
    fl = _holder_fill(path, f)
    d = _real_mirror_deps(path, f, tmp_path)
    caplog.set_level(logging.WARNING)

    assert run(cmf.settle_fill(fl["id"], d)) == cms.PAID

    assert chain.touched() == [SELLER]  # nothing uploaded or modified for the buyer
    got = fill(path, fl["id"])
    assert (got["seller_mirrored"], got["buyer_mirrored"]) == (1, 0)
    assert got["attempts"] == 0
    assert got["error"] == cmf.MIRROR_WAIT_ERROR
    assert _waiting_warnings(caplog, fl["id"], code)
    c = conn(path)
    assert fl["id"] in cms.fills_needing_attention(c)  # the sweep retries it
    c.close()
    # The trade itself is complete for both parties: the buyer's Closet (the DB)
    # holds the unit and the seller was paid. Only the on-chain record waits.
    view = server._closet_fill_view(got, BUYER)
    assert (view["state"], view["fill_state"], view["error"]) == (
        "done",
        cms.PAID,
        cmf.MIRROR_WAIT_ERROR,
    )

    # The next pass is refused again: still nothing submitted, no attempt counted.
    assert run(cmf.settle_fill(fl["id"], d)) == cms.PAID
    assert chain.touched() == [SELLER]
    assert fill(path, fl["id"])["attempts"] == 0

    # The mirror catches up. The listener and the backfill skip an owner with an
    # unmirrored fill, so this is the operator's reconcile: the buyer's contents
    # from the newest on-chain version, the bought unit included, dated to it.
    c = conn(path)
    es.set_closet_contents(c, BUYER, [("Head", "Crown", 1), NEWER_CREDIT], [])
    if kind == "behind":
        es.set_closet_token(
            c, BUYER, BUYER_CLOSET, BUYER_C, status=ct.ACTIVE, applied_ledger_index=1005
        )
    assert not es.get_mirror_pending(c, BUYER)
    c.close()

    assert run(cmf.settle_fill(fl["id"], d)) == cms.MIRRORED

    got = fill(path, fl["id"])
    assert (got["buyer_mirrored"], got["attempts"], got["error"]) == (1, 0, None)
    assert chain.assets_uploaded_for(BUYER) == [
        [
            {"slot": "Accessory", "value": "Crown Jewel", "count": 1},
            {"slot": "Head", "value": "Crown", "count": 1},
        ]
    ]  # the newer version's credit AND the bought unit: nothing erased


def test_settlement_overwrites_current_mirrors_and_completes(tmp_path, monkeypatch):
    """Control: during a fill the DB is ahead of both tokens by the moved unit
    (#443). That is not a stale mirror — both sides are overwritten and marked
    mirrored, and the seller's token no longer lists the sold unit."""
    path, chain, _ = _setup(tmp_path, monkeypatch, CURRENT)
    f = Fakes()
    fl = _holder_fill(path, f)

    assert run(cmf.settle_fill(fl["id"], _real_mirror_deps(path, f, tmp_path))) == cms.MIRRORED

    assert chain.touched() == [BUYER, SELLER]
    assert chain.assets_uploaded_for(SELLER) == [[]]
    assert chain.assets_uploaded_for(BUYER) == [[{"slot": "Head", "value": "Crown", "count": 1}]]
    got = fill(path, fl["id"])
    assert (got["seller_mirrored"], got["buyer_mirrored"], got["attempts"], got["error"]) == (
        1,
        1,
        0,
        None,
    )


def test_the_sweep_retries_a_waiting_fill_until_the_mirror_catches_up(
    closet_env, monkeypatch, tmp_path
):
    """Production wiring: the sweep settles through `_closet_market_deps`, whose
    mirror step is `_closet_market_mirror`."""
    seller, buyer = api_tests.ME, api_tests.BIDDER
    buyer_closet = f"C-{buyer}"
    c = api_tests._db(closet_env)
    es.set_closet_token(
        c, buyer, buyer_closet, BUYER_B, status=ct.ACTIVE, applied_ledger_index=1000
    )
    bid = api_tests._open_bid(closet_env, owner=buyer)
    fl = cms.fill_bid(c, bid["id"], seller, fee_bps=0, platform=None)
    cms.update_fill(c, fl["id"], state=cms.FUNDED, escrow_finish_hash="FIN")
    assert cms.move_asset(c, fl["id"]) == cms.ASSET_MOVED
    cms.update_fill(c, fl["id"], state=cms.PAID, forward_tx_hash="FWD")
    c.close()
    archive = closet_archive(tmp_path, buyer_closet, BEHIND)
    chain = _Chain()
    monkeypatch.setattr(
        server.economy_api,
        "build_settlement_deps",
        lambda c: _settlement_deps(c, chain, archive, str(tmp_path / "records")),
    )

    run(server.sweep_closet_market())

    c = api_tests._db(closet_env)
    got = cms.get_fill(c, fl["id"])
    assert (got["state"], got["seller_mirrored"], got["buyer_mirrored"]) == (cms.PAID, 1, 0)
    assert (got["attempts"], got["error"]) == (0, cmf.MIRROR_WAIT_ERROR)
    assert chain.touched() == [seller]
    es.set_closet_contents(c, buyer, [("Head", "Crown", 1), NEWER_CREDIT], [])
    es.set_closet_token(
        c, buyer, buyer_closet, BUYER_C, status=ct.ACTIVE, applied_ledger_index=1005
    )
    c.close()

    run(server.sweep_closet_market())

    c = api_tests._db(closet_env)
    assert cms.get_fill(c, fl["id"])["state"] == cms.MIRRORED
    c.close()
    assert chain.touched() == sorted([seller, buyer])


# --- closet_market_flow._mirror: a wait is not a failed attempt -------------------


@pytest.mark.parametrize("code", [ct.CLOSET_MIRROR_BEHIND, ct.CLOSET_MIRROR_PENDING])
def test_a_stale_mirror_refusal_is_a_wait_not_a_failed_attempt(code, tmp_path, caplog):
    path = str(tmp_path / "onchain.db")
    make_db(path)
    f = Fakes(mirror_error=ct.ClosetError("refused", code=code))
    fl = _holder_fill(path, f)
    d = deps(path, f, tmp_path)
    caplog.set_level(logging.WARNING)

    assert run(cmf.settle_fill(fl["id"], d)) == cms.PAID

    got = fill(path, fl["id"])
    assert (got["seller_mirrored"], got["buyer_mirrored"]) == (0, 0)
    assert (got["attempts"], got["error"]) == (0, cmf.MIRROR_WAIT_ERROR)
    assert _waiting_warnings(caplog, fl["id"], code)

    f.mirror_error = None
    assert run(cmf.settle_fill(fl["id"], d)) == cms.MIRRORED
    got = fill(path, fl["id"])
    assert (got["attempts"], got["error"]) == (0, None)  # the wait is over


def test_any_other_mirror_error_still_counts_an_attempt(tmp_path):
    """Unchanged: a ClosetError without a stale-mirror code (here the modify
    definitively failed) is a real failure, which the audit's attempt count is for."""
    path = str(tmp_path / "onchain.db")
    make_db(path)
    f = Fakes(mirror_error=ct.ClosetError("failed to modify Closet NFToken URI"))
    fl = _holder_fill(path, f)

    assert run(cmf.settle_fill(fl["id"], deps(path, f, tmp_path))) == cms.PAID

    got = fill(path, fl["id"])
    assert got["attempts"] == 1
    assert got["error"] == f"Closet update for {BUYER} failed; will retry"


def test_a_waiting_fill_surfaces_in_the_nightly_audit_as_stuck(tmp_path):
    """The residual stall: a fill whose owner's mirror stays behind remains
    PAID-but-unmirrored. Once the wait is recorded, retries write nothing, so the
    audit's stuck check fires after an hour, while its failing-payout check
    (attempts) stays quiet."""
    path = str(tmp_path / "onchain.db")
    make_db(path)
    f = Fakes(mirror_error=ct.ClosetError("refused", code=ct.CLOSET_MIRROR_BEHIND))
    fl = _holder_fill(path, f)
    d = deps(path, f, tmp_path)
    run(cmf.settle_fill(fl["id"], d))
    c = conn(path)
    c.execute("UPDATE closet_fills SET updated_ts = updated_ts - 7200 WHERE id = ?", (fl["id"],))
    c.commit()
    waiting_since = cms.get_fill(c, fl["id"])["updated_ts"]
    c.close()

    for _ in range(3):  # three sweep passes
        assert run(cmf.settle_fill(fl["id"], d)) == cms.PAID

    c = conn(path)
    assert cms.get_fill(c, fl["id"])["updated_ts"] == waiting_since
    problems = audit.audit_rows(c, now=waiting_since + 3601)
    c.close()
    assert f"fill {fl['id']}: stuck in paid for 3601s" in problems
    assert not any("payout failing" in p for p in problems)
