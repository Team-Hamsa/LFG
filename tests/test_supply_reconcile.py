# Tests for lfg_core/supply_reconcile.py — genesis-growth reconciliation:
# live character editions missing from the effective genesis (listener-missed
# mints) get their supply_changes growth row written back from index metadata.
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

from lfg_core import economy_store as es  # noqa: E402
from lfg_core import nft_index, supply_reconcile, trait_economy  # noqa: E402
from lfg_core.nft_index import OnchainNft  # noqa: E402


def _attrs(body="Straight", **slots):
    out = [{"trait_type": "Body", "value": body}]
    for slot, value in slots.items():
        out.append({"trait_type": slot.replace("_", " "), "value": value})
    return out


def _token(edition, *, nft_id=None, attrs=None, burned=False, mutable=True, ledger_index=None):
    return OnchainNft(
        nft_id=nft_id or f"ID{edition:06d}",
        nft_number=edition,
        owner="rOWNER",
        is_burned=burned,
        mutable=mutable,
        uri_hex="",
        body="male",
        attributes=_attrs() if attrs is None else attrs,
        image="",
        ledger_index=ledger_index,
    )


def _db():
    conn = nft_index.init_db(":memory:")
    es.init_economy_schema(conn)
    return conn


def _freeze(conn, editions):
    genesis = trait_economy.build_genesis({e: _token(e) for e in editions})
    es.freeze_genesis(conn, genesis, {"network": "testnet"})


def _effective(conn):
    return trait_economy.effective_genesis(es.read_genesis(conn), es.read_supply_changes(conn))


def test_writes_growth_row_for_uncovered_live_edition():
    conn = _db()
    _freeze(conn, [1, 2])
    nft_index.upsert(conn, _token(3, attrs=_attrs(body="Bones", Head="Wizard Hat")))

    report = supply_reconcile.reconcile_growth(conn)

    assert report["written"] == [3]
    eff = _effective(conn)
    assert eff.edition_bodies[3] == ("Bones", "skeleton")
    rec = nft_index.nft_by_number(conn, 3)
    chk = trait_economy.can_harvest(rec, eff, burnable=True)
    assert chk.ok, chk.reason
    (row,) = [r for r in es.read_supply_changes(conn) if r["edition"] == 3]
    assert row["kind"] == "mint"
    assert row["actor"] == "reconciler"
    assert row["trait_deltas"]["Head|Wizard Hat"] == 1


def test_idempotent_second_run_writes_nothing():
    conn = _db()
    _freeze(conn, [1])
    nft_index.upsert(conn, _token(2))
    supply_reconcile.reconcile_growth(conn)

    report = supply_reconcile.reconcile_growth(conn)

    assert report["written"] == []
    assert len([r for r in es.read_supply_changes(conn) if r["edition"] == 2]) == 1


def test_skips_covered_and_burned_and_unnumbered():
    conn = _db()
    _freeze(conn, [1])
    nft_index.upsert(conn, _token(1))  # covered by genesis
    nft_index.upsert(conn, _token(5, burned=True))  # burned
    nft_index.upsert(conn, _token(None, nft_id="IDNONUM"))  # unparsed name

    report = supply_reconcile.reconcile_growth(conn)

    assert report["written"] == []
    assert es.read_supply_changes(conn) == []


def test_skips_unreadable_metadata_and_reports_it():
    conn = _db()
    _freeze(conn, [1])
    nft_index.upsert(conn, _token(7, attrs=[]))

    report = supply_reconcile.reconcile_growth(conn)

    assert report["written"] == []
    assert report["skipped_unreadable"] == [7]
    assert es.read_supply_changes(conn) == []


def test_dry_run_reports_but_writes_nothing():
    conn = _db()
    _freeze(conn, [1])
    nft_index.upsert(conn, _token(4))

    report = supply_reconcile.reconcile_growth(conn, dry_run=True)

    assert report["written"] == [4]
    assert es.read_supply_changes(conn) == []


def test_duplicate_tokens_same_edition_write_one_row():
    conn = _db()
    _freeze(conn, [1])
    nft_index.upsert(conn, _token(9, nft_id="IDDUP1", mutable=False, ledger_index=10))
    nft_index.upsert(conn, _token(9, nft_id="IDDUP2", mutable=True, ledger_index=20))

    report = supply_reconcile.reconcile_growth(conn)

    assert report["written"] == [9]
    assert len([r for r in es.read_supply_changes(conn) if r["edition"] == 9]) == 1
