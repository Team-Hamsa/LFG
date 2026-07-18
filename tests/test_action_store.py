import sqlite3

import pytest

from lfg_core import action_store


def _conn(tmp_path):
    return sqlite3.connect(tmp_path / "actions.db")


def test_session_round_trip_decodes_json_fields(tmp_path):
    conn = _conn(tmp_path)
    action_store.create_session(
        conn,
        session_id="s1",
        account="rBuyer",
        user_id="u1",
        platform="web",
        network="testnet",
        state="preparing",
        created_ts=10,
        campaign="x-mint-link",
    )
    action_store.update_session(
        conn,
        "s1",
        now_ts=11,
        state="awaiting_signature",
        ticket_sequence=7,
        payment_json={"pay_with": "XRP", "amount": "10000000"},
        inner_hashes_json=["PAY", "MINT", "ACCEPT"],
    )
    row = action_store.get_session(conn, "s1")
    assert row["state"] == "awaiting_signature"
    assert row["ticket_sequence"] == 7
    assert row["campaign"] == "x-mint-link"
    assert row["payment_json"] == {"pay_with": "XRP", "amount": "10000000"}
    assert row["inner_hashes_json"] == ["PAY", "MINT", "ACCEPT"]


def test_update_missing_session_fails_loudly(tmp_path):
    conn = _conn(tmp_path)
    with pytest.raises(KeyError):
        action_store.update_session(conn, "missing", now_ts=11, state="failed")


def test_ticket_lease_is_unique_across_connections(tmp_path):
    path = tmp_path / "actions.db"
    c1, c2 = sqlite3.connect(path), sqlite3.connect(path)
    assert action_store.lease_ticket(c1, "testnet", "rIssuer", [7], "s1", 10) == 7
    assert action_store.lease_ticket(c2, "testnet", "rIssuer", [7], "s2", 10) is None


def test_ticket_lease_chooses_lowest_available_sequence(tmp_path):
    conn = _conn(tmp_path)
    assert (
        action_store.lease_ticket(conn, "testnet", "rIssuer", [9, 3, 7], "s1", 10)
        == 3
    )


def test_quarantined_ticket_cannot_be_released_normally(tmp_path):
    conn = _conn(tmp_path)
    action_store.lease_ticket(conn, "testnet", "rIssuer", [7], "s1", 10)
    action_store.mark_ticket(
        conn, "testnet", "rIssuer", 7, state="quarantined"
    )
    assert action_store.release_ticket(conn, "testnet", "rIssuer", 7) is False


def test_leased_ticket_sequences_lists_every_live_lease(tmp_path):
    conn = _conn(tmp_path)
    action_store.lease_ticket(conn, "testnet", "rIssuer", [7], "s1", 10)
    action_store.lease_ticket(conn, "testnet", "rIssuer", [8], "s2", 10)
    assert action_store.leased_ticket_sequences(
        conn, "testnet", "rIssuer"
    ) == {7, 8}


@pytest.mark.parametrize(
    "state",
    [
        "preparing",
        "awaiting_signature",
        "confirming",
        "rejected",
        "expired",
        "failed",
        "indeterminate",
    ],
)
def test_restart_query_includes_every_non_done_ticket_state(tmp_path, state):
    conn = _conn(tmp_path)
    action_store.create_session(
        conn,
        session_id=state,
        account="rBuyer",
        user_id="u1",
        platform="web",
        network="testnet",
        state=state,
        created_ts=10,
    )
    action_store.update_session(
        conn, state, now_ts=11, ticket_sequence=7
    )
    assert [row["session_id"] for row in action_store.list_reconcilable_sessions(conn)] == [
        state
    ]


def test_restart_query_excludes_done_and_ticketless_rows(tmp_path):
    conn = _conn(tmp_path)
    action_store.create_session(
        conn,
        session_id="done",
        account="rBuyer",
        user_id="u1",
        platform="web",
        network="testnet",
        state="done",
        created_ts=10,
    )
    action_store.update_session(conn, "done", now_ts=11, ticket_sequence=7)
    action_store.create_session(
        conn,
        session_id="preparing",
        account="rBuyer",
        user_id="u1",
        platform="web",
        network="testnet",
        state="preparing",
        created_ts=12,
    )
    assert action_store.list_reconcilable_sessions(conn) == []
