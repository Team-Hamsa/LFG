# Tests for scripts/backfill_history.py
import asyncio
import importlib
import logging
import os
import sys

import pytest

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

from lfg_core import history_store
from tests.fixtures import history_txs as fx

bh = importlib.import_module("scripts.backfill_history")


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _entry(tx, hash_, ledger=100):
    t = {k: v for k, v in tx.items() if k != "meta"}
    return {"tx": t, "meta": tx["meta"], "hash": hash_, "ledger_index": ledger, "validated": True}


def _fake_request_fn(pages):
    """pages: list of (entries, marker_or_None). Returns an async fn."""
    calls = []

    async def request_fn(req):
        calls.append(dict(req))
        entries, marker = pages[len(calls) - 1]
        out = {"transactions": entries}
        if marker is not None:
            out["marker"] = marker
        return out

    request_fn.calls = calls
    return request_fn


def test_store_raw_tx(tmp_path):
    conn = history_store.init_history_db(str(tmp_path / "h.db"))
    from lfg_core import history_events

    tx = history_events.normalize_entry(_entry(fx.MINT, "AA" * 32))
    assert bh.store_raw_tx(conn, tx) is True
    assert bh.store_raw_tx(conn, tx) is False
    row = conn.execute("SELECT * FROM xrpl_txs").fetchone()
    assert row["tx_type"] == "NFTokenMint" and row["account"] == fx.ISSUER


def test_backfill_pages_and_resumes(tmp_path):
    conn = history_store.init_history_db(str(tmp_path / "h.db"))
    fn = _fake_request_fn(
        [
            ([_entry(fx.MINT, "01" * 32)], {"ledger": 5, "seq": 0}),
            ([_entry(fx.BURN, "02" * 32)], None),
        ]
    )
    n = _run(bh.backfill_account_tx(conn, fn, fx.ISSUER, "issuer_tx"))
    assert n == 2
    assert fn.calls[0]["forward"] is True
    assert fn.calls[1]["marker"] == {"ledger": 5, "seq": 0}
    # cursor cleared once exhausted
    assert history_store.get_cursor(conn, "issuer_tx") is None

    # resume: a stored cursor is sent on the first request
    history_store.set_cursor(conn, "issuer_tx", '{"ledger": 9, "seq": 1}')
    fn2 = _fake_request_fn([([], None)])
    _run(bh.backfill_account_tx(conn, fn2, fx.ISSUER, "issuer_tx"))
    assert fn2.calls[0]["marker"] == {"ledger": 9, "seq": 1}


def test_backfill_marker_persisted_midway(tmp_path):
    """If a later page raises, the cursor from the last good page survives."""
    conn = history_store.init_history_db(str(tmp_path / "h.db"))

    async def request_fn(req):
        if req.get("marker"):
            raise RuntimeError("boom")
        return {"transactions": [_entry(fx.MINT, "03" * 32)], "marker": {"ledger": 7}}

    try:
        _run(bh.backfill_account_tx(conn, request_fn, fx.ISSUER, "issuer_tx"))
    except RuntimeError:
        pass
    assert history_store.get_cursor(conn, "issuer_tx") == '{"ledger": 7}'


def test_backfill_nft_history_resumes_after_failure(tmp_path):
    """A 2-page nft_history where page 2 raises must leave the page-1 marker
    persisted, and a resumed run must send that marker on its first request."""
    conn = history_store.init_history_db(str(tmp_path / "h.db"))
    nft_id = fx.NFT_A
    source = f"nft_history:{nft_id}"

    calls = []

    async def flaky_request_fn(req):
        calls.append(dict(req))
        if len(calls) == 1:
            return {"transactions": [_entry(fx.MINT, "10" * 32)], "marker": {"seq": 3}}
        raise RuntimeError("boom")

    try:
        _run(bh.backfill_nft_history(conn, flaky_request_fn, nft_id))
    except RuntimeError:
        pass
    assert history_store.get_cursor(conn, source) == '{"seq": 3}'

    # resume: stored marker is sent on the first request, and completion marks "done"
    calls2 = []

    async def resuming_request_fn(req):
        calls2.append(dict(req))
        return {"transactions": [_entry(fx.BURN, "11" * 32)]}

    n = _run(bh.backfill_nft_history(conn, resuming_request_fn, nft_id))
    assert calls2[0]["marker"] == {"seq": 3}
    assert n == 1
    assert history_store.get_cursor(conn, source) == "done"

    # re-running after "done" is a no-op
    assert _run(bh.backfill_nft_history(conn, resuming_request_fn, nft_id)) == 0


def test_rederive_from_raw(tmp_path):
    import importlib
    import sqlite3

    dh = importlib.import_module("scripts.derive_history_events")
    from lfg_core import history_events

    conn = history_store.init_history_db(str(tmp_path / "h.db"))
    for tx, h in ((fx.MINT, "01" * 32), (fx.SALE_XRP, "04" * 32), (fx.AIRDROP, "09" * 32)):
        bh.store_raw_tx(conn, history_events.normalize_entry(_entry(tx, h)))

    oconn = sqlite3.connect(":memory:")
    oconn.execute("CREATE TABLE onchain_nfts (nft_id TEXT PRIMARY KEY, nft_number INTEGER)")
    oconn.execute("INSERT INTO onchain_nfts VALUES (?, 7)", (fx.NFT_A,))

    counts = dh.rederive(
        conn,
        "testnet",
        distributor=fx.DISTRIBUTOR,
        oconn=oconn,
        nft_issuer=fx.ISSUER,
        brix_issuer=fx.BRIX_ISSUER,
    )
    assert counts == {"nft_events": 2, "brix_events": 2}
    rows = conn.execute("SELECT event, nft_number FROM nft_events ORDER BY ts").fetchall()
    assert [(r["event"], r["nft_number"]) for r in rows] == [("mint", 7), ("sale", 7)]
    # idempotent
    counts2 = dh.rederive(
        conn,
        "testnet",
        distributor=fx.DISTRIBUTOR,
        oconn=oconn,
        nft_issuer=fx.ISSUER,
        brix_issuer=fx.BRIX_ISSUER,
    )
    assert counts2 == counts


def test_audit_history_clean():
    import sqlite3

    ah = importlib.import_module("scripts.audit_history")

    hconn = history_store.init_history_db(":memory:")
    hconn.execute(
        "INSERT INTO nft_events (tx_hash, nft_id, event, ts) VALUES (?, ?, 'mint', 1)",
        ("h1", "N1"),
    )
    hconn.execute(
        "INSERT INTO nft_events (tx_hash, nft_id, event, ts) VALUES (?, ?, 'mint', 2)",
        ("h2", "N2"),
    )
    hconn.execute(
        "INSERT INTO nft_events (tx_hash, nft_id, event, ts) VALUES (?, ?, 'mint', 3)",
        ("h3", "N3"),
    )
    hconn.execute(
        "INSERT INTO nft_events (tx_hash, nft_id, event, ts) VALUES (?, ?, 'burn', 4)",
        ("h4", "N3"),
    )
    hconn.commit()

    oconn = sqlite3.connect(":memory:")
    oconn.execute("CREATE TABLE onchain_nfts (nft_id TEXT PRIMARY KEY, is_burned INTEGER)")
    oconn.execute("INSERT INTO onchain_nfts VALUES ('N1', 0)")
    oconn.execute("INSERT INTO onchain_nfts VALUES ('N2', 0)")
    oconn.commit()

    result = ah.audit_history(hconn, oconn)
    assert result == {"mints": 3, "burns": 1, "live_events": 2, "live_index": 2, "drift": 0}


def test_audit_history_drift(capsys):
    import sqlite3

    ah = importlib.import_module("scripts.audit_history")

    hconn = history_store.init_history_db(":memory:")
    hconn.execute(
        "INSERT INTO nft_events (tx_hash, nft_id, event, ts) VALUES (?, ?, 'mint', 1)",
        ("h1", "N1"),
    )
    hconn.execute(
        "INSERT INTO nft_events (tx_hash, nft_id, event, ts) VALUES (?, ?, 'mint', 2)",
        ("h2", "N2"),
    )
    hconn.commit()

    oconn = sqlite3.connect(":memory:")
    oconn.execute("CREATE TABLE onchain_nfts (nft_id TEXT PRIMARY KEY, is_burned INTEGER)")
    oconn.execute("INSERT INTO onchain_nfts VALUES ('N1', 0)")
    oconn.commit()

    result = ah.audit_history(hconn, oconn)
    assert result == {"mints": 2, "burns": 0, "live_events": 2, "live_index": 1, "drift": 1}

    rc = ah.main(
        ["--history-db", ":memory-not-used:", "--network", "testnet"], hconn=hconn, oconn=oconn
    )
    assert rc == 1
    out = capsys.readouterr().out
    assert "FAIL" in out


def test_rederive_skips_tec_txs_in_archive(tmp_path):
    """#235 verbatim: the raw archive stores failed (tec-class) burn attempts
    result-agnostically for audit — nft_history archived 5 tec burns for one
    token — but rederive must derive NO events from them: only the tesSUCCESS
    mint survives."""
    import importlib
    import sqlite3

    dh = importlib.import_module("scripts.derive_history_events")
    from lfg_core import history_events

    conn = history_store.init_history_db(str(tmp_path / "h.db"))
    bh.store_raw_tx(conn, history_events.normalize_entry(_entry(fx.MINT, "01" * 32)))
    for i in range(5):  # 5 failed attempts on the same token, distinct tx hashes
        tec = dict(fx.BURN_TEC)
        tec["hash"] = f"{0xB0 + i:02X}" * 32
        bh.store_raw_tx(conn, history_events.normalize_entry(_entry(tec, tec["hash"])))
    assert conn.execute("SELECT COUNT(*) FROM xrpl_txs").fetchone()[0] == 6  # archive keeps them

    oconn = sqlite3.connect(":memory:")
    oconn.execute("CREATE TABLE onchain_nfts (nft_id TEXT PRIMARY KEY, nft_number INTEGER)")

    counts = dh.rederive(
        conn,
        "testnet",
        oconn=oconn,
        nft_issuer=fx.ISSUER,
        brix_issuer=fx.BRIX_ISSUER,
    )
    assert counts == {"nft_events": 1, "brix_events": 0}
    rows = conn.execute("SELECT event FROM nft_events").fetchall()
    assert [r["event"] for r in rows] == ["mint"]


def test_rederive_filters_foreign_collection(tmp_path):
    """Raw archive may hold foreign txs that touched our accounts; rederive
    must drop nft events whose nft_id embeds another issuer."""
    import importlib
    import sqlite3

    dh = importlib.import_module("scripts.derive_history_events")
    from lfg_core import history_events

    conn = history_store.init_history_db(str(tmp_path / "h.db"))
    for tx, h in ((fx.MINT, "01" * 32), (fx.FOREIGN_BURN, "F1" * 32)):
        bh.store_raw_tx(conn, history_events.normalize_entry(_entry(tx, h)))

    oconn = sqlite3.connect(":memory:")
    oconn.execute("CREATE TABLE onchain_nfts (nft_id TEXT PRIMARY KEY, nft_number INTEGER)")

    counts = dh.rederive(
        conn,
        "testnet",
        oconn=oconn,
        nft_issuer=fx.ISSUER,
        brix_issuer=fx.BRIX_ISSUER,
    )
    assert counts["nft_events"] == 1
    rows = conn.execute("SELECT event, nft_id FROM nft_events").fetchall()
    assert [(r["event"], r["nft_id"]) for r in rows] == [("mint", fx.NFT_A)]


def test_issuers_for_network_cross_env(monkeypatch):
    """--network mainnet under a testnet env must resolve mainnet issuers
    (regression: mainnet rederive under testnet .env filtered out all events).

    config is patched rather than read: in full-suite order another module may
    have frozen lfg_core.config before this file's env-guard preamble ran."""
    import importlib

    dh = importlib.import_module("scripts.derive_history_events")
    from lfg_core import config

    monkeypatch.setattr(config, "XRPL_NETWORK", "testnet")
    monkeypatch.setattr(config, "SWAP_ISSUER_ADDRESS", "rEnvNativeNftIssuer")
    monkeypatch.setattr(config, "SWAP_OFFER_ISSUER", "rEnvNativeBrixIssuer")
    nft, brix = dh.issuers_for_network("mainnet")
    assert nft == "rLfgoMintj3KBcs4s2XKtquvDwEte2kYfJ"
    assert brix == "rLfgoBriX5ZaMP32mtc7RUZJcjnisKh2Px"
    assert dh.issuers_for_network("testnet") == (
        "rEnvNativeNftIssuer",
        "rEnvNativeBrixIssuer",
    )


def test_audit_history_scopes_by_taxon(tmp_path):
    """Issuer-minted other-taxon tokens (e.g. old taxon-1337 tests) must not
    count as drift against the taxon-scoped on-chain index."""
    import importlib
    import sqlite3

    ah = importlib.import_module("scripts.audit_history")

    # Real mainnet taxon-1337 token id (issuer rLfgoMint..., decodes to 1337)
    odd = "00010000D1AE1BC312BEF9C68233FB0C8CF6A338F7C227BEDCBA2C8200000020"
    assert ah.nftoken_taxon(odd) == 1337

    h = history_store.init_history_db(str(tmp_path / "h.db"))
    # One collection token (taxon 1760) + the odd 1337 token, both minted.
    coll = "000813886B27B69875E7C6C1D0D9BB1EBF162F1E67DF54C05C77D2EE00000001"
    for nft_id in (coll, odd):
        history_store.insert_nft_event(
            h,
            {
                "tx_hash": nft_id[:10],
                "nft_id": nft_id,
                "nft_number": 1,
                "event": "mint",
                "from_addr": None,
                "to_addr": "rI",
                "price_drops": None,
                "price_token": None,
                "ledger_index": 1,
                "ts": 1,
            },
        )
    h.commit()
    o = sqlite3.connect(":memory:")
    o.execute("CREATE TABLE onchain_nfts (nft_id TEXT PRIMARY KEY, is_burned INT DEFAULT 0)")
    o.execute("INSERT INTO onchain_nfts VALUES (?, 0)", (coll,))

    unscoped = ah.audit_history(h, o)
    assert unscoped["drift"] == 1
    scoped = ah.audit_history(h, o, taxon=ah.nftoken_taxon(coll))
    assert scoped["drift"] == 0


def test_baseline_sources_default_to_lfgo_issuer_and_signing_account():
    assert {"token_issuer", "signing"}.issubset(bh.DEFAULT_SOURCES)


def test_baseline_certification_rejects_omitted_required_account_sources():
    with pytest.raises(ValueError, match="signing, token_issuer"):
        bh.validate_baseline_source_coverage({"issuer", "brix", "nfts"})


def test_baseline_coverage_snapshot_binds_required_accounts(monkeypatch):
    from lfg_core import config

    monkeypatch.setattr(config, "TOKEN_ISSUER_ADDRESS", "rLfgoTokenIssuer")
    monkeypatch.setattr(config, "SIGNING_ACCOUNT", "rLfgoSigningAccount")
    coverage = bh.baseline_account_coverage(bh.DEFAULT_SOURCES, distributor=None)
    assert coverage["token_issuer"] == "rLfgoTokenIssuer"
    assert coverage["signing"] == "rLfgoSigningAccount"


def test_endpoint_snapshot_reads_actual_ledger_one_identity_and_validated_tip():
    requests = []

    async def request_fn(req):
        requests.append(req)
        if req["ledger_index"] == 1:
            return {"ledger": {"ledger_index": 1, "hash": "ACTUAL-GENESIS"}}
        return {"ledger": {"ledger_index": 777, "hash": "TIP"}}

    snapshot = _run(history_store.fetch_endpoint_snapshot(request_fn))
    assert snapshot == history_store.EndpointSnapshot(
        genesis_hash="ACTUAL-GENESIS", validated_ledger_index=777
    )
    assert requests == [
        {"method": "ledger", "ledger_index": 1, "transactions": False},
        {"method": "ledger", "ledger_index": "validated", "transactions": False},
    ]


def test_certification_rejects_typed_genesis_that_does_not_match_endpoint():
    snapshot = history_store.EndpointSnapshot(
        genesis_hash="ACTUAL-GENESIS", validated_ledger_index=777
    )
    with pytest.raises(ValueError, match="endpoint ledger-1 identity"):
        bh.validate_baseline_endpoint(
            snapshot,
            claimed_genesis_hash="TYPED-GENESIS",
            baseline_ledger_min=1,
            baseline_ledger_max=777,
        )


def test_certification_rejects_baseline_that_stops_before_endpoint_tip():
    snapshot = history_store.EndpointSnapshot(
        genesis_hash="ACTUAL-GENESIS", validated_ledger_index=777
    )
    with pytest.raises(ValueError, match="validated endpoint tip"):
        bh.validate_baseline_endpoint(
            snapshot,
            claimed_genesis_hash="ACTUAL-GENESIS",
            baseline_ledger_min=1,
            baseline_ledger_max=776,
        )


def test_certification_rejects_operator_chosen_non_genesis_lower_bound():
    snapshot = history_store.EndpointSnapshot(
        genesis_hash="ACTUAL-GENESIS", validated_ledger_index=777
    )
    with pytest.raises(ValueError, match="ledger 1"):
        bh.validate_baseline_endpoint(
            snapshot,
            claimed_genesis_hash="ACTUAL-GENESIS",
            baseline_ledger_min=2,
            baseline_ledger_max=777,
        )


def test_certified_account_backfill_is_bound_to_one_explicit_range(tmp_path):
    conn = history_store.init_history_db(str(tmp_path / "h.db"))
    calls = []

    async def request_fn(req):
        calls.append(dict(req))
        return {
            "account": "rRequired",
            "ledger_index_min": 1,
            "ledger_index_max": 777,
            "validated": True,
            "transactions": [],
        }

    assert (
        _run(
            bh.backfill_account_tx(
                conn,
                request_fn,
                "rRequired",
                "required_tx",
                ledger_min=1,
                ledger_max=777,
            )
        )
        == 0
    )
    assert calls[0]["ledger_index_min"] == 1
    assert calls[0]["ledger_index_max"] == 777


@pytest.mark.parametrize(
    "response",
    [
        {"account": "rOther", "ledger_index_min": 1, "ledger_index_max": 777, "validated": True},
        {"account": "rRequired", "ledger_index_min": 2, "ledger_index_max": 777, "validated": True},
        {"account": "rRequired", "ledger_index_min": 1, "ledger_index_max": 778, "validated": True},
        {"account": "rRequired", "ledger_index_min": 1, "ledger_index_max": 777},
    ],
)
def test_certified_account_backfill_rejects_unbound_page_evidence(tmp_path, response):
    conn = history_store.init_history_db(str(tmp_path / "h.db"))

    async def request_fn(_req):
        return {**response, "transactions": []}

    with pytest.raises(ValueError, match="certified account_tx"):
        _run(
            bh.backfill_account_tx(
                conn,
                request_fn,
                "rRequired",
                "required_tx",
                ledger_min=1,
                ledger_max=777,
            )
        )


def test_baseline_coverage_document_binds_accounts_tag_and_common_range():
    document = bh.baseline_coverage_document(
        {"signing": "rSigner", "token_issuer": "rIssuer"},
        source_tag=2606160021,
        ledger_min=1,
        ledger_max=777,
    )
    assert document == {
        "version": 1,
        "source_tag": 2606160021,
        "ledger_min": 1,
        "ledger_max": 777,
        "accounts": {"signing": "rSigner", "token_issuer": "rIssuer"},
    }


def test_archive_baseline_persists_source_tag_and_coverage(tmp_path):
    conn = history_store.init_history_db(str(tmp_path / "h.db"))
    history_store.record_archive_baseline(
        conn,
        network="mainnet",
        genesis_hash="ACTUAL-GENESIS",
        ledger_min=1,
        ledger_max=777,
        provenance="external-audit",
        source_tag=2606160021,
        coverage='{"signing":"rSigner","token_issuer":"rIssuer"}',
        completed_at=100,
    )
    state = history_store.get_archive_state(conn, "mainnet")
    assert state is not None
    assert state.source_tag == 2606160021
    assert state.baseline_coverage == '{"signing":"rSigner","token_issuer":"rIssuer"}'


def test_only_explicit_baseline_recertification_clears_gap_and_live_cursor(tmp_path):
    conn = history_store.init_history_db(str(tmp_path / "h.db"))
    history_store.record_archive_baseline(
        conn,
        network="mainnet",
        genesis_hash="ACTUAL-GENESIS",
        ledger_min=1,
        ledger_max=777,
        provenance="audit-v1",
        source_tag=1,
        completed_at=100,
    )
    history_store.record_validated_ledger(
        conn,
        network="mainnet",
        genesis_hash="ACTUAL-GENESIS",
        ledger_index=778,
        close_time=101,
        source_tag=1,
        observed_at=101,
    )
    history_store.invalidate_archive_continuity(
        conn,
        network="mainnet",
        reason="disconnect",
        gap_after=778,
        invalidated_at=102,
    )

    history_store.record_archive_baseline(
        conn,
        network="mainnet",
        genesis_hash="ACTUAL-GENESIS",
        ledger_min=1,
        ledger_max=800,
        provenance="audit-v2",
        source_tag=2,
        completed_at=110,
    )

    state = history_store.get_archive_state(conn, "mainnet")
    assert state is not None
    assert state.baseline_complete is True
    assert state.source_tag == 2
    assert state.validated_ledger_index is None
    assert state.validated_close_time is None
    assert state.heartbeat_at is None
    assert state.continuity_gap_at is None
    assert state.continuity_gap_reason is None


def test_unvalidated_entries_are_skipped_loudly(tmp_path, caplog):
    """A response shape carrying validation in neither the page nor its entries
    archives nothing. That must not be silent: an empty archive reads as "no
    wallet has ever used our SourceTag", which makes every wallet look eligible
    for a sponsored mint. Verify the drop is counted and warned, not swallowed."""
    conn = history_store.init_history_db(str(tmp_path / "h.db"))
    page = {"transactions": [{"tx": {"hash": "A" * 64}, "meta": {}}]}

    with caplog.at_level(logging.WARNING):
        bh._warn_if_unvalidated("nft_history:TOKEN", page, 1)

    assert "archiving nothing" in caplog.text
    assert "nft_history:TOKEN" in caplog.text
    conn.close()


def test_warn_if_unvalidated_is_quiet_when_nothing_was_dropped(caplog):
    with caplog.at_level(logging.WARNING):
        bh._warn_if_unvalidated("signing_tx", {}, 0)
    assert caplog.text == ""
