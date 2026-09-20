# Tests for lfg_core/leaderboard.py
import os
import sqlite3
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DISCORD_BOT_TOKEN", "x")
os.environ.setdefault("XUMM_API_KEY", "x")
os.environ.setdefault("XUMM_API_SECRET", "x")
os.environ.setdefault("BUNNY_CDN_ACCESS_KEY", "x")
os.environ.setdefault("BUNNY_CDN_STORAGE_ZONE", "x")
os.environ.setdefault("SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2")
os.environ.setdefault("TOKEN_ISSUER_ADDRESS", "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
os.environ.setdefault("TOKEN_CURRENCY_HEX", "4C46474F00000000000000000000000000000000")
os.environ.setdefault("XRPL_NETWORK", "testnet")
os.environ.setdefault("BUNNY_PULL_ZONE", "nft.pullzone.example")

from lfg_core import history_store, leaderboard  # noqa: E402

ISSUER = "rIssuer"
SYS = frozenset({ISSUER})


def _dbs():
    h = history_store.init_history_db(":memory:")
    o = sqlite3.connect(":memory:")
    o.row_factory = sqlite3.Row
    o.execute(
        "CREATE TABLE onchain_nfts (nft_id TEXT PRIMARY KEY, nft_number INT,"
        " owner TEXT, is_burned INT DEFAULT 0, attributes_json TEXT, image TEXT)"
    )
    return h, o


def _ev(h, **kw):
    base = {
        "tx_hash": kw.get("tx_hash", str(id(kw))),
        "nft_id": "N1",
        "nft_number": 1,
        "event": "mint",
        "from_addr": None,
        "to_addr": None,
        "price_drops": None,
        "price_token": None,
        # Ledger sequence rises with close time, and the boards order a
        # token's movements on it. Pinning every fixture event to ledger 1
        # would leave them with no order at all; pass it explicitly to put
        # two events in one ledger.
        "ledger_index": kw.get("ts", 0) + 1,
        "ts": 0,
    }
    base.update(kw)
    history_store.insert_nft_event(h, base)
    h.commit()


def test_period_bounds_today_and_anchored_month():
    now = int(datetime(2026, 7, 4, 15, 0, tzinfo=timezone.utc).timestamp())
    s, e = leaderboard.period_bounds("today", None, now=now)
    assert s == int(datetime(2026, 7, 4, tzinfo=timezone.utc).timestamp()) and e == now
    s, e = leaderboard.period_bounds("month", "2026-01-01", now=now)
    assert (s, e) == (
        int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()),
        int(datetime(2026, 2, 1, tzinfo=timezone.utc).timestamp()),
    )
    s, e = leaderboard.period_bounds("week", "2026-06-30", now=now)  # Tue -> Mon 06-29
    assert s == int(datetime(2026, 6, 29, tzinfo=timezone.utc).timestamp())
    assert leaderboard.period_bounds("all", None, now=now) == (0, now)


def test_period_bounds_unknown_raises():
    now = int(datetime(2026, 7, 4, tzinfo=timezone.utc).timestamp())
    with pytest.raises(ValueError):
        leaderboard.period_bounds("century", None, now=now)


# users_nfts is covered by test_users_nfts_alltime_is_still_a_holdings_census
# and the windowed "Minters" tests below; the windowed half of the old
# combined test asserted the retired net-acquisition delta.


def test_users_swaps_and_nft_swaps():
    h, o = _dbs()
    _member(o, "N1", 1, "rA")
    for i, w in enumerate(["rA", "rA", "rB"]):
        _ev(h, tx_hash=f"m{i}", event="modify", to_addr=w, nft_id="N1", ts=5)
    rows = leaderboard.compute(
        "users_swaps", h, o, start_ts=0, end_ts=10, network="testnet", system_accounts=SYS
    )
    assert rows[0] == {"wallet": "rA", "nft_id": None, "nft_number": None, "value": 2}
    rows = leaderboard.compute(
        "nft_swaps", h, o, start_ts=0, end_ts=10, network="testnet", system_accounts=SYS
    )
    assert rows[0]["nft_id"] == "N1" and rows[0]["value"] == 3


def _rebirth(h, o, *, edition, old_id, new_id, wallet, memo_action=None, prefix=""):
    """Seed a burn -> remint -> issuer delivery of one edition. The burn's
    from_addr is the token's holder at burn time — for a legacy swap that IS
    the swapper (the issuer burns the burnable token in the user's wallet).

    Both tokens are registered in the collection index: a rebirth is by
    definition an edition, and every board is scoped to the index."""
    _member(o, old_id, edition, wallet)
    _member(o, new_id, edition, wallet)
    _ev(
        h,
        tx_hash=f"{prefix}b{edition}",
        event="burn",
        nft_id=old_id,
        nft_number=edition,
        from_addr=wallet,
        ts=10,
    )
    _ev(
        h,
        tx_hash=f"{prefix}m{edition}",
        event="mint",
        nft_id=new_id,
        nft_number=edition,
        to_addr=ISSUER,
        ts=20,
        memo_action=memo_action,
    )
    _ev(
        h,
        tx_hash=f"{prefix}d{edition}",
        event="transfer",
        nft_id=new_id,
        nft_number=edition,
        from_addr=ISSUER,
        to_addr=wallet,
        ts=30,
    )


def test_users_builds_counts_only_assemble_rebirths():
    """A rebirth is only a "build" if its mint carries the assemble memo —
    otherwise it's a legacy burn+remint trait swap (mainnet has 2,000+ of
    those and zero assembles; they were all showing up as builds)."""
    h, o = _dbs()
    # real assemble: rebirth whose mint is memo-stamped action=assemble
    _rebirth(h, o, edition=7, old_id="OLD", new_id="NEW", wallet="rA", memo_action="assemble")
    # legacy remint swap: identical on-chain shape, no memo -> NOT a build
    _rebirth(h, o, edition=8, old_id="OLD8", new_id="NEW8", wallet="rB")
    # non-rebirth issuer transfer must not count either
    _ev(h, tx_hash="m2", event="mint", nft_id="N9", nft_number=9, to_addr=ISSUER, ts=20)
    _ev(
        h,
        tx_hash="d2",
        event="transfer",
        nft_id="N9",
        nft_number=9,
        from_addr=ISSUER,
        to_addr="rB",
        ts=30,
    )
    rows = leaderboard.compute(
        "users_builds", h, o, start_ts=0, end_ts=99, network="testnet", system_accounts=SYS
    )
    assert rows == [{"wallet": "rA", "nft_id": None, "nft_number": None, "value": 1}]


def test_swap_boards_count_legacy_remint_swaps():
    """Legacy trait swaps are burn+remint (mainnet has ZERO modify events), so
    the swap boards must count rebirth deliveries too — except assembles."""
    h, o = _dbs()
    # legacy remint swap for rA (edition 5) — counts as a swap
    _rebirth(h, o, edition=5, old_id="OLD5", new_id="NEW5", wallet="rA")
    # assemble for rB (edition 6) — a build, NOT a swap
    _rebirth(h, o, edition=6, old_id="OLD6", new_id="NEW6", wallet="rB", memo_action="assemble")
    # modern modify swap for rA on the same edition 5 — still counts
    _member(o, "NEW5", 5, "rA")
    _ev(h, tx_hash="mod", event="modify", nft_id="NEW5", nft_number=5, to_addr="rA", ts=40)
    rows = leaderboard.compute(
        "users_swaps", h, o, start_ts=0, end_ts=99, network="testnet", system_accounts=SYS
    )
    assert rows == [{"wallet": "rA", "nft_id": None, "nft_number": None, "value": 2}]
    rows = leaderboard.compute(
        "nft_swaps", h, o, start_ts=0, end_ts=99, network="testnet", system_accounts=SYS
    )
    # per-edition: both of rA's swaps land on edition 5 even though the
    # remint changed the token's nft_id
    assert rows[0]["nft_number"] == 5 and rows[0]["value"] == 2


def test_swap_counted_from_burn_alone_without_delivery_transfer():
    """The index knows 2,028 burned mainnet tokens but the history archive
    captured only 77 issuer delivery transfers — keying swaps on the delivery
    leg undercounted 26x. A burn followed by a remint of the same edition IS
    the swap, attributed to the burn's holder; no delivery event needed."""
    h, o = _dbs()
    _member(o, "OLD5", 5, "rA")
    _member(o, "NEW5", 5, ISSUER)
    _ev(h, tx_hash="b1", event="burn", nft_id="OLD5", nft_number=5, from_addr="rA", ts=10)
    _ev(h, tx_hash="m1", event="mint", nft_id="NEW5", nft_number=5, to_addr=ISSUER, ts=20)
    rows = leaderboard.compute(
        "users_swaps", h, o, start_ts=0, end_ts=99, network="testnet", system_accounts=SYS
    )
    assert rows == [{"wallet": "rA", "nft_id": None, "nft_number": None, "value": 1}]
    rows = leaderboard.compute(
        "nft_swaps", h, o, start_ts=0, end_ts=99, network="testnet", system_accounts=SYS
    )
    assert rows[0]["nft_number"] == 5 and rows[0]["value"] == 1


def test_permanent_burn_without_remint_is_not_a_swap():
    h, o = _dbs()
    _ev(h, tx_hash="b1", event="burn", nft_id="OLD5", nft_number=5, from_addr="rA", ts=10)
    for board in ("users_swaps", "nft_swaps"):
        assert (
            leaderboard.compute(
                board, h, o, start_ts=0, end_ts=99, network="testnet", system_accounts=SYS
            )
            == []
        )


def test_harvest_burn_before_assemble_is_not_a_swap():
    """An economy harvest burns a character whose edition is later reborn via
    Assemble — that burn is not a trait swap. The NEXT mint after the burn
    decides: assemble-memo -> not a swap, even if a real swap remint happens
    later still."""
    h, o = _dbs()
    _ev(h, tx_hash="b1", event="burn", nft_id="OLD5", nft_number=5, from_addr="rA", ts=10)
    _ev(
        h,
        tx_hash="m1",
        event="mint",
        nft_id="NEW5",
        nft_number=5,
        to_addr=ISSUER,
        ts=20,
        memo_action="assemble",
    )
    _ev(h, tx_hash="m2", event="mint", nft_id="NEW5B", nft_number=5, to_addr=ISSUER, ts=30)
    rows = leaderboard.compute(
        "users_swaps", h, o, start_ts=0, end_ts=99, network="testnet", system_accounts=SYS
    )
    assert rows == []


def test_swap_windowing_keys_on_burn_time():
    h, o = _dbs()
    _member(o, "OLD5", 5, "rA")
    _member(o, "NEW5", 5, ISSUER)
    _ev(h, tx_hash="b1", event="burn", nft_id="OLD5", nft_number=5, from_addr="rA", ts=10)
    _ev(h, tx_hash="m1", event="mint", nft_id="NEW5", nft_number=5, to_addr=ISSUER, ts=50)
    in_window = leaderboard.compute(
        "users_swaps", h, o, start_ts=5, end_ts=15, network="testnet", system_accounts=SYS
    )
    assert in_window and in_window[0]["wallet"] == "rA"
    out_window = leaderboard.compute(
        "users_swaps", h, o, start_ts=20, end_ts=99, network="testnet", system_accounts=SYS
    )
    assert out_window == []


def test_nft_swaps_reports_live_token_id_not_lexicographic_max():
    """An edition with a modify on the OLD token then a remint swap has two
    nft_ids in its swap events; MAX() would pick "OLD5" (lexicographically
    larger) — a burned token (Greptile #157). The board must surface the
    edition's live nft_id from the on-chain index."""
    h, o = _dbs()
    _ev(h, tx_hash="modold", event="modify", nft_id="OLD5", nft_number=5, to_addr="rA", ts=5)
    _rebirth(h, o, edition=5, old_id="OLD5", new_id="NEW5", wallet="rA")
    o.execute(
        "INSERT OR REPLACE INTO onchain_nfts (nft_id, nft_number, is_burned) VALUES ('OLD5', 5, 1)"
    )
    o.execute(
        "INSERT OR REPLACE INTO onchain_nfts (nft_id, nft_number, is_burned) VALUES ('NEW5', 5, 0)"
    )
    o.commit()
    rows = leaderboard.compute(
        "nft_swaps", h, o, start_ts=0, end_ts=99, network="testnet", system_accounts=SYS
    )
    assert rows[0]["nft_number"] == 5 and rows[0]["value"] == 2
    assert rows[0]["nft_id"] == "NEW5"


def test_compute_unknown_board_raises():
    h, o = _dbs()
    with pytest.raises(ValueError):
        leaderboard.compute(
            "nope", h, o, start_ts=0, end_ts=1, network="testnet", system_accounts=SYS
        )


def test_brix_rich_alltime_and_windowed():
    h, o = _dbs()
    history_store.upsert_snapshot(h, "2026-07-01", "rA", 10, 0)
    history_store.upsert_snapshot(h, "2026-07-03", "rA", 25, 0)
    history_store.upsert_snapshot(h, "2026-07-03", "rB", 5, 0)
    rows = leaderboard.compute(
        "brix_rich", h, o, start_ts=0, end_ts=99, network="testnet", system_accounts=SYS
    )
    assert [(r["wallet"], r["value"]) for r in rows] == [("rA", 25), ("rB", 5)]

    start = int(datetime(2026, 7, 2, tzinfo=timezone.utc).timestamp())
    end = int(datetime(2026, 7, 4, tzinfo=timezone.utc).timestamp())
    rows = leaderboard.compute(
        "brix_rich", h, o, start_ts=start, end_ts=end, network="testnet", system_accounts=SYS
    )
    assert [(r["wallet"], r["value"]) for r in rows] == [("rA", 15), ("rB", 5)]


def test_brix_lp_uses_lp_tokens_column():
    h, o = _dbs()
    history_store.upsert_snapshot(h, "2026-07-01", "rA", 0, 10)
    history_store.upsert_snapshot(h, "2026-07-03", "rA", 0, 25)
    history_store.upsert_snapshot(h, "2026-07-03", "rB", 0, 5)
    rows = leaderboard.compute(
        "brix_lp", h, o, start_ts=0, end_ts=99, network="testnet", system_accounts=SYS
    )
    assert [(r["wallet"], r["value"]) for r in rows] == [("rA", 25), ("rB", 5)]


def test_brix_rich_excludes_system_accounts():
    h, o = _dbs()
    history_store.upsert_snapshot(h, "2026-07-03", "rA", 25, 0)
    history_store.upsert_snapshot(h, "2026-07-03", ISSUER, 999, 0)
    rows = leaderboard.compute(
        "brix_rich", h, o, start_ts=0, end_ts=99, network="testnet", system_accounts=SYS
    )
    assert [r["wallet"] for r in rows] == ["rA"]


def _brixev(h, **kw):
    base = {
        "tx_hash": str(id(kw)),
        "account": None,
        "counterparty": None,
        "delta": 0,
        "kind": "payment",
        "ts": 0,
    }
    base.update(kw)
    history_store.insert_brix_event(h, base)
    h.commit()


def test_brix_earned_from_system_sources():
    h, o = _dbs()
    _brixev(h, tx_hash="e1", account="rA", counterparty=None, delta=3, kind="airdrop", ts=5)
    _brixev(h, tx_hash="e2", account="rA", counterparty="rB", delta=5, kind="payment", ts=6)
    _brixev(h, tx_hash="e3", account="rB", counterparty=ISSUER, delta=2, kind="payment", ts=7)
    rows = leaderboard.compute(
        "brix_earned", h, o, start_ts=0, end_ts=99, network="testnet", system_accounts=SYS
    )
    assert {(r["wallet"], r["value"]) for r in rows} == {("rA", 3), ("rB", 2)}


def test_brix_earned_excludes_system_account_recipients():
    h, o = _dbs()
    _brixev(h, tx_hash="e1", account="rA", counterparty=None, delta=3, kind="airdrop", ts=5)
    _brixev(h, tx_hash="e2", account=ISSUER, counterparty=None, delta=10, kind="airdrop", ts=6)
    rows = leaderboard.compute(
        "brix_earned", h, o, start_ts=0, end_ts=99, network="testnet", system_accounts=SYS
    )
    assert {(r["wallet"], r["value"]) for r in rows} == {("rA", 3)}


def test_nft_rarity_scores_unique_traits_highest_and_excludes_burned():
    h, o = _dbs()
    o.executemany(
        "INSERT INTO onchain_nfts (nft_id, nft_number, owner, is_burned, attributes_json)"
        " VALUES (?,?,?,?,?)",
        [
            (
                "N1",
                1,
                "rA",
                0,
                '[{"trait_type": "Background", "value": "Blue"},'
                ' {"trait_type": "Hat", "value": "Common"}]',
            ),
            (
                "N2",
                2,
                "rB",
                0,
                '[{"trait_type": "Background", "value": "Blue"},'
                ' {"trait_type": "Hat", "value": "Unique"}]',
            ),
            (
                "N3",
                3,
                "rC",
                0,
                '[{"trait_type": "Background", "value": "Blue"},'
                ' {"trait_type": "Hat", "value": "Common"}]',
            ),
            (
                "N4",
                4,
                ISSUER,
                1,
                '[{"trait_type": "Background", "value": "Blue"},'
                ' {"trait_type": "Hat", "value": "Unique"}]',
            ),
        ],
    )
    rows = leaderboard.compute(
        "nft_rarity", h, o, start_ts=0, end_ts=99, network="testnet", system_accounts=SYS
    )
    assert rows[0]["nft_id"] == "N2"
    assert len(rows) == 3


def test_limit_parameter_threads_through():
    """Verify limit parameter is threaded through board functions, not capped at _LIMIT."""
    h, o = _dbs()
    # Insert 30 modify events by 30 distinct wallets
    for i in range(30):
        wallet = f"rWallet{i:02d}"
        _member(o, f"N{i}", i, wallet)
        _ev(h, tx_hash=f"t{i}", event="modify", to_addr=wallet, nft_id=f"N{i}", ts=5)

    # Test with limit=28: should return 28 rows, not capped at _LIMIT (25)
    rows = leaderboard.compute(
        "users_swaps", h, o, start_ts=0, end_ts=10, network="testnet", system_accounts=SYS, limit=28
    )
    assert len(rows) == 28, f"expected 28 rows with limit=28, got {len(rows)}"

    # Test default limit still gives 25
    rows_default = leaderboard.compute(
        "users_swaps", h, o, start_ts=0, end_ts=10, network="testnet", system_accounts=SYS
    )
    assert len(rows_default) == 25, f"expected 25 rows with default limit, got {len(rows_default)}"


# --- Collection scoping: the archive is not the collection ----------------
# nft_events carries every token our issuer ever touched: collection
# characters, the soulbound per-user Closet tokens (taxon 1762) and tradeable
# trait tokens (taxon 176). Only characters belong on the user-facing boards.


def _member(o, nft_id, number=None, owner=None):
    """Register a token in the collection index (what makes it a character)."""
    o.execute(
        "INSERT OR REPLACE INTO onchain_nfts (nft_id, nft_number, owner) VALUES (?,?,?)",
        (nft_id, number, owner),
    )
    o.commit()


def test_swap_boards_ignore_closet_and_trait_token_modifies():
    """Closet tokens are NFTokenModify'd on every economy op — 902 of mainnet's
    1,512 modify events. Unscoped they outrank every real character on
    nft_swaps (with no edition and no art, so those rows render blank) and
    inflate every wallet's Swappers count."""
    h, o = _dbs()
    _member(o, "CHAR", 5, "rA")  # a collection character
    # a Closet token: in the archive, never in the collection index
    for i in range(3):
        _ev(
            h, tx_hash=f"c{i}", event="modify", nft_id="CLOSET", nft_number=None, to_addr="rA", ts=5
        )
    _ev(h, tx_hash="s0", event="modify", nft_id="CHAR", nft_number=5, to_addr="rA", ts=5)

    rows = leaderboard.compute(
        "users_swaps", h, o, start_ts=0, end_ts=10, network="testnet", system_accounts=SYS
    )
    assert rows == [{"wallet": "rA", "nft_id": None, "nft_number": None, "value": 1}]

    rows = leaderboard.compute(
        "nft_swaps", h, o, start_ts=0, end_ts=10, network="testnet", system_accounts=SYS
    )
    assert [(r["nft_id"], r["value"]) for r in rows] == [("CHAR", 1)]


def test_nft_swaps_never_yields_a_row_without_an_edition():
    """Every blank row on the live board was a non-collection token keying its
    own row through the COALESCE(nft_number, nft_id) fallback."""
    h, o = _dbs()
    _member(o, "CHAR", 5, "rA")
    _ev(h, tx_hash="s0", event="modify", nft_id="CHAR", nft_number=5, to_addr="rA", ts=5)
    for i in range(9):
        _ev(
            h, tx_hash=f"c{i}", event="modify", nft_id=f"CL{i}", nft_number=None, to_addr="rA", ts=5
        )
    rows = leaderboard.compute(
        "nft_swaps", h, o, start_ts=0, end_ts=10, network="testnet", system_accounts=SYS
    )
    assert rows and all(r["nft_number"] is not None for r in rows)


def test_collection_scope_is_rebuilt_per_call_not_cached_across_boards():
    """The scope is a TEMP table on the history connection; a token indexed
    between two calls on the same connection must be picked up."""
    h, o = _dbs()
    _ev(h, tx_hash="s0", event="modify", nft_id="LATE", nft_number=7, to_addr="rA", ts=5)
    assert (
        leaderboard.compute(
            "users_swaps", h, o, start_ts=0, end_ts=10, network="testnet", system_accounts=SYS
        )
        == []
    )
    _member(o, "LATE", 7, "rA")
    rows = leaderboard.compute(
        "users_swaps", h, o, start_ts=0, end_ts=10, network="testnet", system_accounts=SYS
    )
    assert rows == [{"wallet": "rA", "nft_id": None, "nft_number": None, "value": 1}]


# --- users_nfts: All Time = holdings census, windowed = mints -------------


def _delivery(h, *, nft_id, edition, wallet, ts=30, memo_action="mint", prefix=""):
    """Seed a mint (to the issuer, which signs it) + the issuer's delivery."""
    _ev(
        h,
        tx_hash=f"{prefix}m{nft_id}",
        event="mint",
        nft_id=nft_id,
        nft_number=edition,
        to_addr=ISSUER,
        ts=ts - 10,
        memo_action=memo_action,
    )
    _ev(
        h,
        tx_hash=f"{prefix}d{nft_id}",
        event="transfer",
        nft_id=nft_id,
        nft_number=edition,
        from_addr=ISSUER,
        to_addr=wallet,
        ts=ts,
    )


def test_users_nfts_alltime_is_still_a_holdings_census():
    """All Time keeps reading the live index — the chip says "Holders" there."""
    h, o = _dbs()
    for nft_id, number, owner in [("N1", 1, "rA"), ("N2", 2, "rA"), ("N3", 3, "rB")]:
        _member(o, nft_id, number, owner)
    _member(o, "N4", 4, ISSUER)
    rows = leaderboard.compute(
        "users_nfts", h, o, start_ts=0, end_ts=99, network="testnet", system_accounts=SYS
    )
    assert [(r["wallet"], r["value"]) for r in rows] == [("rA", 2), ("rB", 1)]


def test_users_nfts_windowed_counts_mints_not_acquisitions():
    """Windowed, the board is "Minters": editions first delivered by the
    issuer inside the window. A marketplace sale moves a token between
    wallets — it is an acquisition, not a mint, and must not score."""
    h, o = _dbs()
    _member(o, "N1", 1, "rA")
    _member(o, "N2", 2, "rA")
    _delivery(h, nft_id="N1", edition=1, wallet="rA", ts=50)
    _delivery(h, nft_id="N2", edition=2, wallet="rA", ts=52)
    # rB bought one off rA on the marketplace
    _ev(
        h,
        tx_hash="sale",
        event="sale",
        nft_id="N1",
        nft_number=1,
        from_addr="rA",
        to_addr="rB",
        ts=55,
    )
    rows = leaderboard.compute(
        "users_nfts", h, o, start_ts=40, end_ts=60, network="testnet", system_accounts=SYS
    )
    assert rows == [{"wallet": "rA", "nft_id": None, "nft_number": None, "value": 2}]


def test_users_nfts_windowed_excludes_rebirths_assembles_and_loose_tokens():
    """Only brand-new collection editions count. Rebirth deliveries (legacy
    remint swaps, assembles) re-deliver an edition that already existed, and
    Closet/trait tokens are not editions at all — mainnet stamps a Closet
    claim's mint with the very same memo a paid mint carries (action=mint,
    86 of them), so the collection index is the only authority."""
    h, o = _dbs()
    _member(o, "NEW", 1, "rA")
    _delivery(h, nft_id="NEW", edition=1, wallet="rA", ts=50)
    # legacy remint swap: edition 2 burned, reminted, re-delivered
    _member(o, "REBORN", 2, "rB")
    _ev(h, tx_hash="b2", event="burn", nft_id="OLD2", nft_number=2, from_addr="rB", ts=20)
    _delivery(h, nft_id="REBORN", edition=2, wallet="rB", ts=50)
    # assemble of a brand-new edition — a build, not a mint
    _member(o, "BUILT", 3, "rC")
    _delivery(h, nft_id="BUILT", edition=3, wallet="rC", ts=50, memo_action="assemble")
    # a Closet claim and an extracted trait token: never in the index
    _delivery(h, nft_id="CLOSET", edition=None, wallet="rD", ts=50, memo_action="mint")
    _delivery(h, nft_id="TRAIT", edition=None, wallet="rD", ts=50, memo_action="extract")
    rows = leaderboard.compute(
        "users_nfts", h, o, start_ts=40, end_ts=60, network="testnet", system_accounts=SYS
    )
    assert rows == [{"wallet": "rA", "nft_id": None, "nft_number": None, "value": 1}]


def test_users_nfts_windowed_excludes_system_accounts():
    h, o = _dbs()
    _member(o, "N1", 1, ISSUER)
    _delivery(h, nft_id="N1", edition=1, wallet=ISSUER, ts=50)
    assert (
        leaderboard.compute(
            "users_nfts", h, o, start_ts=40, end_ts=60, network="testnet", system_accounts=SYS
        )
        == []
    )


def test_users_nfts_windowed_credits_only_the_editions_first_recipient():
    """An edition returned to the issuer and re-delivered must not mint twice.
    COUNT(DISTINCT t.nft_id) only dedupes inside one wallet's group, so the
    second recipient scored a mint of an edition that already existed
    (Greptile P1 on PR #578; mainnet has one such token today)."""
    h, o = _dbs()
    _member(o, "N1", 1, "rB")
    _delivery(h, nft_id="N1", edition=1, wallet="rA", ts=50)
    # rA hands it back and the issuer re-delivers to rB, same window
    _ev(
        h,
        tx_hash="ret",
        event="transfer",
        nft_id="N1",
        nft_number=1,
        from_addr="rA",
        to_addr=ISSUER,
        ts=52,
    )
    _ev(
        h,
        tx_hash="redeliver",
        event="transfer",
        nft_id="N1",
        nft_number=1,
        from_addr=ISSUER,
        to_addr="rB",
        ts=54,
    )
    rows = leaderboard.compute(
        "users_nfts", h, o, start_ts=40, end_ts=60, network="testnet", system_accounts=SYS
    )
    assert rows == [{"wallet": "rA", "nft_id": None, "nft_number": None, "value": 1}]


def test_users_nfts_windowed_skips_a_redelivery_whose_first_hop_predates_the_window():
    """The first-hop test is deliberately unwindowed: a redelivery is not a
    mint no matter how long after the original it lands."""
    h, o = _dbs()
    _member(o, "N1", 1, "rB")
    _delivery(h, nft_id="N1", edition=1, wallet="rA", ts=10)  # before the window
    _ev(
        h,
        tx_hash="redeliver",
        event="transfer",
        nft_id="N1",
        nft_number=1,
        from_addr=ISSUER,
        to_addr="rB",
        ts=50,
    )
    assert (
        leaderboard.compute(
            "users_nfts", h, o, start_ts=40, end_ts=60, network="testnet", system_accounts=SYS
        )
        == []
    )


def test_swap_boards_ignore_a_non_collection_burn_and_remint():
    """The burn leg is scoped too. Unreachable in today's archive - an event's
    nft_number is only ever filled by looking the token up in the index, so a
    non-NULL edition already implies membership and mainnet has zero rows that
    carry one without it - but the query should not lean on how that column
    happens to be populated (CodeRabbit on PR #578)."""
    h, o = _dbs()
    # a burn + remint pair sharing an edition number, neither one indexed
    _ev(h, tx_hash="xb", event="burn", nft_id="XOLD", nft_number=5, from_addr="rA", ts=10)
    _ev(h, tx_hash="xm", event="mint", nft_id="XNEW", nft_number=5, to_addr=ISSUER, ts=20)
    for board in ("users_swaps", "nft_swaps"):
        assert (
            leaderboard.compute(
                board, h, o, start_ts=0, end_ts=99, network="testnet", system_accounts=SYS
            )
            == []
        ), board
    # once both are collection members it counts again, so the scope is the
    # only thing that suppressed it
    _member(o, "XOLD", 5, "rA")
    _member(o, "XNEW", 5, ISSUER)
    rows = leaderboard.compute(
        "users_swaps", h, o, start_ts=0, end_ts=99, network="testnet", system_accounts=SYS
    )
    assert rows == [{"wallet": "rA", "nft_id": None, "nft_number": None, "value": 1}]


def test_users_nfts_windowed_orders_movements_by_ledger_not_tx_hash():
    """Two movements can share a close time; a transaction hash is not an
    execution order (Greptile on PR #578). Here the earlier movement has the
    LARGER hash, so a hash tie-break calls the redelivery "first" and mints
    it. ledger_index is the archive's real order."""
    h, o = _dbs()
    _member(o, "N1", 1, "rB")
    _ev(
        h,
        tx_hash="zzz-earlier-but-larger-hash",
        event="transfer",
        nft_id="N1",
        nft_number=1,
        from_addr=ISSUER,
        to_addr="rA",
        ts=50,
        ledger_index=99,
    )
    _ev(h, tx_hash="mint1", event="mint", nft_id="N1", nft_number=1, to_addr=ISSUER, ts=40)
    _ev(
        h,
        tx_hash="aaa-later-but-smaller-hash",
        event="transfer",
        nft_id="N1",
        nft_number=1,
        from_addr=ISSUER,
        to_addr="rB",
        ts=50,
        ledger_index=100,
    )
    rows = leaderboard.compute(
        "users_nfts", h, o, start_ts=40, end_ts=60, network="testnet", system_accounts=SYS
    )
    assert rows == [{"wallet": "rA", "nft_id": None, "nft_number": None, "value": 1}]


def test_users_nfts_windowed_still_mints_when_the_buyer_flips_in_the_same_ledger():
    """A same-ledger sibling must not suppress the delivery. A token's first
    movement is always from the issuer — it is minted into the issuer's own
    account — so a flip sharing that ledger can only be the later one."""
    h, o = _dbs()
    _member(o, "N1", 1, "rB")
    _ev(h, tx_hash="mint1", event="mint", nft_id="N1", nft_number=1, to_addr=ISSUER, ts=40)
    _ev(
        h,
        tx_hash="deliver",
        event="transfer",
        nft_id="N1",
        nft_number=1,
        from_addr=ISSUER,
        to_addr="rA",
        ts=50,
        ledger_index=100,
    )
    _ev(
        h,
        tx_hash="flip",
        event="sale",
        nft_id="N1",
        nft_number=1,
        from_addr="rA",
        to_addr="rB",
        ts=50,
        ledger_index=100,
    )
    rows = leaderboard.compute(
        "users_nfts", h, o, start_ts=40, end_ts=60, network="testnet", system_accounts=SYS
    )
    assert rows == [{"wallet": "rA", "nft_id": None, "nft_number": None, "value": 1}]
