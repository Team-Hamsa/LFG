# Listen-path integration: the per-tx seam drives BOTH the index update and the
# trait-economy apply (supply-growth logging + bucket rebuild), reading the
# EFFECTIVE genesis from the DB each tx so re-mints are idempotent. This is the
# wiring #68 added — apply_economy_tx had unit tests but no production caller.

import asyncio
import os
import sqlite3
import sys

import pytest

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
from lfg_core import config, market_store, nft_index, trait_token  # noqa: E402
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
    """The live listener's DB: index + economy + market schemas share one file."""
    c = sqlite3.connect(":memory:")
    c.executescript(nft_index._SCHEMA)
    es.init_economy_schema(c)
    market_store.init_db(c)
    return c


def _freeze(conn, edition_bodies=None):
    genesis = te.Genesis(trait_counts={}, edition_bodies=edition_bodies or {})
    es.freeze_genesis(conn, genesis, {"network": "testnet"})


def _char_meta(edition: int, body: str = "Straight Blue") -> dict:
    attrs = [{"trait_type": "Body", "value": body}]
    attrs += [{"trait_type": s, "value": "None"} for s in NON_BODY]
    return {"name": f"LFG #{edition}", "attributes": attrs}


def _char_token():
    return {
        "nft_id": "CHAR",
        "owner": "rUser",
        "taxon": config.SWAP_TAXON,
        "uri_hex": "CD",
        "issuer": config.SWAP_ISSUER_ADDRESS,
    }


async def _fetch_char_token(nft_id):
    return _char_token()


def _mint_tx():
    return {
        "TransactionType": "NFTokenMint",
        "Issuer": config.SWAP_ISSUER_ADDRESS,
        "meta": {"TransactionResult": "tesSUCCESS", "nftoken_id": "CHAR"},
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
        "meta": {"TransactionResult": "tesSUCCESS", "nftoken_id": "CHAR"},
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
    meta = bt.build_closet_metadata("rUser", [("Head", "None", 2), ("Eyes", "Blue", 1)], [])

    async def fetch_token(nft_id):
        return {
            "nft_id": "CLOSET",
            "owner": "rUser",
            "taxon": config.CLOSET_TAXON,
            "uri_hex": "AB",
            "issuer": config.SWAP_ISSUER_ADDRESS,
        }

    async def fetch_meta(uri_hex):
        return meta

    tx = {
        "TransactionType": "NFTokenModify",
        "NFTokenID": "CLOSET",
        "meta": {"TransactionResult": "tesSUCCESS"},
    }
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
    # schema v2: build_closet_metadata never writes legacy body editions.
    assert es.read_closet_bodies(conn) == []


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
            "issuer": config.SWAP_ISSUER_ADDRESS,
        }

    async def fetch_meta(uri_hex):
        return meta

    tx = {
        "TransactionType": "NFTokenAcceptOffer",
        "meta": {"TransactionResult": "tesSUCCESS", "nftoken_id": "CLOSET_ACC"},
    }
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


def test_listen_path_offer_create_reaches_market_listener():
    """Drive an NFTokenCreateOffer through process_stream_tx (the production
    entrypoint) and confirm a market_listings row is created. Guards the
    integration gap where apply_market_tx has unit tests but is never actually
    called by the live dispatch (mirrors the burn/closet wiring guards above)."""
    from xrpl.core import addresscodec

    conn = _conn()
    _freeze(conn)
    # A real 64-hex NFTokenID embedding OUR issuer's AccountID (hex chars
    # 8..48) -- membership is decided by DB lookup, not by decoding this, but
    # the issuer-bytes pre-filter still needs a well-formed id to pass.
    acct = addresscodec.decode_classic_address(config.SWAP_ISSUER_ADDRESS).hex().upper()
    nft_id = f"000A0000{acct}0000000000000001"
    conn.execute(
        "INSERT INTO onchain_nfts (nft_id, nft_number, owner, is_burned, mutable, uri_hex, body) "
        "VALUES (?, 1, 'rSeller', 0, 0, '', NULL)",
        (nft_id,),
    )
    conn.commit()

    tx = {
        "TransactionType": "NFTokenCreateOffer",
        "Account": "rSeller",
        "NFTokenID": nft_id,
        "ledger_index": 42,
        "meta": {
            "TransactionResult": "tesSUCCESS",
            "AffectedNodes": [
                {
                    "CreatedNode": {
                        "LedgerEntryType": "NFTokenOffer",
                        "LedgerIndex": "OFFERWIRE",
                        "NewFields": {
                            "NFTokenID": nft_id,
                            "Flags": 1,
                            "Amount": "1000000",
                        },
                    }
                }
            ],
        },
    }
    _run(
        oln.process_stream_tx(
            conn,
            tx,
            fetch_token=_none_token,
            fetch_meta=_none_meta,
            is_ours=_is_ours,
        )
    )
    row = conn.execute(
        "SELECT nft_id, kind, is_live FROM market_listings WHERE offer_index='OFFERWIRE'"
    ).fetchone()
    assert row is not None, "offer_create never reached apply_market_tx"
    assert row[0] == nft_id and row[1] == "character" and row[2] == 1


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
    tx = {
        "TransactionType": "NFTokenBurn",
        "NFTokenID": "TRAIT_BURN",
        "meta": {"TransactionResult": "tesSUCCESS"},
    }
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


def test_stream_tx_tec_burn_mutates_nothing(tmp_path):
    """#210/#235 end-to-end: a tec-class NFTokenBurn through the production
    per-message seam must leave the index untouched (is_burned stays 0) and
    derive zero history events. The live-path archive is event-gated by design
    (the network-wide firehose can't archive every failed tx), so the raw tx is
    not stored here either; result-agnostic raw archiving lives in
    scripts/backfill_history.py, which stores tec txs verbatim for audit."""
    from lfg_core import history_events, history_store
    from tests.fixtures import history_txs as fx

    hconn = history_store.init_history_db(str(tmp_path / "h.db"))
    conn = _conn()
    conn.execute(
        "INSERT INTO onchain_nfts (nft_id, nft_number, owner, is_burned, mutable, uri_hex, body) "
        "VALUES (?, 7, 'rOwner', 0, 0, '', NULL)",
        (fx.NFT_A,),
    )
    conn.commit()
    ctx = {
        "nft_issuer": fx.ISSUER,
        "issuer_hex": history_events.issuer_account_hex(fx.ISSUER),
        "brix_issuer": fx.BRIX_ISSUER,
        "brix_hex": fx.BRIX_HEX,
        "distributor": None,
        "numbers": {},
    }

    async def fetch_never(_):
        raise AssertionError("a failed tx must not be resolved at all")

    _run(
        oln.process_stream_tx(
            conn,
            dict(fx.BURN_TEC),
            fetch_token=fetch_never,
            fetch_meta=fetch_never,
            is_ours=lambda t: True,
            history_conn=hconn,
            history_ctx=ctx,
        )
    )
    assert conn.execute(
        "SELECT is_burned FROM onchain_nfts WHERE nft_id=?", (fx.NFT_A,)
    ).fetchone() == (0,)
    assert hconn.execute("SELECT COUNT(*) FROM nft_events").fetchone()[0] == 0
    assert hconn.execute("SELECT COUNT(*) FROM brix_events").fetchone()[0] == 0
    assert hconn.execute("SELECT COUNT(*) FROM xrpl_txs").fetchone()[0] == 0

    # The SAME burn with tesSUCCESS is applied + recorded — the gate is the
    # result code, nothing else about the tx shape.
    async def fetch_none(_):
        return None

    _run(
        oln.process_stream_tx(
            conn,
            dict(fx.BURN),
            fetch_token=fetch_none,
            fetch_meta=fetch_none,
            is_ours=lambda t: True,
            history_conn=hconn,
            history_ctx=ctx,
        )
    )
    assert conn.execute(
        "SELECT is_burned FROM onchain_nfts WHERE nft_id=?", (fx.NFT_A,)
    ).fetchone() == (1,)
    assert hconn.execute("SELECT COUNT(*) FROM nft_events").fetchone()[0] == 1


def test_listener_records_only_newly_archived_sponsored_acceptance(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from lfg_core import history_store
    from lfg_core import sponsored_mint as sm

    app = str(tmp_path / "app.db")
    campaign = sm.start_campaign(app, network="mainnet", actor="admin", now=100)
    with sqlite3.connect(app) as app_conn:
        app_conn.execute(
            """
            INSERT INTO free_mint_claims (
                id, network, wallet, campaign_id, session_id, status,
                reserved_at, reservation_expires_at, offer_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 100, NULL, ?, 100, 100)
            """,
            (
                "listener-claim",
                "mainnet",
                "rListener",
                campaign.campaign_id,
                "session",
                "offered",
                "LISTENER-OFFER",
            ),
        )
    monkeypatch.setattr(
        sm, "db_path", SimpleNamespace(app_db_path=lambda network: app), raising=False
    )
    monkeypatch.setattr(
        oln.history_events,
        "derive_nft_events",
        lambda *_args, **_kwargs: [{"nft_id": "listener-nft"}],
    )
    monkeypatch.setattr(oln.history_events, "nft_id_issuer_matches", lambda *_args: True)
    monkeypatch.setattr(oln.history_events, "derive_brix_events", lambda *_args, **_kwargs: [])

    hconn = history_store.init_history_db(str(tmp_path / "history.db"))
    tx = {
        "TransactionType": "NFTokenAcceptOffer",
        "Account": "rListener",
        "SourceTag": config.SOURCE_TAG,
        "hash": "C" * 64,
        "date": 800_000_000,
        "validated": True,
        "NFTokenSellOffer": "LISTENER-OFFER",
        "meta": {"TransactionResult": "tesSUCCESS"},
    }
    ctx = {
        "nft_issuer": "unused",
        "issuer_hex": "00" * 20,
        "brix_issuer": "unused",
        "brix_hex": "unused",
        "numbers": {},
        "network": "mainnet",
    }

    oln._record_history(hconn, tx, ctx)
    oln._record_history(hconn, tx, ctx)
    with sqlite3.connect(app) as app_conn:
        claim = app_conn.execute(
            "SELECT status, accept_tx_hash FROM free_mint_claims WHERE wallet=?", ("rListener",)
        ).fetchone()
        audits = app_conn.execute(
            "SELECT count(*) FROM free_mint_audit WHERE action='claim_accepted'"
        ).fetchone()[0]
    assert claim == ("accepted", tx["hash"])
    assert audits == 1


def test_listener_keeps_raw_history_when_acceptance_projection_is_deferred(tmp_path, monkeypatch):
    from lfg_core import history_store
    from lfg_core import sponsored_mint as sm

    monkeypatch.setattr(
        oln.history_events,
        "derive_nft_events",
        lambda *_args, **_kwargs: [{"nft_id": "listener-nft"}],
    )
    monkeypatch.setattr(oln.history_events, "nft_id_issuer_matches", lambda *_args: True)
    monkeypatch.setattr(oln.history_events, "derive_brix_events", lambda *_args, **_kwargs: [])
    hconn = history_store.init_history_db(str(tmp_path / "history.db"))
    tx = {
        "TransactionType": "NFTokenAcceptOffer",
        "Account": "rListener",
        "SourceTag": config.SOURCE_TAG,
        "hash": "D" * 64,
        "date": 800_000_000,
        "validated": True,
        "meta": {"TransactionResult": "tesSUCCESS"},
    }
    ctx = {
        "nft_issuer": "unused",
        "issuer_hex": "00" * 20,
        "brix_issuer": "unused",
        "brix_hex": "unused",
        "numbers": {},
        "network": "mainnet",
    }
    calls = {"count": 0}

    def fail_once(*_args, **_kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise sqlite3.OperationalError("busy")
        return True

    monkeypatch.setattr(sm, "observe_sponsored_acceptance", fail_once)
    oln._record_history(hconn, tx, ctx)
    assert hconn.execute("SELECT count(*) FROM xrpl_txs").fetchone()[0] == 1

    oln._record_history(hconn, tx, ctx)
    assert hconn.execute("SELECT count(*) FROM xrpl_txs").fetchone()[0] == 1
    assert calls["count"] == 1


@pytest.mark.parametrize(
    ("validated", "expected"),
    [(True, True), (False, False), (None, False), ("yes", False)],
)
def test_stream_normalization_requires_explicit_validated_flag(validated, expected):
    msg = {
        "type": "transaction",
        "tx_json": {"TransactionType": "NFTokenAcceptOffer"},
        "meta": {"TransactionResult": "tesSUCCESS"},
    }
    if validated is not None:
        msg["validated"] = validated

    tx = oln._normalize_stream_tx(msg)
    assert tx is not None
    assert tx["validated"] is expected


def test_dispatch_archives_tagged_foreign_issuer_mint_before_business_filter(tmp_path, monkeypatch):
    """The loop-level foreign collection optimization cannot hide eligibility."""
    from lfg_core import history_store

    hconn = history_store.init_history_db(str(tmp_path / "history.db"))
    history_store.record_archive_baseline(
        hconn,
        network="testnet",
        genesis_hash="testnet-ledger-one",
        ledger_min=1,
        ledger_max=50,
        provenance="external-audit",
        completed_at=100,
    )
    tx = {
        "TransactionType": "NFTokenMint",
        "Issuer": "rForeignIssuer",
        "Account": "rEligibleWallet",
        "SourceTag": config.SOURCE_TAG,
        "hash": "E" * 64,
        "ledger_index": 51,
        "date": 100,
        "validated": True,
        "meta": {"TransactionResult": "tesSUCCESS"},
    }
    ctx = {
        "network": "testnet",
        "genesis_hash": "testnet-ledger-one",
        "source_tag": config.SOURCE_TAG,
        "nft_issuer": "rOurIssuer",
        "issuer_hex": "00" * 20,
        "brix_issuer": "unused",
        "brix_hex": "unused",
        "numbers": {},
    }

    async def business_processing_must_be_skipped(*_args, **_kwargs):
        raise AssertionError("foreign mint reached business processing")

    monkeypatch.setattr(oln, "process_stream_tx", business_processing_must_be_skipped)
    _run(
        oln._dispatch_stream_tx(
            _conn(),
            tx,
            collection_issuer="rOurIssuer",
            fetch_token=_none_token,
            fetch_meta=_none_meta,
            is_ours=lambda _token: False,
            history_conn=hconn,
            history_ctx=ctx,
        )
    )

    row = hconn.execute(
        "SELECT account, source_tag FROM xrpl_txs WHERE tx_hash = ?", (tx["hash"],)
    ).fetchone()
    assert tuple(row) == ("rEligibleWallet", config.SOURCE_TAG)


def test_listener_reconnect_invalidates_baseline_and_later_ledgers_do_not_heal_it(tmp_path):
    from lfg_core import history_store

    hconn = history_store.init_history_db(str(tmp_path / "history.db"))
    history_store.record_archive_baseline(
        hconn,
        network="testnet",
        genesis_hash="testnet-ledger-one",
        ledger_min=1,
        ledger_max=50,
        provenance="external-audit",
        completed_at=100,
    )
    history_store.record_validated_ledger(
        hconn,
        network="testnet",
        genesis_hash="testnet-ledger-one",
        ledger_index=51,
        close_time=100,
        observed_at=100,
    )

    oln._mark_stream_disconnected(hconn, network="testnet", after_ledger=51, at=101)
    history_store.record_validated_ledger(
        hconn,
        network="testnet",
        genesis_hash="testnet-ledger-one",
        ledger_index=60,
        close_time=110,
        observed_at=110,
    )

    state = history_store.get_archive_state(hconn, "testnet")
    assert state is not None
    assert state.baseline_complete is False
    assert state.continuity_gap_after == 51
    assert state.continuity_gap_reason == "transaction stream disconnected"
    assert state.validated_ledger_index == 60


def test_endpoint_mismatch_is_rejected_before_archive_cursor_advances(tmp_path):
    from lfg_core import history_store

    hconn = history_store.init_history_db(str(tmp_path / "history.db"))
    history_store.record_archive_baseline(
        hconn,
        network="testnet",
        genesis_hash="expected-ledger-one",
        ledger_min=1,
        ledger_max=50,
        provenance="external-audit",
        completed_at=100,
    )
    ctx = {
        "network": "testnet",
        "genesis_hash": "expected-ledger-one",
        "source_tag": config.SOURCE_TAG,
    }
    snapshot = history_store.EndpointSnapshot(
        genesis_hash="different-ledger-one", validated_ledger_index=50
    )

    with pytest.raises(RuntimeError, match="ledger-1 identity"):
        oln._verify_archive_connection(hconn, ctx, snapshot)

    state = history_store.get_archive_state(hconn, "testnet")
    assert state is not None
    assert state.validated_ledger_index is None
    assert state.baseline_complete is False


def test_listener_start_rejects_source_tag_snapshot_change(tmp_path):
    from lfg_core import history_store

    hconn = history_store.init_history_db(str(tmp_path / "history.db"))
    history_store.record_archive_baseline(
        hconn,
        network="testnet",
        genesis_hash="testnet-ledger-one",
        ledger_min=1,
        ledger_max=50,
        provenance="external-audit",
        source_tag=config.SOURCE_TAG + 1,
        completed_at=100,
    )
    ctx = {
        "network": "testnet",
        "genesis_hash": "testnet-ledger-one",
        "source_tag": config.SOURCE_TAG,
    }
    snapshot = history_store.EndpointSnapshot(
        genesis_hash="testnet-ledger-one", validated_ledger_index=50
    )

    with pytest.raises(RuntimeError, match="SourceTag"):
        oln._verify_archive_connection(hconn, ctx, snapshot)

    assert history_store.get_archive_state(hconn, "testnet").baseline_complete is False


def test_listener_start_invalidates_uncovered_ledgers_after_certified_baseline(tmp_path):
    from lfg_core import history_store

    hconn = history_store.init_history_db(str(tmp_path / "history.db"))
    history_store.record_archive_baseline(
        hconn,
        network="testnet",
        genesis_hash="testnet-ledger-one",
        ledger_min=1,
        ledger_max=50,
        provenance="external-audit",
        completed_at=100,
    )
    ctx = {
        "network": "testnet",
        "genesis_hash": "testnet-ledger-one",
        "source_tag": config.SOURCE_TAG,
    }
    snapshot = history_store.EndpointSnapshot(
        genesis_hash="testnet-ledger-one", validated_ledger_index=52
    )

    oln._verify_archive_connection(hconn, ctx, snapshot)

    state = history_store.get_archive_state(hconn, "testnet")
    assert state is not None
    assert state.baseline_complete is False
    assert state.continuity_gap_after == 50
    assert state.continuity_gap_before == 52


def test_listener_restart_with_prior_live_cursor_fails_closed(tmp_path):
    from lfg_core import history_store

    hconn = history_store.init_history_db(str(tmp_path / "history.db"))
    history_store.record_archive_baseline(
        hconn,
        network="testnet",
        genesis_hash="testnet-ledger-one",
        ledger_min=1,
        ledger_max=50,
        provenance="external-audit",
        completed_at=100,
    )
    history_store.record_validated_ledger(
        hconn,
        network="testnet",
        genesis_hash="testnet-ledger-one",
        ledger_index=51,
        close_time=100,
        observed_at=100,
    )
    ctx = {
        "network": "testnet",
        "genesis_hash": "testnet-ledger-one",
        "source_tag": config.SOURCE_TAG,
    }
    snapshot = history_store.EndpointSnapshot(
        genesis_hash="testnet-ledger-one", validated_ledger_index=51
    )

    oln._verify_archive_connection(hconn, ctx, snapshot)

    state = history_store.get_archive_state(hconn, "testnet")
    assert state is not None
    assert state.baseline_complete is False
    assert state.continuity_gap_after == 51
    assert "restart" in state.continuity_gap_reason


def test_uncertified_archive_does_not_block_the_listener_or_write_state(tmp_path):
    """Regression: a stack whose history DB has no certified baseline (every
    stack, until an operator runs the audited backfill) must still run the
    listener. `_verify_archive_connection` previously had no empty-identity
    branch, and `_listen` raised outright before subscribing — pm2 turned that
    into a crash loop that took the NFT index, market listings and history sync
    down with it. With no certified identity the verifier is a no-op: it does
    not raise, and it must not stamp continuity state it cannot vouch for."""
    from lfg_core import history_store

    hconn = history_store.init_history_db(str(tmp_path / "history.db"))
    ctx = {"network": "testnet", "genesis_hash": "", "source_tag": config.SOURCE_TAG}
    snapshot = history_store.EndpointSnapshot(
        genesis_hash="testnet-ledger-one", validated_ledger_index=51
    )

    oln._verify_archive_connection(hconn, ctx, snapshot)

    assert history_store.get_archive_state(hconn, "testnet") is None


def test_uncertified_archive_still_applies_index_events_without_archive_state(tmp_path):
    """The listener's pre-sponsored duties are unchanged with no certified
    archive: a validated tagged tx is archived as raw evidence, but no
    archive_state row is fabricated from an unproven genesis identity."""
    from lfg_core import history_store

    hconn = history_store.init_history_db(str(tmp_path / "history.db"))
    ctx = {"network": "testnet", "genesis_hash": "", "source_tag": config.SOURCE_TAG}
    tx = {
        "validated": True,
        "hash": "A" * 64,
        "ledger_index": 77,
        "TransactionType": "Payment",
        "Account": "rSOMEONE",
        "SourceTag": config.SOURCE_TAG,
        "date": 700000000,
        "meta": {"TransactionResult": "tesSUCCESS"},
    }

    assert oln._archive_eligibility_tx(hconn, tx, ctx) is True

    assert history_store.get_archive_state(hconn, "testnet") is None
    row = hconn.execute("SELECT account FROM xrpl_txs WHERE tx_hash = ?", ("A" * 64,)).fetchone()
    assert row["account"] == "rSOMEONE"
