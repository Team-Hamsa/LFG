# Bucket NFToken lifecycle wrappers, exercised with in-memory fakes (no network).

import asyncio
import sqlite3

import pytest

from lfg_core import closet_token as bt
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


class _Fakes:
    """Records calls; returns canned ids/urls."""

    def __init__(self) -> None:
        self.mints: list[str] = []
        self.offers: list[tuple[str, str]] = []
        self.modifies: list[tuple[str, str, str]] = []
        self.uploads: list[dict] = []

    async def upload(self, meta: dict) -> str:
        self.uploads.append(meta)
        return f"https://cdn/bucket/{len(self.uploads)}.json"

    async def mint(self, url: str) -> str:
        self.mints.append(url)
        return "BUCKETNFT"

    async def offer(self, nft_id: str, owner: str) -> str:
        self.offers.append((nft_id, owner))
        return "OFFER1"

    async def accept(self, offer_id: str) -> dict:
        return {"qr_url": "q", "xumm_url": "x"}

    async def modify(self, nft_id: str, owner: str, url: str) -> str:
        self.modifies.append((nft_id, owner, url))
        return "MODHASH"


def test_ensure_closet_mints_once():
    c, f = _conn(), _Fakes()
    ref = _run(
        bt.ensure_closet(
            c,
            "rUser",
            upload_fn=f.upload,
            mint_fn=f.mint,
            offer_fn=f.offer,
            accept_payload_fn=f.accept,
        )
    )
    assert ref.nft_id == "BUCKETNFT"
    assert ref.minted is True
    assert ref.accept_payload == {"qr_url": "q", "xumm_url": "x"}
    assert es.get_closet_token(c, "rUser")[0] == "BUCKETNFT"
    assert len(f.mints) == 1

    # Second call is a no-op: no new mint, no accept payload.
    ref2 = _run(
        bt.ensure_closet(
            c,
            "rUser",
            upload_fn=f.upload,
            mint_fn=f.mint,
            offer_fn=f.offer,
            accept_payload_fn=f.accept,
        )
    )
    assert ref2.minted is False
    assert ref2.accept_payload is None
    assert len(f.mints) == 1


def test_ensure_closet_raises_on_mint_failure():
    c, f = _conn(), _Fakes()

    async def bad_mint(url: str) -> None:
        return None

    with pytest.raises(bt.ClosetError):
        _run(
            bt.ensure_closet(
                c,
                "rUser",
                upload_fn=f.upload,
                mint_fn=bad_mint,
                offer_fn=f.offer,
                accept_payload_fn=f.accept,
            )
        )
    assert es.get_closet_token(c, "rUser") is None


def test_sync_closet_modifies_and_persists_uri():
    c, f = _conn(), _Fakes()
    _run(
        bt.ensure_closet(
            c,
            "rUser",
            upload_fn=f.upload,
            mint_fn=f.mint,
            offer_fn=f.offer,
            accept_payload_fn=f.accept,
        )
    )
    _run(
        bt.sync_closet(
            c, "rUser", [("Head", "None", 2)], [3536], upload_fn=f.upload, modify_fn=f.modify
        )
    )
    assert len(f.modifies) == 1
    nft_id, owner, url = f.modifies[0]
    assert nft_id == "BUCKETNFT" and owner == "rUser"
    _, uri_hex = es.get_closet_token(c, "rUser")
    assert uri_hex == url.encode().hex().upper()


def test_sync_closet_without_bucket_raises():
    c, f = _conn(), _Fakes()
    with pytest.raises(bt.ClosetError):
        _run(bt.sync_closet(c, "rUser", [], [], upload_fn=f.upload, modify_fn=f.modify))
