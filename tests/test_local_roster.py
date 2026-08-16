# tests/test_local_roster.py
# Local-first swapper roster: /api/nfts must be served from the listener-fresh
# on-chain index (onchain_<net>.db) + the uri_hex metadata cache — not from a
# live account_nfts ledger call plus per-token IPFS gateway fetches. The
# public gateways failing (mainnet, 2026-07-10) both blanked every swapper
# tile and silently DROPPED uncached NFTs from the roster. The live ledger
# call survives only as a fallback for an unbuilt index.
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
import json  # noqa: E402
from typing import Any  # noqa: E402

from lfg_core import nft_index, swap_meta  # noqa: E402
from lfg_service import app as server  # noqa: E402

_META = {
    "name": "LFGO #12",
    "image": "ipfs://bafylocal/12.png",
    "attributes": [{"trait_type": "Body", "value": "Buck Straight"}],
}
# lowercase on purpose: that is how the index stores uri_hex (vs the ledger's
# uppercase) — the cache join must survive it.
_URI_HEX = "697066733a2f2f62616679726f7374657231"


def _seed_index(tmp_path, monkeypatch, rows: list[dict[str, Any]]):
    db = tmp_path / "onchain_local.db"
    monkeypatch.setenv("ONCHAIN_DB_PATH", str(db))
    conn = nft_index.init_db(str(db))
    for r in rows:
        conn.execute(
            "INSERT INTO onchain_nfts (nft_id, nft_number, owner, is_burned, mutable,"
            " uri_hex, attributes_json, image) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                r["nft_id"],
                r.get("nft_number"),
                r.get("owner"),
                r.get("is_burned", 0),
                r.get("mutable", 1),
                r.get("uri_hex", ""),
                json.dumps(r.get("attributes", [])),
                r.get("image", ""),
            ),
        )
    conn.commit()
    return conn


def _no_network(monkeypatch):
    async def ledger_boom(wallet, issuer):  # pragma: no cover - must not run
        raise AssertionError("roster hit the live ledger")

    async def gateway_boom(uri_hex, http=None):  # pragma: no cover - must not run
        raise AssertionError(f"roster hit the IPFS gateway for {uri_hex}")

    monkeypatch.setattr(server.xrpl_ops, "get_account_nfts", ledger_boom)
    monkeypatch.setattr(swap_meta, "fetch_metadata", gateway_boom)


def _run(coro):
    # get_event_loop (not asyncio.run) on purpose: asyncio.run closes + unsets
    # the loop, breaking the suite-order tests that reuse it.
    return asyncio.get_event_loop().run_until_complete(coro)


def test_wallet_nfts_served_from_index_without_any_network(tmp_path, monkeypatch):
    # realistic token ID: the leading 0019 bytes are the on-ledger flags
    # (burnable+transferable+mutable), which to_token derives flags from
    nft_id = "0019" + "A" * 60
    conn = _seed_index(
        tmp_path,
        monkeypatch,
        [{"nft_id": nft_id, "nft_number": 12, "owner": "rWallet", "uri_hex": _URI_HEX}],
    )
    nft_index.meta_cache_put_many(conn, {_URI_HEX: _META})
    conn.close()
    _no_network(monkeypatch)
    nfts = _run(server._wallet_nfts("rWallet"))
    assert [n["number"] for n in nfts] == [12]
    assert nfts[0]["nft_id"] == nft_id
    assert nfts[0]["mutable"] is True
    assert nfts[0]["uri_hex"] == _URI_HEX


def test_wallet_nfts_never_fetches_inline_and_synthesizes_misses(tmp_path, monkeypatch):
    """A cache miss must not trigger an inline gateway fetch: with the
    gateway dead, ONE permanently-unreadable token stalled every roster load
    the full 20s fetch_metadata timeout (prod 00:56, 225 NFTs held hostage).
    Readable rows (the index has their edition + attributes) are synthesized
    from the row itself; rows the heavyweight backfill couldn't read (no
    edition number) can never pass normalize_nft and are skipped silently."""
    uncached_hex = "697066733a2f2f62616679756e636163686564"
    unreadable_hex = "697066733a2f2f6261666b756e7265616461626c65"
    conn = _seed_index(
        tmp_path,
        monkeypatch,
        [
            {
                "nft_id": "0009" + "A" * 60,
                "nft_number": 12,
                "owner": "rWallet",
                "uri_hex": _URI_HEX,
            },
            {
                "nft_id": "0019" + "B" * 60,
                "nft_number": 13,
                "owner": "rWallet",
                "uri_hex": uncached_hex,
                "image": "ipfs://bafyuncached/13.png",
                "attributes": [{"trait_type": "Body", "value": "Buck Straight"}],
            },
            {
                "nft_id": "0009" + "C" * 60,
                "nft_number": None,
                "owner": "rWallet",
                "uri_hex": unreadable_hex,
            },
        ],
    )
    nft_index.meta_cache_put_many(conn, {_URI_HEX: _META})
    conn.close()
    _no_network(monkeypatch)  # any fetch_metadata call fails the test
    nfts = _run(server._wallet_nfts("rWallet"))
    assert [n["number"] for n in nfts] == [12, 13]
    synth = nfts[1]
    assert "#13" in synth["name"]
    # normalize_nft resolves ipfs:// for every record (cache hits too);
    # url_forms makes this shape archive-hit in /api/img regardless
    assert synth["image"] == "https://bafyuncached.ipfs.dweb.link/13.png"
    assert synth["mutable"] is True  # flags from the 0019 token-ID bytes
    assert synth["uri_hex"] == uncached_hex
    assert {"trait_type": "Body", "value": "Buck Straight"} in synth["attributes"]


def test_wallet_nfts_all_unreadable_wallet_stays_local(tmp_path, monkeypatch):
    """A wallet whose indexed rows are ALL unreadable must return empty
    WITHOUT touching the ledger or gateways (Greptile P1 on #165): those
    tokens are unreachable everywhere — the multi-gateway backfill failed
    too — so the fallback could only re-enter the slow remote path to show
    nothing."""
    conn = _seed_index(
        tmp_path,
        monkeypatch,
        [
            {
                "nft_id": "0009" + "C" * 60,
                "nft_number": None,
                "owner": "rWallet",
                "uri_hex": "697066733a2f2f6261666b756e7265616461626c65",
            }
        ],
    )
    conn.close()
    _no_network(monkeypatch)
    assert _run(server._wallet_nfts("rWallet")) == []


def test_wallet_nfts_excludes_burned_and_foreign_tokens(tmp_path, monkeypatch):
    conn = _seed_index(
        tmp_path,
        monkeypatch,
        [
            {"nft_id": "A" * 64, "nft_number": 12, "owner": "rWallet", "uri_hex": _URI_HEX},
            {
                "nft_id": "B" * 64,
                "nft_number": 13,
                "owner": "rWallet",
                "uri_hex": _URI_HEX,
                "is_burned": 1,
            },
            {"nft_id": "C" * 64, "nft_number": 14, "owner": "rOther", "uri_hex": _URI_HEX},
        ],
    )
    nft_index.meta_cache_put_many(conn, {_URI_HEX: _META})
    conn.close()
    _no_network(monkeypatch)
    nfts = _run(server._wallet_nfts("rWallet"))
    assert [n["nft_id"] for n in nfts] == ["A" * 64]


def test_wallet_nfts_empty_index_result_falls_back_to_ledger(tmp_path, monkeypatch):
    """An index with rows for other wallets but none for this one may just be
    partially backfilled — it must not silently hide holdings (Greptile P1
    on #162): an empty local result always re-checks the live ledger."""
    conn = _seed_index(
        tmp_path,
        monkeypatch,
        [{"nft_id": "C" * 64, "nft_number": 14, "owner": "rOther", "uri_hex": _URI_HEX}],
    )
    nft_index.meta_cache_put_many(conn, {_URI_HEX: _META})
    conn.close()
    called = []

    async def fake_account_nfts(wallet, issuer):
        called.append(wallet)
        return [{"nft_id": "A" * 64, "uri_hex": _URI_HEX, "flags": 25}]

    monkeypatch.setattr(server.xrpl_ops, "get_account_nfts", fake_account_nfts)
    nfts = _run(server._wallet_nfts("rWallet"))
    assert called == ["rWallet"]
    assert [n["number"] for n in nfts] == [12]


def test_wallet_nfts_empty_wallet_survives_dead_ledger(tmp_path, monkeypatch):
    """When the index legitimately says "this wallet holds nothing" and the
    fallback ledger check itself fails, the empty local answer stands — an
    empty roster beats a 502 while the public node is down."""
    conn = _seed_index(
        tmp_path,
        monkeypatch,
        [{"nft_id": "C" * 64, "nft_number": 14, "owner": "rOther", "uri_hex": _URI_HEX}],
    )
    conn.close()
    _no_network(monkeypatch)  # the ledger call raising IS the scenario
    assert _run(server._wallet_nfts("rWallet")) == []


def test_wallet_nfts_falls_back_to_ledger_when_index_unbuilt(tmp_path, monkeypatch):
    """A zero-row index means it was never backfilled on this deployment —
    the live account_nfts path must still serve."""
    _seed_index(tmp_path, monkeypatch, []).close()
    called = []

    async def fake_account_nfts(wallet, issuer):
        called.append(wallet)
        return [{"nft_id": "A" * 64, "uri_hex": _URI_HEX, "flags": 25}]

    async def fake_fetch(uri_hex, http=None):
        return _META

    monkeypatch.setattr(server.xrpl_ops, "get_account_nfts", fake_account_nfts)
    monkeypatch.setattr(swap_meta, "fetch_metadata", fake_fetch)
    nfts = _run(server._wallet_nfts("rWallet"))
    assert called == ["rWallet"]
    assert [n["number"] for n in nfts] == [12]


def test_to_token_derives_flags_from_nft_id_when_mutable_unknown():
    """3487/3535 live mainnet rows carry mutable=NULL (Bithomp CSV import).
    Guessing 0 would silently route a genuinely mutable NFT down the swap
    burn-remint path instead of NFTokenModify. The NFTokenID's first two
    bytes ARE the on-ledger flags (verified across the live set: 0009 ↔
    non-mutable, 0019 ↔ mutable) — derive from there, no network needed."""
    base = "1B58D1AE1BC312BEF9C68233FB0C8CF6A338F7C227BE2FA32A85049438CA"
    rec = lambda nft_id, mutable: nft_index.OnchainNft(  # noqa: E731
        nft_id=nft_id,
        nft_number=1,
        owner="rWallet",
        is_burned=False,
        mutable=mutable,
        uri_hex=_URI_HEX,
        body="male",
        attributes=[],
        image="",
        ledger_index=None,
    )
    assert nft_index.to_token(rec("0019" + base, None))["flags"] == 0x0019
    assert nft_index.to_token(rec("0009" + base, None))["flags"] == 0x0009
    # an explicit column still wins for a malformed/unparseable ID
    assert nft_index.to_token(rec("zzzz" + base, True))["flags"] & nft_index.NFT_FLAG_MUTABLE
    assert nft_index.to_token(rec("zzzz" + base, None))["flags"] == 0


def test_wallet_nfts_mutable_true_for_null_column_flag19_token(tmp_path, monkeypatch):
    """End-to-end: a mutable-by-ID token whose index row predates the
    listener's mutable column must reach the swap flow as mutable=True."""
    conn = _seed_index(
        tmp_path,
        monkeypatch,
        [
            {
                "nft_id": "0019"
                + "1B58D1AE1BC312BEF9C68233FB0C8CF6A338F7C227BE2FA32A85"
                + "049438E5",
                "nft_number": 12,
                "owner": "rWallet",
                "uri_hex": _URI_HEX,
                "mutable": None,
            }
        ],
    )
    nft_index.meta_cache_put_many(conn, {_URI_HEX: _META})
    conn.close()
    _no_network(monkeypatch)
    nfts = _run(server._wallet_nfts("rWallet"))
    assert nfts[0]["mutable"] is True


def test_swap_fee_quote_is_time_bounded(monkeypatch):
    """The fee quote is the roster's one remaining live-ledger touch (BRIX
    balance + AMM rate are not in any local store). A hung public node must
    degrade it to None, never stall /api/nfts."""

    async def hangs(wallet, total):
        await asyncio.sleep(3600)

    monkeypatch.setattr(server.swap_flow, "detect_swap_payment", hangs)
    monkeypatch.setattr(server, "_SWAP_FEE_QUOTE_TIMEOUT", 0.05)
    assert _run(server._swap_fee_quote("rWallet")) is None


def test_wallet_nfts_cache_hit_serves_repointed_index_image(tmp_path, monkeypatch):
    """#286 repointed onchain_nfts.image ipfs://->CDN, but the uri metadata
    cache keeps the on-chain ipfs:// URI. The roster must serve the index
    row's (archive-mappable) URL over the cache's, or /api/img misses the
    local archive and falls through to the dead IPFS gateway — blank tiles
    for every swapped edition (258/266/281/284 on mainnet, 2026-07-20)."""
    nft_id = "0019" + "B" * 60
    cdn_url = "https://lfgo.b-cdn.net/LFGO/12/12_2.png"
    conn = _seed_index(
        tmp_path,
        monkeypatch,
        [
            {
                "nft_id": nft_id,
                "nft_number": 12,
                "owner": "rWallet",
                "uri_hex": _URI_HEX,
                "image": cdn_url,
            }
        ],
    )
    nft_index.meta_cache_put_many(conn, {_URI_HEX: _META})
    conn.close()
    _no_network(monkeypatch)
    nfts = _run(server._wallet_nfts("rWallet"))
    assert nfts[0]["image"] == cdn_url
    # cached attributes stay authoritative — only the image is overridden
    assert {"trait_type": "Body", "value": "Buck Straight"} in nfts[0]["attributes"]
