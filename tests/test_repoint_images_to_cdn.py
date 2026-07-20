# Tests for scripts/repoint_images_to_cdn.py — probe-and-repoint of the on-chain
# index image column to the working CDN URL (offline, injected prober).
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
)
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

import repoint_images_to_cdn as rp  # noqa: E402

from lfg_core import nft_index  # noqa: E402


def _seed(conn, nft_id, edition, image, burned=False):
    nft_index.upsert(
        conn,
        nft_index.OnchainNft(
            nft_id=nft_id,
            nft_number=edition,
            owner="rOwner",
            is_burned=burned,
            mutable=True,
            uri_hex="6868",
            body="male",
            attributes=[{"trait_type": "Body", "value": "Straight"}],
            image=image,
            ledger_index=1,
        ),
    )


def _prober_for(resolving):
    resolving = set(resolving)

    async def prober(url):
        return url in resolving

    return prober


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_repoints_first_resolving_candidate(tmp_path):
    conn = nft_index.init_db(str(tmp_path / "idx.db"))
    _seed(conn, "A", 5, "ipfs://bafyA/5.png")
    # edition 5 resolves only at variant _2
    win = f"{rp.CDN_HOST}/5/5_2.png"
    summary = _run(rp.repoint_images(conn, prober=_prober_for({win})))
    assert conn.execute("SELECT image FROM onchain_nfts WHERE nft_id='A'").fetchone()[0] == win
    assert summary["repointed_editions"] == 1
    assert summary["nohit_editions"] == []


def test_skips_already_cdn_rows_without_probing(tmp_path):
    conn = nft_index.init_db(str(tmp_path / "idx.db"))
    cdn = f"{rp.CDN_HOST}/7/7_0.png"
    _seed(conn, "B", 7, cdn)
    probed = []

    async def prober(url):
        probed.append(url)
        return True

    summary = _run(rp.repoint_images(conn, prober=prober))
    assert probed == []  # never touched the network
    assert summary["skipped_already_cdn"] == 1
    assert summary["target_editions"] == 0
    assert conn.execute("SELECT image FROM onchain_nfts WHERE nft_id='B'").fetchone()[0] == cdn


def test_nohit_edition_left_untouched_and_reported(tmp_path):
    conn = nft_index.init_db(str(tmp_path / "idx.db"))
    _seed(conn, "C", 220, "ipfs://bafyC/220.png")
    summary = _run(rp.repoint_images(conn, prober=_prober_for(set())))  # nothing resolves
    assert summary["nohit_editions"] == [220]
    assert (
        conn.execute("SELECT image FROM onchain_nfts WHERE nft_id='C'").fetchone()[0]
        == "ipfs://bafyC/220.png"
    )


def test_dry_run_writes_nothing(tmp_path):
    conn = nft_index.init_db(str(tmp_path / "idx.db"))
    _seed(conn, "A", 5, "ipfs://bafyA/5.png")
    win = f"{rp.CDN_HOST}/5/5_0.png"
    summary = _run(rp.repoint_images(conn, prober=_prober_for({win}), dry_run=True))
    assert summary["repointed_editions"] == 1  # would repoint
    assert (
        conn.execute("SELECT image FROM onchain_nfts WHERE nft_id='A'").fetchone()[0]
        == "ipfs://bafyA/5.png"
    )


def test_idempotent_second_run_is_noop(tmp_path):
    conn = nft_index.init_db(str(tmp_path / "idx.db"))
    _seed(conn, "A", 5, "ipfs://bafyA/5.png")
    win = f"{rp.CDN_HOST}/5/5_0.png"
    _run(rp.repoint_images(conn, prober=_prober_for({win})))
    second = _run(rp.repoint_images(conn, prober=_prober_for({win})))
    assert second["target_editions"] == 0  # now CDN, skipped
    assert second["repointed_rows"] == 0


def test_repoints_all_live_duplicates_of_an_edition(tmp_path):
    conn = nft_index.init_db(str(tmp_path / "idx.db"))
    _seed(conn, "D1", 9, "ipfs://bafyD/9.png")
    _seed(conn, "D2", 9, "")  # duplicate live token of same edition, empty image
    win = f"{rp.CDN_HOST}/9/9_1.png"
    summary = _run(rp.repoint_images(conn, prober=_prober_for({win})))
    assert summary["repointed_rows"] == 2
    for nid in ("D1", "D2"):
        assert (
            conn.execute("SELECT image FROM onchain_nfts WHERE nft_id=?", (nid,)).fetchone()[0]
            == win
        )


def test_force_reprobes_cdn_rows(tmp_path):
    conn = nft_index.init_db(str(tmp_path / "idx.db"))
    stale = f"{rp.CDN_HOST}/7/7_0.png"
    _seed(conn, "B", 7, stale)
    fresh = f"{rp.CDN_HOST}/7/7_1.png"
    summary = _run(rp.repoint_images(conn, prober=_prober_for({fresh}), force=True))
    assert summary["target_editions"] == 1
    assert conn.execute("SELECT image FROM onchain_nfts WHERE nft_id='B'").fetchone()[0] == fresh
