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

import json

from lfg_core import history_store, sponsored_mint
from tests.fixtures import history_txs as fx

# Fixture ledger ranges must sit above the real earliest-available ledger (32570).
L0 = history_store.EARLIEST_AVAILABLE_LEDGER

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
                "ledger_index": history_store.EARLIEST_AVAILABLE_LEDGER,
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


def test_baseline_certification_requires_the_full_default_source_set():
    # #331: the two-account minimum is not enough — a narrowed sweep attests
    # less than the eligibility baseline is trusted to prove.
    assert bh.REQUIRED_BASELINE_SOURCES == bh.DEFAULT_SOURCES
    with pytest.raises(ValueError, match="brix, distributor, issuer, nfts"):
        bh.validate_baseline_source_coverage({"token_issuer", "signing"})
    bh.validate_baseline_source_coverage(bh.DEFAULT_SOURCES, distributor="rDistributor")


def test_baseline_certification_requires_a_distributor_address():
    # Bot finding on #350: without --distributor the distributor branch never
    # runs, so certifying would attest a sweep that never happened — refuse.
    with pytest.raises(ValueError, match="--distributor"):
        bh.validate_baseline_source_coverage(bh.DEFAULT_SOURCES)
    with pytest.raises(ValueError, match="--distributor"):
        bh.validate_baseline_source_coverage(bh.DEFAULT_SOURCES, distributor="")
    bh.validate_baseline_source_coverage(bh.DEFAULT_SOURCES, distributor="rDistributor")


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
        if req["ledger_index"] == history_store.EARLIEST_AVAILABLE_LEDGER:
            return {
                "ledger": {
                    "ledger_index": history_store.EARLIEST_AVAILABLE_LEDGER,
                    "hash": "ACTUAL-GENESIS",
                }
            }
        return {"ledger": {"ledger_index": L0 + 777, "hash": "TIP"}}

    snapshot = _run(history_store.fetch_endpoint_snapshot(request_fn))
    assert snapshot == history_store.EndpointSnapshot(
        genesis_hash="ACTUAL-GENESIS", validated_ledger_index=L0 + 777
    )
    assert requests == [
        {
            "method": "ledger",
            "ledger_index": history_store.EARLIEST_AVAILABLE_LEDGER,
            "transactions": False,
        },
        {"method": "ledger", "ledger_index": "validated", "transactions": False},
    ]


def test_certification_rejects_typed_genesis_that_does_not_match_endpoint():
    snapshot = history_store.EndpointSnapshot(
        genesis_hash="ACTUAL-GENESIS", validated_ledger_index=L0 + 777
    )
    with pytest.raises(ValueError, match="endpoint chain identity"):
        bh.validate_baseline_endpoint(
            snapshot,
            claimed_genesis_hash="TYPED-GENESIS",
            baseline_ledger_min=history_store.EARLIEST_AVAILABLE_LEDGER,
            baseline_ledger_max=L0 + 777,
        )


def test_certification_rejects_baseline_that_stops_before_endpoint_tip():
    snapshot = history_store.EndpointSnapshot(
        genesis_hash="ACTUAL-GENESIS", validated_ledger_index=L0 + 777
    )
    with pytest.raises(ValueError, match="validated endpoint tip"):
        bh.validate_baseline_endpoint(
            snapshot,
            claimed_genesis_hash="ACTUAL-GENESIS",
            baseline_ledger_min=history_store.EARLIEST_AVAILABLE_LEDGER,
            baseline_ledger_max=L0 + 776,
        )


def test_certification_rejects_operator_chosen_non_genesis_lower_bound():
    snapshot = history_store.EndpointSnapshot(
        genesis_hash="ACTUAL-GENESIS", validated_ledger_index=L0 + 777
    )
    with pytest.raises(ValueError, match="must start at ledger 32570"):
        bh.validate_baseline_endpoint(
            snapshot,
            claimed_genesis_hash="ACTUAL-GENESIS",
            baseline_ledger_min=2,
            baseline_ledger_max=L0 + 777,
        )


def test_certified_account_backfill_is_bound_to_one_explicit_range(tmp_path):
    conn = history_store.init_history_db(str(tmp_path / "h.db"))
    calls = []

    async def request_fn(req):
        calls.append(dict(req))
        # Echo the requested range back, as a real endpoint does — the certified
        # path checks the page proves the exact account and bounds it asked for.
        return {
            "account": "rRequired",
            "ledger_index_min": req["ledger_index_min"],
            "ledger_index_max": req["ledger_index_max"],
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
                ledger_min=history_store.EARLIEST_AVAILABLE_LEDGER,
                ledger_max=L0 + 777,
            )
        )
        == 0
    )
    assert calls[0]["ledger_index_min"] == history_store.EARLIEST_AVAILABLE_LEDGER
    assert calls[0]["ledger_index_max"] == L0 + 777


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
                ledger_min=history_store.EARLIEST_AVAILABLE_LEDGER,
                ledger_max=L0 + 777,
            )
        )


def test_baseline_coverage_document_binds_accounts_tag_range_and_sources():
    document = bh.baseline_coverage_document(
        {"signing": "rSigner", "token_issuer": "rIssuer"},
        sources=bh.DEFAULT_SOURCES,
        source_tag=2606160021,
        ledger_min=history_store.EARLIEST_AVAILABLE_LEDGER,
        ledger_max=L0 + 777,
    )
    assert document == {
        "version": sponsored_mint.BASELINE_COVERAGE_VERSION,
        "source_tag": 2606160021,
        "ledger_min": history_store.EARLIEST_AVAILABLE_LEDGER,
        "ledger_max": L0 + 777,
        "sources": ["brix", "distributor", "issuer", "nfts", "signing", "token_issuer"],
        "accounts": {"signing": "rSigner", "token_issuer": "rIssuer"},
    }


def _coverage_row(monkeypatch, **overrides):
    from lfg_core import config

    monkeypatch.setattr(config, "TOKEN_ISSUER_ADDRESS", "rLfgoTokenIssuer")
    monkeypatch.setattr(config, "SIGNING_ACCOUNT", "rLfgoSigningAccount")
    document = {
        "version": sponsored_mint.BASELINE_COVERAGE_VERSION,
        "source_tag": config.SOURCE_TAG,
        "ledger_min": history_store.EARLIEST_AVAILABLE_LEDGER,
        "ledger_max": L0 + 777,
        "sources": sorted(sponsored_mint.BASELINE_REQUIRED_SOURCES),
        "accounts": {"signing": "rLfgoSigningAccount", "token_issuer": "rLfgoTokenIssuer"},
    }
    document.update(overrides)
    for key in [k for k, v in overrides.items() if v is _DROP]:
        del document[key]
    return {
        "baseline_coverage": json.dumps(document),
        "baseline_ledger_min": history_store.EARLIEST_AVAILABLE_LEDGER,
        "baseline_ledger_max": L0 + 777,
    }


_DROP = object()


def test_runtime_gate_accepts_a_full_source_attestation(monkeypatch):
    row = _coverage_row(monkeypatch)
    assert sponsored_mint._baseline_coverage_is_bound(row)


def test_runtime_gate_rejects_a_narrowed_source_attestation(monkeypatch):
    row = _coverage_row(monkeypatch, sources=["signing", "token_issuer"])
    assert not sponsored_mint._baseline_coverage_is_bound(row)


def test_runtime_gate_rejects_a_document_without_a_source_attestation(monkeypatch):
    row = _coverage_row(monkeypatch, sources=_DROP)
    assert not sponsored_mint._baseline_coverage_is_bound(row)


def test_runtime_gate_rejects_a_malformed_source_attestation(monkeypatch):
    row = _coverage_row(monkeypatch, sources="issuer,brix,token_issuer,signing,distributor,nfts")
    assert not sponsored_mint._baseline_coverage_is_bound(row)


def test_runtime_gate_rejects_version_one_and_unknown_coverage_versions(monkeypatch):
    # A pre-#331 version-1 document cannot carry the sources attestation, and
    # an unknown future version must not fall through as trusted.
    for version in (1, 3, "2", None):
        row = _coverage_row(monkeypatch, version=version)
        assert not sponsored_mint._baseline_coverage_is_bound(row), version


def test_baseline_coverage_sources_parser_is_fail_closed():
    assert sponsored_mint.baseline_coverage_sources(None) is None
    assert sponsored_mint.baseline_coverage_sources("") is None
    assert sponsored_mint.baseline_coverage_sources("not json") is None
    assert sponsored_mint.baseline_coverage_sources(json.dumps({"version": 1})) is None
    assert (
        sponsored_mint.baseline_coverage_sources(
            json.dumps({"version": sponsored_mint.BASELINE_COVERAGE_VERSION, "sources": [1]})
        )
        is None
    )
    assert sponsored_mint.baseline_coverage_sources(
        json.dumps({"version": sponsored_mint.BASELINE_COVERAGE_VERSION, "sources": ["b", "a"]})
    ) == ["a", "b"]


def test_archive_baseline_persists_source_tag_and_coverage(tmp_path):
    conn = history_store.init_history_db(str(tmp_path / "h.db"))
    history_store.record_archive_baseline(
        conn,
        network="mainnet",
        genesis_hash="ACTUAL-GENESIS",
        ledger_min=history_store.EARLIEST_AVAILABLE_LEDGER,
        ledger_max=L0 + 777,
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
        ledger_min=history_store.EARLIEST_AVAILABLE_LEDGER,
        ledger_max=L0 + 777,
        provenance="audit-v1",
        source_tag=1,
        completed_at=100,
    )
    history_store.record_validated_ledger(
        conn,
        network="mainnet",
        genesis_hash="ACTUAL-GENESIS",
        ledger_index=L0 + 778,
        close_time=101,
        source_tag=1,
        observed_at=101,
    )
    history_store.invalidate_archive_continuity(
        conn,
        network="mainnet",
        reason="disconnect",
        gap_after=L0 + 778,
        invalidated_at=102,
    )

    history_store.record_archive_baseline(
        conn,
        network="mainnet",
        genesis_hash="ACTUAL-GENESIS",
        ledger_min=history_store.EARLIEST_AVAILABLE_LEDGER,
        ledger_max=L0 + 800,
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


def test_certification_cannot_erase_a_gap_above_the_certified_tip(tmp_path):
    """Greptile #328 (P1): a certification run proves coverage of its range and
    nothing above it. The listener streams concurrently with the backfill, so a
    disconnect can stamp a gap past the certified tip. Clearing that would make
    the archive read as certified-complete while missing the gap's transactions
    — and a wallet that IS already tagged would look eligible for a free mint."""
    conn = history_store.init_history_db(str(tmp_path / "h.db"))
    history_store.record_archive_baseline(
        conn,
        network="testnet",
        genesis_hash="g",
        ledger_min=history_store.EARLIEST_AVAILABLE_LEDGER,
        ledger_max=L0 + 100,
        provenance="first",
        completed_at=10,
    )
    # Stream drops at ledger 150, well past the tip the next run will certify.
    history_store.invalidate_archive_continuity(
        conn,
        network="testnet",
        reason="transaction stream disconnected",
        gap_after=L0 + 150,
        gap_before=L0 + 200,
        invalidated_at=20,
    )

    history_store.record_archive_baseline(
        conn,
        network="testnet",
        genesis_hash="g",
        ledger_min=history_store.EARLIEST_AVAILABLE_LEDGER,
        ledger_max=L0 + 100,
        provenance="second",
        completed_at=30,
    )

    state = history_store.get_archive_state(conn, "testnet")
    assert state.continuity_gap_after == L0 + 150, "gap above the certified tip was erased"
    assert state.baseline_complete is False, "archive claimed complete despite an uncovered gap"


def test_certification_clears_a_gap_it_provably_re_swept(tmp_path):
    """The mirror case: a gap wholly inside the newly certified range WAS
    re-fetched by the backfill, so certification legitimately clears it and the
    archive becomes usable again. Without this, a gap would be permanent."""
    conn = history_store.init_history_db(str(tmp_path / "h.db"))
    history_store.record_archive_baseline(
        conn,
        network="testnet",
        genesis_hash="g",
        ledger_min=history_store.EARLIEST_AVAILABLE_LEDGER,
        ledger_max=L0 + 100,
        provenance="first",
        completed_at=10,
    )
    history_store.invalidate_archive_continuity(
        conn,
        network="testnet",
        reason="listener process restart lacks exact stream catch-up",
        gap_after=L0 + 40,
        gap_before=L0 + 60,
        invalidated_at=20,
    )

    history_store.record_archive_baseline(
        conn,
        network="testnet",
        genesis_hash="g",
        ledger_min=history_store.EARLIEST_AVAILABLE_LEDGER,
        ledger_max=L0 + 500,
        provenance="re-swept through 500",
        completed_at=30,
    )

    state = history_store.get_archive_state(conn, "testnet")
    assert state.continuity_gap_at is None
    assert state.continuity_gap_before is None
    assert state.baseline_complete is True


def test_certification_keeps_an_unbounded_gap_the_sweep_never_reached(tmp_path):
    """An open-ended gap (`_mark_stream_disconnected` records only a lower
    bound) clears once the sweep runs past where continuity was lost, since
    account_tx paging genuinely re-fetches that range. It must NOT clear when
    the certified tip stops short of the loss point — that is Greptile's
    scenario, and the common one, because ledger_max is pinned to the tip
    observed BEFORE a long backfill starts while the stream keeps running."""
    conn = history_store.init_history_db(str(tmp_path / "h.db"))
    history_store.record_archive_baseline(
        conn,
        network="testnet",
        genesis_hash="g",
        ledger_min=history_store.EARLIEST_AVAILABLE_LEDGER,
        ledger_max=L0 + 100,
        provenance="first",
        completed_at=10,
    )
    history_store.invalidate_archive_continuity(
        conn,
        network="testnet",
        reason="transaction stream disconnected",
        gap_after=L0 + 900,
        invalidated_at=20,
    )

    history_store.record_archive_baseline(
        conn,
        network="testnet",
        genesis_hash="g",
        ledger_min=history_store.EARLIEST_AVAILABLE_LEDGER,
        ledger_max=L0 + 500,
        provenance="sweep stopped short of the loss point",
        completed_at=30,
    )

    state = history_store.get_archive_state(conn, "testnet")
    assert state.continuity_gap_after == L0 + 900
    assert state.baseline_complete is False

    # Sweeping past the loss point is the proof that clears it.
    history_store.record_archive_baseline(
        conn,
        network="testnet",
        genesis_hash="g",
        ledger_min=history_store.EARLIEST_AVAILABLE_LEDGER,
        ledger_max=L0 + 1000,
        provenance="re-swept past the loss point",
        completed_at=40,
    )
    state = history_store.get_archive_state(conn, "testnet")
    assert state.continuity_gap_at is None
    assert state.baseline_complete is True


def test_gap_reported_with_no_bounds_falls_back_to_the_certified_tip(tmp_path):
    """A gap must never be recorded unbounded, or it can never be cleared.

    `record_archive_baseline` sets `validated_ledger_index = NULL`, and the
    listener's disconnect handlers derive their bound from exactly that
    column. So a disconnect between a certification and the listener's next
    validated-ledger write reports gap_after=None, and the clearing CASE
    (which requires a non-NULL bound) then pins baseline_complete to 0 on
    every future certification — permanently. The certified tip is the
    correct fallback: coverage was proven through it, so a later sweep past
    it genuinely proves the gap covered.
    """
    conn = history_store.init_history_db(str(tmp_path / "h.db"))
    history_store.record_archive_baseline(
        conn,
        network="testnet",
        genesis_hash="g",
        ledger_min=history_store.EARLIEST_AVAILABLE_LEDGER,
        ledger_max=L0 + 100,
        provenance="first",
        completed_at=10,
    )
    # The disconnect the listener actually reports after a certification.
    history_store.invalidate_archive_continuity(
        conn,
        network="testnet",
        reason="transaction stream disconnected",
        gap_after=None,
        gap_before=None,
        invalidated_at=20,
    )
    state = history_store.get_archive_state(conn, "testnet")
    assert state.baseline_complete is False
    assert state.continuity_gap_after == L0 + 100, "gap must carry a provable bound"

    history_store.record_archive_baseline(
        conn,
        network="testnet",
        genesis_hash="g",
        ledger_min=history_store.EARLIEST_AVAILABLE_LEDGER,
        ledger_max=L0 + 500,
        provenance="re-swept past the certified tip",
        completed_at=30,
    )
    state = history_store.get_archive_state(conn, "testnet")
    assert state.continuity_gap_at is None
    assert state.baseline_complete is True


def test_pre_source_tag_migration_leaves_a_recertifiable_archive(tmp_path):
    """Upgrading a pre-`source_tag` archive must demand recertification, not
    make it impossible. The migration invalidates the old baseline; if it
    stamped an unbounded gap, no later certification could ever clear it and
    the upgrade would permanently brick the feature on that stack."""
    import sqlite3

    path = str(tmp_path / "h.db")
    old = sqlite3.connect(path)
    old.execute(
        """CREATE TABLE archive_state (
               network               TEXT PRIMARY KEY,
               genesis_hash          TEXT NOT NULL,
               baseline_complete     INTEGER NOT NULL DEFAULT 0,
               baseline_ledger_min   INTEGER,
               baseline_ledger_max   INTEGER,
               baseline_provenance   TEXT,
               baseline_completed_at INTEGER,
               validated_ledger_index INTEGER,
               validated_close_time  INTEGER,
               heartbeat_at          INTEGER,
               updated_at            INTEGER NOT NULL
           )"""
    )
    old.execute(
        "INSERT INTO archive_state (network, genesis_hash, baseline_complete, "
        "baseline_ledger_min, baseline_ledger_max, baseline_provenance, updated_at) "
        "VALUES (?, ?, 1, ?, ?, ?, ?)",
        ("testnet", "g", history_store.EARLIEST_AVAILABLE_LEDGER, L0 + 100, "legacy audit", 1),
    )
    old.commit()
    old.close()

    conn = history_store.init_history_db(path)
    state = history_store.get_archive_state(conn, "testnet")
    assert state.baseline_complete is False, "migration must force recertification"
    assert state.continuity_gap_after == L0 + 100, "and must leave it recertifiable"

    history_store.record_archive_baseline(
        conn,
        network="testnet",
        genesis_hash="g",
        ledger_min=history_store.EARLIEST_AVAILABLE_LEDGER,
        ledger_max=L0 + 500,
        provenance="recertified after upgrade",
        completed_at=30,
    )
    state = history_store.get_archive_state(conn, "testnet")
    assert state.baseline_complete is True
    assert state.continuity_gap_at is None


def test_identity_probe_never_asks_for_a_ledger_the_chain_cannot_serve():
    """Ledgers 1-32569 were lost in 2012 and no XRPL node serves them. Verified
    against live clio (mainnet wss://s2-clio.ripple.com and testnet) while
    reviewing PR #328:

        ledger_index=L0 + 1     -> lgrNotFound   (both networks)
        ledger_index=32570 -> success

    The identity probe used to request ledger 1, so fetch_endpoint_snapshot
    raised on EVERY reconnect. In scripts/onchain_listener.py that raise is
    caught by the outer reconnect handler, which backs off and retries forever
    — the listener never reached its `async for msg in client` and processed
    zero transactions, taking the NFT index, market listings and history sync
    with it. `account_tx` likewise rejects ledger_index_min below this bound
    (lgrIdxMalformed), so --complete-audited-baseline could never finish either.

    CI passed throughout because the fixture below stubbed the ledger-1
    response. This test pins the bound itself so a stub can't hide it again."""
    assert history_store.EARLIEST_AVAILABLE_LEDGER == 32570

    requested = []

    async def request_fn(req):
        requested.append(req)
        if req["ledger_index"] == "validated":
            return {"ledger_index": 900000, "ledger_hash": "TIP"}
        # Model the real endpoint: anything below the bound does not exist.
        if int(req["ledger_index"]) < history_store.EARLIEST_AVAILABLE_LEDGER:
            raise RuntimeError("ledger identity request failed: lgrNotFound")
        return {"ledger": {"ledger_index": req["ledger_index"], "hash": "CHAIN-ANCHOR"}}

    snapshot = _run(history_store.fetch_endpoint_snapshot(request_fn))

    assert snapshot.genesis_hash == "CHAIN-ANCHOR"
    assert snapshot.validated_ledger_index == 900000
    assert requested[0]["ledger_index"] == history_store.EARLIEST_AVAILABLE_LEDGER


def test_certification_refuses_a_baseline_starting_below_the_earliest_ledger():
    """The mirror: a sweep claiming to start at ledger 1 is claiming coverage of
    ledgers that do not exist, and account_tx would reject the request anyway."""
    snapshot = history_store.EndpointSnapshot(genesis_hash="G", validated_ledger_index=900000)

    with pytest.raises(ValueError, match="must start at ledger 32570"):
        bh.validate_baseline_endpoint(
            snapshot,
            claimed_genesis_hash="G",
            baseline_ledger_min=1,
            baseline_ledger_max=900000,
        )

    # The real bound is accepted.
    bh.validate_baseline_endpoint(
        snapshot,
        claimed_genesis_hash="G",
        baseline_ledger_min=history_store.EARLIEST_AVAILABLE_LEDGER,
        baseline_ledger_max=900000,
    )


# --- Bounded gap catch-up (#329) -------------------------------------------


def _v2_coverage(tip):
    return json.dumps(
        bh.baseline_coverage_document(
            {"signing": "rSigner", "token_issuer": "rIssuer"},
            sources=set(bh.DEFAULT_SOURCE_ORDER),
            source_tag=2606160021,
            ledger_min=history_store.EARLIEST_AVAILABLE_LEDGER,
            ledger_max=tip,
        ),
        sort_keys=True,
        separators=(",", ":"),
    )


def _seed_certified_archive_with_gap(
    conn,
    *,
    network="testnet",
    tip=L0 + 100,
    gap_after=L0 + 140,
    gap_before=None,
    coverage="v2",
    source_tag=None,
):
    """A previously certified archive whose listener later lost continuity."""
    history_store.record_archive_baseline(
        conn,
        network=network,
        genesis_hash="CHAIN-ANCHOR",
        ledger_min=history_store.EARLIEST_AVAILABLE_LEDGER,
        ledger_max=tip,
        provenance="first full certification",
        source_tag=source_tag,
        coverage=_v2_coverage(tip) if coverage == "v2" else coverage,
        completed_at=10,
    )
    history_store.record_validated_ledger(
        conn,
        network=network,
        genesis_hash="CHAIN-ANCHOR",
        ledger_index=gap_after,
        close_time=11,
        source_tag=source_tag,
        observed_at=11,
    )
    history_store.invalidate_archive_continuity(
        conn,
        network=network,
        reason="listener process restart lacks exact stream catch-up",
        gap_after=gap_after,
        gap_before=gap_before,
        invalidated_at=20,
    )
    return history_store.get_archive_state(conn, network)


def test_catchup_refuses_an_uncertified_archive():
    """No archive_state row means there is no prior certification to extend —
    a bounded run cannot claim cumulative coverage it never had."""
    with pytest.raises(ValueError, match="full certification"):
        bh.validate_catchup_state(None)


def test_catchup_refuses_an_archive_without_a_prior_full_baseline(tmp_path):
    """A row that only ever saw the listener (validated cursor, no baseline)
    has no proven [earliest, tip] floor for the catch-up to stand on."""
    conn = history_store.init_history_db(str(tmp_path / "h.db"))
    history_store.record_validated_ledger(
        conn,
        network="testnet",
        genesis_hash="CHAIN-ANCHOR",
        ledger_index=L0 + 50,
        close_time=1,
        observed_at=1,
    )
    history_store.invalidate_archive_continuity(
        conn, network="testnet", reason="restart", gap_after=L0 + 50, invalidated_at=2
    )
    state = history_store.get_archive_state(conn, "testnet")
    with pytest.raises(ValueError, match="full certification"):
        bh.validate_catchup_state(state)


def test_catchup_refuses_an_archive_with_no_gap(tmp_path):
    """Nothing to catch up: the mode must refuse rather than re-certify."""
    conn = history_store.init_history_db(str(tmp_path / "h.db"))
    history_store.record_archive_baseline(
        conn,
        network="testnet",
        genesis_hash="CHAIN-ANCHOR",
        ledger_min=history_store.EARLIEST_AVAILABLE_LEDGER,
        ledger_max=L0 + 100,
        provenance="first",
        coverage=_v2_coverage(L0 + 100),
        completed_at=10,
    )
    state = history_store.get_archive_state(conn, "testnet")
    with pytest.raises(ValueError, match="no continuity gap"):
        bh.validate_catchup_state(state)


def test_catchup_refuses_a_legacy_archive_without_a_coverage_document(tmp_path):
    """Greptile P1 (#353): an archive migrated from the pre-SourceTag baseline
    format keeps its bounds and a bounded gap but carries no v2 coverage
    document — its historical breadth was never attested under the current
    rules, so a bounded catch-up must not re-certify it."""
    conn = history_store.init_history_db(str(tmp_path / "h.db"))
    _seed_certified_archive_with_gap(conn, coverage=None)
    state = history_store.get_archive_state(conn, "testnet")
    with pytest.raises(ValueError, match="coverage document"):
        bh.validate_catchup_state(state)

    # A pre-#331 version-1 document cannot attest the swept sources either.
    conn2 = history_store.init_history_db(str(tmp_path / "h2.db"))
    _seed_certified_archive_with_gap(
        conn2, coverage='{"version":1,"ledger_min":32570,"ledger_max":32670}'
    )
    state2 = history_store.get_archive_state(conn2, "testnet")
    with pytest.raises(ValueError, match="coverage document"):
        bh.validate_catchup_state(state2)


def test_catchup_refuses_a_source_tag_mismatch(tmp_path):
    """CodeRabbit (#353): record_archive_baseline overwrites source_tag
    unconditionally, so a catch-up run configured with a different tag would
    silently rebrand an archive certified for another one. Refuse instead."""
    conn = history_store.init_history_db(str(tmp_path / "h.db"))
    _seed_certified_archive_with_gap(conn, source_tag=111)
    state = history_store.get_archive_state(conn, "testnet")
    with pytest.raises(ValueError, match="SourceTag"):
        bh.validate_catchup_state(state, expected_source_tag=222)
    # The matching tag is accepted.
    assert bh.validate_catchup_state(state, expected_source_tag=111) == L0 + 140


def test_catchup_refuses_an_unbounded_gap(tmp_path):
    """A gap with continuity_gap_after IS NULL has no provable lower bound, so
    a bounded page cannot cover it — only full certification can."""
    conn = history_store.init_history_db(str(tmp_path / "h.db"))
    _seed_certified_archive_with_gap(conn)
    # Force the pathological unbounded shape (invalidate_archive_continuity
    # normally coalesces a bound in; a hand-damaged or pre-fix row may not).
    conn.execute("UPDATE archive_state SET continuity_gap_after = NULL")
    conn.commit()
    state = history_store.get_archive_state(conn, "testnet")
    with pytest.raises(ValueError, match="full certification"):
        bh.validate_catchup_state(state)


def test_catchup_bounds_page_only_the_gap(tmp_path):
    """The paging window is [gap_after, endpoint tip], never [earliest, tip]."""
    conn = history_store.init_history_db(str(tmp_path / "h.db"))
    state = _seed_certified_archive_with_gap(conn, gap_after=L0 + 140)
    snapshot = history_store.EndpointSnapshot(
        genesis_hash="CHAIN-ANCHOR", validated_ledger_index=L0 + 500
    )
    page_min, page_max = bh.catchup_bounds(state, snapshot)
    assert page_min == L0 + 140
    assert page_max == L0 + 500


def test_catchup_refuses_a_genesis_mismatch(tmp_path):
    """A bounded run against the wrong chain (testnet reset, wrong endpoint)
    must refuse before paging anything."""
    conn = history_store.init_history_db(str(tmp_path / "h.db"))
    state = _seed_certified_archive_with_gap(conn)
    snapshot = history_store.EndpointSnapshot(
        genesis_hash="A-DIFFERENT-CHAIN", validated_ledger_index=L0 + 500
    )
    with pytest.raises(ValueError, match="chain identity"):
        bh.catchup_bounds(state, snapshot)
    # An operator-typed --genesis-hash is cross-checked too.
    good = history_store.EndpointSnapshot(
        genesis_hash="CHAIN-ANCHOR", validated_ledger_index=L0 + 500
    )
    with pytest.raises(ValueError, match="chain identity"):
        bh.catchup_bounds(state, good, claimed_genesis_hash="TYPO")


def test_catchup_refuses_a_tip_below_the_gap_lower_bound(tmp_path):
    """If the endpoint cannot even see the gap's start, the gap cannot be
    provably covered — fail closed."""
    conn = history_store.init_history_db(str(tmp_path / "h.db"))
    state = _seed_certified_archive_with_gap(conn, gap_after=L0 + 900)
    snapshot = history_store.EndpointSnapshot(
        genesis_hash="CHAIN-ANCHOR", validated_ledger_index=L0 + 500
    )
    with pytest.raises(ValueError, match="below the gap"):
        bh.catchup_bounds(state, snapshot)


def test_catchup_clears_the_gap_and_restores_the_baseline(tmp_path):
    """The recorded baseline stays [earliest, tip] (cumulative coverage) even
    though only the gap window was paged; the gap clears and archive_state
    reads certified-complete again."""
    conn = history_store.init_history_db(str(tmp_path / "h.db"))
    state = _seed_certified_archive_with_gap(conn, gap_after=L0 + 140, gap_before=L0 + 160)
    snapshot = history_store.EndpointSnapshot(
        genesis_hash="CHAIN-ANCHOR", validated_ledger_index=L0 + 500
    )
    page_min, tip = bh.catchup_bounds(state, snapshot)

    cleared = bh.record_catchup_baseline(
        conn,
        network="testnet",
        genesis_hash="CHAIN-ANCHOR",
        tip=tip,
        paged_min=page_min,
        provenance="ops re-swept the deploy gap",
        sources=set(bh.DEFAULT_SOURCE_ORDER),
        accounts={"signing": "rSigner", "token_issuer": "rIssuer"},
        source_tag=2606160021,
    )
    assert cleared is True

    after = history_store.get_archive_state(conn, "testnet")
    assert after.baseline_complete is True
    assert after.continuity_gap_at is None
    assert after.continuity_gap_after is None
    assert after.continuity_gap_before is None
    assert after.continuity_gap_reason is None
    # The recorded range is cumulative, not the bounded paging window.
    assert after.baseline_ledger_min == history_store.EARLIEST_AVAILABLE_LEDGER
    assert after.baseline_ledger_max == tip
    # The provenance records that this was a bounded catch-up and over what.
    assert "bounded catch-up" in after.baseline_provenance
    assert str(page_min) in after.baseline_provenance
    # The coverage document carries the cumulative range + full source set,
    # exactly what sponsored_mint._baseline_coverage_is_bound verifies.
    coverage = json.loads(after.baseline_coverage)
    assert coverage["ledger_min"] == history_store.EARLIEST_AVAILABLE_LEDGER
    assert coverage["ledger_max"] == tip
    assert set(coverage["sources"]) == set(bh.DEFAULT_SOURCE_ORDER)


def test_catchup_below_gap_before_leaves_the_gap_intact(tmp_path):
    """A tip past gap_after but short of gap_before does not prove coverage of
    the gap's upper extent: record_archive_baseline keeps the gap and
    baseline_complete stays 0. The helper reports failure."""
    conn = history_store.init_history_db(str(tmp_path / "h.db"))
    state = _seed_certified_archive_with_gap(conn, gap_after=L0 + 140, gap_before=L0 + 800)
    snapshot = history_store.EndpointSnapshot(
        genesis_hash="CHAIN-ANCHOR", validated_ledger_index=L0 + 500
    )
    page_min, tip = bh.catchup_bounds(state, snapshot)

    cleared = bh.record_catchup_baseline(
        conn,
        network="testnet",
        genesis_hash="CHAIN-ANCHOR",
        tip=tip,
        paged_min=page_min,
        provenance="tip lags the resume point",
        sources=set(bh.DEFAULT_SOURCE_ORDER),
        accounts={},
        source_tag=1,
    )
    assert cleared is False

    after = history_store.get_archive_state(conn, "testnet")
    assert after.baseline_complete is False
    assert after.continuity_gap_before == L0 + 800


def test_catchup_still_requires_full_source_coverage():
    """A bounded run is still a certification: narrowing --sources (or omitting
    the distributor address) must refuse exactly like the full run (#331)."""
    with pytest.raises(ValueError, match="requires sources"):
        bh.validate_baseline_source_coverage({"issuer", "brix"}, distributor="rD")
    with pytest.raises(ValueError, match="--distributor"):
        bh.validate_baseline_source_coverage(set(bh.DEFAULT_SOURCE_ORDER), distributor=None)
