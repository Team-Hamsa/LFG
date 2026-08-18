# Tests for #381: --catch-up-from-gap skips tokens provably burned before the
# gap window, fail-closed on unknown burn positions; certification unaffected.
import importlib
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

bh = importlib.import_module("scripts.backfill_history")

WINDOW_MIN = 90_000_000


def _onchain_db(rows):
    """rows: [(nft_id, is_burned)]"""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE onchain_nfts (nft_id TEXT PRIMARY KEY, is_burned INTEGER)")
    conn.executemany("INSERT INTO onchain_nfts VALUES (?, ?)", rows)
    return conn


def _history_db(burn_events):
    """burn_events: [(nft_id, ledger_index_or_None)]"""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE nft_events (tx_hash TEXT, nft_id TEXT, event TEXT, ledger_index INTEGER)"
    )
    for i, (nft_id, li) in enumerate(burn_events):
        conn.execute("INSERT INTO nft_events VALUES (?, ?, 'burn', ?)", (f"tx{i}", nft_id, li))
    return conn


def test_burned_before_window_skipped():
    oconn = _onchain_db([("A", 1), ("B", 0)])
    hconn = _history_db([("A", WINDOW_MIN - 1)])
    kept, skipped = bh.skip_burned_before_window(oconn, hconn, ["A", "B"], WINDOW_MIN)
    assert kept == ["B"]
    assert skipped == 1


def test_burned_inside_window_paged():
    oconn = _onchain_db([("A", 1)])
    hconn = _history_db([("A", WINDOW_MIN)])  # at the bound: NOT strictly before
    kept, skipped = bh.skip_burned_before_window(oconn, hconn, ["A"], WINDOW_MIN)
    assert kept == ["A"]
    assert skipped == 0


def test_burned_after_window_open_paged():
    oconn = _onchain_db([("A", 1)])
    hconn = _history_db([("A", WINDOW_MIN + 500)])
    kept, skipped = bh.skip_burned_before_window(oconn, hconn, ["A"], WINDOW_MIN)
    assert kept == ["A"]
    assert skipped == 0


def test_unknown_burn_position_paged():
    # is_burned=1 but no burn event / NULL ledger_index → fail-closed, page it.
    oconn = _onchain_db([("A", 1), ("B", 1)])
    hconn = _history_db([("B", None)])  # A: no event at all; B: NULL position
    kept, skipped = bh.skip_burned_before_window(oconn, hconn, ["A", "B"], WINDOW_MIN)
    assert kept == ["A", "B"]
    assert skipped == 0


def test_unparsable_burn_position_paged():
    oconn = _onchain_db([("A", 1)])
    hconn = _history_db([("A", "not-a-ledger")])
    kept, skipped = bh.skip_burned_before_window(oconn, hconn, ["A"], WINDOW_MIN)
    assert kept == ["A"]
    assert skipped == 0


def test_missing_nft_events_table_pages_everything():
    oconn = _onchain_db([("A", 1)])
    hconn = sqlite3.connect(":memory:")  # no nft_events table at all
    kept, skipped = bh.skip_burned_before_window(oconn, hconn, ["A", "B"], WINDOW_MIN)
    assert kept == ["A", "B"]
    assert skipped == 0


def test_missing_onchain_table_pages_everything():
    oconn = sqlite3.connect(":memory:")
    hconn = _history_db([("A", WINDOW_MIN - 1)])
    kept, skipped = bh.skip_burned_before_window(oconn, hconn, ["A"], WINDOW_MIN)
    assert kept == ["A"]
    assert skipped == 0


def test_latest_burn_governs():
    # Multiple burn events (re-derivation overlap / dup editions): the MAX
    # ledger_index is used, so a later in-window burn keeps the token paged.
    oconn = _onchain_db([("A", 1)])
    hconn = _history_db([("A", WINDOW_MIN - 100), ("A", WINDOW_MIN + 5)])
    kept, skipped = bh.skip_burned_before_window(oconn, hconn, ["A"], WINDOW_MIN)
    assert kept == ["A"]
    assert skipped == 0


def test_not_marked_burned_paged_even_with_old_burn_event():
    # Index says live → page regardless of any stray burn event row.
    oconn = _onchain_db([("A", 0)])
    hconn = _history_db([("A", WINDOW_MIN - 1)])
    kept, skipped = bh.skip_burned_before_window(oconn, hconn, ["A"], WINDOW_MIN)
    assert kept == ["A"]
    assert skipped == 0


def test_certification_mode_never_calls_filter():
    # The call site is guarded on args.catch_up_from_gap, mutually exclusive
    # with --complete-audited-baseline: assert the source-level guard exists.
    import inspect

    src = inspect.getsource(bh._amain)
    idx = src.index("skip_burned_before_window")
    guard = src.rindex("if args.catch_up_from_gap", 0, idx)
    assert guard != -1


def test_nonpositive_burn_ledger_paged():
    # Malformed evidence (0 / negative ledger index) must not count as proof
    # the burn predates the window — fail closed and page the token.
    oconn = _onchain_db([("A", 1), ("B", 1)])
    hconn = _history_db([("A", 0), ("B", -5)])
    kept, skipped = bh.skip_burned_before_window(oconn, hconn, ["A", "B"], WINDOW_MIN)
    assert kept == ["A", "B"]
    assert skipped == 0
