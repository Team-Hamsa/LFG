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
from lfg_core import config, market_store, nft_index, sponsored_mint, trait_token  # noqa: E402
from lfg_core import economy_store as es  # noqa: E402
from lfg_core import history_store as _hs  # noqa: E402

# Fixture ledger ranges must sit above the real earliest-available ledger (32570).
L0 = _hs.EARLIEST_AVAILABLE_LEDGER
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
        ledger_min=history_store.EARLIEST_AVAILABLE_LEDGER,
        ledger_max=L0 + 50,
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
        ledger_min=history_store.EARLIEST_AVAILABLE_LEDGER,
        ledger_max=L0 + 50,
        provenance="external-audit",
        completed_at=100,
    )
    history_store.record_validated_ledger(
        hconn,
        network="testnet",
        genesis_hash="testnet-ledger-one",
        ledger_index=L0 + 51,
        close_time=100,
        observed_at=100,
    )

    oln._mark_stream_disconnected(hconn, network="testnet", after_ledger=L0 + 51, at=101)
    history_store.record_validated_ledger(
        hconn,
        network="testnet",
        genesis_hash="testnet-ledger-one",
        ledger_index=L0 + 60,
        close_time=110,
        observed_at=110,
    )

    state = history_store.get_archive_state(hconn, "testnet")
    assert state is not None
    assert state.baseline_complete is False
    assert state.continuity_gap_after == L0 + 51
    assert state.continuity_gap_reason == "transaction stream disconnected"
    assert state.validated_ledger_index == L0 + 60


def test_endpoint_mismatch_is_rejected_before_archive_cursor_advances(tmp_path):
    from lfg_core import history_store

    hconn = history_store.init_history_db(str(tmp_path / "history.db"))
    history_store.record_archive_baseline(
        hconn,
        network="testnet",
        genesis_hash="expected-ledger-one",
        ledger_min=history_store.EARLIEST_AVAILABLE_LEDGER,
        ledger_max=L0 + 50,
        provenance="external-audit",
        completed_at=100,
    )
    ctx = {
        "network": "testnet",
        "genesis_hash": "expected-ledger-one",
        "source_tag": config.SOURCE_TAG,
    }
    snapshot = history_store.EndpointSnapshot(
        genesis_hash="different-ledger-one", validated_ledger_index=L0 + 50
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
        ledger_min=history_store.EARLIEST_AVAILABLE_LEDGER,
        ledger_max=L0 + 50,
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
        genesis_hash="testnet-ledger-one", validated_ledger_index=L0 + 50
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
        ledger_min=history_store.EARLIEST_AVAILABLE_LEDGER,
        ledger_max=L0 + 50,
        provenance="external-audit",
        completed_at=100,
    )
    ctx = {
        "network": "testnet",
        "genesis_hash": "testnet-ledger-one",
        "source_tag": config.SOURCE_TAG,
    }
    snapshot = history_store.EndpointSnapshot(
        genesis_hash="testnet-ledger-one", validated_ledger_index=L0 + 52
    )

    oln._verify_archive_connection(hconn, ctx, snapshot)

    state = history_store.get_archive_state(hconn, "testnet")
    assert state is not None
    assert state.baseline_complete is False
    assert state.continuity_gap_after == L0 + 50
    assert state.continuity_gap_before == L0 + 52


def test_listener_restart_with_prior_live_cursor_fails_closed(tmp_path):
    from lfg_core import history_store

    hconn = history_store.init_history_db(str(tmp_path / "history.db"))
    history_store.record_archive_baseline(
        hconn,
        network="testnet",
        genesis_hash="testnet-ledger-one",
        ledger_min=history_store.EARLIEST_AVAILABLE_LEDGER,
        ledger_max=L0 + 50,
        provenance="external-audit",
        completed_at=100,
    )
    history_store.record_validated_ledger(
        hconn,
        network="testnet",
        genesis_hash="testnet-ledger-one",
        ledger_index=L0 + 51,
        close_time=100,
        observed_at=100,
    )
    ctx = {
        "network": "testnet",
        "genesis_hash": "testnet-ledger-one",
        "source_tag": config.SOURCE_TAG,
    }
    snapshot = history_store.EndpointSnapshot(
        genesis_hash="testnet-ledger-one", validated_ledger_index=L0 + 51
    )

    oln._verify_archive_connection(hconn, ctx, snapshot)

    state = history_store.get_archive_state(hconn, "testnet")
    assert state is not None
    assert state.baseline_complete is False
    assert state.continuity_gap_after == L0 + 51
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
        genesis_hash="testnet-ledger-one", validated_ledger_index=L0 + 51
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


def test_archive_failure_does_not_stall_index_and_market_applies(tmp_path, monkeypatch):
    """CodeRabbit on #328: eligibility archiving runs before the business
    applies so a raising apply_tx can't lose the evidence — but the reverse must
    not hold. The index, market listings and derived history predate the
    sponsored archive and are what the marketplace and Activity read; a sqlite
    hiccup in archiving must not skip them for that transaction."""
    from lfg_core import history_store

    hconn = history_store.init_history_db(str(tmp_path / "history.db"))
    ctx = {"network": "testnet", "genesis_hash": "", "source_tag": config.SOURCE_TAG}

    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(oln, "_archive_eligibility_tx", boom)

    applied = []

    async def fake_apply_tx(conn, tx, *args, **kwargs):
        applied.append(tx.get("hash"))

    async def noop(*args, **kwargs):
        return None

    monkeypatch.setattr(oln.nft_listener, "apply_tx", fake_apply_tx)
    monkeypatch.setattr(oln.nft_listener, "apply_economy_tx", noop)
    monkeypatch.setattr(oln.nft_listener, "apply_market_tx", noop)
    monkeypatch.setattr(oln, "_record_history", lambda *a, **k: None)

    tx = {"validated": True, "hash": "B" * 64, "TransactionType": "Payment", "ledger_index": 5}

    async def scenario():
        await oln.process_stream_tx(
            sqlite3.connect(":memory:"),
            tx,
            fetch_token=noop,
            fetch_meta=noop,
            is_ours=lambda t: False,
            history_conn=hconn,
            history_ctx=ctx,
        )

    # _run, not asyncio.run: asyncio.run sets the policy's current loop to None
    # on exit, which strands later tests that call asyncio.get_event_loop()
    # (test_pending_offers, test_rarity). That is why this module has its own
    # runner — see the note on _run.
    _run(scenario())

    assert applied == ["B" * 64], "index apply was skipped because archiving raised"


def _certified(hconn, *, ledger_max, network="testnet"):
    from lfg_core import history_store

    history_store.record_archive_baseline(
        hconn,
        network=network,
        genesis_hash="testnet-ledger-one",
        ledger_min=history_store.EARLIEST_AVAILABLE_LEDGER,
        ledger_max=ledger_max,
        provenance="external-audit",
        completed_at=100,
    )


def test_reconnect_at_the_certified_tip_is_the_intended_clean_path(tmp_path):
    """Greptile #328 round 2 flags the equal-tip branch as "skips continuity
    invalidation". It is the only route to a usable archive and it is sound:
    the baseline swept [1, N] via account_tx over CLOSED, immutable validated
    ledgers, and the stream resumes at N+1. Invalidating here would mean
    certification could never produce a usable archive at all — the documented
    flow is certify at tip N, start the listener at tip N."""
    from lfg_core import history_store

    hconn = history_store.init_history_db(str(tmp_path / "history.db"))
    _certified(hconn, ledger_max=L0 + 500)
    ctx = {
        "network": "testnet",
        "genesis_hash": "testnet-ledger-one",
        "source_tag": config.SOURCE_TAG,
    }
    snapshot = history_store.EndpointSnapshot(
        genesis_hash="testnet-ledger-one", validated_ledger_index=L0 + 500
    )

    oln._verify_archive_connection(hconn, ctx, snapshot)

    state = history_store.get_archive_state(hconn, "testnet")
    assert state.continuity_gap_at is None
    assert state.baseline_complete is True


def test_reconnect_cannot_resurrect_an_uncovered_gap(tmp_path):
    """The residual Greptile is reaching for — a gap around a restart making
    the archive usable again — is closed one layer down, by the certification
    fix in 8f7e370. A gap the sweep never re-covered keeps baseline_complete=0,
    so _verify_archive_connection returns before the equal-tip branch and
    archive_is_usable stays closed no matter what the stream does next."""
    from lfg_core import history_store

    hconn = history_store.init_history_db(str(tmp_path / "history.db"))
    _certified(hconn, ledger_max=L0 + 500)
    # Continuity lost well past the tip the next certification will reach.
    history_store.invalidate_archive_continuity(
        hconn,
        network="testnet",
        reason="transaction stream disconnected",
        gap_after=L0 + 900,
        invalidated_at=110,
    )
    # Re-certifying at a tip BELOW the loss point must not clear it.
    _certified(hconn, ledger_max=L0 + 600)
    assert history_store.get_archive_state(hconn, "testnet").baseline_complete is False

    ctx = {
        "network": "testnet",
        "genesis_hash": "testnet-ledger-one",
        "source_tag": config.SOURCE_TAG,
    }
    snapshot = history_store.EndpointSnapshot(
        genesis_hash="testnet-ledger-one", validated_ledger_index=L0 + 600
    )
    oln._verify_archive_connection(hconn, ctx, snapshot)

    state = history_store.get_archive_state(hconn, "testnet")
    assert state.continuity_gap_after == L0 + 900, "the uncovered gap was lost"
    assert state.baseline_complete is False
    assert not sponsored_mint.archive_is_usable(
        str(tmp_path / "history.db"), network="testnet", now=200
    )


# ---------------------------------------------------------------------------
# #333 — batched eligibility-archive commits (ArchiveBatch)
# ---------------------------------------------------------------------------

GEN = "testnet-ledger-one"


def _certified_hconn(tmp_path, name="history.db"):
    hconn = _hs.init_history_db(str(tmp_path / name))
    _hs.record_archive_baseline(
        hconn,
        network="testnet",
        genesis_hash=GEN,
        ledger_min=_hs.EARLIEST_AVAILABLE_LEDGER,
        ledger_max=L0 + 50,
        provenance="external-audit",
        completed_at=100,
    )
    return hconn


def _batch_ctx(genesis_hash=GEN):
    return {
        "network": "testnet",
        "genesis_hash": genesis_hash,
        "source_tag": config.SOURCE_TAG,
        "nft_issuer": "rOurIssuer",
        "issuer_hex": "00" * 20,
        "brix_issuer": "unused",
        "brix_hex": "unused",
        "numbers": {},
    }


def _stream_tx(i, tagged=False):
    tx = {
        "TransactionType": "Payment",
        "Account": f"rWallet{i}",
        "hash": f"{i:064X}",
        "ledger_index": L0 + 100 + i,
        "date": 800_000_000 + i,
        "validated": True,
        "meta": {"TransactionResult": "tesSUCCESS"},
    }
    if tagged:
        tx["SourceTag"] = config.SOURCE_TAG
    return tx


class _CommitCountingConn:
    """sqlite3.Connection.commit is read-only; count commits via delegation."""

    def __init__(self, conn):
        self._conn = conn
        self.commits = 0

    def commit(self):
        self.commits += 1
        self._conn.commit()

    def __getattr__(self, name):
        return getattr(self._conn, name)


def test_archive_batch_flushes_by_count_with_single_commit(tmp_path):
    hconn = _CommitCountingConn(_certified_hconn(tmp_path))
    batch = oln.ArchiveBatch(hconn, _batch_ctx(), max_txs=5, max_seconds=999)
    for i in range(4):
        batch.add(_stream_tx(i, tagged=(i == 0)))
        assert batch.due() is False
    batch.add(_stream_tx(4))
    assert batch.due() is True
    batch.flush()
    assert hconn.commits == 1, "one flush = one commit"
    assert hconn.execute("SELECT count(*) FROM xrpl_txs").fetchone()[0] == 1
    state = _hs.get_archive_state(hconn, "testnet")
    assert state.validated_ledger_index == L0 + 104
    assert batch.due() is False and not batch.pending


def test_archive_batch_flushes_by_time(tmp_path):
    hconn = _certified_hconn(tmp_path)
    batch = oln.ArchiveBatch(hconn, _batch_ctx(), max_txs=1000, max_seconds=0.0)
    batch.add(_stream_tx(1))
    assert batch.due() is True
    batch.flush()
    assert _hs.get_archive_state(hconn, "testnet").validated_ledger_index == L0 + 101


def test_archive_batch_failed_flush_leaves_heartbeat_unadvanced_and_retries(tmp_path, monkeypatch):
    """Evidence and freshness fail together: a failed flush rolls back the
    tagged rows AND the heartbeat, so the freshness gate closes as the
    heartbeat ages past SPONSORED_MINT_ARCHIVE_MAX_LAG_SECONDS; a later
    successful flush persists the retained evidence atomically."""
    hconn = _certified_hconn(tmp_path)
    batch = oln.ArchiveBatch(hconn, _batch_ctx(), max_txs=1, max_seconds=999)
    before = _hs.get_archive_state(hconn, "testnet")
    batch.add(_stream_tx(1, tagged=True))

    real_rvl = _hs.record_validated_ledger

    def boom(*a, **k):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(oln.history_store, "record_validated_ledger", boom)
    with pytest.raises(sqlite3.OperationalError):
        batch.flush()
    # rolled back: no evidence, no heartbeat advance
    assert hconn.execute("SELECT count(*) FROM xrpl_txs").fetchone()[0] == 0
    after = _hs.get_archive_state(hconn, "testnet")
    assert after.heartbeat_at == before.heartbeat_at
    # with the heartbeat frozen, the freshness gate closes once max lag passes
    lag = config.SPONSORED_MINT_ARCHIVE_MAX_LAG_SECONDS
    assert not sponsored_mint.archive_is_usable(
        str(tmp_path / "history.db"),
        network="testnet",
        now=(after.heartbeat_at or 100) + lag + 61,
    )
    # the batch retained its state and a healthy retry lands everything
    assert batch.pending
    monkeypatch.setattr(oln.history_store, "record_validated_ledger", real_rvl)
    batch.flush()
    assert hconn.execute("SELECT count(*) FROM xrpl_txs").fetchone()[0] == 1
    assert _hs.get_archive_state(hconn, "testnet").validated_ledger_index == L0 + 101


def test_archive_batch_stamps_heartbeat_with_last_observation_time(tmp_path, monkeypatch):
    hconn = _certified_hconn(tmp_path)
    batch = oln.ArchiveBatch(hconn, _batch_ctx(), max_txs=1000, max_seconds=999)
    monkeypatch.setattr(oln.time, "time", lambda: 5000)
    batch.add(_stream_tx(1))
    monkeypatch.setattr(oln.time, "time", lambda: 5003)
    batch.add(_stream_tx(2))
    # flush happens much later — the heartbeat must NOT claim flush-time currency
    monkeypatch.setattr(oln.time, "time", lambda: 9999)
    batch.flush()
    state = _hs.get_archive_state(hconn, "testnet")
    assert state.heartbeat_at == 5003
    assert state.validated_ledger_index == L0 + 102


def test_archive_batch_cursor_is_highest_in_window(tmp_path):
    hconn = _certified_hconn(tmp_path)
    batch = oln.ArchiveBatch(hconn, _batch_ctx(), max_txs=1000, max_seconds=999)
    for i in (7, 3, 9, 1):
        batch.add(_stream_tx(i))
    batch.flush()
    state = _hs.get_archive_state(hconn, "testnet")
    assert state.validated_ledger_index == L0 + 109
    # tx_unix_time applies the ripple-epoch offset to the tx `date` field
    assert state.validated_close_time == 800_000_009 + 946_684_800
    # a later flush with only lower ledgers cannot regress the cursor
    batch.add(_stream_tx(2))
    batch.flush()
    assert _hs.get_archive_state(hconn, "testnet").validated_ledger_index == L0 + 109


def test_archive_batch_uncertified_path_persists_tagged_rows_without_heartbeat(tmp_path):
    hconn = _hs.init_history_db(str(tmp_path / "history.db"))
    batch = oln.ArchiveBatch(hconn, _batch_ctx(genesis_hash=""), max_txs=1000, max_seconds=999)
    batch.add(_stream_tx(1, tagged=True))
    batch.add(_stream_tx(2))
    batch.flush()
    assert hconn.execute("SELECT count(*) FROM xrpl_txs").fetchone()[0] == 1
    assert _hs.get_archive_state(hconn, "testnet") is None


def test_archive_batch_skips_unvalidated_and_hashless(tmp_path):
    hconn = _certified_hconn(tmp_path)
    batch = oln.ArchiveBatch(hconn, _batch_ctx(), max_txs=1000, max_seconds=999)
    tx = _stream_tx(1, tagged=True)
    tx["validated"] = False
    batch.add(tx)
    tx2 = _stream_tx(2, tagged=True)
    del tx2["hash"]
    batch.add(tx2)
    assert not batch.pending
    batch.flush()
    assert hconn.execute("SELECT count(*) FROM xrpl_txs").fetchone()[0] == 0


def test_archive_batch_observes_every_buffered_tagged_tx(tmp_path, monkeypatch):
    """Observation must not key off the INSERT's rowcount: a duplicate raw row
    (e.g. one _record_history already committed) still gets its acceptance
    observed — record_acceptance is idempotent, so replays are harmless."""
    hconn = _certified_hconn(tmp_path)
    calls = []
    monkeypatch.setattr(
        oln.sponsored_mint,
        "observe_sponsored_acceptance",
        lambda tx, meta, network: calls.append(tx["hash"]),
    )
    batch = oln.ArchiveBatch(hconn, _batch_ctx(), max_txs=1000, max_seconds=999)
    batch.add(_stream_tx(1, tagged=True))
    batch.flush()
    batch.add(_stream_tx(1, tagged=True))  # duplicate hash — INSERT OR IGNORE
    batch.add(_stream_tx(3, tagged=True))
    batch.flush()
    assert calls == [_stream_tx(1)["hash"], _stream_tx(1)["hash"], _stream_tx(3)["hash"]]
    assert hconn.execute("SELECT count(*) FROM xrpl_txs").fetchone()[0] == 2


def test_flush_advances_claim_even_when_record_history_committed_the_row_first(
    tmp_path, monkeypatch
):
    """Regression for the PR #362 P1: on the batched path _record_history
    (already_archived=True) commits a derived-events tx's raw row BEFORE the
    flush runs; the flush's INSERT OR IGNORE then reports a duplicate, which
    must NOT suppress the acceptance observation — the claim still advances
    offered -> accepted, exactly once (idempotent audit)."""
    from types import SimpleNamespace

    from lfg_core import sponsored_mint as sm

    app = str(tmp_path / "app.db")
    campaign = sm.start_campaign(app, network="testnet", actor="admin", now=100)
    with sqlite3.connect(app) as app_conn:
        app_conn.execute(
            """
            INSERT INTO free_mint_claims (
                id, network, wallet, campaign_id, session_id, status,
                reserved_at, reservation_expires_at, offer_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 100, NULL, ?, 100, 100)
            """,
            (
                "batch-claim",
                "testnet",
                "rBatchWallet",
                campaign.campaign_id,
                "session",
                "offered",
                "BATCH-OFFER",
            ),
        )
    monkeypatch.setattr(
        sm, "db_path", SimpleNamespace(app_db_path=lambda network: app), raising=False
    )
    monkeypatch.setattr(
        oln.history_events,
        "derive_nft_events",
        lambda *_args, **_kwargs: [{"nft_id": "batch-nft"}],
    )
    monkeypatch.setattr(oln.history_events, "nft_id_issuer_matches", lambda *_args: True)
    monkeypatch.setattr(oln.history_events, "derive_brix_events", lambda *_args, **_kwargs: [])

    hconn = _certified_hconn(tmp_path)
    tx = {
        "TransactionType": "NFTokenAcceptOffer",
        "Account": "rBatchWallet",
        "SourceTag": config.SOURCE_TAG,
        "hash": "F" * 64,
        "ledger_index": L0 + 105,
        "date": 800_000_000,
        "validated": True,
        "NFTokenSellOffer": "BATCH-OFFER",
        "meta": {"TransactionResult": "tesSUCCESS"},
    }
    ctx = _batch_ctx()
    batch = oln.ArchiveBatch(hconn, ctx, max_txs=1000, max_seconds=999)
    # Production order on the batched path: buffer first, then _record_history
    # commits the raw row inline (already_archived=True), then the flush lands.
    batch.add(tx)
    oln._record_history(hconn, tx, ctx, already_archived=True)
    assert hconn.execute("SELECT count(*) FROM xrpl_txs").fetchone()[0] == 1
    batch.flush()
    with sqlite3.connect(app) as app_conn:
        claim = app_conn.execute(
            "SELECT status, accept_tx_hash FROM free_mint_claims WHERE wallet=?",
            ("rBatchWallet",),
        ).fetchone()
        audits = app_conn.execute(
            "SELECT count(*) FROM free_mint_audit WHERE action='claim_accepted'"
        ).fetchone()[0]
    assert claim == ("accepted", tx["hash"])
    assert audits == 1
    # a replayed flush of the same tx stays idempotent
    batch.add(tx)
    batch.flush()
    with sqlite3.connect(app) as app_conn:
        audits = app_conn.execute(
            "SELECT count(*) FROM free_mint_audit WHERE action='claim_accepted'"
        ).fetchone()[0]
    assert audits == 1


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("0", 200), ("-5", 200), ("nan", 200), ("bogus", 200), ("300", 300), (None, 200)],
)
def test_flush_threshold_env_falls_back_on_invalid_values(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("X_TEST_FLUSH", raising=False)
    else:
        monkeypatch.setenv("X_TEST_FLUSH", raw)
    assert oln._read_flush_threshold("X_TEST_FLUSH", 200, minimum=1, cast=int) == expected


def test_archive_batch_retention_cap_breach_fails_closed_and_bounds_memory(tmp_path, monkeypatch):
    """A wedged DB cannot grow the retained batch without limit. On breaching
    the cap the batch invalidates archive continuity (same fail-closed posture
    as a stream disconnect) BEFORE dropping the buffer, so dropped evidence can
    never coexist with a usable archive."""
    hconn = _certified_hconn(tmp_path)
    batch = oln.ArchiveBatch(
        hconn, _batch_ctx(), max_txs=1000, max_seconds=999, max_retained=2, retry_seconds=0.0
    )
    for i in range(3):
        batch.add(_stream_tx(i, tagged=True))

    def wedged(*a, **k):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(oln.history_store, "insert_tx", wedged)
    with pytest.raises(sqlite3.OperationalError):
        batch.flush()
    # buffer dropped (memory bounded), continuity broken, gate closed
    assert not batch.pending
    state = _hs.get_archive_state(hconn, "testnet")
    assert state.baseline_complete is False
    assert state.continuity_gap_reason is not None
    assert not sponsored_mint.archive_is_usable(
        str(tmp_path / "history.db"), network="testnet", now=200
    )


def test_archive_batch_backs_off_between_failed_flushes(tmp_path, monkeypatch):
    hconn = _certified_hconn(tmp_path)
    batch = oln.ArchiveBatch(
        hconn, _batch_ctx(), max_txs=1, max_seconds=0.0, max_retained=100, retry_seconds=60.0
    )
    batch.add(_stream_tx(1, tagged=True))
    assert batch.due() is True

    def wedged(*a, **k):
        raise sqlite3.OperationalError("busy")

    real_insert = _hs.insert_tx
    monkeypatch.setattr(oln.history_store, "insert_tx", wedged)
    batch.flush_logged()
    assert batch.pending
    # within the retry window: not due, despite count/time thresholds passing
    assert batch.due() is False
    # once the window elapses, retry is due again
    batch._failed_flush_monotonic -= 120
    assert batch.due() is True
    monkeypatch.setattr(oln.history_store, "insert_tx", real_insert)
    batch.flush()
    assert not batch.pending
    assert batch._failed_flush_monotonic is None
    assert hconn.execute("SELECT count(*) FROM xrpl_txs").fetchone()[0] == 1


def test_flush_lands_before_continuity_invalidation(tmp_path):
    """The disconnect path flushes pending evidence FIRST, so the recorded gap
    bound reflects the last archived ledger and no observed tx is lost."""
    hconn = _certified_hconn(tmp_path)
    batch = oln.ArchiveBatch(hconn, _batch_ctx(), max_txs=1000, max_seconds=999)
    batch.add(_stream_tx(5, tagged=True))
    oln._flush_and_mark_disconnected(batch, hconn, network="testnet")
    assert hconn.execute("SELECT count(*) FROM xrpl_txs").fetchone()[0] == 1
    state = _hs.get_archive_state(hconn, "testnet")
    assert state.baseline_complete is False
    assert state.continuity_gap_after == L0 + 105
    assert not batch.pending


def test_idle_flush_loop_flushes_a_quiet_batch(tmp_path):
    hconn = _certified_hconn(tmp_path)
    batch = oln.ArchiveBatch(hconn, _batch_ctx(), max_txs=1000, max_seconds=0.01)
    batch.add(_stream_tx(1, tagged=True))

    async def drive():
        task = asyncio.get_event_loop().create_task(
            oln._archive_idle_flush_loop(batch, interval=0.01)
        )
        try:
            for _ in range(100):
                await asyncio.sleep(0.01)
                if not batch.pending:
                    break
        finally:
            task.cancel()

    _run(drive())
    assert not batch.pending
    assert hconn.execute("SELECT count(*) FROM xrpl_txs").fetchone()[0] == 1


def test_dispatch_stream_tx_batches_instead_of_committing_inline(tmp_path, monkeypatch):
    """With a batch supplied, _dispatch_stream_tx/process_stream_tx must not
    touch the history DB per-tx — evidence waits for the flush."""
    hconn = _certified_hconn(tmp_path)
    batch = oln.ArchiveBatch(hconn, _batch_ctx(), max_txs=1000, max_seconds=999)

    def no_inline(*a, **k):
        raise AssertionError("per-tx archive call on the batched path")

    monkeypatch.setattr(oln, "_archive_eligibility_tx", no_inline)
    tx = _stream_tx(1, tagged=True)
    tx["TransactionType"] = "NFTokenMint"
    tx["Issuer"] = "rForeignIssuer"
    _run(
        oln._dispatch_stream_tx(
            _conn(),
            tx,
            collection_issuer="rOurIssuer",
            fetch_token=_none_token,
            fetch_meta=_none_meta,
            is_ours=lambda _t: False,
            history_conn=hconn,
            history_ctx=_batch_ctx(),
            archive_batch=batch,
        )
    )
    assert hconn.execute("SELECT count(*) FROM xrpl_txs").fetchone()[0] == 0
    assert batch.pending
    batch.flush()
    assert hconn.execute("SELECT count(*) FROM xrpl_txs").fetchone()[0] == 1
