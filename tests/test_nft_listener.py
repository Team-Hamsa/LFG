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

from lfg_core import economy_store, nft_index, nft_listener, xrpl_ops  # noqa: E402


def test_parse_nft_info():
    result = {
        "nft_id": "ABC",
        "owner": "rOwner",
        "is_burned": False,
        "flags": 16,
        "uri": "6868",
        "issuer": "rIssuer",
        "nft_taxon": 1760,
    }
    parsed = xrpl_ops._parse_nft_info(result)
    assert parsed == {
        "nft_id": "ABC",
        "owner": "rOwner",
        "flags": 16,
        "uri_hex": "6868",
        "is_burned": False,
        "issuer": "rIssuer",
        "taxon": 1760,
    }


_TES = {"TransactionResult": "tesSUCCESS"}
MINT = {"TransactionType": "NFTokenMint", "meta": {"nftoken_id": "MINTED", **_TES}}
ACCEPT = {"TransactionType": "NFTokenAcceptOffer", "meta": {"nftoken_id": "MOVED", **_TES}}
BURN = {"TransactionType": "NFTokenBurn", "NFTokenID": "GONE", "meta": {**_TES}}
MODIFY = {"TransactionType": "NFTokenModify", "NFTokenID": "CHANGED", "meta": {**_TES}}
PAYMENT = {"TransactionType": "Payment", "meta": {**_TES}}
# tec-class: ledger-included but the burn did NOT happen (#210).
BURN_TEC = {
    "TransactionType": "NFTokenBurn",
    "NFTokenID": "GONE",
    "meta": {"TransactionResult": "tecNO_ENTRY"},
}


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

    # apply_tx must also warm the uri metadata cache with what it fetched:
    # the local-first roster serves cache hits with the token's REAL metadata
    # (incl. burnCount for swap outputs) and only synthesizes on a miss, so a
    # freshly minted/modified token should be cached by the time it's browsed.
    cached = nft_index.meta_cache_get_many(conn, ["aa", "bb", "cc2"])
    assert cached["aa"]["name"] == "#1"
    assert cached["cc2"]["attributes"][0]["value"] == "Wonder"


def test_apply_tx_skips_foreign_collection(tmp_path):
    # accept/burn/modify arrive for ALL NFTs on the network; is_ours scopes upserts.
    conn = nft_index.init_db(str(tmp_path / "idx.db"))

    async def fetch_token(nft_id):
        return {"nft_id": nft_id, "owner": "rX", "flags": 0, "uri_hex": "", "issuer": "rOTHER"}

    async def fetch_meta(uri_hex):
        return None

    foreign = {
        "TransactionType": "NFTokenAcceptOffer",
        "meta": {"nftoken_id": "FOREIGN", "TransactionResult": "tesSUCCESS"},
    }
    _run(
        nft_listener.apply_tx(
            conn, foreign, fetch_token, fetch_meta, is_ours=lambda t: t.get("issuer") == "rMINE"
        )
    )
    assert conn.execute("SELECT COUNT(*) FROM onchain_nfts").fetchone()[0] == 0


def test_burn_ignores_unknown_token(tmp_path):
    conn = nft_index.init_db(str(tmp_path / "idx.db"))

    async def fetch_token(nft_id):
        return None

    async def fetch_meta(uri_hex):
        return None

    _run(nft_listener.apply_tx(conn, BURN, fetch_token, fetch_meta))
    # GONE was never in the index -> burn adds nothing
    assert conn.execute("SELECT COUNT(*) FROM onchain_nfts").fetchone()[0] == 0


def test_tec_burn_does_not_flip_is_burned(tmp_path):
    """#210: a tec-class NFTokenBurn is ledger-included but performed nothing —
    apply_tx must not mark the (still-live) token burned."""
    conn = nft_index.init_db(str(tmp_path / "idx.db"))
    nft_index.upsert(
        conn,
        nft_index.token_record(
            {"nft_id": "GONE", "flags": 0, "uri_hex": "z", "is_burned": False}, {"name": "#9"}
        ),
    )

    async def fetch_token(nft_id):
        raise AssertionError("a failed tx must not be resolved at all")

    _run(nft_listener.apply_tx(conn, BURN_TEC, fetch_token, fetch_token))
    row = conn.execute("SELECT is_burned FROM onchain_nfts WHERE nft_id='GONE'").fetchone()
    assert row[0] == 0


def test_tec_missing_result_is_not_success(tmp_path):
    """Strict gate: no meta / no TransactionResult is not provably successful."""
    assert not nft_listener.tx_succeeded({"TransactionType": "NFTokenBurn", "meta": {}})
    assert not nft_listener.tx_succeeded({"TransactionType": "NFTokenBurn"})
    assert nft_listener.tx_succeeded(BURN)

    conn = nft_index.init_db(str(tmp_path / "idx.db"))
    nft_index.upsert(
        conn,
        nft_index.token_record(
            {"nft_id": "GONE", "flags": 0, "uri_hex": "z", "is_burned": False}, {"name": "#9"}
        ),
    )
    no_result = {"TransactionType": "NFTokenBurn", "NFTokenID": "GONE", "meta": {}}

    async def fetch_none(_):
        return None

    _run(nft_listener.apply_tx(conn, no_result, fetch_none, fetch_none))
    row = conn.execute("SELECT is_burned FROM onchain_nfts WHERE nft_id='GONE'").fetchone()
    assert row[0] == 0


def test_tec_burn_does_not_delete_trait_token(tmp_path):
    """A tec burn of a trait token must leave its trait_tokens row alone —
    the token is still live on-ledger."""
    conn = nft_index.init_db(str(tmp_path / "idx.db"))
    economy_store.init_economy_schema(conn)
    economy_store.upsert_trait_token(conn, "GONE", "rUser", "Hat", "Cap")

    async def fetch_none(_):
        return None

    _run(
        nft_listener.apply_economy_tx(
            conn, BURN_TEC, fetch_token_fn=fetch_none, fetch_meta_fn=fetch_none
        )
    )
    assert economy_store.read_trait_tokens(conn) == [("GONE", "rUser", "Hat", "Cap")]
