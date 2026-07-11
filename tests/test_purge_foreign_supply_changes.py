# Tests for scripts/purge_foreign_supply_changes.py: it must flag ONLY
# supply_changes rows whose nft_id embeds a foreign issuer, keep our-issuer
# rows and reason-without-nft_id rows, and leave the DB untouched on a dry run.

import importlib
import os
import sqlite3
import sys

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

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "scripts"))

from lfg_core import economy_store as es  # noqa: E402
from lfg_core import nft_listener  # noqa: E402

purge = importlib.import_module("purge_foreign_supply_changes")


def _nft_id(issuer_hex: str) -> str:
    """A 64-hex NFTokenID embedding `issuer_hex` (40 hex) at chars 8..48."""
    assert len(issuer_hex) == 40
    return "00080000" + issuer_hex + "0000000000000001"


def _record(conn: sqlite3.Connection, reason: str) -> None:
    es.record_supply_change(
        conn, "mint", 1, "Straight Blue", "male", {"Head|None": 1}, "listener", reason
    )


def test_classify_splits_foreign_ours_and_unparseable():
    conn = sqlite3.connect(":memory:")
    es.init_economy_schema(conn)

    ours_hex = nft_listener._issuer_account_hex()
    foreign_hex = "AB" * 20  # 40 hex, not our issuer
    ours_id = _nft_id(ours_hex)
    foreign_id = _nft_id(foreign_hex)

    _record(conn, f"new-edition mint {ours_id}")
    _record(conn, f"new-edition mint {foreign_id}")
    _record(conn, "genesis freeze; no nft_id here")

    foreign, ours, unparseable = purge._classify_rows(conn, ours_hex)
    assert [r[1] for r in ours] == [ours_id]
    assert [r[1] for r in foreign] == [foreign_id]
    assert len(unparseable) == 1


def test_dry_run_deletes_nothing_apply_deletes_only_foreign(tmp_path, monkeypatch):
    db_path = str(tmp_path / "onchain_testnet.db")
    conn = sqlite3.connect(db_path)
    es.init_economy_schema(conn)
    ours_hex = nft_listener._issuer_account_hex()
    ours_id = _nft_id(ours_hex)
    foreign_id = _nft_id("CD" * 20)
    _record(conn, f"new-edition mint {ours_id}")
    _record(conn, f"new-edition mint {foreign_id}")
    conn.commit()
    conn.close()

    monkeypatch.setenv("ONCHAIN_DB_PATH", db_path)

    # Dry run: no deletion.
    monkeypatch.setattr(sys, "argv", ["purge", "--network", "testnet"])
    assert purge.main() == 0
    conn = sqlite3.connect(db_path)
    assert len(es.read_supply_changes(conn)) == 2
    conn.close()

    # Apply: only the foreign row is deleted.
    monkeypatch.setattr(sys, "argv", ["purge", "--network", "testnet", "--apply"])
    assert purge.main() == 0
    conn = sqlite3.connect(db_path)
    rows = es.read_supply_changes(conn)
    assert len(rows) == 1
    assert ours_id in rows[0]["reason"]
    conn.close()


def test_missing_db_returns_2(monkeypatch, tmp_path):
    monkeypatch.setenv("ONCHAIN_DB_PATH", str(tmp_path / "does_not_exist.db"))
    # --network must match ambient XRPL_NETWORK (testnet here) to reach the DB check.
    monkeypatch.setattr(sys, "argv", ["purge", "--network", "testnet"])
    assert purge.main() == 2


def test_network_mismatch_refuses_before_touching_db(monkeypatch, capsys):
    # Ambient XRPL_NETWORK is testnet; asking to purge mainnet would classify with
    # the wrong issuer and wipe the whole table, so it must refuse with exit 2.
    monkeypatch.setattr(sys, "argv", ["purge", "--network", "mainnet", "--apply"])
    assert purge.main() == 2
    assert "refusing" in capsys.readouterr().err
