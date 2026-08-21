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
