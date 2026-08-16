# tests/test_fix_iridescent_body.py — #301 correction driver, against temp DBs
# with stubbed ledger/CDN.
# Env-guard preamble (copy verbatim, see tests/test_seasons.py).
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
import json  # noqa: E402
import sqlite3  # noqa: E402

import pytest  # noqa: E402

import scripts.fix_iridescent_body as fx  # noqa: E402
from lfg_core import nft_index  # noqa: E402
from lfg_core.body_fix import BAD, GOOD  # noqa: E402
from lfg_core.nft_index import NFT_FLAG_MUTABLE  # noqa: E402

LIVE_ID = "00191B58" + "0" * 52 + "AAAA"
BURNED_ID = "00191B58" + "0" * 52 + "BBBB"
GOOD_ID = "00191B58" + "0" * 52 + "CCCC"
OWNER = "rUserOwnerXXXXXXXXXXXXXXXXXXXXX"
URI_HEX = "68747470733A2F2F63646E2F6D2E6A736F6E"  # https://cdn/m.json


def _attrs(body):
    return [
        {"trait_type": "Background", "value": "Moving Pink Clouds"},
        {"trait_type": "Body", "value": body},
    ]


def _seed_index(path):
    conn = nft_index.init_db(path)
    for nft_id, body, burned in (
        (LIVE_ID, BAD, 0),
        (BURNED_ID, BAD, 1),
        (GOOD_ID, GOOD, 0),
    ):
        conn.execute(
            "INSERT INTO onchain_nfts (nft_id, nft_number, owner, is_burned, mutable,"
            " uri_hex, body, attributes_json, image, ledger_index)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                nft_id,
                64,
                OWNER,
                burned,
                1,
                URI_HEX,
                "skeleton",
                json.dumps(_attrs(body)),
                "https://cdn/x.png",
                1,
            ),
        )
    conn.commit()
    conn.close()


def _seed_app(path):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE LFG (nft_number INTEGER PRIMARY KEY, nft_id TEXT, Body TEXT)")
    conn.execute("INSERT INTO LFG VALUES (64, ?, ?)", (LIVE_ID, BAD))
    conn.execute("INSERT INTO LFG VALUES (99, 'other', 'Skeleton')")
    conn.commit()
    conn.close()


@pytest.fixture()
def dbs(tmp_path):
    index_db = str(tmp_path / "onchain_test.db")
    app_db = str(tmp_path / "app.db")
    _seed_index(index_db)
    _seed_app(app_db)
    return index_db, app_db


def _stub_ledger(monkeypatch, *, mutable=True, calls=None):
    calls = calls if calls is not None else {}
    calls.setdefault("modify", [])
    calls.setdefault("upload", [])
    flags = NFT_FLAG_MUTABLE if mutable else 0

    async def fake_nft_info(nft_id, clio=None):
        return {
            "nft_id": nft_id,
            "owner": OWNER,
            "flags": flags,
            "uri_hex": URI_HEX,
            "is_burned": False,
        }

    async def fake_fetch(http, uri_hex):
        return {"edition": 64, "image": "https://cdn/x.png", "attributes": _attrs(BAD)}

    async def fake_upload(folder, path_on_cdn, data, content_type):
        calls["upload"].append((folder, path_on_cdn, content_type))
        return f"https://cdn.example/{folder}/{path_on_cdn}"

    async def fake_modify(nft_id, owner, uri, platform="backend"):
        calls["modify"].append((nft_id, owner, uri, platform))
        return "FAKEHASH"

    monkeypatch.setattr(fx.xrpl_ops, "nft_info", fake_nft_info)
    monkeypatch.setattr(fx.nft_index, "fetch_metadata_multi", fake_fetch)
    monkeypatch.setattr(fx.cdn, "upload_to_bunny", fake_upload)
    monkeypatch.setattr(fx.xrpl_ops, "modify_nft", fake_modify)
    return calls


def _await(coro):
    # Repo convention: never asyncio.run() in tests — it unsets the ambient
    # event loop and breaks later get_event_loop()-based tests in a full run.
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _run(index_db, app_db, apply):
    return _await(fx.run(network="testnet", apply=apply, index_db=index_db, app_db=app_db))


def test_discover_targets_finds_live_bad_only(dbs):
    index_db, app_db = dbs
    with sqlite3.connect(index_db) as ic, sqlite3.connect(app_db) as ac:
        ic.row_factory = sqlite3.Row
        targets = fx.discover_targets(ic, ac)
    assert [t.nft_id for t in targets.tokens] == [LIVE_ID]
    assert targets.editions == [64]


def test_dry_run_mutates_nothing(dbs, monkeypatch):
    index_db, app_db = dbs
    calls = _stub_ledger(monkeypatch)
    results = _run(index_db, app_db, apply=False)
    assert calls["modify"] == [] and calls["upload"] == []
    assert [r.status for r in results] == ["planned"]
    with sqlite3.connect(index_db) as ic:
        (attrs,) = ic.execute(
            "SELECT attributes_json FROM onchain_nfts WHERE nft_id=?", (LIVE_ID,)
        ).fetchone()
    assert BAD in attrs
    with sqlite3.connect(app_db) as ac:
        (body,) = ac.execute("SELECT Body FROM LFG WHERE nft_number=64").fetchone()
    assert body == BAD


def test_apply_rewrites_ledger_and_mirrors(dbs, monkeypatch):
    index_db, app_db = dbs
    calls = _stub_ledger(monkeypatch)
    results = _run(index_db, app_db, apply=True)
    assert [r.status for r in results] == ["corrected"]
    assert len(calls["modify"]) == 1
    nft_id, owner, uri, _platform = calls["modify"][0]
    assert nft_id == LIVE_ID and owner == OWNER
    assert uri.startswith("https://cdn.example/")
    with sqlite3.connect(index_db) as ic:
        (attrs, uri_hex) = ic.execute(
            "SELECT attributes_json, uri_hex FROM onchain_nfts WHERE nft_id=?",
            (LIVE_ID,),
        ).fetchone()
    assert GOOD in attrs and BAD not in attrs.replace(GOOD, "")
    assert bytes.fromhex(uri_hex).decode() == uri
    with sqlite3.connect(app_db) as ac:
        (body,) = ac.execute("SELECT Body FROM LFG WHERE nft_number=64").fetchone()
    assert body == GOOD


def test_non_mutable_is_skipped(dbs, monkeypatch):
    index_db, app_db = dbs
    calls = _stub_ledger(monkeypatch, mutable=False)
    results = _run(index_db, app_db, apply=True)
    assert [r.status for r in results] == ["skipped_non_mutable"]
    assert calls["modify"] == []
    with sqlite3.connect(app_db) as ac:
        (body,) = ac.execute("SELECT Body FROM LFG WHERE nft_number=64").fetchone()
    assert body == BAD


def test_nft_info_unavailable_fails_closed(dbs, monkeypatch):
    index_db, app_db = dbs
    calls = _stub_ledger(monkeypatch)

    async def none_info(nft_id, clio=None):
        return None

    monkeypatch.setattr(fx.xrpl_ops, "nft_info", none_info)
    results = _run(index_db, app_db, apply=True)
    assert [r.status for r in results] == ["failed"]
    assert calls["modify"] == []


def test_rerun_is_noop(dbs, monkeypatch):
    index_db, app_db = dbs
    _stub_ledger(monkeypatch)
    _run(index_db, app_db, apply=True)
    calls2 = _stub_ledger(monkeypatch)
    results = _run(index_db, app_db, apply=True)
    assert results == []
    assert calls2["modify"] == []


def test_indeterminate_modify_recorded_and_batch_continues(dbs, monkeypatch):
    index_db, app_db = dbs
    calls = _stub_ledger(monkeypatch)
    from lfg_core.xrpl_ops import IndeterminateResultError

    async def boom(nft_id, owner, uri, platform="backend"):
        raise IndeterminateResultError("submission outcome unknown")

    monkeypatch.setattr(fx.xrpl_ops, "modify_nft", boom)
    results = _run(index_db, app_db, apply=True)
    # the token is journaled indeterminate, not crashed; mirrors untouched
    assert [r.status for r in results] == ["indeterminate"]
    assert calls["modify"] == []
    with sqlite3.connect(index_db) as ic:
        (attrs,) = ic.execute(
            "SELECT attributes_json FROM onchain_nfts WHERE nft_id=?", (LIVE_ID,)
        ).fetchone()
    assert BAD in attrs
    with sqlite3.connect(app_db) as ac:
        (body,) = ac.execute("SELECT Body FROM LFG WHERE nft_number=64").fetchone()
    assert body == BAD


def test_confirmed_owner_persisted_to_index(dbs, monkeypatch):
    """The nft_info-confirmed owner (post-transfer) wins over the stale index snapshot."""
    index_db, app_db = dbs
    calls = _stub_ledger(monkeypatch)
    new_owner = "rNewOwnerYYYYYYYYYYYYYYYYYYYYYY"

    async def fresh_info(nft_id, clio=None):
        return {
            "nft_id": nft_id,
            "owner": new_owner,
            "flags": NFT_FLAG_MUTABLE,
            "uri_hex": URI_HEX,
            "is_burned": False,
        }

    monkeypatch.setattr(fx.xrpl_ops, "nft_info", fresh_info)
    _run(index_db, app_db, apply=True)
    assert calls["modify"][0][1] == new_owner
    with sqlite3.connect(index_db) as ic:
        (owner,) = ic.execute(
            "SELECT owner FROM onchain_nfts WHERE nft_id=?", (LIVE_ID,)
        ).fetchone()
    assert owner == new_owner


def _strand_app_only(index_db, app_db):
    """Simulate a partial mirror failure / unreadable index: the index row is
    already canonical while LFG.Body still carries the typo."""
    with sqlite3.connect(index_db) as ic:
        ic.execute(
            "UPDATE onchain_nfts SET attributes_json=? WHERE nft_id=?",
            (json.dumps(_attrs(GOOD)), LIVE_ID),
        )
        ic.commit()


def test_app_db_only_edition_repaired(dbs, monkeypatch):
    index_db, app_db = dbs
    _strand_app_only(index_db, app_db)
    calls = _stub_ledger(monkeypatch)
    results = _run(index_db, app_db, apply=True)
    # no live token carries the typo -> pure local repair, no ledger op
    assert calls["modify"] == [] and calls["upload"] == []
    assert [r.status for r in results] == ["corrected"]
    assert results[0].edition == 64 and results[0].nft_id == ""
    with sqlite3.connect(app_db) as ac:
        (body,) = ac.execute("SELECT Body FROM LFG WHERE nft_number=64").fetchone()
    assert body == GOOD


def test_app_db_only_edition_dry_run_is_planned_and_untouched(dbs, monkeypatch):
    index_db, app_db = dbs
    _strand_app_only(index_db, app_db)
    _stub_ledger(monkeypatch)
    results = _run(index_db, app_db, apply=False)
    assert [r.status for r in results] == ["planned"]
    with sqlite3.connect(app_db) as ac:
        (body,) = ac.execute("SELECT Body FROM LFG WHERE nft_number=64").fetchone()
    assert body == BAD


def test_rerun_converges_after_partial_mirror_failure(dbs, monkeypatch):
    """Index synced, LFG commit lost (crash between mirror writes): a rerun
    must still find and repair the LFG row, then reach a true no-op."""
    index_db, app_db = dbs
    _stub_ledger(monkeypatch)
    _run(index_db, app_db, apply=True)
    # simulate the LFG update having been lost
    with sqlite3.connect(app_db) as ac:
        ac.execute("UPDATE LFG SET Body=? WHERE nft_number=64", (BAD,))
        ac.commit()
    calls2 = _stub_ledger(monkeypatch)
    results = _run(index_db, app_db, apply=True)
    assert calls2["modify"] == []  # ledger/index already canonical
    assert [r.status for r in results] == ["corrected"]
    with sqlite3.connect(app_db) as ac:
        (body,) = ac.execute("SELECT Body FROM LFG WHERE nft_number=64").fetchone()
    assert body == GOOD
    assert _run(index_db, app_db, apply=True) == []


def test_unreadable_live_index_row_blocks_app_only_rewrite(dbs, monkeypatch):
    """A live index row with empty/unreadable attributes may still hide the
    on-ledger typo: LFG.Body must NOT be rewritten (it is the discovery key),
    the edition is reported skipped_unreadable_metadata, and a rerun still
    finds it."""
    index_db, app_db = dbs
    with sqlite3.connect(index_db) as ic:
        ic.execute("UPDATE onchain_nfts SET attributes_json='[]' WHERE nft_id=?", (LIVE_ID,))
        ic.commit()
    calls = _stub_ledger(monkeypatch)
    results = _run(index_db, app_db, apply=True)
    assert [r.status for r in results] == ["skipped_unreadable_metadata"]
    assert calls["modify"] == [] and calls["upload"] == []
    with sqlite3.connect(app_db) as ac:
        (body,) = ac.execute("SELECT Body FROM LFG WHERE nft_number=64").fetchone()
    assert body == BAD  # discovery key preserved
    # a rerun still discovers the edition (nothing was destroyed)
    results2 = _run(index_db, app_db, apply=True)
    assert [r.status for r in results2] == ["skipped_unreadable_metadata"]
