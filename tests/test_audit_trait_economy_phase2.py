# scripts/audit_trait_economy.py end-to-end: a supply_changes ledger row lets a
# legitimately-grown edition pass conservation; remove it and it reads as drift.

import os
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

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "scripts"))

import audit_trait_economy as ate  # noqa: E402

from lfg_core import economy_store as es  # noqa: E402
from lfg_core import nft_index, trait_economy  # noqa: E402

NON_BODY = trait_economy.NON_BODY_SLOTS


def _char(edition: int) -> nft_index.OnchainNft:
    attrs = [{"trait_type": "Body", "value": "Straight Blue"}]
    attrs += [{"trait_type": s, "value": "None"} for s in NON_BODY]
    return nft_index.OnchainNft(
        nft_id=f"NFT{edition}",
        nft_number=edition,
        owner="rUser",
        is_burned=False,
        mutable=True,
        uri_hex="",
        body="male",
        attributes=attrs,
        image="",
        ledger_index=edition,
    )


def _setup_db(path: str) -> None:
    conn = nft_index.init_db(path)
    es.init_economy_schema(conn)
    # Live characters: edition 1 (genesis) + edition 3536 (grown via ledger).
    nft_index.upsert(conn, _char(1))
    nft_index.upsert(conn, _char(3536))
    genesis = trait_economy.Genesis(
        trait_counts={(s, "None"): 1 for s in NON_BODY},
        edition_bodies={1: ("Straight Blue", "male")},
    )
    es.freeze_genesis(conn, genesis, {"max_edition": "3535"})
    es.record_supply_change(
        conn,
        "mint",
        3536,
        "Straight Blue",
        "male",
        {f"{s}|None": 1 for s in NON_BODY},
        "test",
        "grew beyond 3535",
    )
    conn.close()


def _run_audit(tmp_path, monkeypatch) -> int:
    db = str(tmp_path / "onchain_testnet.db")
    monkeypatch.setenv("ONCHAIN_DB_PATH", db)
    if not os.path.isfile(db):
        _setup_db(db)
    monkeypatch.setattr(
        sys,
        "argv",
        ["audit_trait_economy.py", "--network", "testnet", "--report-dir", str(tmp_path / "r")],
    )
    return ate.main()


def test_logged_growth_passes(tmp_path, monkeypatch):
    assert _run_audit(tmp_path, monkeypatch) == 0  # ledger explains edition 3536


def test_unlogged_growth_is_drift(tmp_path, monkeypatch):
    db = str(tmp_path / "onchain_testnet.db")
    _setup_db(db)
    conn = nft_index.init_db(db)
    conn.execute("DELETE FROM supply_changes")
    conn.commit()
    conn.close()
    assert _run_audit(tmp_path, monkeypatch) == 1  # now edition 3536 is unexplained drift
