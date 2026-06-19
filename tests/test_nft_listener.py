# Tests for lfg_core/nft_listener.py
import asyncio
import os
import sys

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

from lfg_core import nft_index, nft_listener, xrpl_ops  # noqa: E402


def test_parse_nft_info():
    result = {
        "nft_id": "ABC",
        "owner": "rOwner",
        "is_burned": False,
        "flags": 16,
        "uri": "6868",
    }
    parsed = xrpl_ops._parse_nft_info(result)
    assert parsed == {
        "nft_id": "ABC",
        "owner": "rOwner",
        "flags": 16,
        "uri_hex": "6868",
        "is_burned": False,
    }


MINT = {"TransactionType": "NFTokenMint", "meta": {"nftoken_id": "MINTED"}}
ACCEPT = {"TransactionType": "NFTokenAcceptOffer", "meta": {"nftoken_id": "MOVED"}}
BURN = {"TransactionType": "NFTokenBurn", "NFTokenID": "GONE", "meta": {}}
MODIFY = {"TransactionType": "NFTokenModify", "NFTokenID": "CHANGED", "meta": {}}
PAYMENT = {"TransactionType": "Payment", "meta": {}}


def test_classify_tx():
    assert nft_listener.classify_tx(MINT) == "mint"
    assert nft_listener.classify_tx(ACCEPT) == "accept"
    assert nft_listener.classify_tx(BURN) == "burn"
    assert nft_listener.classify_tx(MODIFY) == "modify"
    assert nft_listener.classify_tx(PAYMENT) is None


def test_affected_nft_ids():
    assert nft_listener.affected_nft_ids(MINT) == ["MINTED"]
    assert nft_listener.affected_nft_ids(ACCEPT) == ["MOVED"]
    assert nft_listener.affected_nft_ids(BURN) == ["GONE"]
    assert nft_listener.affected_nft_ids(MODIFY) == ["CHANGED"]
    assert nft_listener.affected_nft_ids(PAYMENT) == []


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_apply_tx_mint_accept_burn_modify(tmp_path):
    conn = nft_index.init_db(str(tmp_path / "idx.db"))

    # fetch_token_fn resolves nft_info-shaped data; fetch_meta_fn resolves metadata.
    token_state = {
        "MINTED": {
            "nft_id": "MINTED",
            "owner": "rMinter",
            "flags": 0x10,
            "uri_hex": "aa",
            "is_burned": False,
        },
        "MOVED": {
            "nft_id": "MOVED",
            "owner": "rNewOwner",
            "flags": 0x10,
            "uri_hex": "bb",
            "is_burned": False,
        },
        "CHANGED": {
            "nft_id": "CHANGED",
            "owner": "rO",
            "flags": 0x10,
            "uri_hex": "cc2",
            "is_burned": False,
        },
    }
    meta = {
        "aa": {"name": "#1", "attributes": [{"trait_type": "Clothing", "value": "Hoodie"}]},
        "bb": {"name": "#2", "attributes": []},
        "cc2": {"name": "#3", "attributes": [{"trait_type": "Clothing", "value": "Wonder"}]},
    }

    async def fetch_token(nft_id):
        return token_state.get(nft_id)

    async def fetch_meta(uri_hex):
        return meta.get(uri_hex)

    # Pre-seed CHANGED with old metadata, then a modify re-fetches.
    nft_index.upsert(
        conn,
        nft_index.token_record(
            {"nft_id": "CHANGED", "flags": 0x10, "uri_hex": "cc1", "is_burned": False},
            {"name": "#3", "attributes": [{"trait_type": "Clothing", "value": "Old"}]},
        ),
    )

    _run(nft_listener.apply_tx(conn, MINT, fetch_token, fetch_meta))
    _run(nft_listener.apply_tx(conn, ACCEPT, fetch_token, fetch_meta))
    _run(nft_listener.apply_tx(conn, MODIFY, fetch_token, fetch_meta))

    # pre-seed the burn target so burn flips a real row
    nft_index.upsert(
        conn,
        nft_index.token_record(
            {"nft_id": "GONE", "flags": 0, "uri_hex": "z", "is_burned": False}, {"name": "#9"}
        ),
    )
    _run(nft_listener.apply_tx(conn, BURN, fetch_token, fetch_meta))

    conn.row_factory = __import__("sqlite3").Row
    rows = {r["nft_id"]: r for r in conn.execute("SELECT * FROM onchain_nfts")}
    assert rows["MINTED"]["owner"] == "rMinter"
    assert rows["MOVED"]["owner"] == "rNewOwner"
    assert rows["GONE"]["is_burned"] == 1
    # modify re-fetched: Old -> Wonder
    import json

    changed_attrs = [a["value"] for a in json.loads(rows["CHANGED"]["attributes_json"])]
    assert "Wonder" in changed_attrs and "Old" not in changed_attrs
