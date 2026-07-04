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
from lfg_core import config, nft_index, trait_token  # noqa: E402
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


def test_listen_path_accept_closet_promotes_pending_to_active():
    """NFTokenAcceptOffer for a CLOSET_TAXON token must reach the economy handler
    via process_stream_tx (C1 fix) and promote the record to ACTIVE. Before the
    fix the accept kind was filtered out, leaving the DB record as None."""
    conn = _conn()
    _freeze(conn)
    meta = bt.build_closet_metadata("rUser", [], [])

    async def fetch_token(nft_id):
        return {
            "nft_id": "CLOSET_ACC",
            "owner": "rUser",
            "taxon": config.CLOSET_TAXON,
            "uri_hex": "EF",
        }

    async def fetch_meta(uri_hex):
        return meta

    tx = {"TransactionType": "NFTokenAcceptOffer", "meta": {"nftoken_id": "CLOSET_ACC"}}
    _run(
        oln.process_stream_tx(
            conn,
            tx,
            fetch_token=fetch_token,
            fetch_meta=fetch_meta,
            is_ours=_is_ours,
        )
    )
    record = es.get_closet_record(conn, "rUser")
    assert record is not None, "accept was filtered before reaching economy handler"
    assert record[2] == bt.ACTIVE, f"expected ACTIVE, got {record[2]}"


def test_listen_path_burn_deletes_trait_token():
    """Drive a TRAIT_TAXON NFTokenBurn through process_stream_tx (the production
    entrypoint) and confirm the trait_tokens row is deleted. This guards the
    integration gap where a unit test on apply_economy_tx would pass even if the
    live dispatch filter dropped the burn kind."""
    conn = _conn()
    _freeze(conn)
    # Seed an existing trait_tokens row to be deleted.
    es.upsert_trait_token(conn, "TRAIT_BURN", "rUser", "Hat", "Cap")
    assert len(es.read_trait_tokens(conn)) == 1

    async def fetch_token(nft_id):
        return {
            "nft_id": "TRAIT_BURN",
            "owner": "rUser",
            "taxon": config.TRAIT_TAXON,
            "uri_hex": "BB",
            "is_burned": True,
        }

    async def fetch_meta(uri_hex):
        return trait_token.build_trait_metadata("Hat", "Cap", "https://example.com/img.png")

    # NFTokenBurn carries NFTokenID directly (not in meta.nftoken_id).
    tx = {"TransactionType": "NFTokenBurn", "NFTokenID": "TRAIT_BURN"}
    _run(
        oln.process_stream_tx(
            conn,
            tx,
            fetch_token=fetch_token,
            fetch_meta=fetch_meta,
            is_ours=_is_ours,
        )
    )
    assert es.read_trait_tokens(conn) == [], "Burn did not delete trait_tokens row"


async def _none_token(nft_id):
    return None


async def _none_meta(uri_hex):
    return None


def test_stream_tx_feeds_history(tmp_path):
    from lfg_core import history_store
    from tests.fixtures import history_txs as fx

    hconn = history_store.init_history_db(str(tmp_path / "h.db"))
    conn = _conn()
    ctx = {
        "nft_issuer": fx.ISSUER,
        "brix_issuer": fx.BRIX_ISSUER,
        "brix_hex": fx.BRIX_HEX,
        "distributor": None,
        "numbers": {},
    }
    tx = dict(fx.AIRDROP)  # BRIX-only tx: index apply is a no-op, history isn't
    _run(
        oln.process_stream_tx(
            conn,
            tx,
            fetch_token=_none_token,
            fetch_meta=_none_meta,
            is_ours=lambda t: False,
            history_conn=hconn,
            history_ctx=ctx,
        )
    )
    assert hconn.execute("SELECT COUNT(*) FROM xrpl_txs").fetchone()[0] == 1
    assert hconn.execute("SELECT COUNT(*) FROM brix_events").fetchone()[0] == 2


def test_stream_tx_history_resolves_number_from_live_index(tmp_path):
    """ctx["numbers"] is a startup snapshot; a token present in the index (with
    a number) but absent from that snapshot must still get its nft_number
    resolved via a live lookup on the index conn, instead of staying None."""
    from lfg_core import history_store
    from tests.fixtures import history_txs as fx

    hconn = history_store.init_history_db(str(tmp_path / "h.db"))
    conn = _conn()
    conn.execute(
        "INSERT INTO onchain_nfts (nft_id, nft_number, owner, is_burned, mutable, uri_hex, body) "
        "VALUES (?, ?, 'rOwner', 0, 0, '', NULL)",
        (fx.NFT_A, 42),
    )
    conn.commit()
    ctx = {
        "nft_issuer": fx.ISSUER,
        "brix_issuer": fx.BRIX_ISSUER,
        "brix_hex": fx.BRIX_HEX,
        "distributor": None,
        "numbers": {},  # missing the just-minted token's number, by design
    }
    _run(
        oln.process_stream_tx(
            conn,
            dict(fx.BURN),
            fetch_token=_none_token,
            fetch_meta=_none_meta,
            is_ours=lambda t: False,
            history_conn=hconn,
            history_ctx=ctx,
        )
    )
    row = hconn.execute("SELECT nft_number FROM nft_events WHERE nft_id=?", (fx.NFT_A,)).fetchone()
    assert row["nft_number"] == 42
    # and it's cached for next time
    assert ctx["numbers"][fx.NFT_A] == 42


def test_stream_tx_history_filters_foreign_collection(tmp_path):
    """Firehose txs from OTHER NFT collections must not pollute the archive."""
    from lfg_core import history_events, history_store
    from tests.fixtures import history_txs as fx

    hconn = history_store.init_history_db(str(tmp_path / "h.db"))
    conn = _conn()
    ctx = {
        "nft_issuer": fx.ISSUER,
        "issuer_hex": history_events.issuer_account_hex(fx.ISSUER),
        "brix_issuer": fx.BRIX_ISSUER,
        "brix_hex": fx.BRIX_HEX,
        "distributor": None,
        "numbers": {},
    }
    for tx in (dict(fx.FOREIGN_BURN), dict(fx.FOREIGN_MODIFY)):
        _run(
            oln.process_stream_tx(
                conn,
                tx,
                fetch_token=_none_token,
                fetch_meta=_none_meta,
                is_ours=lambda t: False,
                history_conn=hconn,
                history_ctx=ctx,
            )
        )
    assert hconn.execute("SELECT COUNT(*) FROM xrpl_txs").fetchone()[0] == 0
    assert hconn.execute("SELECT COUNT(*) FROM nft_events").fetchone()[0] == 0

    # Our-collection burn is still recorded.
    _run(
        oln.process_stream_tx(
            conn,
            dict(fx.BURN),
            fetch_token=_none_token,
            fetch_meta=_none_meta,
            is_ours=lambda t: False,
            history_conn=hconn,
            history_ctx=ctx,
        )
    )
    assert hconn.execute("SELECT COUNT(*) FROM xrpl_txs").fetchone()[0] == 1
    assert hconn.execute("SELECT COUNT(*) FROM nft_events").fetchone()[0] == 1
