# tests/test_backfill_metadata_cache.py
# scripts/backfill_metadata_cache.py fills the uri_metadata_cache from local
# sources (case migration + Bithomp CSV) with a live fetch only for leftovers,
# so the local-first swapper roster never needs the network on its hot path.
#
# Env-guard preamble (verbatim from tests/test_seasons.py lines 1-18): importing
# lfg_core.config freezes its constants (e.g. IMG_PROXY_ALLOWED_BASES,
# LAYER_SOURCE) at import time; set the same defaults test_smoke.py uses so
# collection order can't strand them.
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

import csv  # noqa: E402

from lfg_core import nft_index  # noqa: E402
from scripts import backfill_metadata_cache as bmc  # noqa: E402

_IPFS_URI = "ipfs://bafybackfill/12.json"
_IPFS_HEX = _IPFS_URI.encode().hex()
_CDN_URI = "https://nft.pullzone.example/output/12_3.json"
_CDN_HEX = _CDN_URI.encode().hex()


def _conn():
    conn = nft_index.init_db(":memory:")
    return conn


def _insert_live(conn, nft_id: str, uri_hex: str):
    conn.execute(
        "INSERT INTO onchain_nfts (nft_id, uri_hex, is_burned) VALUES (?, ?, 0)",
        (nft_id, uri_hex),
    )
    conn.commit()


def _write_csv(path, rows):
    fields = ["NFT ID", "Name", "URI", "Image", "Video", "Attribute Body", "Attribute Head"]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def test_csv_metadata_reconstructs_legacy_ipfs_row():
    got = bmc.csv_metadata(
        {
            "NFT ID": "A" * 64,
            "Name": "Let's Effing Go! #12",
            "URI": _IPFS_URI,
            "Image": "ipfs://bafybackfill/12.png",
            "Video": "",
            "Attribute Body": "Buck Straight",
            "Attribute Head": "Halo",
        }
    )
    assert got is not None
    uri_hex, meta = got
    assert uri_hex == _IPFS_HEX
    assert meta["name"] == "Let's Effing Go! #12"
    assert meta["image"] == "ipfs://bafybackfill/12.png"
    assert meta["video"] is None
    assert {"trait_type": "Head", "value": "Halo"} in meta["attributes"]
    assert "burnCount" not in meta  # legacy mints have none; 0 is the default


def test_csv_metadata_refuses_non_ipfs_uris():
    """Swap outputs (CDN URIs) carry a burnCount the CSV lacks — a wrong 0
    would collide upload basenames on the next swap. Only the live fetch may
    source those."""
    row = {"NFT ID": "A" * 64, "Name": "Let's Effing Go! #12", "URI": _CDN_URI, "Image": ""}
    assert bmc.csv_metadata(row) is None
    assert bmc.csv_metadata({**row, "URI": ""}) is None
    assert bmc.csv_metadata({**row, "URI": _IPFS_URI, "Name": ""}) is None


def test_run_fills_from_csv_then_fetches_leftovers(tmp_path, monkeypatch):
    conn = _conn()
    _insert_live(conn, "A" * 64, _IPFS_HEX)
    _insert_live(conn, "B" * 64, _CDN_HEX)
    csv_path = tmp_path / "data.csv"
    _write_csv(
        csv_path,
        [
            {
                "NFT ID": "A" * 64,
                "Name": "Let's Effing Go! #12",
                "URI": _IPFS_URI,
                "Image": "ipfs://bafybackfill/12.png",
                "Attribute Body": "Buck Straight",
            }
        ],
    )

    async def fake_fetch(http, uri_hex):
        assert uri_hex == _CDN_HEX  # the ipfs one must be satisfied by the CSV
        return {"name": "Let's Effing Go! #13", "image": "x", "attributes": [], "burnCount": 3}

    monkeypatch.setattr(nft_index, "fetch_metadata_multi", fake_fetch)
    stats = bmc.run(conn, [str(csv_path)], fetch=True)
    assert stats["from_csv"] == 1
    assert stats["fetched"] == 1
    assert stats["still_missing"] == 0
    cached = nft_index.meta_cache_get_many(conn, [_IPFS_HEX, _CDN_HEX])
    assert cached[_IPFS_HEX]["name"] == "Let's Effing Go! #12"
    assert cached[_CDN_HEX]["burnCount"] == 3


def test_run_is_idempotent_and_respects_no_fetch(tmp_path, monkeypatch):
    conn = _conn()
    _insert_live(conn, "A" * 64, _IPFS_HEX)
    nft_index.meta_cache_put_many(conn, {_IPFS_HEX: {"name": "Let's Effing Go! #12"}})

    async def boom(http, uri_hex):  # pragma: no cover - must not run
        raise AssertionError("cached URI was re-fetched")

    monkeypatch.setattr(nft_index, "fetch_metadata_multi", boom)
    stats = bmc.run(conn, [], fetch=True)
    assert stats["already_cached"] == 1
    assert stats["still_missing"] == 0

    _insert_live(conn, "B" * 64, _CDN_HEX)
    stats = bmc.run(conn, [], fetch=False)
    assert stats["fetched"] == 0
    assert stats["still_missing"] == 1


def test_run_migrates_uppercase_cache_rows(monkeypatch):
    """Rows cached before normalization (UPPERCASE ledger URIs) must satisfy
    lowercase index URIs after the case-migration pass — no refetch."""
    conn = _conn()
    _insert_live(conn, "A" * 64, _IPFS_HEX.lower())
    conn.execute(
        "INSERT INTO uri_metadata_cache (uri_hex, metadata_json) VALUES (?, ?)",
        (_IPFS_HEX.upper(), '{"name": "Let\'s Effing Go! #12"}'),
    )
    conn.commit()

    async def boom(http, uri_hex):  # pragma: no cover - must not run
        raise AssertionError("case-migrated URI was re-fetched")

    monkeypatch.setattr(nft_index, "fetch_metadata_multi", boom)
    stats = bmc.run(conn, [], fetch=True)
    assert stats["case_migrated"] == 1
    assert stats["still_missing"] == 0
