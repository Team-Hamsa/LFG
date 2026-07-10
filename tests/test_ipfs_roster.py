# tests/test_ipfs_roster.py
# #153: mainnet legacy NFTs carry ipfs:// metadata/image URIs. Two fixes under
# test here:
#   A) /api/img must proxy the *.ipfs.dweb.link gateway host that
#      swap_meta.resolve_ipfs emits (hostname-suffix match — NOT a URL-prefix
#      match — so look-alike hosts stay rejected), and
#   B) swap_meta.load_wallet_nfts must consult an injected uri_hex-keyed raw
#      metadata cache (lfg_core.nft_index's uri_metadata_cache table) so a
#      roster load doesn't re-fetch every token's JSON from public IPFS
#      gateways on every request.
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

import asyncio  # noqa: E402
import sqlite3  # noqa: E402
from typing import Any  # noqa: E402
from urllib.parse import quote  # noqa: E402

import pytest  # noqa: E402

from lfg_core import nft_index, swap_meta  # noqa: E402
from lfg_service import app as server  # noqa: E402


def _img_request(url: str):
    from aiohttp.test_utils import make_mocked_request

    return make_mocked_request("GET", "/api/img?u=" + quote(url, safe=""))


# --- A) image proxy: allow the resolve_ipfs gateway host, reject look-alikes ---


def test_img_proxy_accepts_ipfs_gateway_host(monkeypatch):
    fetched = []

    async def fake_fetch(url):
        fetched.append(url)
        return b"\x89PNG fake", "image/png"

    monkeypatch.setattr(server, "_fetch_cdn", fake_fetch)
    # exactly the shape swap_meta.resolve_ipfs("ipfs://<cid>/2946.png") emits
    url = "https://bafybeih3g6qo7ozdpczkppnnxymo546xnbihlvizwphlt2gsse6enmsnn4.ipfs.dweb.link/2946.png"
    loop = asyncio.get_event_loop()
    resp = loop.run_until_complete(server.handle_img(_img_request(url)))
    assert resp.status == 200
    assert fetched == [url]


def test_img_proxy_rejects_ipfs_gateway_look_alikes(monkeypatch):
    async def fake_fetch(url):  # pragma: no cover - must never be reached
        raise AssertionError("look-alike URL was fetched")

    monkeypatch.setattr(server, "_fetch_cdn", fake_fetch)
    loop = asyncio.get_event_loop()
    for bad in (
        # suffix appears in the path, not the host
        "https://evil.example/cid.ipfs.dweb.link/x.png",
        # host merely *contains* the gateway suffix
        "https://cid.ipfs.dweb.link.evil.example/x.png",
        # no subdomain label before the suffix ("xipfs" is not ".ipfs")
        "https://xipfs.dweb.link/x.png",
        # plain http is not allowed
        "http://cid.ipfs.dweb.link/x.png",
    ):
        resp = loop.run_until_complete(server.handle_img(_img_request(bad)))
        assert resp.status == 400, f"{bad} should be rejected"


# --- B) uri_hex-keyed raw metadata cache ---

_URI_HEX_1 = "697066733A2F2F6261667931"  # any stable hex key
_URI_HEX_2 = "697066733A2F2F6261667932"

_META_1 = {
    "name": "LFGO #12",
    "image": "ipfs://bafy1/12.png",
    "attributes": [{"trait_type": "Body", "value": "Buck"}],
}
_META_2 = {
    "name": "LFGO #34",
    "image": "ipfs://bafy2/34.png",
    "attributes": [{"trait_type": "Body", "value": "Doe"}],
}


def _cache_conn() -> sqlite3.Connection:
    conn = nft_index.init_db(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


def test_meta_cache_roundtrip():
    conn = _cache_conn()
    assert nft_index.meta_cache_get_many(conn, [_URI_HEX_1]) == {}
    nft_index.meta_cache_put_many(conn, {_URI_HEX_1: _META_1})
    got = nft_index.meta_cache_get_many(conn, [_URI_HEX_1, _URI_HEX_2])
    assert got == {_URI_HEX_1: _META_1}
    # re-put is idempotent, not an error
    nft_index.meta_cache_put_many(conn, {_URI_HEX_1: _META_1})


def test_meta_cache_is_uri_hex_case_insensitive():
    """Ledger account_nfts returns UPPERCASE hex URIs while onchain_nfts rows
    store lowercase — the deployed mainnet cache joined 0/3535 live tokens
    because of exactly this. Keys must round-trip across case, and results
    must come back keyed by whatever form the caller asked with."""
    conn = _cache_conn()
    nft_index.meta_cache_put_many(conn, {_URI_HEX_1.upper(): _META_1})
    got = nft_index.meta_cache_get_many(conn, [_URI_HEX_1.lower()])
    assert got == {_URI_HEX_1.lower(): _META_1}
    nft_index.meta_cache_put_many(conn, {_URI_HEX_2.lower(): _META_2})
    assert nft_index.meta_cache_get_many(conn, [_URI_HEX_2.upper()]) == {
        _URI_HEX_2.upper(): _META_2
    }
    # both cases of one URI collapse to a single stored row, not duplicates
    nft_index.meta_cache_put_many(conn, {_URI_HEX_1.lower(): _META_1})
    n = conn.execute(
        "SELECT COUNT(*) FROM uri_metadata_cache WHERE UPPER(uri_hex) = ?",
        (_URI_HEX_1.upper(),),
    ).fetchone()[0]
    assert n == 1


def test_meta_cache_migrates_preexisting_mixed_case_rows():
    """Rows written before the normalization (uppercase, from ledger URIs)
    must be folded into the canonical form by init_db so old deployments'
    caches keep serving."""
    conn = nft_index.init_db(":memory:")
    conn.execute(
        "INSERT INTO uri_metadata_cache (uri_hex, metadata_json) VALUES (?, ?)",
        (_URI_HEX_1.upper(), '{"name": "LFGO #12"}'),
    )
    conn.commit()
    nft_index.migrate_meta_cache_case(conn)
    got = nft_index.meta_cache_get_many(conn, [_URI_HEX_1.lower()])
    assert got[_URI_HEX_1.lower()]["name"] == "LFGO #12"


def _raw_token(uri_hex: str, nft_id: str) -> dict[str, Any]:
    return {"nft_id": nft_id, "uri_hex": uri_hex, "flags": 25}


@pytest.fixture()
def _no_gateway(monkeypatch):
    """Fail the test if load_wallet_nfts hits the network for a cached token."""

    async def boom(uri_hex, http=None):  # pragma: no cover - must not run
        raise AssertionError(f"unexpected gateway fetch for {uri_hex}")

    monkeypatch.setattr(swap_meta, "fetch_metadata", boom)


def test_load_wallet_nfts_serves_cached_metadata_without_fetch(_no_gateway):
    conn = _cache_conn()
    nft_index.meta_cache_put_many(conn, {_URI_HEX_1: _META_1, _URI_HEX_2: _META_2})
    cache = nft_index.UriMetadataCache(conn)

    async def fake_account_nfts(wallet, issuer):
        return [_raw_token(_URI_HEX_1, "A" * 64), _raw_token(_URI_HEX_2, "B" * 64)]

    nfts = asyncio.get_event_loop().run_until_complete(
        swap_meta.load_wallet_nfts("rWallet", fake_account_nfts, meta_cache=cache)
    )
    assert [n["number"] for n in nfts] == [12, 34]


def test_load_wallet_nfts_fetches_misses_and_backfills_cache(monkeypatch):
    conn = _cache_conn()
    nft_index.meta_cache_put_many(conn, {_URI_HEX_1: _META_1})
    cache = nft_index.UriMetadataCache(conn)
    fetched = []

    async def fake_fetch(uri_hex, http=None):
        fetched.append(uri_hex)
        return _META_2

    monkeypatch.setattr(swap_meta, "fetch_metadata", fake_fetch)

    async def fake_account_nfts(wallet, issuer):
        return [_raw_token(_URI_HEX_1, "A" * 64), _raw_token(_URI_HEX_2, "B" * 64)]

    nfts = asyncio.get_event_loop().run_until_complete(
        swap_meta.load_wallet_nfts("rWallet", fake_account_nfts, meta_cache=cache)
    )
    assert [n["number"] for n in nfts] == [12, 34]
    # only the miss was fetched, and it is now cached for the next load
    assert fetched == [_URI_HEX_2]
    assert nft_index.meta_cache_get_many(conn, [_URI_HEX_2]) == {_URI_HEX_2: _META_2}


def test_meta_cache_get_many_exceeds_sqlite_param_limit():
    """A whale wallet can hold more distinct URIs than SQLite's per-statement
    parameter limit (999 on older builds, 32766 since 3.32); get_many must
    chunk, not raise — a raise would silently disable the cache for exactly
    the wallets that need it most (Greptile #154 P2). 40k keys exceeds the
    limit on every build."""
    conn = _cache_conn()
    keys = [f"{i:08X}" for i in range(40_000)]
    nft_index.meta_cache_put_many(conn, {k: {"name": f"LFGO #{i}"} for i, k in enumerate(keys)})
    got = nft_index.meta_cache_get_many(conn, keys)
    assert len(got) == 40_000


def test_load_wallet_nfts_fetches_duplicate_uri_once(monkeypatch):
    """Duplicate editions share a uri_hex. The URI must be fetched once and
    the result reused for every token carrying it — a repeat fetch whose
    second attempt fails must not clobber the first success (Greptile #154 P1)."""
    conn = _cache_conn()
    cache = nft_index.UriMetadataCache(conn)
    calls = []

    async def fake_fetch(uri_hex, http=None):
        calls.append(uri_hex)
        if len(calls) > 1:  # a second fetch of the same URI would flake to None
            return None
        return _META_1

    monkeypatch.setattr(swap_meta, "fetch_metadata", fake_fetch)

    async def fake_account_nfts(wallet, issuer):
        return [_raw_token(_URI_HEX_1, "A" * 64), _raw_token(_URI_HEX_1, "B" * 64)]

    nfts = asyncio.get_event_loop().run_until_complete(
        swap_meta.load_wallet_nfts("rWallet", fake_account_nfts, meta_cache=cache)
    )
    assert calls == [_URI_HEX_1]
    assert [n["nft_id"] for n in nfts] == ["A" * 64, "B" * 64]


def test_service_wallet_nfts_attaches_index_cache(monkeypatch, tmp_path):
    """Both service call sites (roster + swap-start re-verify) go through
    _wallet_nfts, which must hand load_wallet_nfts a UriMetadataCache bound to
    the per-network index DB."""
    monkeypatch.setenv("ONCHAIN_DB_PATH", str(tmp_path / "onchain_test.db"))
    seen = {}

    async def fake_load(wallet, get_account_nfts, meta_cache=None):
        seen["wallet"] = wallet
        seen["meta_cache"] = meta_cache
        return []

    monkeypatch.setattr(server.swap_meta, "load_wallet_nfts", fake_load)
    out = asyncio.get_event_loop().run_until_complete(server._wallet_nfts("rWallet"))
    assert out == []
    assert seen["wallet"] == "rWallet"
    assert isinstance(seen["meta_cache"], nft_index.UriMetadataCache)


def test_load_wallet_nfts_survives_broken_cache(monkeypatch):
    """A cache failure must degrade to the live fetch, never break the roster."""

    class BrokenCache:
        def get_many(self, uri_hexes):
            raise sqlite3.OperationalError("disk I/O error")

        def put_many(self, metas):
            raise sqlite3.OperationalError("disk I/O error")

    async def fake_fetch(uri_hex, http=None):
        return _META_1

    monkeypatch.setattr(swap_meta, "fetch_metadata", fake_fetch)

    async def fake_account_nfts(wallet, issuer):
        return [_raw_token(_URI_HEX_1, "A" * 64)]

    nfts = asyncio.get_event_loop().run_until_complete(
        swap_meta.load_wallet_nfts("rWallet", fake_account_nfts, meta_cache=BrokenCache())
    )
    assert [n["number"] for n in nfts] == [12]


# --- D) raw ipfs:// image URIs (leaderboard rows serve onchain_nfts.image
#         verbatim, which is ipfs:// for 3,474 of 3,535 live mainnet tokens) ---


def test_img_proxy_resolves_raw_ipfs_uri(monkeypatch):
    """/api/img?u=ipfs://<cid>/<path> must be translated to the dweb.link
    gateway and served — the leaderboard client passes the index's raw image
    URI straight through, so without this every legacy image 400s."""
    fetched = []

    async def fake_fetch(url):
        fetched.append(url)
        return b"\x89PNG fake", "image/png"

    monkeypatch.setattr(server, "_fetch_cdn", fake_fetch)
    cid = "bafybeih3g6qo7ozdpczkppnnxymo546xnbihlvizwphlt2gsse6enmsnn4"
    resp = asyncio.get_event_loop().run_until_complete(
        server.handle_img(_img_request(f"ipfs://{cid}/2946.png"))
    )
    assert resp.status == 200
    assert fetched == [f"https://{cid}.ipfs.dweb.link/2946.png"]


# --- E) roster must survive one transient XRPL fetch failure (#153: a single
#         websocket open timeout was 502ing the whole swapper) ---


def test_load_wallet_nfts_retries_transient_account_fetch():
    conn = _cache_conn()
    nft_index.meta_cache_put_many(conn, {_URI_HEX_1: _META_1})
    calls = []

    async def flaky_account_nfts(wallet, issuer):
        calls.append(1)
        if len(calls) == 1:
            raise asyncio.TimeoutError  # str() is empty — the mainnet 502 signature
        return [_raw_token(_URI_HEX_1, "A" * 64)]

    nfts = asyncio.get_event_loop().run_until_complete(
        swap_meta.load_wallet_nfts(
            "rWallet", flaky_account_nfts, meta_cache=nft_index.UriMetadataCache(conn)
        )
    )
    assert len(calls) == 2
    assert [n["number"] for n in nfts] == [12]


def test_load_wallet_nfts_does_not_retry_programming_errors():
    """The retry is for transient network failures only — a logic bug
    (e.g. ValueError) must surface immediately, not fire twice."""
    calls = []

    async def buggy_account_nfts(wallet, issuer):
        calls.append(1)
        raise ValueError("not transient")

    with pytest.raises(ValueError):
        asyncio.get_event_loop().run_until_complete(
            swap_meta.load_wallet_nfts("rWallet", buggy_account_nfts)
        )
    assert len(calls) == 1


def test_load_wallet_nfts_raises_after_persistent_failure():
    calls = []

    async def dead_account_nfts(wallet, issuer):
        calls.append(1)
        raise asyncio.TimeoutError

    with pytest.raises(asyncio.TimeoutError):
        asyncio.get_event_loop().run_until_complete(
            swap_meta.load_wallet_nfts("rWallet", dead_account_nfts)
        )
    assert len(calls) == 2  # exactly one retry, then surface the error
