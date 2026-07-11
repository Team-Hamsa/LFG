# tests/test_image_archive.py
# #153/#156: the XRPL is a reference, not our image host. Every live edition's
# art is archived locally (images_<network>/, built by
# scripts/rebuild_cdn_images.py); /api/img must serve an archived edition's
# image straight from disk — mapping the requested URL back to its edition via
# the on-chain index — and only fall back to the CDN/IPFS-gateway proxy when
# the archive misses.
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
from urllib.parse import quote  # noqa: E402

from lfg_core import image_archive, nft_index  # noqa: E402
from lfg_service import app as server  # noqa: E402

_IPFS_URL = "ipfs://bafyarchived/5.png"


# ------------------------------------------------------------- archive_dir


def test_archive_dir_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("IMAGES_DIR", str(tmp_path))
    assert image_archive.archive_dir("mainnet") == str(tmp_path)


def test_archive_dir_default_is_per_network(monkeypatch):
    monkeypatch.delenv("IMAGES_DIR", raising=False)
    assert image_archive.archive_dir("mainnet").endswith("images_mainnet")
    assert image_archive.archive_dir("testnet").endswith("images_testnet")


# ------------------------------------------------------------- local_image


def test_local_image_finds_archived_still(monkeypatch, tmp_path):
    monkeypatch.setenv("IMAGES_DIR", str(tmp_path))
    (tmp_path / "5.png").write_bytes(b"\x89PNG local")
    got = image_archive.local_image("mainnet", 5)
    assert got == (str(tmp_path / "5.png"), "image/png")


def test_local_image_supports_gif(monkeypatch, tmp_path):
    monkeypatch.setenv("IMAGES_DIR", str(tmp_path))
    (tmp_path / "7.gif").write_bytes(b"GIF89a")
    got = image_archive.local_image("mainnet", 7)
    assert got == (str(tmp_path / "7.gif"), "image/gif")


def test_local_image_none_on_miss(monkeypatch, tmp_path):
    monkeypatch.setenv("IMAGES_DIR", str(tmp_path))
    assert image_archive.local_image("mainnet", 999) is None


# --------------------------------------------------------- edition_for_url


def _index_conn() -> sqlite3.Connection:
    conn = nft_index.init_db(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


def _insert(conn, *, nft_id, nft_number, image, is_burned=0):
    conn.execute(
        "INSERT INTO onchain_nfts (nft_id, nft_number, image, is_burned) VALUES (?, ?, ?, ?)",
        (nft_id, nft_number, image, is_burned),
    )
    conn.commit()


def test_edition_for_url_matches_live_row():
    conn = _index_conn()
    _insert(conn, nft_id="A" * 64, nft_number=5, image=_IPFS_URL)
    assert image_archive.edition_for_url(conn, _IPFS_URL) == 5


def test_edition_for_url_ignores_burned_rows():
    conn = _index_conn()
    _insert(conn, nft_id="B" * 64, nft_number=5, image=_IPFS_URL, is_burned=1)
    assert image_archive.edition_for_url(conn, _IPFS_URL) is None


def test_edition_for_url_none_on_miss_and_empty():
    conn = _index_conn()
    assert image_archive.edition_for_url(conn, "ipfs://nope/x.png") is None
    assert image_archive.edition_for_url(conn, "") is None


# ------------------------------------------------- URL-shape canonicalization
#
# The index stores image URLs in mixed shapes: Bithomp-imported rows keep the
# on-chain ipfs:// URI verbatim, while listener-written rows (token_record)
# store the dweb.link-resolved form. Surfaces serve whichever shape their row
# has, so the archive lookup must match a URL against every equivalent form —
# otherwise a swapper/marketplace tile silently degrades to the IPFS proxy.

_DWEB_URL = "https://bafyarchived.ipfs.dweb.link/5.png"


def test_url_forms_covers_raw_and_gateway_shapes():
    assert image_archive.url_forms(_IPFS_URL) == [_IPFS_URL, _DWEB_URL]
    assert image_archive.url_forms(_DWEB_URL) == [_DWEB_URL, _IPFS_URL]
    # path-style gateway URLs (nft_index.IPFS_GATEWAYS) also map back
    assert _IPFS_URL in image_archive.url_forms("https://ipfs.io/ipfs/bafyarchived/5.png")
    # non-IPFS URLs pass through untouched
    cdn = "https://nft.pullzone.example/output/5_1.png"
    assert image_archive.url_forms(cdn) == [cdn]
    assert image_archive.url_forms("") == []


def test_edition_for_url_matches_dweb_request_against_raw_row():
    conn = _index_conn()
    _insert(conn, nft_id="A" * 64, nft_number=5, image=_IPFS_URL)
    assert image_archive.edition_for_url(conn, _DWEB_URL) == 5


def test_edition_for_url_matches_raw_request_against_dweb_row():
    conn = _index_conn()
    _insert(conn, nft_id="A" * 64, nft_number=5, image=_DWEB_URL)
    assert image_archive.edition_for_url(conn, _IPFS_URL) == 5


def test_url_forms_pathless_cid_covers_slash_variants():
    """Six live editions (e.g. #59, #258) carry PATH-LESS ipfs://<cid> image
    URIs — the CID is the file. resolve_ipfs renders those with a trailing
    slash (https://<cid>.ipfs.dweb.link/), so the raw and resolved shapes
    disagree about the slash and an exact-shape lookup misses. All slash
    variants must be equivalent."""
    raw = "ipfs://bafkpathless"
    for form in (raw, raw + "/", "https://bafkpathless.ipfs.dweb.link/"):
        got = image_archive.url_forms(form)
        assert raw in got, form
        assert raw + "/" in got, form
        assert "https://bafkpathless.ipfs.dweb.link/" in got, form


def test_edition_for_url_pathless_cid_dweb_request_matches_raw_row():
    """The exact prod failure: index stores `ipfs://<cid>` (no path), the
    swapper serves the resolved `https://<cid>.ipfs.dweb.link/` — must match."""
    conn = _index_conn()
    _insert(conn, nft_id="A" * 64, nft_number=59, image="ipfs://bafkpathless")
    assert image_archive.edition_for_url(conn, "https://bafkpathless.ipfs.dweb.link/") == 59
    assert image_archive.edition_for_url(conn, "ipfs://bafkpathless/") == 59


def test_edition_for_url_gateway_shapes_still_ignore_burned():
    conn = _index_conn()
    _insert(conn, nft_id="B" * 64, nft_number=5, image=_IPFS_URL, is_burned=1)
    assert image_archive.edition_for_url(conn, _DWEB_URL) is None


# --------------------------------------------- /api/img local-first serving


def _img_request(url: str):
    from aiohttp.test_utils import make_mocked_request

    return make_mocked_request("GET", "/api/img?u=" + quote(url, safe=""))


def _seed_env(monkeypatch, tmp_path, *, with_file: bool) -> None:
    db = tmp_path / "onchain.db"
    monkeypatch.setenv("ONCHAIN_DB_PATH", str(db))
    monkeypatch.setenv("IMAGES_DIR", str(tmp_path / "images"))
    os.makedirs(tmp_path / "images", exist_ok=True)
    conn = nft_index.init_db(str(db))
    _insert(conn, nft_id="A" * 64, nft_number=5, image=_IPFS_URL)
    conn.close()
    if with_file:
        (tmp_path / "images" / "5.png").write_bytes(b"\x89PNG archived bytes")


def test_img_serves_archived_edition_without_network(monkeypatch, tmp_path):
    _seed_env(monkeypatch, tmp_path, with_file=True)

    async def boom(url):  # pragma: no cover - must never be reached
        raise AssertionError("archived image hit the network")

    monkeypatch.setattr(server, "_fetch_cdn", boom)
    resp = asyncio.get_event_loop().run_until_complete(server.handle_img(_img_request(_IPFS_URL)))
    assert resp.status == 200
    assert resp.body == b"\x89PNG archived bytes"
    assert resp.content_type == "image/png"


def test_img_serves_archive_for_dweb_form_of_raw_row(monkeypatch, tmp_path):
    """The swapper serves dweb-resolved image URLs while the index row stores
    the raw ipfs:// URI — the archive must still hit (this exact mismatch sent
    every swapper tile to the flaky public gateway)."""
    _seed_env(monkeypatch, tmp_path, with_file=True)

    async def boom(url):  # pragma: no cover - must never be reached
        raise AssertionError("archived image hit the network")

    monkeypatch.setattr(server, "_fetch_cdn", boom)
    resp = asyncio.get_event_loop().run_until_complete(
        server.handle_img(_img_request("https://bafyarchived.ipfs.dweb.link/5.png"))
    )
    assert resp.status == 200
    assert resp.body == b"\x89PNG archived bytes"


def test_img_falls_back_to_proxy_when_archive_misses(monkeypatch, tmp_path):
    _seed_env(monkeypatch, tmp_path, with_file=False)
    fetched = []

    async def fake_fetch(url):
        fetched.append(url)
        return b"\x89PNG gateway", "image/png"

    monkeypatch.setattr(server, "_fetch_cdn", fake_fetch)
    resp = asyncio.get_event_loop().run_until_complete(server.handle_img(_img_request(_IPFS_URL)))
    assert resp.status == 200
    assert fetched == ["https://bafyarchived.ipfs.dweb.link/5.png"]


def test_img_survives_broken_archive_lookup(monkeypatch, tmp_path):
    """An archive/index failure must degrade to the proxy, never 500."""
    _seed_env(monkeypatch, tmp_path, with_file=True)

    def broken(conn, url):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(server.image_archive, "edition_for_url", broken)

    async def fake_fetch(url):
        return b"\x89PNG gateway", "image/png"

    monkeypatch.setattr(server, "_fetch_cdn", fake_fetch)
    resp = asyncio.get_event_loop().run_until_complete(server.handle_img(_img_request(_IPFS_URL)))
    assert resp.status == 200
