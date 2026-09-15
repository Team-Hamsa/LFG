"""Fee-cover store (spec: docs/superpowers/specs/2026-09-14-marketplace-fee-cover-design.md)."""

from __future__ import annotations

import sqlite3

import pytest

from lfg_core import fee_cover_store as store

NET = "testnet"
KNOBS = store.Knobs(
    coverage_bps=10_000,
    budget_drops=50_000_000,
    wallet_cap_drops=5_000_000,
    min_bid_drops=1_000_000,
)


@pytest.fixture()
def conn(tmp_path):
    c = store.connect(str(tmp_path / "app.db"))
    yield c
    c.close()


def _audit_rows(conn):
    return [
        dict(r)
        for r in conn.execute("SELECT action, result, actor FROM fee_cover_audit ORDER BY id")
    ]


def test_start_creates_a_live_campaign_with_the_knobs(conn):
    campaign, result = store.start_campaign(
        conn, network=NET, actor="discord:1", knobs=KNOBS, duration_seconds=None, now=1000
    )
    assert result == "started"
    assert campaign.status == "active"
    assert campaign.coverage_bps == 10_000
    assert campaign.budget_drops == 50_000_000
    assert campaign.wallet_cap_drops == 5_000_000
    assert campaign.min_bid_drops == 1_000_000
    assert campaign.wallet_window_seconds == store.DEFAULT_WALLET_WINDOW_SECONDS
    assert campaign.ends_at is None
    assert store.live_campaign(conn, NET, now=2000) == campaign
    assert _audit_rows(conn) == [{"action": "start", "result": "started", "actor": "discord:1"}]


def test_start_twice_is_idempotent_and_audited(conn):
    first, _ = store.start_campaign(
        conn, network=NET, actor="discord:1", knobs=KNOBS, duration_seconds=None, now=1000
    )
    second, result = store.start_campaign(
        conn, network=NET, actor="discord:2", knobs=KNOBS, duration_seconds=None, now=1001
    )
    assert result == "already_active"
    assert second.id == first.id
    assert [r["result"] for r in _audit_rows(conn)] == ["started", "already_active"]


def test_expired_campaign_is_not_live_and_start_replaces_it(conn):
    old, _ = store.start_campaign(
        conn, network=NET, actor="discord:1", knobs=KNOBS, duration_seconds=100, now=1000
    )
    assert old.ends_at == 1100
    assert store.live_campaign(conn, NET, now=1099) is not None
    assert store.live_campaign(conn, NET, now=1100) is None
    new, result = store.start_campaign(
        conn, network=NET, actor="discord:1", knobs=KNOBS, duration_seconds=None, now=1200
    )
    assert result == "started"
    assert new.id != old.id
    retired = store.get_campaign(conn, old.id)
    assert retired is not None and retired.status == "stopped" and retired.stopped_by == "system"


def test_update_changes_knobs_only_on_a_live_campaign(conn):
    missing, result = store.update_campaign(
        conn, network=NET, actor="discord:1", knobs=KNOBS, now=1000
    )
    assert (missing, result) == (None, "no_live_campaign")
    store.start_campaign(
        conn, network=NET, actor="discord:1", knobs=KNOBS, duration_seconds=None, now=1000
    )
    new_knobs = store.Knobs(
        coverage_bps=5_000, budget_drops=10_000_000, wallet_cap_drops=2_000_000, min_bid_drops=0
    )
    updated, result = store.update_campaign(
        conn, network=NET, actor="discord:1", knobs=new_knobs, now=1001
    )
    assert result == "updated"
    assert updated is not None
    assert (
        updated.coverage_bps,
        updated.budget_drops,
        updated.wallet_cap_drops,
        updated.min_bid_drops,
    ) == (5_000, 10_000_000, 2_000_000, 0)


def test_stop_then_stop_again(conn):
    started, _ = store.start_campaign(
        conn, network=NET, actor="discord:1", knobs=KNOBS, duration_seconds=None, now=1000
    )
    stopped, result = store.stop_campaign(conn, network=NET, actor="discord:2", now=1500)
    assert result == "stopped"
    assert stopped is not None and stopped.id == started.id
    assert (
        stopped.status == "stopped"
        and stopped.stopped_by == "discord:2"
        and stopped.stopped_at == 1500
    )
    again, result = store.stop_campaign(conn, network=NET, actor="discord:2", now=1600)
    assert result == "already_inactive"
    assert again is not None and again.id == started.id
    assert store.live_campaign(conn, NET, now=1700) is None


@pytest.mark.parametrize(
    "knobs",
    [
        store.Knobs(coverage_bps=10_001, budget_drops=1, wallet_cap_drops=1, min_bid_drops=0),
        store.Knobs(coverage_bps=-1, budget_drops=1, wallet_cap_drops=1, min_bid_drops=0),
        store.Knobs(coverage_bps=100, budget_drops=0, wallet_cap_drops=1, min_bid_drops=0),
        store.Knobs(coverage_bps=100, budget_drops=1, wallet_cap_drops=0, min_bid_drops=0),
        store.Knobs(coverage_bps=100, budget_drops=1, wallet_cap_drops=1, min_bid_drops=-1),
    ],
)
def test_invalid_knobs_are_rejected(conn, knobs):
    with pytest.raises(ValueError):
        store.start_campaign(
            conn, network=NET, actor="discord:1", knobs=knobs, duration_seconds=None, now=1000
        )


def test_blank_actor_and_bad_duration_are_rejected(conn):
    with pytest.raises(ValueError):
        store.start_campaign(
            conn, network=NET, actor="  ", knobs=KNOBS, duration_seconds=None, now=1000
        )
    with pytest.raises(ValueError):
        store.start_campaign(
            conn, network=NET, actor="discord:1", knobs=KNOBS, duration_seconds=0, now=1000
        )


def test_sqlite_allows_one_active_campaign_per_network(conn):
    store.start_campaign(
        conn, network=NET, actor="discord:1", knobs=KNOBS, duration_seconds=None, now=1000
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO fee_cover_campaigns (network, status, coverage_bps, budget_drops, wallet_cap_drops,"
            " min_bid_drops, started_at, started_by) VALUES (?, 'active', 1, 1, 1, 0, 1, 'x')",
            (NET,),
        )
    conn.rollback()  # the failed INSERT left Python's implicit transaction open
    # a different network is independent
    store.start_campaign(
        conn, network="mainnet", actor="discord:1", knobs=KNOBS, duration_seconds=None, now=1000
    )
