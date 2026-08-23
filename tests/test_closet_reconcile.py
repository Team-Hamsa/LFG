# Detection + repair of Closet ownership anomalies (#383): a closet_tokens row
# keyed to a project signing account, or one Closet NFToken claimed by two
# owners. The write path that made them is fixed in nft_listener; these cover
# the audit check and the one-off reconciler that repair databases already
# carrying the damage.

import importlib
import os
import sqlite3
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "scripts"))

from lfg_core import closet_reconcile as cr  # noqa: E402
from lfg_core import closet_token as ct  # noqa: E402
from lfg_core import config  # noqa: E402
from lfg_core import economy_store as es  # noqa: E402

reconcile = importlib.import_module("reconcile_closet_tokens")

ISSUER = config.SWAP_ISSUER_ADDRESS
USER = "rET8NWdfFwoyqxDeoyq1RUaeqDKVA3m6Du"
OTHER = "rOtherUserWalletAddressForTests11"


def _seed(conn, owner, nft_id, status=ct.PENDING_ACCEPT):
    """Insert a closet_tokens row bypassing the store guard — these rows are
    exactly what the guard now prevents, so they can only be reproduced raw."""
    conn.execute(
        "INSERT INTO closet_tokens (owner, nft_id, uri_hex, status) VALUES (?,?,?,?)",
        (owner, nft_id, "AB", status),
    )
    conn.commit()


def _conn(path=":memory:") -> sqlite3.Connection:
    c = sqlite3.connect(path)
    es.init_economy_schema(c)
    # A DB that already carries the damage could never have taken the index.
    c.execute("DROP INDEX IF EXISTS idx_closet_tokens_nft_id")
    return c


# --- detection --------------------------------------------------------------


def test_clean_database_reports_ok():
    c = _conn()
    _seed(c, USER, "N1", ct.ACTIVE)
    report = cr.audit_closet_ownership(c)
    assert report.ok
    assert report.project_rows == []
    assert report.duplicate_tokens == {}


def test_issuer_keyed_row_is_flagged():
    c = _conn()
    _seed(c, ISSUER, "N1")
    report = cr.audit_closet_ownership(c)
    assert not report.ok
    assert [r.owner for r in report.project_rows] == [ISSUER]
    assert report.project_rows[0].nft_id == "N1"


def test_duplicate_token_is_flagged_and_classified_resolvable():
    """The #383 shape: the real user's row plus an issuer row on the same token.
    Deleting the issuer row resolves it, so it is not an unresolved duplicate."""
    c = _conn()
    _seed(c, USER, "N1", ct.PENDING_ACCEPT)
    _seed(c, ISSUER, "N1")
    report = cr.audit_closet_ownership(c)
    assert not report.ok
    assert report.duplicate_tokens == {"N1": sorted([USER, ISSUER])}
    assert report.unresolved_duplicates == {}


def test_user_to_user_duplicate_is_unresolved():
    """No project row to delete — a Closet is soulbound, so exactly one of these
    is real and only clio can say which. Never guessed at."""
    c = _conn()
    _seed(c, USER, "N1", ct.ACTIVE)
    _seed(c, OTHER, "N1", ct.ACTIVE)
    report = cr.audit_closet_ownership(c)
    assert report.unresolved_duplicates == {"N1": sorted([USER, OTHER])}


# --- repair -----------------------------------------------------------------


def test_repair_deletes_the_project_row_and_its_loose_assets():
    c = _conn()
    _seed(c, USER, "N1", ct.PENDING_ACCEPT)
    _seed(c, ISSUER, "N1")
    c.execute(
        "INSERT INTO closet_assets (owner, slot, value, count) VALUES (?,?,?,?)",
        (ISSUER, "Hat", "Cap", 1),
    )
    c.commit()

    assert cr.repair_closet_ownership(c, cr.audit_closet_ownership(c)) == 1

    assert cr.audit_closet_ownership(c).ok
    assert es.get_closet_record(c, ISSUER) is None
    assert [a for a in es.read_closet_assets(c) if a[0] == ISSUER] == []
    # The real owner is untouched.
    assert es.get_closet_record(c, USER) is not None


def test_repair_leaves_user_to_user_duplicates_alone():
    c = _conn()
    _seed(c, USER, "N1", ct.ACTIVE)
    _seed(c, OTHER, "N1", ct.ACTIVE)
    assert cr.repair_closet_ownership(c, cr.audit_closet_ownership(c)) == 0
    assert es.get_closet_record(c, USER) is not None
    assert es.get_closet_record(c, OTHER) is not None


# --- CLI --------------------------------------------------------------------


def _db(tmp_path, monkeypatch, rows):
    db = str(tmp_path / "onchain_testnet.db")
    c = _conn(db)
    for owner, nft_id in rows:
        _seed(c, owner, nft_id)
    c.close()
    monkeypatch.setattr(reconcile.nft_index, "index_db_path", lambda _net: db)
    monkeypatch.setattr(reconcile.config, "XRPL_NETWORK", "testnet")
    return db


def _run(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["reconcile_closet_tokens.py", *argv])
    return reconcile.main()


def test_cli_dry_run_reports_but_writes_nothing(tmp_path, monkeypatch, capsys):
    db = _db(tmp_path, monkeypatch, [(USER, "N1"), (ISSUER, "N1")])
    before = open(db, "rb").read()

    assert _run(monkeypatch, "--network", "testnet") == 1

    out = capsys.readouterr().out
    assert "would delete (dry-run; pass --apply)" in out
    assert ISSUER in out
    assert open(db, "rb").read() == before  # byte-identical: nothing written


def test_cli_apply_repairs_and_takes_the_unique_index(tmp_path, monkeypatch, capsys):
    db = _db(tmp_path, monkeypatch, [(USER, "N1"), (ISSUER, "N1")])

    assert _run(monkeypatch, "--network", "testnet", "--apply") == 0

    assert "Post-repair state: OK" in capsys.readouterr().out
    c = sqlite3.connect(db)
    assert es.get_closet_record(c, ISSUER) is None
    assert es.get_closet_record(c, USER) is not None
    assert (
        c.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_closet_tokens_nft_id'"
        ).fetchone()
        is not None
    )


def test_cli_apply_on_a_clean_db_changes_nothing_and_exits_zero(tmp_path, monkeypatch, capsys):
    """The expected first mainnet run: the anomaly is already gone, so the
    reconciler must be a quiet no-op rather than claim a repair."""
    _db(tmp_path, monkeypatch, [(USER, "N1")])
    assert _run(monkeypatch, "--network", "testnet", "--apply") == 0
    out = capsys.readouterr().out
    assert "Closets keyed to a project account: none" in out
    assert "Scrubbed" not in out


def test_cli_unresolved_duplicate_exits_nonzero_without_deleting(tmp_path, monkeypatch, capsys):
    db = _db(tmp_path, monkeypatch, [(USER, "N1"), (OTHER, "N1")])
    assert _run(monkeypatch, "--network", "testnet", "--apply") == 1
    assert "NOT repaired here" in capsys.readouterr().out
    c = sqlite3.connect(db)
    assert es.get_closet_record(c, USER) is not None
    assert es.get_closet_record(c, OTHER) is not None


def test_cli_refuses_a_network_mismatch_before_touching_the_db(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(reconcile.config, "XRPL_NETWORK", "testnet")

    def _boom(_net):
        raise AssertionError("must refuse before resolving a DB path")

    monkeypatch.setattr(reconcile.nft_index, "index_db_path", _boom)
    assert _run(monkeypatch, "--network", "mainnet", "--apply") == 2
    assert "refusing" in capsys.readouterr().err


def test_cli_missing_db_returns_2(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(reconcile.config, "XRPL_NETWORK", "testnet")
    monkeypatch.setattr(
        reconcile.nft_index, "index_db_path", lambda _net: str(tmp_path / "nope.db")
    )
    assert _run(monkeypatch, "--network", "testnet") == 2
    assert "index DB not found" in capsys.readouterr().err
