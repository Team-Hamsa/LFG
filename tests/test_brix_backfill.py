"""#412 gap reimbursement: strict historical reconstruction via epoch_state.

Env pins come from the repo-root conftest.py (#323) — no per-file preamble.
"""

from __future__ import annotations

import pytest

from lfg_core import brix_backfill, brix_drip, history_store

ALICE, BOB, SYS = "rAlice", "rBob", "rSystem"
D = {
    "2026-01-01": 1767225600,
    "2026-01-02": 1767312000,
    "2026-01-03": 1767398400,
    "2026-01-04": 1767484800,
    "2026-01-05": 1767571200,
}
FAR = 4102444800  # 2100-01-01


@pytest.fixture()
def h(tmp_path):
    conn = history_store.init_history_db(str(tmp_path / "h.db"))
    brix_drip.ensure_schema(conn)
    conn.execute(
        "INSERT INTO archive_state (network, genesis_hash, baseline_complete, validated_close_time,"
        " updated_at) VALUES ('testnet', ?, 1, ?, 1)",
        ("G" * 64, FAR),
    )
    conn.commit()
    yield conn
    conn.close()


_n = {"i": 0}


def ev(
    conn,
    event,
    *,
    ts,
    nft_id="N1",
    from_addr=None,
    to_addr=None,
    offer_index=None,
    offer_flags=None,
):
    _n["i"] += 1
    history_store.insert_nft_event(
        conn,
        {
            "tx_hash": f"T{_n['i']}",
            "nft_id": nft_id,
            "event": event,
            "from_addr": from_addr,
            "to_addr": to_addr,
            "ts": ts,
            "ledger_index": _n["i"],
            "offer_index": offer_index,
            "offer_flags": offer_flags,
        },
    )
    conn.commit()


def plan(
    h,
    *,
    start="2026-01-01",
    end="2026-01-04",
    apply=False,
    system=frozenset(),
    eligible=None,
    today=None,
):
    if eligible is None:
        # Every nft_id the fixtures write is a collection character unless a
        # test says otherwise (#411 C1). Owners are taken from the replay so
        # the C2 drift check is a no-op by default.
        from lfg_core import epoch_state

        eligible = {k: v.owner for k, v in epoch_state.state_at_epoch(h, end).items()}
    return brix_backfill.plan_gap_backfill(
        h,
        "testnet",
        system,
        start=start,
        end=end,
        apply=apply,
        eligible=eligible,
        today=today,
    )


def test_epoch_range_inclusive_and_validated():
    assert brix_backfill.epoch_range("2026-01-01", "2026-01-03") == [
        "2026-01-01",
        "2026-01-02",
        "2026-01-03",
    ]
    with pytest.raises(ValueError):
        brix_backfill.epoch_range("2026-01-03", "2026-01-01")


def test_nft_earns_only_from_its_mint_epoch(h):
    ev(h, "mint", ts=D["2026-01-03"] + 1, to_addr=ALICE)
    p = plan(h)
    assert p.total_brix == 2 and p.wallets == {ALICE: 2}  # 01-03, 01-04
    assert [(line.epoch, line.brix) for line in p.epochs] == [
        ("2026-01-01", 0),
        ("2026-01-02", 0),
        ("2026-01-03", 1),
        ("2026-01-04", 1),
    ]


def test_transfer_splits_credit_at_epoch_boundary(h):
    ev(h, "mint", ts=D["2026-01-01"] + 1, to_addr=ALICE)
    ev(h, "transfer", ts=D["2026-01-03"] + 1, from_addr=ALICE, to_addr=BOB)
    p = plan(h)
    assert p.wallets == {ALICE: 2, BOB: 2}


def test_listed_epochs_earn_nothing(h):
    ev(h, "mint", ts=D["2026-01-01"] + 1, to_addr=ALICE)
    ev(h, "offer_create", ts=D["2026-01-02"] + 1, from_addr=ALICE, offer_index="O1", offer_flags=1)
    ev(h, "offer_cancel", ts=D["2026-01-04"] + 1, from_addr=ALICE, offer_index="O1", offer_flags=1)
    p = plan(h)
    assert p.wallets == {ALICE: 2}  # 01-01 and 01-04
    assert [line.listed for line in p.epochs] == [0, 1, 1, 0]


def test_burned_token_stops_earning(h):
    ev(h, "mint", ts=D["2026-01-01"] + 1, to_addr=ALICE)
    ev(h, "burn", ts=D["2026-01-03"] + 1, from_addr=ALICE)
    assert plan(h).wallets == {ALICE: 2}


def test_system_wallets_earn_nothing(h):
    ev(h, "mint", ts=D["2026-01-01"] + 1, to_addr=SYS)
    assert plan(h, system=frozenset({SYS})).total_brix == 0


def test_exploit_regression_current_holder_gets_nothing_for_pre_purchase_epochs(h):
    """A floor NFT bought AFTER the window must not hand its buyer the window."""
    ev(h, "mint", ts=D["2026-01-01"] + 1, to_addr=ALICE)
    ev(h, "sale", ts=D["2026-01-05"] + 1, from_addr=ALICE, to_addr=BOB)  # after end=01-04
    p = plan(h)
    assert p.wallets == {ALICE: 4} and BOB not in p.wallets


def test_apply_writes_rows_claimable_through_existing_flow_and_is_idempotent(h):
    ev(h, "mint", ts=D["2026-01-01"] + 1, to_addr=ALICE)
    first = plan(h, apply=True)
    assert first.written == 4 and brix_drip.claimable(h, ALICE) == 4
    second = plan(h, apply=True)
    assert second.written == 0 and brix_drip.claimable(h, ALICE) == 4
    assert h.execute("SELECT COUNT(*) FROM brix_accruals").fetchone()[0] == 4
    # the dry run after a full apply reports nothing left owed
    assert plan(h).total_brix == 0


def test_apply_never_touches_the_nightly_cursor(h):
    ev(h, "mint", ts=D["2026-01-01"] + 1, to_addr=ALICE)
    brix_drip.set_meta(h, brix_drip.LAST_ACCRUED_EPOCH, "2025-12-31")
    plan(h, apply=True)
    assert brix_drip.get_meta(h, brix_drip.LAST_ACCRUED_EPOCH) == "2025-12-31"
    h.execute("DELETE FROM brix_meta")
    h.commit()
    plan(h, apply=True)
    assert brix_drip.get_meta(h, brix_drip.LAST_ACCRUED_EPOCH) is None


def test_uncertified_epoch_is_reported_and_skipped_not_fatal(h):
    ev(h, "mint", ts=D["2026-01-01"] + 1, to_addr=ALICE)
    h.execute(
        "UPDATE archive_state SET validated_close_time = ?", (D["2026-01-04"],)
    )  # saw through 01-03 only
    h.commit()
    p = plan(h, apply=True)
    assert [e for e, _ in p.deferred] == ["2026-01-04"]
    assert p.written == 3 and brix_drip.claimable(h, ALICE) == 3


def test_backfilled_accrual_binds_under_one_open_claim_index(h):
    ev(h, "mint", ts=D["2026-01-01"] + 1, to_addr=ALICE)
    plan(h, apply=True)
    claim_id, amount = brix_drip.open_claim(h, ALICE, last_ledger_seq=10_000)
    assert amount == 4
    assert (
        h.execute(
            "SELECT COUNT(*) FROM brix_accruals WHERE owner=? AND claim_id IS NOT NULL", (ALICE,)
        ).fetchone()[0]
        == 4
    )


# --- #411 fix wave: eligibility scope, owner drift, unknown refusal ---------


def test_backfill_ignores_tokens_outside_the_collection_index(h):
    """C1: a Closet/trait token lives in nft_events but never in onchain_nfts,
    so it must earn nothing here either."""
    ev(h, "mint", ts=D["2026-01-01"] + 1, nft_id="CHAR", to_addr=ALICE)
    ev(h, "mint", ts=D["2026-01-01"] + 1, nft_id="CLOSET", to_addr=ALICE)
    p = plan(h, eligible={"CHAR": ALICE})
    assert p.wallets == {ALICE: 4} and p.nfts == 1
    assert all(line.ineligible == 1 for line in p.epochs)


def test_dry_run_reports_owner_drift_without_refusing(h):
    ev(h, "mint", ts=D["2026-01-01"] + 1, to_addr=ALICE)
    p = plan(h, eligible={"N1": BOB})
    assert p.owner_drift == ["N1"] and p.refused is None
    assert p.written == 0


def test_apply_refuses_and_writes_nothing_on_owner_drift(h):
    ev(h, "mint", ts=D["2026-01-01"] + 1, to_addr=ALICE)
    p = plan(h, apply=True, eligible={"N1": BOB})
    assert p.refused is not None and "derive_history_events" in p.refused
    assert p.written == 0
    assert h.execute("SELECT COUNT(*) FROM brix_accruals").fetchone()[0] == 0


def test_apply_refuses_and_writes_nothing_on_unknown_listing_state(h):
    """I1 for the backfill: legacy offer_create rows with no offer_flags make
    listing state unreconstructable — reimbursing from that is a guess."""
    ev(h, "mint", ts=D["2026-01-01"] + 1, to_addr=ALICE)
    ev(h, "offer_create", ts=D["2026-01-02"] + 1, from_addr=ALICE)  # no flags
    p = plan(h, apply=True)
    assert p.refused is not None and "unknown listing state" in p.refused
    assert p.written == 0
    assert h.execute("SELECT COUNT(*) FROM brix_accruals").fetchone()[0] == 0


def test_apply_writes_when_clean(h):
    ev(h, "mint", ts=D["2026-01-01"] + 1, to_addr=ALICE)
    p = plan(h, apply=True)
    assert p.refused is None and p.owner_drift == []
    assert p.written == 4
    assert brix_drip.claimable(h, ALICE) == 4


def test_refused_apply_leaves_payable_earlier_epochs_unwritten(h):
    """The refusal must be decided BEFORE any write, even though the earlier
    epochs of the window were perfectly payable."""
    ev(h, "mint", ts=D["2026-01-01"] + 1, to_addr=ALICE)
    ev(h, "offer_create", ts=D["2026-01-04"] + 1, from_addr=ALICE)  # no flags → unknown
    before = h.execute("SELECT COUNT(*) FROM brix_accruals").fetchone()[0]
    p = plan(h, apply=True)
    assert p.refused is not None and p.written == 0
    assert h.execute("SELECT COUNT(*) FROM brix_accruals").fetchone()[0] == before


def test_apply_is_idempotent_across_runs(h):
    ev(h, "mint", ts=D["2026-01-01"] + 1, to_addr=ALICE)
    first = plan(h, apply=True)
    second = plan(h, apply=True)
    assert (first.written, second.written) == (4, 0)
    assert brix_drip.claimable(h, ALICE) == 4


def test_a_transfer_after_the_window_is_not_owner_drift(h):
    """Greptile P1: drift is compared at the archive's CURRENT state, so a
    legitimate post-`end` transfer that the index already reflects is not
    drift — and the window's close-time holder is still credited."""
    ev(h, "mint", ts=D["2026-01-01"] + 1, to_addr=ALICE)
    ev(h, "transfer", ts=D["2026-01-05"] + 1, from_addr=ALICE, to_addr=BOB)
    p = plan(h, apply=True, eligible={"N1": BOB}, today="2026-01-06")
    assert p.owner_drift == [] and p.refused is None
    assert p.written == 4 and brix_drip.claimable(h, ALICE) == 4
    assert brix_drip.claimable(h, BOB) == 0


def test_an_index_owner_with_no_archived_transfer_still_drifts(h):
    """The stale-derived-table case: nothing in nft_events explains the index
    owner even at today, so --apply is still refused."""
    ev(h, "mint", ts=D["2026-01-01"] + 1, to_addr=ALICE)
    p = plan(h, apply=True, eligible={"N1": BOB}, today="2026-01-06")
    assert p.owner_drift == ["N1"] and p.refused is not None
    assert p.written == 0


def test_a_burned_token_with_no_archived_transfer_still_drifts_and_refuses(h):
    """Greptile #411 P1: the `tok.live` filter used to exclude burned tokens
    from drift entirely, so a missing archived transfer followed by a
    recorded burn slipped an --apply through even though the window still
    pays the stale pre-transfer owner. Mint to Alice, no transfer archived,
    burn from Bob after the window — the index (which keeps `owner` on
    burned rows) says Bob, the archive never explains it, so this must
    refuse exactly like the still-live case above."""
    ev(h, "mint", ts=D["2026-01-01"] + 1, to_addr=ALICE)
    ev(h, "burn", ts=D["2026-01-05"] + 1, from_addr=BOB)
    p = plan(h, apply=True, eligible={"N1": BOB}, today="2026-01-06")
    assert p.owner_drift == ["N1"] and p.refused is not None
    assert p.written == 0
    assert h.execute("SELECT COUNT(*) FROM brix_accruals").fetchone()[0] == 0


def test_a_burned_token_with_a_complete_archive_is_not_drift(h):
    """Sanity check: when the transfer IS archived before the burn, the
    replay's owner matches the index and there is no false-positive drift
    just because the token happens to be burned by `today`."""
    ev(h, "mint", ts=D["2026-01-01"] + 1, to_addr=ALICE)
    ev(h, "transfer", ts=D["2026-01-02"] + 1, from_addr=ALICE, to_addr=BOB)
    ev(h, "burn", ts=D["2026-01-05"] + 1, from_addr=BOB)
    p = plan(h, apply=True, eligible={"N1": BOB}, today="2026-01-06")
    assert p.owner_drift == [] and p.refused is None
    assert p.written == 4
