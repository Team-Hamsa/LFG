# #322 — stop re-freezing genesis: out-of-band character burns must flow into
# the supply_changes ledger as idempotent `-1` shrinkage rows. Covers the store
# nft_id column, the pure burn_shrinkage_deltas helper, the listener recorder,
# the reconcile_shrinkage sweep, and the audit's drift classification.
import asyncio
import os
import sqlite3
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

from lfg_core import economy_store as es  # noqa: E402
from lfg_core import nft_index, nft_listener, supply_reconcile  # noqa: E402
from lfg_core import trait_economy as te  # noqa: E402
from lfg_core.nft_index import OnchainNft  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _attrs(body="Straight", **slots):
    out = [{"trait_type": "Body", "value": body}]
    for slot, value in slots.items():
        out.append({"trait_type": slot.replace("_", " "), "value": value})
    return out


def _dressed_attrs(body="Straight"):
    # Explicit value for every slot; unspecified slots default to "None" via
    # slot_value, which still counts as a dressed character's asset.
    return _attrs(body=body, Head="Wizard Hat", Eyes="Blue")


def _token(edition, *, nft_id=None, attrs=None, burned=False, mutable=True, ledger_index=None):
    return OnchainNft(
        nft_id=nft_id or f"ID{edition:06d}",
        nft_number=edition,
        owner="rOWNER",
        is_burned=burned,
        mutable=mutable,
        uri_hex="",
        body="male",
        attributes=_dressed_attrs() if attrs is None else attrs,
        image="",
        ledger_index=ledger_index,
    )


def _blank_token(edition, **kw):
    return _token(edition, attrs=te.blank_attributes(), **kw)


def _db():
    conn = nft_index.init_db(":memory:")
    es.init_economy_schema(conn)
    return conn


def _freeze(conn, tokens):
    genesis = te.build_genesis({t.nft_number: t for t in tokens})
    es.freeze_genesis(conn, genesis, {"network": "testnet"})


def _effective(conn):
    return te.effective_genesis(es.read_genesis(conn), es.read_supply_changes(conn))


def _burn_rows(conn):
    return [r for r in es.read_supply_changes(conn) if r["kind"] == "burn"]


def _burn_tx(nft_id):
    return {
        "TransactionType": "NFTokenBurn",
        "NFTokenID": nft_id,
        "meta": {"TransactionResult": "tesSUCCESS"},
    }


async def _none_fetch(*_a, **_k):
    return None


def _apply_burn(conn, nft_id):
    _run(
        nft_listener.apply_economy_tx(
            conn,
            _burn_tx(nft_id),
            fetch_token_fn=_none_fetch,
            fetch_meta_fn=_none_fetch,
            genesis=_effective(conn),
        )
    )


# ---------------------------------------------------------------- store


def test_record_and_lookup_by_nft_id():
    conn = _db()
    es.record_supply_change(
        conn,
        "burn",
        3560,
        "Straight",
        "male",
        {"Eyes|None": -1},
        "listener",
        "out-of-band burn NFTABC",
        nft_id="NFTABC",
    )
    assert es.supply_change_exists_for_nft(conn, "NFTABC") is True
    assert es.supply_change_exists_for_nft(conn, "NFTZZZ") is False
    assert es.supply_change_exists_for_nft(conn, "NFTABC", kind="mint") is False
    assert es.read_supply_changes(conn)[-1]["nft_id"] == "NFTABC"


def test_record_without_nft_id_back_compatible():
    conn = _db()
    es.record_supply_change(conn, "mint", 1, "Straight", "male", {"Eyes|Blue": 1}, "x", "r")
    assert es.read_supply_changes(conn)[-1]["nft_id"] is None


def test_self_migration_adds_nft_id_column():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE supply_changes (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, "
        "edition INTEGER, body_value TEXT, body_class TEXT, trait_deltas_json TEXT, "
        "actor TEXT, reason TEXT, applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
    )
    conn.commit()
    es.init_economy_schema(conn)  # must ALTER-add nft_id, not crash
    cols = {r[1] for r in conn.execute("PRAGMA table_info(supply_changes)")}
    assert "nft_id" in cols


def test_exists_lookup_false_on_pre_migration_db():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE supply_changes (id INTEGER PRIMARY KEY, kind TEXT)")
    assert es.supply_change_exists_for_nft(conn, "X") is False


# ------------------------------------------------- pure deltas helper


def test_dressed_char_yields_negative_deltas():
    rec = _token(1)
    d = te.burn_shrinkage_deltas(rec)
    assert d is not None
    assert all(v == -1 for v in d.values())
    assert set(d) == {f"{s}|{te.slot_value(rec, s)}" for s in te.NON_BODY_SLOTS}
    assert not any(k.startswith("Body|") for k in d)


def test_blank_char_yields_none():
    assert te.burn_shrinkage_deltas(_blank_token(1)) is None


def test_unreadable_char_yields_none():
    assert te.burn_shrinkage_deltas(_token(1, attrs=[])) is None


def test_body_compensation_only_on_mismatch():
    genesis = te.build_genesis({1: _token(1, attrs=_dressed_attrs(body="Straight"))})
    same = _token(1, attrs=_dressed_attrs(body="Straight"))
    assert te.body_compensation_deltas(same, genesis) == {}
    swapped = _token(1, attrs=_dressed_attrs(body="Bones"))
    assert te.body_compensation_deltas(swapped, genesis) == {
        "Body|Bones": -1,
        "Body|Straight": +1,
    }


# ------------------------------------------------- listener recorder


def test_out_of_band_dressed_burn_records_one_shrinkage_and_conserves():
    conn = _db()
    keep, gone = _token(1), _token(2, nft_id="GONE")
    _freeze(conn, [keep, gone])
    nft_index.upsert(conn, keep)
    nft_index.upsert(conn, gone)
    nft_index.mark_burned(conn, "GONE")

    _apply_burn(conn, "GONE")

    rows = _burn_rows(conn)
    assert len(rows) == 1
    assert rows[0]["nft_id"] == "GONE"
    assert rows[0]["actor"] == "listener"
    assert all(v == -1 for v in rows[0]["trait_deltas"].values())
    # conservation now holds without any re-freeze
    census = te.asset_census({1: keep}, [], [])
    report = te.verify_conservation(es.read_genesis(conn), census, es.read_supply_changes(conn))
    assert report.ok, report.trait_drift
    # idempotent on replay
    _apply_burn(conn, "GONE")
    assert len(_burn_rows(conn)) == 1


def test_blank_burn_records_no_shrinkage():
    conn = _db()
    dressed = _token(1)
    _freeze(conn, [dressed])
    blank = _blank_token(1, nft_id="BLANK1")
    nft_index.upsert(conn, blank)
    nft_index.mark_burned(conn, "BLANK1")
    _apply_burn(conn, "BLANK1")
    assert _burn_rows(conn) == []


def test_flow_owned_burn_not_double_counted():
    conn = _db()
    gone = _token(2, nft_id="GONE")
    _freeze(conn, [gone])
    nft_index.upsert(conn, gone)
    nft_index.mark_burned(conn, "GONE")
    # a flow (legacy harvest upgrade) already logged this burn with its nft_id
    es.record_supply_change(
        conn,
        "burn",
        2,
        "Straight",
        "male",
        {"Head|Wizard Hat": -1},
        "harvest",
        "legacy harvest upgrade s1",
        nft_id="GONE",
    )
    _apply_burn(conn, "GONE")
    assert len(_burn_rows(conn)) == 1


def test_unknown_or_foreign_burn_records_nothing():
    conn = _db()
    _freeze(conn, [_token(1)])
    _apply_burn(conn, "NOT_IN_INDEX")
    assert _burn_rows(conn) == []


def test_burn_with_live_duplicate_at_edition_records_nothing():
    conn = _db()
    old, new = _token(3, nft_id="OLD3"), _token(3, nft_id="NEW3", ledger_index=99)
    _freeze(conn, [old])
    nft_index.upsert(conn, old)
    nft_index.upsert(conn, new)
    nft_index.mark_burned(conn, "OLD3")
    _apply_burn(conn, "OLD3")
    assert _burn_rows(conn) == []


def test_economy_only_conn_without_index_table_is_harmless():
    conn = sqlite3.connect(":memory:")
    es.init_economy_schema(conn)
    _run(
        nft_listener.apply_economy_tx(
            conn,
            _burn_tx("ANY"),
            fetch_token_fn=_none_fetch,
            fetch_meta_fn=_none_fetch,
            genesis=te.Genesis(trait_counts={}, edition_bodies={}),
        )
    )
    assert es.read_supply_changes(conn) == []


# ------------------------------------------------- reconcile sweep


def test_reconcile_shrinkage_writes_missing_rows_idempotently():
    conn = _db()
    live = _token(1)
    burned = [_token(e, nft_id=f"B{e}", burned=True) for e in (2, 3, 4)]
    _freeze(conn, [live] + burned)
    nft_index.upsert(conn, live)
    for t in burned:
        nft_index.upsert(conn, t)

    dry = supply_reconcile.reconcile_shrinkage(conn, dry_run=True)
    assert [e for e, _ in dry["written"]] == [2, 3, 4]
    assert _burn_rows(conn) == []

    applied = supply_reconcile.reconcile_shrinkage(conn)
    assert [e for e, _ in applied["written"]] == [2, 3, 4]
    assert len(_burn_rows(conn)) == 3
    assert {r["nft_id"] for r in _burn_rows(conn)} == {"B2", "B3", "B4"}

    again = supply_reconcile.reconcile_shrinkage(conn)
    assert again["written"] == []
    assert len(_burn_rows(conn)) == 3

    census = te.asset_census({1: live}, [], [])
    report = te.verify_conservation(es.read_genesis(conn), census, es.read_supply_changes(conn))
    assert report.ok, report.trait_drift


def test_reconcile_shrinkage_skips_blank_live_and_unreadable():
    conn = _db()
    genesis_tokens = [_token(e) for e in (1, 2, 3, 4)]
    _freeze(conn, genesis_tokens)
    nft_index.upsert(conn, _blank_token(1, nft_id="BLANK", burned=True))
    nft_index.upsert(conn, _token(2, nft_id="LIVEDUP", ledger_index=5))
    nft_index.upsert(conn, _token(2, nft_id="OLDDUP", burned=True))
    nft_index.upsert(conn, _token(3, nft_id="NOMETA", attrs=[], burned=True))

    report = supply_reconcile.reconcile_shrinkage(conn)

    assert report["written"] == []
    assert report["skipped_unreadable"] == [(3, "NOMETA")]
    assert _burn_rows(conn) == []


def test_reconcile_shrinkage_burned_duplicates_of_dead_edition_write_once():
    conn = _db()
    _freeze(conn, [_token(5)])
    nft_index.upsert(conn, _token(5, nft_id="DUP_A", burned=True))
    nft_index.upsert(conn, _token(5, nft_id="DUP_B", burned=True))
    report = supply_reconcile.reconcile_shrinkage(conn)
    assert len(report["written"]) == 1
    assert len(_burn_rows(conn)) == 1


# ------------------------------------------------- audit classification


def test_classify_drift_benign_vs_real():
    sys.path.insert(
        0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
    )
    import audit_trait_economy as audit

    report = te.ConservationReport(
        trait_drift={
            ("Head", "A"): -1,
            ("Head", "B"): +1,  # slot nets to zero -> benign swap substitution
            ("Eyes", "None"): -4,  # slot nets non-zero -> real drift
        },
        ok=False,
    )
    classes = audit.classify_drift(report)
    assert classes["benign_swap"] == {("Head", "A"): -1, ("Head", "B"): +1}
    assert classes["real"] == {("Eyes", "None"): -4}

    body = audit.build_alert_body(
        "testnet",
        10,
        report,
        te.CompletenessReport(orphan_bodies=[], slot_anomalies={}, ok=True),
        "reports/x.md",
    )
    assert "Real conservation drift" in body
    assert "benign swap substitution" in body
    assert "re-freeze" in body
