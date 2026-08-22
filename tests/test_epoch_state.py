"""Epoch-state replay from the history archive (#411 option 2).

Env pins come from the repo-root conftest.py (#323) — no per-file preamble.
"""

from __future__ import annotations

import pytest

from lfg_core import epoch_state, history_store

ISSUER, ALICE, BOB, BROKER = "rIssuer", "rAlice", "rBob", "rBroker"
NFT = "N1"
D = {  # unix ts for 00:00Z of a few consecutive UTC days
    "2026-01-01": 1767225600,
    "2026-01-02": 1767312000,
    "2026-01-03": 1767398400,
    "2026-01-04": 1767484800,
}


@pytest.fixture()
def hconn(tmp_path):
    c = history_store.init_history_db(str(tmp_path / "h.db"))
    yield c
    c.close()


_seq = {"n": 0}


def ev(
    conn,
    event,
    *,
    ts,
    nft_id=NFT,
    from_addr=None,
    to_addr=None,
    offer_index=None,
    offer_flags=None,
    ledger_index=None,
):
    _seq["n"] += 1
    history_store.insert_nft_event(
        conn,
        {
            "tx_hash": f"T{_seq['n']}",
            "nft_id": nft_id,
            "event": event,
            "from_addr": from_addr,
            "to_addr": to_addr,
            "ts": ts,
            "ledger_index": ledger_index if ledger_index is not None else _seq["n"],
            "offer_index": offer_index,
            "offer_flags": offer_flags,
        },
    )
    conn.commit()


def test_epoch_close_ts_is_next_midnight_exclusive():
    assert epoch_state.epoch_close_ts("2026-01-01") == D["2026-01-02"]


def test_mint_then_hold_is_live_unlisted_owned_by_minter(hconn):
    ev(hconn, "mint", ts=D["2026-01-01"] + 10, to_addr=ALICE)
    tok = epoch_state.state_at_epoch(hconn, "2026-01-01")[NFT]
    assert (tok.owner, tok.listed, tok.live) == (ALICE, False, True)


def test_event_after_epoch_close_is_invisible(hconn):
    ev(hconn, "mint", ts=D["2026-01-02"], to_addr=ALICE)  # exactly at the bound → next epoch
    assert NFT not in epoch_state.state_at_epoch(hconn, "2026-01-01")
    assert NFT in epoch_state.state_at_epoch(hconn, "2026-01-02")


def test_transfer_credit_follows_holder_at_close(hconn):
    ev(hconn, "mint", ts=D["2026-01-01"] + 1, to_addr=ALICE)
    ev(hconn, "transfer", ts=D["2026-01-02"] + 5, from_addr=ALICE, to_addr=BOB)
    assert epoch_state.state_at_epoch(hconn, "2026-01-01")[NFT].owner == ALICE
    assert epoch_state.state_at_epoch(hconn, "2026-01-02")[NFT].owner == BOB


def test_sale_moves_ownership(hconn):
    ev(hconn, "mint", ts=D["2026-01-01"] + 1, to_addr=ALICE)
    ev(hconn, "sale", ts=D["2026-01-01"] + 2, from_addr=ALICE, to_addr=BOB)
    assert epoch_state.state_at_epoch(hconn, "2026-01-01")[NFT].owner == BOB


def test_burn_ends_life(hconn):
    ev(hconn, "mint", ts=D["2026-01-01"] + 1, to_addr=ALICE)
    ev(hconn, "burn", ts=D["2026-01-02"] + 1, from_addr=ALICE)
    assert epoch_state.state_at_epoch(hconn, "2026-01-01")[NFT].live is True
    tok = epoch_state.state_at_epoch(hconn, "2026-01-02")[NFT]
    assert tok.live is False and tok.is_burned is True


def test_sell_offer_open_at_close_is_listed(hconn):
    ev(hconn, "mint", ts=D["2026-01-01"] + 1, to_addr=ALICE)
    ev(
        hconn,
        "offer_create",
        ts=D["2026-01-01"] + 2,
        from_addr=ALICE,
        offer_index="O1",
        offer_flags=1,
    )
    assert epoch_state.state_at_epoch(hconn, "2026-01-01")[NFT].listed is True


def test_offer_opened_and_cancelled_inside_epoch_is_unlisted(hconn):
    ev(hconn, "mint", ts=D["2026-01-01"] + 1, to_addr=ALICE)
    ev(
        hconn,
        "offer_create",
        ts=D["2026-01-01"] + 2,
        from_addr=ALICE,
        offer_index="O1",
        offer_flags=1,
    )
    ev(
        hconn,
        "offer_cancel",
        ts=D["2026-01-01"] + 3,
        from_addr=ALICE,
        offer_index="O1",
        offer_flags=1,
    )
    assert epoch_state.state_at_epoch(hconn, "2026-01-01")[NFT].listed is False


def test_offer_left_by_previous_owner_does_not_count(hconn):
    ev(hconn, "mint", ts=D["2026-01-01"] + 1, to_addr=ALICE)
    ev(
        hconn,
        "offer_create",
        ts=D["2026-01-01"] + 2,
        from_addr=ALICE,
        offer_index="O1",
        offer_flags=1,
    )
    # Bob acquires via an accepted BUY offer (sell offer O1 survives on-ledger, unfillable)
    ev(hconn, "sale", ts=D["2026-01-01"] + 3, from_addr=ALICE, to_addr=BOB, offer_index=None)
    tok = epoch_state.state_at_epoch(hconn, "2026-01-01")[NFT]
    assert tok.owner == BOB and tok.listed is False


def test_sale_through_sell_offer_closes_it(hconn):
    ev(hconn, "mint", ts=D["2026-01-01"] + 1, to_addr=ALICE)
    ev(
        hconn,
        "offer_create",
        ts=D["2026-01-01"] + 2,
        from_addr=ALICE,
        offer_index="O1",
        offer_flags=1,
    )
    ev(hconn, "sale", ts=D["2026-01-01"] + 3, from_addr=ALICE, to_addr=BOB, offer_index="O1")
    ev(hconn, "transfer", ts=D["2026-01-01"] + 4, from_addr=BOB, to_addr=ALICE)  # back to Alice
    # O1 was consumed — Alice holding again must NOT look listed
    assert epoch_state.state_at_epoch(hconn, "2026-01-01")[NFT].listed is False


def test_destination_locked_sell_offer_counts(hconn):
    ev(hconn, "mint", ts=D["2026-01-01"] + 1, to_addr=ALICE)
    ev(
        hconn,
        "offer_create",
        ts=D["2026-01-01"] + 2,
        from_addr=ALICE,
        to_addr=BROKER,
        offer_index="O1",
        offer_flags=1,
    )
    assert epoch_state.state_at_epoch(hconn, "2026-01-01")[NFT].listed is True


def test_buy_offer_never_counts(hconn):
    ev(hconn, "mint", ts=D["2026-01-01"] + 1, to_addr=ALICE)
    ev(
        hconn,
        "offer_create",
        ts=D["2026-01-01"] + 2,
        from_addr=BOB,
        offer_index="B1",
        offer_flags=0,
    )
    assert epoch_state.state_at_epoch(hconn, "2026-01-01")[NFT].listed is False


def test_legacy_offer_create_without_index_is_unknown(hconn):
    """A pre-#411 row can't be matched to its cancel → fail closed (None)."""
    ev(hconn, "mint", ts=D["2026-01-01"] + 1, to_addr=ALICE)
    ev(
        hconn,
        "offer_create",
        ts=D["2026-01-01"] + 2,
        from_addr=ALICE,
        offer_index=None,
        offer_flags=None,
    )
    assert epoch_state.state_at_epoch(hconn, "2026-01-01")[NFT].listed is None


def test_cancel_without_index_while_offers_open_is_unknown(hconn):
    ev(hconn, "mint", ts=D["2026-01-01"] + 1, to_addr=ALICE)
    ev(
        hconn,
        "offer_create",
        ts=D["2026-01-01"] + 2,
        from_addr=ALICE,
        offer_index="O1",
        offer_flags=1,
    )
    ev(
        hconn,
        "offer_cancel",
        ts=D["2026-01-01"] + 3,
        from_addr=ALICE,
        offer_index=None,
        offer_flags=1,
    )
    assert epoch_state.state_at_epoch(hconn, "2026-01-01")[NFT].listed is None


def test_replay_is_incremental_and_matches_fresh_state(hconn):
    ev(hconn, "mint", ts=D["2026-01-01"] + 1, to_addr=ALICE)
    ev(
        hconn,
        "offer_create",
        ts=D["2026-01-02"] + 1,
        from_addr=ALICE,
        offer_index="O1",
        offer_flags=1,
    )
    ev(hconn, "transfer", ts=D["2026-01-03"] + 1, from_addr=ALICE, to_addr=BOB)
    r = epoch_state.EpochReplay(hconn)
    for d in ("2026-01-01", "2026-01-02", "2026-01-03"):
        assert r.advance_to(d) == epoch_state.state_at_epoch(hconn, d)
    with pytest.raises(ValueError):
        r.advance_to("2026-01-02")  # cannot go backwards


def test_replay_orders_by_ledger_then_ts_not_insertion(hconn):
    # inserted out of order: cancel first, then create, with ledger_index saying create came first
    ev(hconn, "mint", ts=D["2026-01-01"] + 1, to_addr=ALICE, ledger_index=1)
    ev(
        hconn,
        "offer_cancel",
        ts=D["2026-01-01"] + 3,
        from_addr=ALICE,
        offer_index="O1",
        offer_flags=1,
        ledger_index=3,
    )
    ev(
        hconn,
        "offer_create",
        ts=D["2026-01-01"] + 2,
        from_addr=ALICE,
        offer_index="O1",
        offer_flags=1,
        ledger_index=2,
    )
    assert epoch_state.state_at_epoch(hconn, "2026-01-01")[NFT].listed is False


def _archive_row(conn, *, complete=1, validated_close=None, gap_after=None, gap_reason=None):
    conn.execute(
        "INSERT INTO archive_state (network, genesis_hash, baseline_complete, validated_close_time,"
        " continuity_gap_after, continuity_gap_reason, updated_at) VALUES (?,?,?,?,?,?,?)",
        ("testnet", "G" * 64, complete, validated_close, gap_after, gap_reason, 1),
    )
    conn.commit()


def test_certify_no_archive_row_defers(hconn):
    assert epoch_state.certify_epoch(hconn, "testnet", "2026-01-01") is not None


def test_certify_payable_when_complete_and_validated_past_close(hconn):
    _archive_row(hconn, validated_close=D["2026-01-02"])
    assert epoch_state.certify_epoch(hconn, "testnet", "2026-01-01") is None


def test_certify_defers_when_archive_has_not_seen_epoch_close(hconn):
    _archive_row(hconn, validated_close=D["2026-01-02"] - 1)
    reason = epoch_state.certify_epoch(hconn, "testnet", "2026-01-01")
    assert reason and "not yet closed" in reason


def test_certify_defers_on_incomplete_baseline(hconn):
    _archive_row(hconn, complete=0, validated_close=D["2026-01-04"])
    assert "baseline" in (epoch_state.certify_epoch(hconn, "testnet", "2026-01-01") or "")


def test_certify_defers_on_continuity_gap(hconn):
    _archive_row(
        hconn, validated_close=D["2026-01-04"], gap_after=123, gap_reason="listener restart"
    )
    reason = epoch_state.certify_epoch(hconn, "testnet", "2026-01-01")
    assert reason and "listener restart" in reason


def test_certify_is_per_network(hconn):
    _archive_row(hconn, validated_close=D["2026-01-04"])
    assert epoch_state.certify_epoch(hconn, "mainnet", "2026-01-01") is not None


def test_bare_continuity_gap_at_defers_as_unbounded(tmp_path):
    """T3: a gap recorded with only `continuity_gap_at` (no reason) must still
    defer, and say so — an unbounded gap is the least trustworthy kind."""
    from lfg_core import epoch_state, history_store

    conn = history_store.init_history_db(str(tmp_path / "h.db"))
    conn.execute(
        "INSERT INTO archive_state (network, genesis_hash, baseline_complete,"
        " validated_close_time, continuity_gap_at, updated_at)"
        " VALUES ('testnet', ?, 1, ?, 12345, 1)",
        ("G" * 64, 4102444800),
    )
    conn.commit()
    reason = epoch_state.certify_epoch(conn, "testnet", "2026-01-01")
    assert reason is not None and "unbounded" in reason


def test_multi_offer_cancel_closes_every_joined_index(hconn):
    ev(hconn, "mint", ts=D["2026-01-01"] + 1, to_addr=ALICE)
    ev(
        hconn,
        "offer_create",
        ts=D["2026-01-01"] + 2,
        from_addr=ALICE,
        offer_index="O1",
        offer_flags=1,
    )
    ev(
        hconn,
        "offer_create",
        ts=D["2026-01-01"] + 3,
        from_addr=ALICE,
        offer_index="O2",
        offer_flags=1,
    )
    ev(
        hconn,
        "offer_cancel",
        ts=D["2026-01-01"] + 4,
        from_addr=ALICE,
        offer_index="O1,O2",
        offer_flags=1,
    )
    tok = epoch_state.state_at_epoch(hconn, "2026-01-01")[NFT]
    assert tok.listed is False


def test_modify_establishes_owner_and_life(hconn):
    ev(hconn, "modify", ts=D["2026-01-01"] + 1, to_addr=ALICE)
    tok = epoch_state.state_at_epoch(hconn, "2026-01-01")[NFT]
    assert (tok.owner, tok.live, tok.listed) == (ALICE, True, False)


def test_sell_offer_establishes_owner_when_unknown(hconn):
    ev(
        hconn,
        "offer_create",
        ts=D["2026-01-01"] + 1,
        from_addr=ALICE,
        offer_index="O1",
        offer_flags=1,
    )
    tok = epoch_state.state_at_epoch(hconn, "2026-01-01")[NFT]
    assert (tok.owner, tok.live, tok.listed) == (ALICE, True, True)


def test_buy_offer_never_establishes_owner(hconn):
    ev(
        hconn,
        "offer_create",
        ts=D["2026-01-01"] + 1,
        from_addr=BOB,
        offer_index="O1",
        offer_flags=0,
    )
    tok = epoch_state.state_at_epoch(hconn, "2026-01-01")[NFT]
    assert tok.owner is None and tok.listed is False


def test_sell_offer_does_not_override_known_owner(hconn):
    ev(hconn, "mint", ts=D["2026-01-01"] + 1, to_addr=ALICE)
    ev(hconn, "transfer", ts=D["2026-01-01"] + 2, from_addr=ALICE, to_addr=BOB)
    ev(
        hconn,
        "offer_create",
        ts=D["2026-01-01"] + 3,
        from_addr=ALICE,  # stale offer from the previous holder
        offer_index="O9",
        offer_flags=1,
    )
    tok = epoch_state.state_at_epoch(hconn, "2026-01-01")[NFT]
    assert tok.owner == BOB and tok.listed is False
