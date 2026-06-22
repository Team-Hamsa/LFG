# Tests for scripts/backfill_onchain.py
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

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
)
import backfill_onchain as bf  # noqa: E402

from lfg_core import nft_index  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_backfill_keeps_duplicates_burned_and_unreadable(tmp_path):
    conn = nft_index.init_db(str(tmp_path / "idx.db"))
    # Two tokens share edition #3547 (the duplicate the edition-keyed DB can't hold),
    # one burned token, one whose metadata won't fetch.
    tokens = [
        {"nft_id": "ID_CLEAN", "owner": "rA", "is_burned": False, "flags": 0x10, "uri_hex": "aa"},
        {"nft_id": "ID_WONDER", "owner": "rB", "is_burned": False, "flags": 0x10, "uri_hex": "bb"},
        {"nft_id": "ID_BURNED", "owner": "rC", "is_burned": True, "flags": 0, "uri_hex": "cc"},
        {"nft_id": "ID_NOMETA", "owner": "rD", "is_burned": False, "flags": 0, "uri_hex": "dd"},
    ]
    meta = {
        "aa": {
            "name": "#3547",
            "attributes": [{"trait_type": "Clothing", "value": "Crop Hoodie Pink"}],
        },
        "bb": {"name": "#3547", "attributes": [{"trait_type": "Clothing", "value": "Wonder"}]},
        "cc": {"name": "#10", "attributes": []},
    }

    async def enum():
        return tokens

    async def fetch(uri_hex):
        return meta.get(uri_hex)

    counts = _run(bf.run_backfill(conn, enum, fetch))
    assert counts == {"total": 4, "with_metadata": 3, "unreadable": 1}

    rows = {
        r[0]: r
        for r in conn.execute(
            "SELECT nft_id, nft_number, is_burned, attributes_json FROM onchain_nfts"
        )
    }
    # both #3547 variants kept as separate rows
    assert rows["ID_CLEAN"][1] == 3547 and rows["ID_WONDER"][1] == 3547
    assert rows["ID_BURNED"][2] == 1
    assert rows["ID_NOMETA"][3] == "[]"  # recorded, not dropped

    # idempotent re-run: row count stable
    _run(bf.run_backfill(conn, enum, fetch))
    assert conn.execute("SELECT COUNT(*) FROM onchain_nfts").fetchone()[0] == 4


def test_retry_unreadable_recovers_then_stops(tmp_path):
    conn = nft_index.init_db(str(tmp_path / "idx.db"))
    # Seed one unreadable token (empty attrs, has uri).
    nft_index.upsert(
        conn,
        nft_index.token_record(
            {"nft_id": "ID_X", "flags": 0x10, "uri_hex": "dd", "is_burned": False}, None
        ),
    )
    assert len(nft_index.retryable_unreadable(conn)) == 1

    calls = {"n": 0}

    async def fetch_first_fails(uri_hex):
        # Fails on pass 1, succeeds on pass 2 -> exercises the loop.
        calls["n"] += 1
        if calls["n"] < 2:
            return None
        return {"name": "#5", "attributes": [{"trait_type": "Clothing", "value": "Hoodie"}]}

    counts = _run(bf.retry_unreadable(conn, fetch_first_fails, max_passes=5))
    assert counts["recovered"] == 1
    assert nft_index.retryable_unreadable(conn) == []  # nothing left to retry
