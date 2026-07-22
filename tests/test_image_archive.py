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


# ------------------------------------------------- /api/img byte ranges (#250)
#
# Animated NFTs route their .mp4 through /api/img; iOS WebKit's media loader
# probes with `Range: bytes=0-1` and refuses progressive mp4 playback unless
# the server answers 206 with a Content-Range — so both the archive-hit and
# CDN-fallback responses must honor single byte ranges.


def _range_request(url: str, range_header: str | None):
    from aiohttp.test_utils import make_mocked_request

    headers = {"Range": range_header} if range_header else {}
    return make_mocked_request("GET", "/api/img?u=" + quote(url, safe=""), headers=headers)


def _proxy_fetch(monkeypatch, body: bytes, ctype: str) -> None:
    async def fake_fetch(url):
        return body, ctype

    monkeypatch.setattr(server, "_fetch_cdn", fake_fetch)


def test_img_proxy_range_returns_206_slice(monkeypatch, tmp_path):
    """The exact WebKit probe: bytes=0-1 against a CDN-proxied mp4."""
    _seed_env(monkeypatch, tmp_path, with_file=False)
    _proxy_fetch(monkeypatch, b"mp4 body bytes", "video/mp4")
    resp = asyncio.get_event_loop().run_until_complete(
        server.handle_img(_range_request(_IPFS_URL, "bytes=0-1"))
    )
    assert resp.status == 206
    assert resp.body == b"mp"
    assert resp.headers["Content-Range"] == "bytes 0-1/14"
    assert resp.headers["Accept-Ranges"] == "bytes"


def test_img_proxy_range_open_ended_and_suffix(monkeypatch, tmp_path):
    _seed_env(monkeypatch, tmp_path, with_file=False)
    _proxy_fetch(monkeypatch, b"0123456789", "video/mp4")
    run = asyncio.get_event_loop().run_until_complete
    resp = run(server.handle_img(_range_request(_IPFS_URL, "bytes=4-")))
    assert (resp.status, resp.body) == (206, b"456789")
    assert resp.headers["Content-Range"] == "bytes 4-9/10"
    resp = run(server.handle_img(_range_request(_IPFS_URL, "bytes=-3")))
    assert (resp.status, resp.body) == (206, b"789")
    assert resp.headers["Content-Range"] == "bytes 7-9/10"


def test_img_proxy_range_unsatisfiable_is_416(monkeypatch, tmp_path):
    _seed_env(monkeypatch, tmp_path, with_file=False)
    _proxy_fetch(monkeypatch, b"0123456789", "video/mp4")
    resp = asyncio.get_event_loop().run_until_complete(
        server.handle_img(_range_request(_IPFS_URL, "bytes=100-"))
    )
    assert resp.status == 416
    assert resp.headers["Content-Range"] == "bytes */10"


def test_img_proxy_no_range_advertises_accept_ranges(monkeypatch, tmp_path):
    """Plain 200s now advertise Accept-Ranges so media loaders know to probe;
    a malformed Range header is ignored per RFC 9110 (200, not an error)."""
    _seed_env(monkeypatch, tmp_path, with_file=False)
    _proxy_fetch(monkeypatch, b"0123456789", "video/mp4")
    run = asyncio.get_event_loop().run_until_complete
    for header in (None, "bytes=", "items=0-1", "bytes=2-1-0"):
        resp = run(server.handle_img(_range_request(_IPFS_URL, header)))
        assert (resp.status, resp.body) == (200, b"0123456789"), header
        assert resp.headers["Accept-Ranges"] == "bytes"


def test_img_archive_hit_honors_range(monkeypatch, tmp_path):
    """Archive-served bodies must be range-capable too (same media loader)."""
    _seed_env(monkeypatch, tmp_path, with_file=True)

    async def boom(url):  # pragma: no cover - must never be reached
        raise AssertionError("archived image hit the network")

    monkeypatch.setattr(server, "_fetch_cdn", boom)
    resp = asyncio.get_event_loop().run_until_complete(
        server.handle_img(_range_request(_IPFS_URL, "bytes=0-3"))
    )
    assert resp.status == 206
    assert resp.body == b"\x89PNG"
    assert resp.headers["Content-Range"].startswith("bytes 0-3/")


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


def test_edition_for_url_shared_by_many_editions_is_ambiguous():
    """A URL that several LIVE editions point at is not per-edition art — the
    blank silhouette every harvested character shares is the real case. The
    archive is keyed by edition and holds each one's own (now stale) dressed
    still, so resolving to MIN() served edition A's old artwork for edition
    B's blank. Ambiguous URLs must fall through to the real fetch."""
    conn = _index_conn()
    blank = "https://nft.example.com/blank/silhouette.png"
    _insert(conn, nft_id="C" * 64, nft_number=3557, image=blank)
    _insert(conn, nft_id="D" * 64, nft_number=3569, image=blank)
    assert image_archive.edition_for_url(conn, blank) is None


def test_edition_for_url_still_resolves_when_duplicates_are_burned():
    """Only LIVE rows create ambiguity: an edition whose burned predecessors
    carried the same URL still resolves (that is one edition's own art)."""
    conn = _index_conn()
    url = "https://cdn.example.com/LFGO/9/9_0_abc.png"
    _insert(conn, nft_id="E" * 64, nft_number=9, image=url, is_burned=1)
    _insert(conn, nft_id="F" * 64, nft_number=9, image=url)
    assert image_archive.edition_for_url(conn, url) == 9


def test_edition_for_url_same_edition_twice_live_is_not_ambiguous():
    """Two live tokens of the SAME edition (duplicate mints) share art — that
    is unambiguous, the edition is still the answer."""
    conn = _index_conn()
    url = "https://cdn.example.com/LFGO/11/11_0_abc.png"
    _insert(conn, nft_id="1" * 64, nft_number=11, image=url)
    _insert(conn, nft_id="2" * 64, nft_number=11, image=url)
    assert image_archive.edition_for_url(conn, url) == 11


def test_drop_archived_removes_still_and_thumb(monkeypatch, tmp_path):
    """Economy ops change an edition's art without composing into the archive,
    so the archived copy must be invalidated or /api/img keeps serving it."""
    monkeypatch.setenv("IMAGES_DIR", str(tmp_path))
    (tmp_path / "42.png").write_bytes(b"\x89PNG old still")
    (tmp_path / "42.mp4").write_bytes(b"old animation")
    thumbs = tmp_path / image_archive.THUMB_SUBDIR
    thumbs.mkdir()
    (thumbs / "42.webp").write_bytes(b"old thumb")

    image_archive.drop_archived("testnet", 42)

    assert image_archive.local_image("testnet", 42) is None
    assert image_archive.local_thumb("testnet", 42) is None


def test_drop_archived_is_silent_when_nothing_archived(monkeypatch, tmp_path):
    monkeypatch.setenv("IMAGES_DIR", str(tmp_path))
    image_archive.drop_archived("testnet", 999)  # must not raise


def test_drop_archived_removes_the_thumb_before_the_still(monkeypatch, tmp_path):
    """handle_img consults the thumb BEFORE the full still, so a partial
    deletion must never leave the thumbnail as the surviving copy. Record the
    unlink order and assert the thumb goes first."""
    monkeypatch.setenv("IMAGES_DIR", str(tmp_path))
    (tmp_path / "42.png").write_bytes(b"still")
    thumbs = tmp_path / image_archive.THUMB_SUBDIR
    thumbs.mkdir()
    (thumbs / "42.webp").write_bytes(b"thumb")

    removed: list[str] = []
    real_remove = os.remove

    def _tracking_remove(path):
        removed.append(os.path.basename(path))
        real_remove(path)

    monkeypatch.setattr(os, "remove", _tracking_remove)
    image_archive.drop_archived("testnet", 42)
    assert removed[0] == "42.webp", removed


def test_drop_archived_continues_after_a_failed_unlink(monkeypatch, tmp_path):
    """One unremovable path must not strand the others (the thumb is attempted
    first, so the still is still cleaned when a later unlink fails)."""
    monkeypatch.setenv("IMAGES_DIR", str(tmp_path))
    (tmp_path / "42.png").write_bytes(b"still")
    thumbs = tmp_path / image_archive.THUMB_SUBDIR
    thumbs.mkdir()
    (thumbs / "42.webp").write_bytes(b"thumb")

    real_remove = os.remove

    def _flaky_remove(path):
        if path.endswith("42.webp"):
            raise OSError("permission denied")
        real_remove(path)

    monkeypatch.setattr(os, "remove", _flaky_remove)
    image_archive.drop_archived("testnet", 42)  # must not raise
    assert image_archive.local_image("testnet", 42) is None
