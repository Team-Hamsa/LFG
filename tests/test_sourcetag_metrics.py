# Tests for scripts/sourcetag_metrics.py
import importlib
import json
import os
import sys

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
os.environ.setdefault("BUNNY_PULL_ZONE", "")
os.environ.setdefault("LAYER_SOURCE", "local")
os.environ.setdefault("ECONOMY_ENABLED", "1")

from lfg_core import config, history_store  # noqa: E402

stm = importlib.import_module("scripts.sourcetag_metrics")

TAG = config.SOURCE_TAG
USER_A = "rUserAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
USER_B = "rUserBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
OPERATOR = "rHU8nu9zSnCpkL3gShG4aGawHzaRVfmKwQ"

# 2026-07-20T12:00:00Z and 2026-07-22T12:00:00Z, as UNIX seconds. These are
# stored verbatim: close_time in xrpl_txs is unix, not the ripple epoch.
DAY0 = 1784548800
DAY2 = DAY0 + 2 * 86400


def _valid_payload(**overrides):
    """A complete, schema-valid payload; override individual fields per test."""
    payload = {
        "source_tag": TAG,
        "network": "testnet",
        "total_tagged_txs": 5,
        "unique_wallets": 2,
        "unique_actors": 2,
        "by_type": {"Payment": 5},
        "daily": [{"date": "2026-07-20", "count": 5}],
        "excluded": sorted(stm.OPERATOR_WALLETS),
        "first_tagged_tx": "2026-07-20",
        "archive_max_close_time": "2026-07-20T12:00:00+00:00",
        "as_of": "2026-07-22T00:20:00+00:00",
        "xrp_payment_volume": {"in_drops": 0, "out_drops": 0, "other_drops": 0},
    }
    payload.update(overrides)
    return payload


def _db(tmp_path, rows):
    """rows: (hash, close_time, tx_type, account, source_tag)"""
    path = str(tmp_path / "history_testnet.db")
    conn = history_store.init_history_db(path)
    conn.executemany(
        "INSERT INTO xrpl_txs (tx_hash, ledger_index, close_time, tx_type,"
        " account, source_tag, raw_json) VALUES (?,1,?,?,?,?,'{}')",
        rows,
    )
    conn.commit()
    conn.close()
    return path


def test_counts_all_tagged_txs_but_excludes_our_wallets_from_unique(tmp_path):
    path = _db(
        tmp_path,
        [
            ("h1", DAY0, "NFTokenMint", config.SIGNING_ACCOUNT, TAG),
            ("h2", DAY0, "NFTokenAcceptOffer", USER_A, TAG),
            ("h3", DAY2, "NFTokenAcceptOffer", USER_B, TAG),
            ("h4", DAY2, "Payment", OPERATOR, TAG),
            ("h5", DAY2, "Payment", USER_A, None),  # untagged, must not count
        ],
    )
    out = stm.collect(path, "testnet")

    # every tagged row counts, including the backend-signed mint
    assert out["total_tagged_txs"] == 4
    # ...but only non-project signers are unique wallets
    assert out["unique_wallets"] == 2
    assert out["source_tag"] == TAG
    assert out["network"] == "testnet"


def test_by_type_is_descending_and_covers_all_tagged_rows(tmp_path):
    path = _db(
        tmp_path,
        [
            ("h1", DAY0, "NFTokenMint", config.SIGNING_ACCOUNT, TAG),
            ("h2", DAY0, "NFTokenMint", config.SIGNING_ACCOUNT, TAG),
            ("h3", DAY0, "NFTokenAcceptOffer", USER_A, TAG),
        ],
    )
    out = stm.collect(path, "testnet")
    assert list(out["by_type"].items()) == [("NFTokenMint", 2), ("NFTokenAcceptOffer", 1)]


def test_daily_series_is_gap_filled_and_uses_unix_close_time(tmp_path):
    path = _db(
        tmp_path,
        [
            ("h1", DAY0, "NFTokenMint", USER_A, TAG),
            ("h2", DAY2, "NFTokenMint", USER_A, TAG),
        ],
    )
    out = stm.collect(path, "testnet")
    # DAY0 is 2026-07-20; the intervening day must appear as a zero
    assert out["daily"] == [
        {"date": "2026-07-20", "count": 1},
        {"date": "2026-07-21", "count": 0},
        {"date": "2026-07-22", "count": 1},
    ]
    assert out["first_tagged_tx"] == "2026-07-20"


def test_excluded_addresses_are_reported(tmp_path):
    path = _db(tmp_path, [("h1", DAY0, "Payment", USER_A, TAG)])
    out = stm.collect(path, "testnet")
    assert config.SIGNING_ACCOUNT in out["excluded"]
    assert OPERATOR in out["excluded"]
    assert out["excluded"] == sorted(out["excluded"])


def test_no_tagged_rows_yields_zeros_not_a_crash(tmp_path):
    path = _db(tmp_path, [("h1", DAY0, "Payment", USER_A, None)])
    out = stm.collect(path, "testnet")
    assert out["total_tagged_txs"] == 0
    assert out["unique_wallets"] == 0
    assert out["by_type"] == {}
    assert out["daily"] == []
    assert out["first_tagged_tx"] is None
    assert json.dumps(out)  # serialisable


def test_validate_payload_accepts_a_real_payload(tmp_path):
    path = _db(tmp_path, [("h1", DAY0, "Payment", USER_A, TAG)])
    stm.validate_payload(stm.collect(path, "testnet"))  # must not raise


def test_validate_payload_rejects_unknown_keys(tmp_path):
    path = _db(tmp_path, [("h1", DAY0, "Payment", USER_A, TAG)])
    payload = stm.collect(path, "testnet")
    payload["raw_tx"] = {"Account": "rX", "secret": "shhh"}
    with pytest.raises(ValueError, match="unexpected key"):
        stm.validate_payload(payload)


def test_validate_payload_rejects_non_whitelisted_value_shapes(tmp_path):
    path = _db(tmp_path, [("h1", DAY0, "Payment", USER_A, TAG)])
    payload = stm.collect(path, "testnet")
    payload["network"] = "sEdTM1uX8pu2do5XvTnutH6HsouMaM2"  # looks like a seed
    with pytest.raises(ValueError):
        stm.validate_payload(payload)


def test_validate_payload_rejects_missing_required_key():
    payload = _valid_payload()
    del payload["excluded"]
    with pytest.raises(ValueError, match="excluded"):
        stm.validate_payload(payload)


def test_validate_payload_rejects_malformed_as_of():
    payload = _valid_payload(as_of="now")
    with pytest.raises(ValueError, match="as_of"):
        stm.validate_payload(payload)


def test_out_writes_file(tmp_path):
    path = _db(tmp_path, [("h1", DAY0, "Payment", USER_A, TAG)])
    dest = tmp_path / "snapshots" / "sourcetag.json"
    rc = stm.main(["--network", "testnet", "--db", path, "--out", str(dest)])
    assert rc == 0
    assert json.loads(dest.read_text())["total_tagged_txs"] == 1


def test_push_flag_is_retired():
    """Make Waves closed 2026-09-21 and metrics/sourcetag.json is frozen as
    submitted. `--push` committed straight to main past every gate, so it is
    gone, and a stale pm2 entry still passing it fails loudly instead."""
    with pytest.raises(SystemExit) as exc:
        stm.main(["--network", "testnet", "--push"])
    assert exc.value.code == 2
    assert not hasattr(stm, "push_to_github")


def test_no_out_prints_without_touching_the_frozen_snapshot(tmp_path, monkeypatch, capsys):
    path = _db(tmp_path, [("h1", DAY0, "Payment", USER_A, TAG)])
    fake_cwd = tmp_path / "checkout"
    fake_cwd.mkdir()
    monkeypatch.chdir(fake_cwd)
    rc = stm.main(["--network", "testnet", "--db", path, "--json"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["total_tagged_txs"] == 1
    assert list(fake_cwd.iterdir()) == []


def test_out_refuses_to_write_a_payload_that_fails_validation(tmp_path, monkeypatch, capsys):
    path = _db(tmp_path, [("h1", DAY0, "Payment", USER_A, TAG)])
    dest = tmp_path / "out.json"

    def reject(payload):
        raise ValueError("unexpected key(s) in payload: ['sneaky']")

    monkeypatch.setattr(stm, "validate_payload", reject)
    rc = stm.main(["--network", "testnet", "--db", path, "--out", str(dest)])
    assert rc == 2
    assert not dest.exists()
    assert "sneaky" in capsys.readouterr().err


@pytest.mark.parametrize("dest", ["metrics/sourcetag.json", "checkout/metrics/sourcetag.json"])
def test_out_refuses_the_frozen_snapshot_path(tmp_path, monkeypatch, capsys, dest):
    """metrics/sourcetag.json is frozen as submitted to Make Waves. Refuse it
    in any checkout (worktrees included) before writing, not after."""
    path = _db(tmp_path, [("h1", DAY0, "Payment", USER_A, TAG)])
    monkeypatch.chdir(tmp_path)
    frozen = tmp_path / dest
    frozen.parent.mkdir(parents=True, exist_ok=True)
    frozen.write_text("frozen\n")
    rc = stm.main(["--network", "testnet", "--db", path, "--out", dest])
    assert rc == 2
    assert frozen.read_text() == "frozen\n"
    assert "frozen" in capsys.readouterr().err


def test_missing_db_exits_nonzero_without_writing(tmp_path):
    dest = tmp_path / "out.json"
    rc = stm.main(["--network", "testnet", "--db", str(tmp_path / "nope.db"), "--out", str(dest)])
    assert rc != 0
    assert not dest.exists()


def test_validate_payload_rejects_trailing_newline_via_dollar_sign():
    """`$` in re.match also matches just before a trailing '\\n' — must use
    fullmatch so a smuggled newline can't sneak a value past validation."""
    payload = _valid_payload(network="testnet\n")
    with pytest.raises(ValueError):
        stm.validate_payload(payload)

    payload = _valid_payload(first_tagged_tx="2026-07-20\n")
    with pytest.raises(ValueError):
        stm.validate_payload(payload)

    payload = _valid_payload(as_of="2026-07-22T00:20:00+00:00\n")
    with pytest.raises(ValueError):
        stm.validate_payload(payload)


def test_validate_payload_rejects_non_string_by_type_key():
    """A NULL tx_type becomes the dict key None; str(None) == 'None' passes a
    letters-only regex — the key itself must be a str, not coerced."""
    payload = _valid_payload(by_type={None: 3})
    with pytest.raises(ValueError, match="by_type"):
        stm.validate_payload(payload)


def test_historical_signer_stays_excluded_after_key_rotation(tmp_path, monkeypatch):
    """A previous backend signing address must stay excluded even after
    config.SIGNING_ACCOUNT rotates to a different address — rows archived
    under the old address must never reclassify as user activity."""
    historical = next(iter(stm.HISTORICAL_SIGNING_ADDRESSES))
    rotated = "rNewSignerAAAAAAAAAAAAAAAAAAAAAAAA"
    monkeypatch.setattr(config, "SIGNING_ACCOUNT", rotated)
    monkeypatch.setattr(stm.config, "SIGNING_ACCOUNT", rotated)

    excluded = stm.excluded_wallets()
    assert historical in excluded
    assert rotated in excluded

    path = _db(
        tmp_path,
        [
            ("h1", DAY0, "NFTokenMint", historical, TAG),
            ("h2", DAY0, "Payment", USER_A, TAG),
        ],
    )
    out = stm.collect(path, "testnet")
    # the historical signer's row counts toward total activity but must not
    # be counted as a unique external wallet, even post-rotation
    assert out["total_tagged_txs"] == 2
    assert out["unique_wallets"] == 1


def test_collect_reads_are_internally_consistent(tmp_path):
    """total_tagged_txs must always equal the sum of by_type and the sum of
    daily counts — the three are read inside one explicit transaction so a
    concurrent writer can never make them disagree.

    This fixture doesn't simulate a true concurrent writer thread (sqlite3
    connections aren't trivially shareable across threads against the same
    file in-process without extra plumbing); it asserts the invariant that
    property is supposed to guarantee.
    """
    path = _db(
        tmp_path,
        [
            ("h1", DAY0, "NFTokenMint", USER_A, TAG),
            ("h2", DAY0, "NFTokenAcceptOffer", USER_B, TAG),
            ("h3", DAY2, "NFTokenMint", USER_A, TAG),
        ],
    )
    out = stm.collect(path, "testnet")
    assert out["total_tagged_txs"] == sum(out["by_type"].values())
    assert out["total_tagged_txs"] == sum(d["count"] for d in out["daily"])


def test_collect_commits_transaction_so_connection_is_reusable(tmp_path):
    """collect() must leave no dangling transaction behind — a second read
    against the same underlying file must see fresh data, proving the BEGIN
    opened inside collect() was actually committed rather than left open."""
    path = _db(tmp_path, [("h1", DAY0, "Payment", USER_A, TAG)])
    first = stm.collect(path, "testnet")
    assert first["total_tagged_txs"] == 1

    import sqlite3 as _sqlite3

    conn = _sqlite3.connect(path)
    conn.execute(
        "INSERT INTO xrpl_txs (tx_hash, ledger_index, close_time, tx_type,"
        " account, source_tag, raw_json) VALUES ('h2',1,?,?,?,?,'{}')",
        (DAY0, "Payment", USER_B, TAG),
    )
    conn.commit()
    conn.close()

    second = stm.collect(path, "testnet")
    assert second["total_tagged_txs"] == 2


# ---------------------------------------------------------------------------
# XRP Payment volume, split by direction relative to the project's wallets.
# ---------------------------------------------------------------------------


def _pay(acct, dest, delivered, result="tesSUCCESS"):
    """raw_json for a Payment whose meta.delivered_amount is `delivered`."""
    return json.dumps(
        {
            "Account": acct,
            "Destination": dest,
            "TransactionType": "Payment",
            "meta": {"TransactionResult": result, "delivered_amount": delivered},
        }
    )


def _db_raw(tmp_path, rows):
    """rows: (hash, tx_type, account, source_tag, raw_json)"""
    path = str(tmp_path / "history_testnet.db")
    conn = history_store.init_history_db(path)
    conn.executemany(
        "INSERT INTO xrpl_txs (tx_hash, ledger_index, close_time, tx_type,"
        " account, source_tag, raw_json) VALUES (?,1,?,?,?,?,?)",
        [(h, DAY0, t, a, s, r) for (h, t, a, s, r) in rows],
    )
    conn.commit()
    conn.close()
    return path


def test_xrp_payment_volume_splits_in_out_other_and_ignores_iou(tmp_path):
    brix = {"currency": "BRIX", "issuer": OPERATOR, "value": "500"}
    db = _db_raw(
        tmp_path,
        [
            # user -> project: IN
            ("h1", "Payment", USER_A, TAG, _pay(USER_A, OPERATOR, "10000000")),
            ("h2", "Payment", USER_B, TAG, _pay(USER_B, OPERATOR, "2500000")),
            # project -> user: OUT
            ("h3", "Payment", OPERATOR, TAG, _pay(OPERATOR, USER_A, "3000000")),
            # user -> user: OTHER
            ("h4", "Payment", USER_A, TAG, _pay(USER_A, USER_B, "1000000")),
            # IOU payment: never counted
            ("h5", "Payment", USER_A, TAG, _pay(USER_A, OPERATOR, brix)),
            # failed XRP payment: never counted
            ("h6", "Payment", USER_A, TAG, _pay(USER_A, OPERATOR, "9000000", "tecNO_DST")),
            # untagged XRP payment: never counted
            ("h7", "Payment", USER_A, 1, _pay(USER_A, OPERATOR, "7000000")),
            # tagged non-Payment with an Amount-looking meta: never counted
            ("h8", "NFTokenAcceptOffer", USER_A, TAG, _pay(USER_A, OPERATOR, "8000000")),
        ],
    )
    out = stm.collect(db, "testnet")
    assert out["xrp_payment_volume"] == {
        "in_drops": 12_500_000,
        "out_drops": 3_000_000,
        "other_drops": 1_000_000,
    }
    stm.validate_payload(out)


def test_xrp_payment_volume_is_zero_with_no_payments(tmp_path):
    db = _db(tmp_path, [("h1", DAY0, "NFTokenMint", OPERATOR, TAG)])
    out = stm.collect(db, "testnet")
    assert out["xrp_payment_volume"] == {"in_drops": 0, "out_drops": 0, "other_drops": 0}


@pytest.mark.parametrize(
    "bad",
    [
        None,
        {"in_drops": 1, "out_drops": 2},  # missing key
        {"in_drops": 1, "out_drops": 2, "other_drops": 3, "extra": 4},
        {"in_drops": -1, "out_drops": 2, "other_drops": 3},
        {"in_drops": 1.5, "out_drops": 2, "other_drops": 3},
        {"in_drops": True, "out_drops": 2, "other_drops": 3},
        {"in_drops": "1", "out_drops": 2, "other_drops": 3},
    ],
)
def test_validate_payload_rejects_bad_xrp_payment_volume(bad):
    with pytest.raises(ValueError):
        stm.validate_payload(_valid_payload(xrp_payment_volume=bad))


def test_xrp_payment_volume_skips_non_numeric_delivered_amount(tmp_path):
    """A text `delivered_amount` that isn't a drops string (the historical
    ``"unavailable"`` sentinel on pre-2014 partial payments) must be skipped,
    not abort the nightly run."""
    db = _db_raw(
        tmp_path,
        [
            ("h1", "Payment", USER_A, TAG, _pay(USER_A, OPERATOR, "10000000")),
            ("h2", "Payment", USER_A, TAG, _pay(USER_A, OPERATOR, "unavailable")),
            ("h3", "Payment", OPERATOR, TAG, _pay(OPERATOR, USER_A, "-5")),
        ],
    )
    out = stm.collect(db, "testnet")
    assert out["xrp_payment_volume"] == {
        "in_drops": 10_000_000,
        "out_drops": 0,
        "other_drops": 0,
    }


# --- funder dedup (#490): unique_actors collapses one-operator wallet farms ---

EXCHANGE = next(iter(__import__("lfg_core.funding", fromlist=["x"]).EXCHANGES))
FARM_FUNDER = "rFarmFunderrrrrrrrrrrrrrrrrrrrrrrr"


def _app_db(tmp_path, rows):
    """rows: (wallet, funder|None). Builds an app DB with wallet_funders."""
    import sqlite3 as _sq

    from lfg_core import funding

    path = str(tmp_path / "lfg_nfts.db")
    conn = _sq.connect(path)
    funding.ensure_schema(conn)
    for wallet, funder in rows:
        funding.record_funder(conn, wallet, funder, 1)
    conn.commit()
    conn.close()
    return path


def test_dedup_by_funder_collapses_wallets_sharing_a_non_exchange_funder():
    wallets = ["rA", "rB", "rC"]
    funders = {"rA": FARM_FUNDER, "rB": FARM_FUNDER, "rC": "rOther"}
    assert stm.dedup_by_funder(wallets, funders) == 2


def test_dedup_by_funder_keeps_exchange_funded_wallets_separate():
    wallets = ["rA", "rB"]
    assert stm.dedup_by_funder(wallets, {"rA": EXCHANGE, "rB": EXCHANGE}) == 2


def test_dedup_by_funder_counts_wallets_with_no_funder_row_individually():
    assert stm.dedup_by_funder(["rA", "rB"], {}) == 2


def test_dedup_by_funder_folds_a_funder_that_is_itself_a_tagged_wallet():
    # The farm's parent wallet also transacted: parent + children are one actor.
    wallets = ["rParent", "rChild1", "rChild2"]
    funders = {"rChild1": "rParent", "rChild2": "rParent"}
    assert stm.dedup_by_funder(wallets, funders) == 1


def test_collect_reports_unique_actors_deduped_by_funder(tmp_path):
    path = _db(
        tmp_path,
        [
            ("h1", DAY0, "Payment", USER_A, TAG),
            ("h2", DAY0, "Payment", USER_B, TAG),
        ],
    )
    app = _app_db(tmp_path, [(USER_A, FARM_FUNDER), (USER_B, FARM_FUNDER)])

    out = stm.collect(path, "testnet", app_db=app)

    assert out["unique_wallets"] == 2
    assert out["unique_actors"] == 1


def test_collect_unique_actors_falls_open_to_raw_count_without_an_app_db(tmp_path):
    path = _db(tmp_path, [("h1", DAY0, "Payment", USER_A, TAG)])

    out = stm.collect(path, "testnet", app_db=str(tmp_path / "nope.db"))

    assert out["unique_actors"] == out["unique_wallets"] == 1


def test_validate_payload_requires_unique_actors_to_be_an_int():
    with pytest.raises(ValueError):
        stm.validate_payload(_valid_payload(unique_actors="1"))


def test_load_funders_returns_empty_when_the_table_does_not_exist(tmp_path):
    import sqlite3 as _sq

    path = str(tmp_path / "empty.db")
    _sq.connect(path).close()
    assert stm.load_funders(path) == {}


def test_load_funders_surfaces_a_corrupt_db_instead_of_silently_deduping_nothing(tmp_path):
    """A corrupt/unreadable app DB must NOT look like 'no funder coverage' —
    that would quietly publish unique_actors == unique_wallets with no signal."""
    import sqlite3 as _sq

    path = tmp_path / "corrupt.db"
    path.write_bytes(b"SQLite format 3\x00" + b"\x00" * 200)
    with pytest.raises(stm.AppDBError) as exc:
        stm.load_funders(str(path))
    assert exc.value.path == str(path)
    assert isinstance(exc.value.__cause__, _sq.Error)


def test_main_exits_2_when_the_app_db_is_unreadable(tmp_path, monkeypatch, capsys):
    history = _db(tmp_path, [("h1", DAY0, "Payment", USER_A, TAG)])
    bad = tmp_path / "corrupt.db"
    bad.write_bytes(b"SQLite format 3\x00" + b"\x00" * 200)
    monkeypatch.chdir(tmp_path)

    rc = stm.main(["--network", "testnet", "--db", history, "--app-db", str(bad)])

    assert rc == 2
    err = capsys.readouterr().err
    # the diagnostic must name the APP db, not the (perfectly fine) archive
    assert str(bad) in err
    assert history not in err
    assert not (tmp_path / "metrics" / "sourcetag.json").exists()
