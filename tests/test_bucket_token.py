# Bucket NFToken metadata builder/parser round-trips (pure).

import asyncio
import sqlite3

from lfg_core import bucket_token as bt
from lfg_core import economy_store as es


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    es.init_economy_schema(c)
    return c


class _BucketFakes:
    def __init__(self) -> None:
        self.mints: list[str] = []
        self.uploads = 0

    async def upload(self, meta: dict) -> str:
        self.uploads += 1
        return f"https://cdn/bucket/{self.uploads}.json"

    async def mint(self, url: str) -> str:
        self.mints.append(url)
        return f"FRESHNFT{len(self.mints)}"

    async def offer(self, nft_id: str, owner: str) -> str:
        return "OFFER1"

    async def accept(self, offer_id: str) -> dict:
        return {"xumm_url": "x"}


def test_metadata_roundtrips():
    assets = [("Head", "None", 3), ("Background", "Blue", 1)]
    bodies = [3536, 12]
    meta = bt.build_bucket_metadata("rUser", assets, bodies)
    assert meta["lfg_bucket"]["bodies"] == [12, 3536]  # sorted
    assert meta["name"] == "LFG Bucket — rUser"
    got_assets, got_bodies = bt.parse_bucket_metadata(meta)
    assert sorted(got_assets) == sorted(assets)
    assert got_bodies == [12, 3536]


def test_none_assets_preserved():
    meta = bt.build_bucket_metadata("rUser", [("Head", "None", 2)], [])
    got_assets, got_bodies = bt.parse_bucket_metadata(meta)
    assert got_assets == [("Head", "None", 2)]
    assert got_bodies == []


def test_empty_bucket():
    meta = bt.build_bucket_metadata("rUser", [], [])
    assert bt.parse_bucket_metadata(meta) == ([], [])


def test_parse_tolerates_garbage():
    assert bt.parse_bucket_metadata({}) == ([], [])
    assert bt.parse_bucket_metadata({"lfg_bucket": "x"}) == ([], [])
    assert bt.parse_bucket_metadata({"lfg_bucket": {"assets": "x"}}) == ([], [])
    # malformed entries are skipped, valid ones kept
    mixed = {
        "lfg_bucket": {
            "assets": [{"slot": "Head"}, {"slot": "Eyes", "value": "Blue", "count": 1}],
            "bodies": ["x", 7],
        }
    }
    assert bt.parse_bucket_metadata(mixed) == ([("Eyes", "Blue", 1)], [7])


def test_ensure_bucket_remints_when_record_stale():
    """A DB record that no longer exists on-ledger (e.g. after a testnet reset)
    is treated as stale: ensure_bucket mints a fresh bucket rather than trusting
    the dead nft_id (which would later make NFTokenModify fail tecNO_ENTRY)."""
    c, f = _conn(), _BucketFakes()
    es.set_bucket_token(c, "rUser", "STALENFT", "AABB")

    async def absent(nft_id: str) -> bool:
        return False

    ref = _run(
        bt.ensure_bucket(
            c,
            "rUser",
            upload_fn=f.upload,
            mint_fn=f.mint,
            offer_fn=f.offer,
            accept_payload_fn=f.accept,
            exists_fn=absent,
        )
    )
    assert ref.minted is True
    assert ref.nft_id != "STALENFT"
    assert len(f.mints) == 1
    # The stale row was overwritten with the fresh token.
    assert es.get_bucket_token(c, "rUser")[0] == ref.nft_id


def test_ensure_bucket_keeps_record_when_on_ledger():
    """A DB record that still exists on-ledger is returned as-is (no re-mint)."""
    c, f = _conn(), _BucketFakes()
    es.set_bucket_token(c, "rUser", "LIVENFT", "AABB")

    async def present(nft_id: str) -> bool:
        return True

    ref = _run(
        bt.ensure_bucket(
            c,
            "rUser",
            upload_fn=f.upload,
            mint_fn=f.mint,
            offer_fn=f.offer,
            accept_payload_fn=f.accept,
            exists_fn=present,
        )
    )
    assert ref.minted is False
    assert ref.nft_id == "LIVENFT"
    assert f.mints == []


def test_ensure_bucket_no_exists_fn_trusts_record():
    """Back-compat: callers that don't pass exists_fn trust the DB record."""
    c, f = _conn(), _BucketFakes()
    es.set_bucket_token(c, "rUser", "LIVENFT", "AABB")

    ref = _run(
        bt.ensure_bucket(
            c,
            "rUser",
            upload_fn=f.upload,
            mint_fn=f.mint,
            offer_fn=f.offer,
            accept_payload_fn=f.accept,
        )
    )
    assert ref.minted is False
    assert ref.nft_id == "LIVENFT"
    assert f.mints == []
