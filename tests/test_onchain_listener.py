# Listen-path integration: the per-tx seam drives BOTH the index update and the
# trait-economy apply (supply-growth logging + bucket rebuild), reading the
# EFFECTIVE genesis from the DB each tx so re-mints are idempotent. This is the
# wiring #68 added — apply_economy_tx had unit tests but no production caller.

import asyncio
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

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "scripts"))

import onchain_listener as oln  # noqa: E402

from lfg_core import closet_token as bt  # noqa: E402
from lfg_core import config, nft_index  # noqa: E402
from lfg_core import economy_store as es  # noqa: E402
from lfg_core import trait_economy as te  # noqa: E402

NON_BODY = te.NON_BODY_SLOTS


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _conn() -> sqlite3.Connection:
    """The live listener's DB: index + economy schemas share one file."""
    c = sqlite3.connect(":memory:")
    c.executescript(nft_index._SCHEMA)
    es.init_economy_schema(c)
    return c


def _freeze(conn, edition_bodies=None):
    genesis = te.Genesis(trait_counts={}, edition_bodies=edition_bodies or {})
    es.freeze_genesis(conn, genesis, {"network": "testnet"})


def _char_meta(edition: int, body: str = "Straight Blue") -> dict:
    attrs = [{"trait_type": "Body", "value": body}]
    attrs += [{"trait_type": s, "value": "None"} for s in NON_BODY]
    return {"name": f"LFG #{edition}", "attributes": attrs}


def _char_token():
    return {"nft_id": "CHAR", "owner": "rUser", "taxon": config.SWAP_TAXON, "uri_hex": "CD"}


async def _fetch_char_token(nft_id):
    return _char_token()


def _mint_tx():
    return {
        "TransactionType": "NFTokenMint",
        "Issuer": config.SWAP_ISSUER_ADDRESS,
        "meta": {"nftoken_id": "CHAR"},
    }


def _is_ours(_token):
    return True


def test_listen_path_logs_growth_for_unknown_edition():
    conn = _conn()
    _freeze(conn)  # edition 3536 unknown

    async def fetch_meta(uri_hex):
        return _char_meta(3536)

    _run(
        oln.process_stream_tx(
            conn,
            _mint_tx(),
            fetch_token=_fetch_char_token,
            fetch_meta=fetch_meta,
            is_ours=_is_ours,
        )
    )
    rows = es.read_supply_changes(conn)
    assert len(rows) == 1
    assert rows[0]["kind"] == "mint" and rows[0]["edition"] == 3536


def test_listen_path_growth_is_idempotent_across_remints():
    """Re-reading EFFECTIVE genesis each tx means a second mint of the same new
    edition is recognised and logs nothing — the whole point of passing the
    folded genesis, not the frozen baseline."""
    conn = _conn()
    _freeze(conn)

    async def fetch_meta(uri_hex):
        return _char_meta(3536)

    for _ in range(2):
        _run(
            oln.process_stream_tx(
                conn,
                _mint_tx(),
                fetch_token=_fetch_char_token,
                fetch_meta=fetch_meta,
                is_ours=_is_ours,
            )
        )
    assert len(es.read_supply_changes(conn)) == 1


def test_listen_path_known_edition_logs_nothing():
    conn = _conn()
    _freeze(conn, {7: ("Straight Blue", "male")})

    async def fetch_meta(uri_hex):
        return _char_meta(7)

    tx = {
        "TransactionType": "NFTokenMint",
        "Issuer": config.SWAP_ISSUER_ADDRESS,
        "meta": {"nftoken_id": "CHAR"},
    }
    _run(
        oln.process_stream_tx(
            conn,
            tx,
            fetch_token=_fetch_char_token,
            fetch_meta=fetch_meta,
            is_ours=_is_ours,
        )
    )
    assert es.read_supply_changes(conn) == []


def test_listen_path_skips_economy_when_genesis_unfrozen():
    """No frozen genesis → every mint would look 'unknown'. Gate on
    genesis_exists so the index still updates but no spurious growth is logged."""
    conn = _conn()  # genesis NOT frozen

    async def fetch_meta(uri_hex):
        return _char_meta(3536)

    _run(
        oln.process_stream_tx(
            conn,
            _mint_tx(),
            fetch_token=_fetch_char_token,
            fetch_meta=fetch_meta,
            is_ours=_is_ours,
        )
    )
    assert es.read_supply_changes(conn) == []


def test_listen_path_fetches_token_and_meta_once_per_mint():
    """apply_tx and apply_economy_tx both resolve the same token/metadata; the
    per-tx memo caches must collapse that to a single clio + IPFS round-trip."""
    conn = _conn()
    _freeze(conn)
    token_calls = {"n": 0}
    meta_calls = {"n": 0}

    async def fetch_token(nft_id):
        token_calls["n"] += 1
        return _char_token()

    async def fetch_meta(uri_hex):
        meta_calls["n"] += 1
        return _char_meta(3536)

    _run(
        oln.process_stream_tx(
            conn,
            _mint_tx(),
            fetch_token=fetch_token,
            fetch_meta=fetch_meta,
            is_ours=_is_ours,
        )
    )
    assert token_calls["n"] == 1
    assert meta_calls["n"] == 1


def test_listen_path_rebuilds_bucket_from_modify():
    conn = _conn()
    _freeze(conn)
    meta = bt.build_closet_metadata("rUser", [("Head", "None", 2), ("Eyes", "Blue", 1)], [3536])

    async def fetch_token(nft_id):
        return {"nft_id": "CLOSET", "owner": "rUser", "taxon": config.CLOSET_TAXON, "uri_hex": "AB"}

    async def fetch_meta(uri_hex):
        return meta

    tx = {"TransactionType": "NFTokenModify", "NFTokenID": "CLOSET"}
    _run(
        oln.process_stream_tx(
            conn,
            tx,
            fetch_token=fetch_token,
            fetch_meta=fetch_meta,
            is_ours=_is_ours,
        )
    )
    assets = {(s, v): n for o, s, v, n in es.read_closet_assets(conn)}
    assert assets == {("Head", "None"): 2, ("Eyes", "Blue"): 1}
    assert es.read_closet_bodies(conn) == [("rUser", 3536)]
