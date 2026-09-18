# tests/test_backfill_economy.py
# Env-guard preamble: importing lfg_core.config freezes its constants (e.g.
# IMG_PROXY_ALLOWED_BASES, LAYER_SOURCE) at import time; set the same defaults
# test_smoke.py uses so collection order can't strand them. (Copy the block
# verbatim from tests/test_backfill_market.py — same keys/values.)
import os

os.environ.setdefault("XUMM_API_KEY", "test")
os.environ.setdefault("XUMM_API_SECRET", "test")
os.environ.setdefault("SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2")
os.environ.setdefault("TOKEN_ISSUER_ADDRESS", "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
os.environ.setdefault("TOKEN_CURRENCY_HEX", "4C46474F00000000000000000000000000000000")
os.environ.setdefault("BUNNY_CDN_ACCESS_KEY", "test")
os.environ.setdefault("BUNNY_CDN_STORAGE_ZONE", "test")
os.environ.setdefault("LAYER_SOURCE", "local")
os.environ.setdefault("BUNNY_PULL_ZONE", "nft.pullzone.example")

import asyncio  # noqa: E402
import sqlite3  # noqa: E402
import sys  # noqa: E402
from typing import Any  # noqa: E402

import pytest  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))

import backfill_economy as be  # noqa: E402

from lfg_core import closet_token as ct  # noqa: E402
from lfg_core import config  # noqa: E402
from lfg_core import economy_store as es  # noqa: E402
from lfg_core import trait_token as tt  # noqa: E402
from tests.closet_archive_helpers import closet_archive, closet_uri  # noqa: E402

ISSUER = config.SWAP_ISSUER_ADDRESS
OWNER = "rOwnerAddr0000000000000000000000000"
CLOSET_TAXON = config.CLOSET_TAXON
TRAIT_TAXON = config.TRAIT_TAXON
LEGACY_TAXON = config.LEGACY_BUCKET_TAXON


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _conn(tmp_path: Any) -> sqlite3.Connection:
    c = sqlite3.connect(str(tmp_path / "onchain_test.db"))
    es.init_economy_schema(c)
    return c


def _tok(nft_id: str, owner: str, uri_hex: str, is_burned: bool = False) -> dict[str, Any]:
    return {
        "nft_id": nft_id,
        "owner": owner,
        "is_burned": is_burned,
        "flags": 8,
        "uri_hex": uri_hex,
        "ledger_index": 1,
    }


def _enum(mapping: dict[int, list[dict[str, Any]]]) -> Any:
    async def enum(taxon: int) -> list[dict[str, Any]]:
        return list(mapping.get(taxon, []))

    return enum


def _meta(mapping: dict[str, Any]) -> Any:
    async def fetch(uri_hex: str) -> Any:
        return mapping.get(uri_hex)

    return fetch


# --- closets ----------------------------------------------------------------


def test_closet_reconciled_active_with_contents(tmp_path):
    conn = _conn(tmp_path)
    meta = ct.build_closet_metadata(OWNER, [("Hat", "Cap", 2)], [])
    enum = _enum({CLOSET_TAXON: [_tok("CLOSET1", OWNER, "URIC")]})
    fetch = _meta({"URIC": meta})

    stats = _run(be.backfill_economy(conn, enum, fetch, issuer=ISSUER))

    assert stats["closets_applied"] == 1
    rec = es.get_closet_record(conn, OWNER)
    assert rec is not None and rec[2] == ct.ACTIVE
    assets = {(s, v): n for o, s, v, n in es.read_closet_assets(conn) if o == OWNER}
    assert assets[("Hat", "Cap")] == 2
    # schema v2: build_closet_metadata never writes legacy body editions.
    bodies = {e for o, e in es.read_closet_bodies(conn) if o == OWNER}
    assert bodies == set()


def test_closet_issuer_held_pending_is_skipped_not_stored_under_issuer(tmp_path):
    # An issuer-held (pending_accept) Closet's on-ledger owner is the issuer, not
    # the user the offer targets. Backfill must SKIP it (leave the ensure_closet
    # pending record intact) rather than rebuild a Closet under the issuer
    # address, which would strand the real user's Closet (#190).
    conn = _conn(tmp_path)
    meta = ct.build_closet_metadata(ISSUER, [], [])
    enum = _enum({CLOSET_TAXON: [_tok("CLOSET2", ISSUER, "URIP")]})
    fetch = _meta({"URIP": meta})

    stats = _run(be.backfill_economy(conn, enum, fetch, issuer=ISSUER))

    assert stats["closets_applied"] == 0
    assert stats["closets_skipped"] == 1
    assert es.get_closet_record(conn, ISSUER) is None


def test_issuer_held_scrubs_prior_bogus_issuer_row(tmp_path):
    # A prior buggy run may have recorded a Closet under the ISSUER address. On
    # rerun the issuer-held skip must also scrub that bogus row so the real
    # user's pending Closet is not stranded (#190).
    conn = _conn(tmp_path)
    # Seeded with raw SQL: the store now REFUSES to write a Closet under a
    # project signing account (#383), which is exactly the damage this test
    # reproduces from a pre-guard database.
    conn.execute(
        "INSERT INTO closet_tokens (owner, nft_id, uri_hex, status) VALUES (?,?,?,?)",
        (ISSUER, "OLDBOGUS", "URIX", ct.PENDING_ACCEPT),
    )
    conn.execute(
        "INSERT INTO closet_assets (owner, slot, value, count) VALUES (?,?,?,?)",
        (ISSUER, "Hat", "Cap", 1),
    )
    conn.execute("INSERT INTO closet_bodies (owner, edition) VALUES (?,?)", (ISSUER, 3))
    conn.commit()
    meta = ct.build_closet_metadata(ISSUER, [], [])
    enum = _enum({CLOSET_TAXON: [_tok("CLOSET9", ISSUER, "URIP")]})
    fetch = _meta({"URIP": meta})

    _run(be.backfill_economy(conn, enum, fetch, issuer=ISSUER))

    assert es.get_closet_record(conn, ISSUER) is None
    assert [a for a in es.read_closet_assets(conn) if a[0] == ISSUER] == []


def test_closet_unreadable_metadata_does_not_wipe_existing(tmp_path):
    # A transient/unreadable metadata read must not be treated as an empty closet
    # — that would wipe the owner's real contents and clear mirror_pending (#190).
    conn = _conn(tmp_path)
    es.set_closet_contents(conn, OWNER, [("Hat", "Cap", 3)], [5])
    enum = _enum({CLOSET_TAXON: [_tok("CLOSET3", OWNER, "URIB")]})
    fetch = _meta({})  # URIB -> None (fetch failure)

    stats = _run(be.backfill_economy(conn, enum, fetch, issuer=ISSUER))

    assert stats["closets_applied"] == 0
    assert stats["closets_skipped"] == 1
    assets = {(s, v): n for o, s, v, n in es.read_closet_assets(conn) if o == OWNER}
    assert assets[("Hat", "Cap")] == 3  # untouched, not wiped


def test_legacy_bucket_taxon_reconciled(tmp_path):
    conn = _conn(tmp_path)
    meta = ct.build_closet_metadata(OWNER, [("Hat", "Cap", 1)], [])
    enum = _enum({LEGACY_TAXON: [_tok("BUCK1", OWNER, "URIB")]})
    fetch = _meta({"URIB": meta})

    stats = _run(be.backfill_economy(conn, enum, fetch, issuer=ISSUER))

    assert stats["closets_applied"] == 1
    assets = {(s, v): n for o, s, v, n in es.read_closet_assets(conn) if o == OWNER}
    assert assets[("Hat", "Cap")] == 1


def test_burned_closet_skipped(tmp_path):
    conn = _conn(tmp_path)
    enum = _enum({CLOSET_TAXON: [_tok("DEAD", OWNER, "URIX", is_burned=True)]})
    fetch = _meta({})

    stats = _run(be.backfill_economy(conn, enum, fetch, issuer=ISSUER))

    assert stats["closets_seen"] == 0
    assert es.get_closet_record(conn, OWNER) is None


# --- trait tokens -----------------------------------------------------------


def test_trait_upserted(tmp_path):
    conn = _conn(tmp_path)
    meta = tt.build_trait_metadata("Hat", "Cap", "https://cdn/h.png")
    enum = _enum({TRAIT_TAXON: [_tok("TRAITX", OWNER, "URIT")]})
    fetch = _meta({"URIT": meta})

    stats = _run(be.backfill_economy(conn, enum, fetch, issuer=ISSUER))

    assert stats["traits_upserted"] == 1
    assert ("TRAITX", OWNER, "Hat", "Cap") in es.read_trait_tokens(conn)


def test_stale_trait_row_deleted(tmp_path):
    conn = _conn(tmp_path)
    es.upsert_trait_token(conn, "GHOST", OWNER, "Hat", "Cap")  # no on-chain backing
    enum = _enum({TRAIT_TAXON: []})
    fetch = _meta({})

    stats = _run(be.backfill_economy(conn, enum, fetch, issuer=ISSUER))

    assert stats["traits_deleted_stale"] == 1
    assert es.read_trait_tokens(conn) == []


def test_burned_trait_row_deleted(tmp_path):
    conn = _conn(tmp_path)
    es.upsert_trait_token(conn, "BURNED", OWNER, "Hat", "Cap")
    enum = _enum({TRAIT_TAXON: [_tok("BURNED", OWNER, "URIB", is_burned=True)]})
    fetch = _meta({})

    stats = _run(be.backfill_economy(conn, enum, fetch, issuer=ISSUER))

    assert stats["traits_deleted_stale"] == 1
    assert es.read_trait_tokens(conn) == []


def test_live_but_unreadable_trait_not_deleted(tmp_path):
    """A live trait token whose metadata can't be fetched is NOT dropped: its
    membership is decided by the enumeration's is_burned flag, not metadata, so
    a transient IPFS miss can never strand a real token."""
    conn = _conn(tmp_path)
    es.upsert_trait_token(conn, "LIVE", OWNER, "Hat", "Cap")  # pre-existing row
    enum = _enum({TRAIT_TAXON: [_tok("LIVE", OWNER, "URIU")]})
    fetch = _meta({})  # metadata unreadable -> None

    stats = _run(be.backfill_economy(conn, enum, fetch, issuer=ISSUER))

    assert stats["traits_deleted_stale"] == 0
    assert ("LIVE", OWNER, "Hat", "Cap") in es.read_trait_tokens(conn)


# --- idempotency ------------------------------------------------------------


def test_rerun_idempotent(tmp_path):
    conn = _conn(tmp_path)
    cmeta = ct.build_closet_metadata(OWNER, [("Hat", "Cap", 1)], [3])
    tmeta = tt.build_trait_metadata("Head", "Crown", "https://cdn/c.png")
    enum = _enum(
        {
            CLOSET_TAXON: [_tok("C1", OWNER, "UC")],
            TRAIT_TAXON: [_tok("T1", OWNER, "UT")],
        }
    )
    fetch = _meta({"UC": cmeta, "UT": tmeta})

    _run(be.backfill_economy(conn, enum, fetch, issuer=ISSUER))
    first_assets = sorted(es.read_closet_assets(conn))
    first_bodies = sorted(es.read_closet_bodies(conn))
    first_traits = sorted(es.read_trait_tokens(conn))

    stats = _run(be.backfill_economy(conn, enum, fetch, issuer=ISSUER))

    assert sorted(es.read_closet_assets(conn)) == first_assets
    assert sorted(es.read_closet_bodies(conn)) == first_bodies
    assert sorted(es.read_trait_tokens(conn)) == first_traits
    assert stats["traits_deleted_stale"] == 0  # nothing goes stale on a clean re-run


# --- parser -----------------------------------------------------------------


def test_network_default_uses_economy_network(monkeypatch):
    monkeypatch.setattr(config, "ECONOMY_NETWORK", "testnet")
    args = be._build_parser().parse_args([])
    assert args.network == "testnet"


def test_network_bad_env_requires_flag(monkeypatch):
    monkeypatch.setattr(config, "ECONOMY_NETWORK", "devnet")
    with pytest.raises(SystemExit):
        be._build_parser().parse_args([])
    assert be._build_parser().parse_args(["--network", "mainnet"]).network == "mainnet"


# --- #522: a rebuild must not claim a version it cannot date ----------------


def test_lagging_snapshot_rebuild_does_not_claim_the_newer_version(tmp_path):
    """This script rebuilds from a clio snapshot — the very source that lags a
    just-validated modify. Rewriting the mirror one version back while KEEPING
    the newer ledger stamp would tell the flows' stale-mirror guard the mirror is
    current, and the next flow would erase the newer version on-chain: the
    original bug, reached through the guard."""
    conn = _conn(tmp_path)
    version_b, version_c = closet_uri("b"), closet_uri("c")
    # The listener mirrored version C (ledger 1005); the archive holds both.
    es.set_closet_token(
        conn, OWNER, "CLOSET1", version_c, status=ct.ACTIVE, applied_ledger_index=1005
    )
    es.set_closet_contents(conn, OWNER, [("Hat", "Cap", 2), ("Body", "Ape Xray", 1)], [])
    archive = closet_archive(
        tmp_path, "CLOSET1", [("modify", version_b, 1000), ("modify", version_c, 1005)]
    )

    # clio still serves version B, so the rebuild writes B's contents back.
    enum = _enum({CLOSET_TAXON: [_tok("CLOSET1", OWNER, version_b)]})
    fetch = _meta({version_b: ct.build_closet_metadata(OWNER, [("Hat", "Cap", 2)], [])})
    _run(be.backfill_economy(conn, enum, fetch, issuer=ISSUER, history_db_path=archive))

    with pytest.raises(ct.ClosetError) as ei:
        ct.ensure_mirror_current(conn, OWNER, history_db_path=archive)
    assert ei.value.code == ct.CLOSET_MIRROR_BEHIND
    # Dated by the archive as the version it actually wrote — NOT the newer one it replaced.
    assert es.get_closet_applied_ledger(conn, OWNER) == 1000


def test_rebuild_stamps_the_version_it_wrote_so_a_later_older_tx_cannot_undo_it(tmp_path):
    """A rebuild that leaves the mirror undatable is not safe either: a lagging
    listener then handles an OLDER Closet modify, finds no stamp to compare
    against, and replaces the recovered version with the older one. The archive
    can date the URI this rebuild wrote — stamp it with that evidence."""
    from lfg_core import nft_listener
    from tests.closet_archive_helpers import closet_version_tx

    conn = _conn(tmp_path)
    version_b, version_c = closet_uri("b"), closet_uri("c")
    es.set_closet_token(
        conn, OWNER, "CLOSET1", version_b, status=ct.ACTIVE, applied_ledger_index=1000
    )
    es.set_closet_contents(conn, OWNER, [("Hat", "Cap", 2)], [])
    archive = closet_archive(
        tmp_path, "CLOSET1", [("modify", version_b, 1000), ("modify", version_c, 1005)]
    )

    # clio has caught up: the rebuild recovers version C.
    contents_c = [("Hat", "Cap", 2), ("Body", "Ape Xray", 1)]
    enum = _enum({CLOSET_TAXON: [_tok("CLOSET1", OWNER, version_c)]})
    fetch = _meta({version_c: ct.build_closet_metadata(OWNER, contents_c, [])})
    _run(be.backfill_economy(conn, enum, fetch, issuer=ISSUER, history_db_path=archive))

    assert es.get_closet_applied_ledger(conn, OWNER) == 1005  # dated from the archive

    # The listener, still behind, now handles the OLDER version B.
    async def fetch_token(nft_id):
        return {
            "nft_id": nft_id,
            "owner": OWNER,
            "taxon": CLOSET_TAXON,
            "uri_hex": version_b,
            "issuer": ISSUER,
        }

    async def fetch_meta(uri_hex):
        return ct.build_closet_metadata(OWNER, [("Hat", "Cap", 2)], [])

    _run(
        nft_listener.apply_economy_tx(
            conn,
            closet_version_tx("modify", "CLOSET1", version_b, 1000),
            fetch_token_fn=fetch_token,
            fetch_meta_fn=fetch_meta,
            genesis=None,
        )
    )

    assets = {(s, v): n for o, s, v, n in es.read_closet_assets(conn) if o == OWNER}
    assert assets.get(("Body", "Ape Xray")) == 1  # the recovery survived


def test_rebuild_from_the_newest_version_lets_flows_run_again(tmp_path):
    """The recovery path stays open: once clio serves the newest version, the
    rebuild's own URI dates the mirror through the archive and the guard passes."""
    conn = _conn(tmp_path)
    version_b, version_c = closet_uri("b"), closet_uri("c")
    es.set_closet_token(conn, OWNER, "CLOSET1", version_b, status=ct.ACTIVE)
    es.set_closet_contents(conn, OWNER, [("Hat", "Cap", 2)], [])
    archive = closet_archive(
        tmp_path, "CLOSET1", [("modify", version_b, 1000), ("modify", version_c, 1005)]
    )

    enum = _enum({CLOSET_TAXON: [_tok("CLOSET1", OWNER, version_c)]})
    fetch = _meta(
        {
            version_c: ct.build_closet_metadata(
                OWNER, [("Hat", "Cap", 2), ("Body", "Ape Xray", 1)], []
            )
        }
    )
    _run(be.backfill_economy(conn, enum, fetch, issuer=ISSUER, history_db_path=archive))

    ct.ensure_mirror_current(conn, OWNER, history_db_path=archive)
    assets = {(s, v): n for o, s, v, n in es.read_closet_assets(conn) if o == OWNER}
    assert assets[("Body", "Ape Xray")] == 1


def test_rebuild_stamps_a_closet_that_had_no_row_yet(tmp_path):
    """The re-dating must survive a fresh or reset index. It used to run as an
    UPDATE before `set_closet_token` inserted the row, so on a fresh index it
    matched nothing and the row landed undated — exactly the case the recovery
    story depends on."""
    conn = _conn(tmp_path)
    version_c = closet_uri("c")
    archive = closet_archive(
        tmp_path, "CLOSET1", [("modify", closet_uri("b"), 1000), ("modify", version_c, 1005)]
    )

    enum = _enum({CLOSET_TAXON: [_tok("CLOSET1", OWNER, version_c)]})
    fetch = _meta({version_c: ct.build_closet_metadata(OWNER, [("Hat", "Cap", 2)], [])})
    _run(be.backfill_economy(conn, enum, fetch, issuer=ISSUER, history_db_path=archive))

    assert es.get_closet_record(conn, OWNER) is not None
    assert es.get_closet_applied_ledger(conn, OWNER) == 1005


def test_archive_lookup_crash_skips_the_stamp_not_the_run(tmp_path, monkeypatch):
    """Every other failure mode here skips one Closet; an unexpected error from
    the archive lookup must not be the one that kills the whole reconcile."""
    conn = _conn(tmp_path)
    version_c = closet_uri("c")
    archive = closet_archive(tmp_path, "CLOSET1", [("modify", version_c, 1005)])

    def boom(*a, **k):
        raise TypeError("malformed archive row")

    monkeypatch.setattr(be.history_store, "uri_version_position", boom)
    enum = _enum({CLOSET_TAXON: [_tok("CLOSET1", OWNER, version_c)]})
    fetch = _meta({version_c: ct.build_closet_metadata(OWNER, [("Hat", "Cap", 2)], [])})

    stats = _run(be.backfill_economy(conn, enum, fetch, issuer=ISSUER, history_db_path=archive))

    assert stats["closets_applied"] == 1  # the run completed and rebuilt the Closet
    assert es.get_closet_applied_ledger(conn, OWNER) is None  # just left undated


def test_rebuild_dates_the_version_it_wrote_with_its_archived_tx_index(tmp_path):
    """#537: the re-dating carries the archived version's TransactionIndex, so a
    same-ledger older modify the listener reaches later is still refused."""
    conn = _conn(tmp_path)
    version_c = closet_uri("c")
    archive = closet_archive(
        tmp_path,
        "CLOSET1",
        [("modify", closet_uri("b"), 1005, 2), ("modify", version_c, 1005, 7)],
    )

    enum = _enum({CLOSET_TAXON: [_tok("CLOSET1", OWNER, version_c)]})
    fetch = _meta({version_c: ct.build_closet_metadata(OWNER, [("Hat", "Cap", 2)], [])})
    _run(be.backfill_economy(conn, enum, fetch, issuer=ISSUER, history_db_path=archive))

    assert es.get_closet_applied_position(conn, OWNER) == (1005, 7)


def test_rebuild_and_its_redating_commit_together(tmp_path, monkeypatch):
    """#535: the rebuilt contents, the token record and the re-dated stamp are
    one version's mirror, so a failure part-way leaves the old mirror whole."""
    conn = _conn(tmp_path)
    version_b, version_c = closet_uri("b"), closet_uri("c")
    es.set_closet_token(
        conn, OWNER, "CLOSET1", version_b, status=ct.ACTIVE, applied_ledger_index=1000
    )
    es.set_closet_contents(conn, OWNER, [("Hat", "Old", 1)], [])
    archive = closet_archive(
        tmp_path, "CLOSET1", [("modify", version_b, 1000), ("modify", version_c, 1005)]
    )

    def crash(*_a, **_k):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(be.economy_store, "set_closet_applied_ledger", crash)
    enum = _enum({CLOSET_TAXON: [_tok("CLOSET1", OWNER, version_c)]})
    fetch = _meta({version_c: ct.build_closet_metadata(OWNER, [("Hat", "Cap", 2)], [])})
    with pytest.raises(sqlite3.OperationalError):
        _run(be.backfill_economy(conn, enum, fetch, issuer=ISSUER, history_db_path=archive))
    monkeypatch.undo()

    assert {(s, v): n for o, s, v, n in es.read_closet_assets(conn) if o == OWNER} == {
        ("Hat", "Old"): 1
    }
    assert es.get_closet_token(conn, OWNER) == ("CLOSET1", version_b)
    assert es.get_closet_applied_position(conn, OWNER) == (1000, None)
