# tests/test_checkout_store_guard.py
# The root conftest.py pins every per-network store (app DB, onchain/history
# DBs, image archive, X state, journal dirs, layer cache) into a per-session
# temp root, and guards the checkout: an audit hook fails any test that still
# opens a store inside the repo root / starting CWD, and the session fails if
# a store file appears or vanishes there. Before the pins, the suite wrote a
# worktree's lfg_nfts_testnet.db full of identities, sign requests and
# free-mint claims — run from ~/LFG those rows would have landed in prod
# (2026-09-16, same class as the 2026-09-08 fixture-leak incident).
import json
import os
import sqlite3
import subprocess
import sys

import pytest

import conftest
from lfg_core import (
    bulk_mint_flow,
    burn2mint_flow,
    config,
    db_path,
    history_store,
    image_archive,
    nft_index,
)

GUARD = conftest._CHECKOUT_STORE_GUARD


def _inside_checkout(path: str) -> bool:
    abspath = os.path.abspath(path)
    return any(abspath.startswith(root.rstrip(os.sep) + os.sep) for root in GUARD.roots)


def test_guard_roots_cover_the_repo_root_and_starting_cwd():
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    assert repo_root in GUARD.roots
    assert os.getcwd() in GUARD.roots or os.path.realpath(os.getcwd()) in GUARD.roots


def test_every_store_path_resolves_outside_the_checkout():
    paths = {
        "db_path.app_db_path()": db_path.app_db_path(),
        "db_path.app_db_path('mainnet')": db_path.app_db_path("mainnet"),
        "db_path.app_db_path('testnet')": db_path.app_db_path("testnet"),
        "config.DB_PATH": config.DB_PATH,
        "nft_index.index_db_path('mainnet')": nft_index.index_db_path("mainnet"),
        "nft_index.index_db_path('testnet')": nft_index.index_db_path("testnet"),
        "history_store.history_db_path('mainnet')": history_store.history_db_path("mainnet"),
        "history_store.history_db_path('testnet')": history_store.history_db_path("testnet"),
        "image_archive.archive_dir('mainnet')": image_archive.archive_dir("mainnet"),
        "image_archive.archive_dir('testnet')": image_archive.archive_dir("testnet"),
        "config.X_STATE_DB_PATH": config.X_STATE_DB_PATH,
        "config.ECONOMY_RECORDS_DIR": config.ECONOMY_RECORDS_DIR,
        "config.SWAP_RECORDS_DIR": config.SWAP_RECORDS_DIR,
        "config.LAYER_CACHE_DIR": config.LAYER_CACHE_DIR,
        "$LAYER_DIM_CACHE": os.environ.get("LAYER_DIM_CACHE", ".layer_dimensions_cache.json"),
        "bulk_mint_flow.JOBS_DIR": bulk_mint_flow.JOBS_DIR,
        "burn2mint_flow.JOBS_DIR": burn2mint_flow.JOBS_DIR,
    }
    assert {name: path for name, path in paths.items() if _inside_checkout(path)} == {}


def test_explicit_exports_win_except_the_hard_set_job_dirs(tmp_path):
    # The store pins are setdefault: a deliberate shell export still wins (the
    # guard below still fails the run if it points into the checkout). The two
    # job dirs stay hard-set (#459) — an inherited export must never aim the
    # suite at a live record dir. Import the root conftest fresh in a child
    # with the parent's pins scrubbed, so only the exports below are "shell".
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    pinned = ("DB_PATH", "ONCHAIN_DB_PATH", "BULK_MINT_JOBS_DIR", "BURN2MINT_JOBS_DIR")
    env = {k: v for k, v in os.environ.items() if k not in pinned}
    env.update(
        PYTHONPATH=repo_root,
        DB_PATH=str(tmp_path / "mine.db"),
        BULK_MINT_JOBS_DIR=str(tmp_path / "live_jobs"),
    )
    script = (
        "import json, os, shutil, conftest\n"
        "shutil.rmtree(conftest._STORE_ROOT)\n"
        f"print(json.dumps({{k: os.environ[k] for k in {pinned!r}}}))\n"
        "print(conftest._STORE_ROOT)\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    values_line, store_root = proc.stdout.strip().splitlines()
    values = json.loads(values_line)
    assert values["DB_PATH"] == str(tmp_path / "mine.db")
    assert values["ONCHAIN_DB_PATH"] == os.path.join(store_root, "onchain.db")
    assert values["BULK_MINT_JOBS_DIR"] == os.path.join(store_root, "bulk_mint_jobs")
    assert values["BURN2MINT_JOBS_DIR"] == os.path.join(store_root, "burn2mint_jobs")
    assert not (tmp_path / "live_jobs").exists()


@pytest.mark.parametrize(
    ("rel", "flagged"),
    [
        ("lfg_nfts.db", True),
        ("lfg_nfts_testnet.db", True),
        ("lfg_nfts_testnet.db-wal", True),
        ("onchain_mainnet.db", True),
        ("history_testnet.db-shm", True),
        ("x_state.db", True),
        ("bulk_mint_jobs", True),
        ("burn2mint_jobs/abc.json", True),
        ("economy_records/extract-1.json", True),
        ("swap_records/s1.json", True),
        ("images_mainnet/thumbs/1.webp", True),
        (".layer_cache/male/Body/x.png", True),
        ("reports/fix_iridescent_body-testnet-20260916-202129.json", True),
        (".layer_dimensions_cache.json", True),
        ("trait_config.yaml", False),
        ("lfg_core/config.py", False),
        ("tests/fixtures/sample.db", False),
        ("layers/male/Body/Straight.png", False),
        (".pytest_cache/v/cache/nodeids", False),
    ],
)
def test_guard_classifies_checkout_paths(tmp_path, rel, flagged):
    guard = conftest._CheckoutStoreGuard((str(tmp_path),))
    assert (guard.store_path(str(tmp_path / rel)) is not None) is flagged


def test_guard_ignores_stores_outside_its_roots(tmp_path):
    guard = conftest._CheckoutStoreGuard((str(tmp_path / "checkout"),))
    assert guard.store_path(str(tmp_path / "lfg_nfts.db")) is None
    # A sibling directory that merely shares the root's name as a prefix.
    assert guard.store_path(str(tmp_path / "checkout-2" / "lfg_nfts.db")) is None


def test_guard_audit_event_shapes(tmp_path):
    # Argument shapes as CPython 3.10 raises them: os.* report "no dir_fd" as
    # -1, sqlite3.connect passes the fs-encoded path as bytes.
    guard = conftest._CheckoutStoreGuard((str(tmp_path),))
    db = str(tmp_path / "lfg_nfts_testnet.db")
    record = str(tmp_path / "swap_records" / "s.json")
    outside = str(tmp_path / "scratch" / "s.json.tmp")
    flagged = [
        ("sqlite3.connect", (os.fsencode(db),)),
        ("sqlite3.connect", (os.fsencode(f"file:{db}?mode=ro"),)),
        ("open", (record, "w", 0)),
        ("open", (db, None, 0)),  # absolute os.open()
        ("os.mkdir", (str(tmp_path / "swap_records"), 0o777, -1)),
        ("os.rename", (outside, record, -1, -1)),
        ("os.remove", (db, -1)),
        ("os.listdir", (str(tmp_path / "bulk_mint_jobs"),)),
        ("shutil.rmtree", (str(tmp_path / "images_testnet"),)),
    ]
    ignored = [
        ("sqlite3.connect", (b":memory:",)),
        ("sqlite3.connect", (b"",)),
        ("open", ("lfg_nfts_testnet.db", None, 0)),  # relative os.open(): may be dir_fd-relative
        ("open", (3, "r", 0)),  # an fd
        ("os.remove", ("lfg_nfts_testnet.db", 7)),  # dir_fd-relative
        ("os.scandir", (4,)),
        ("os.listdir", (None,)),
        ("socket.connect", (db,)),
        ("open", ()),  # malformed args must not raise out of the hook
    ]
    for event, args in flagged + ignored:
        guard.audit(event, args)
    assert [(event, path) for _, event, path in guard.hits] == [
        ("sqlite3.connect", db),
        ("sqlite3.connect", db),
        ("open", record),
        ("open", db),
        ("os.mkdir", str(tmp_path / "swap_records")),
        ("os.rename", record),
        ("os.remove", db),
        ("os.listdir", str(tmp_path / "bulk_mint_jobs")),
        ("shutil.rmtree", str(tmp_path / "images_testnet")),
    ]
    assert all("test_guard_audit_event_shapes" in test for test, _, _ in guard.hits)


def test_live_audit_hook_catches_store_access(tmp_path, monkeypatch):
    # End to end through the hook conftest installed with sys.addaudithook:
    # point it at a throwaway "checkout" and touch stores there for real.
    monkeypatch.setattr(GUARD, "roots", (str(tmp_path),))
    start = len(GUARD.hits)
    try:
        sqlite3.connect(tmp_path / "lfg_nfts_testnet.db").close()
        (tmp_path / "burn2mint_jobs").mkdir()
        (tmp_path / "burn2mint_jobs" / "s.json").write_text("{}")
        caught = list(GUARD.hits[start:])
    finally:
        # These hits are the point of the test — drop them so the autouse
        # guard fixture doesn't fail it at teardown.
        del GUARD.hits[start:]
    assert {(event, os.path.relpath(path, tmp_path)) for _, event, path in caught} == {
        ("sqlite3.connect", "lfg_nfts_testnet.db"),
        ("os.mkdir", "burn2mint_jobs"),
        ("open", os.path.join("burn2mint_jobs", "s.json")),
    }
    assert all("test_live_audit_hook_catches_store_access" in test for test, _, _ in caught)


def test_store_entries_track_created_and_removed_stores(tmp_path):
    (tmp_path / "onchain_testnet.db").touch()
    (tmp_path / "onchain_testnet.db-wal").touch()  # sidecars churn under live writers
    (tmp_path / "README.md").touch()
    guard = conftest._CheckoutStoreGuard((str(tmp_path),))
    before = guard.store_entries()
    assert before == {str(tmp_path / "onchain_testnet.db")}

    (tmp_path / "onchain_testnet.db").unlink()
    (tmp_path / "lfg_nfts_testnet.db").touch()
    (tmp_path / "economy_records").mkdir()
    after = guard.store_entries()
    assert after - before == {
        str(tmp_path / "lfg_nfts_testnet.db"),
        str(tmp_path / "economy_records"),
    }
    assert before - after == {str(tmp_path / "onchain_testnet.db")}
