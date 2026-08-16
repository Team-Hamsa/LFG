# tests/test_economy_archive_invalidation.py
# The trait-economy ops change a character's art WITHOUT composing a still
# into the images_<network>/ archive (unlike mint/swap, which promote_still).
# The edition's on-chain image URL still maps back to it, so a leftover
# archived still would keep being served as its thumbnail — harvest must drop
# it (the edition now wears the shared blank silhouette) and assemble must
# drop it (freshly composed art went straight to the CDN).
#
# Env-guard preamble per tests convention.
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

from lfg_core import closet_token as ct  # noqa: E402
from lfg_core import config, image_archive  # noqa: E402
from lfg_core import economy_flow as ef  # noqa: E402
from lfg_core import economy_store as es  # noqa: E402
from lfg_core import trait_economy as te  # noqa: E402
from lfg_core.nft_index import OnchainNft  # noqa: E402

NON_BODY = te.NON_BODY_SLOTS
EDITION = 7


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _Fakes:
    async def closet_upload(self, meta):
        return "https://cdn/b/1.json"

    async def closet_mint(self, url):
        return "CLOSET0"

    async def closet_offer(self, nft_id, owner):
        return "OFFER0"

    async def closet_accept(self, offer_id):
        return {"xumm_url": "x"}

    async def closet_modify(self, nft_id, owner, url):
        return "TXHASH"

    async def closet_exists(self, nft_id):
        return True

    async def closet_owner(self, nft_id):
        return "rUser"

    async def char_compose(self, attrs, body_class, edition, rev):
        return ("https://cdn/i.png", None, "https://cdn/m.json")

    async def char_mint(self, url):
        return "NEWNFT"

    async def char_modify(self, nft_id, owner, url):
        return "MODIFYHASH"

    async def char_burn(self, nft_id, owner):
        return "BURNHASH"

    async def char_offer(self, nft_id, owner):
        return "OFFER1"

    async def char_accept(self, offer_id):
        return {"xumm_url": "x"}

    async def blank_meta(self, edition):
        return f"https://cdn/blank/{edition}.json"


def _deps(conn, records_dir):
    f = _Fakes()
    return ef.EconomyDeps(
        conn=conn,
        closet_upload_fn=f.closet_upload,
        closet_mint_fn=f.closet_mint,
        closet_offer_fn=f.closet_offer,
        closet_accept_fn=f.closet_accept,
        closet_modify_fn=f.closet_modify,
        char_compose_fn=f.char_compose,
        char_mint_fn=f.char_mint,
        char_modify_fn=f.char_modify,
        char_burn_fn=f.char_burn,
        char_offer_fn=f.char_offer,
        char_accept_fn=f.char_accept,
        closet_exists_fn=f.closet_exists,
        closet_owner_fn=f.closet_owner,
        blank_meta_fn=f.blank_meta,
        records_dir=str(records_dir),
    )


def _conn(*, blank: bool = False) -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    es.init_economy_schema(c)
    genesis = te.Genesis(
        trait_counts={(s, "None"): 1 for s in NON_BODY},
        edition_bodies={EDITION: ("Straight Blue", "male")},
    )
    es.freeze_genesis(c, genesis, {})
    es.set_closet_token(c, "rUser", "CLOSET0", "00", status=ct.ACTIVE, offer_id=None)
    if blank:
        es.set_closet_contents(
            c,
            "rUser",
            [(s, "None", 1) for s in NON_BODY] + [("Body", "Straight Blue", 1)],
            [],
        )
    return c


def _char(*, blank: bool = False) -> OnchainNft:
    body = "None" if blank else "Straight Blue"
    attrs = [{"trait_type": "Body", "value": body}]
    attrs += [{"trait_type": s, "value": "None"} for s in NON_BODY]
    return OnchainNft(
        nft_id=f"NFT{EDITION}",
        nft_number=EDITION,
        owner="rUser",
        is_burned=False,
        mutable=True,
        uri_hex="AABB",
        body="male",
        attributes=attrs,
        image="",
        ledger_index=1,
    )


def _seed_archive(tmp_path, monkeypatch):
    """An archived still + thumb for EDITION, as a pre-op character would have."""
    monkeypatch.setenv("IMAGES_DIR", str(tmp_path))
    # The flows skip invalidation when the economy runs on a different chain
    # than the archive the proxy serves — pin them equal for these tests.
    monkeypatch.setattr(config, "ECONOMY_NETWORK", config.XRPL_NETWORK)
    (tmp_path / f"{EDITION}.png").write_bytes(b"\x89PNG dressed still")
    thumbs = tmp_path / image_archive.THUMB_SUBDIR
    thumbs.mkdir(exist_ok=True)
    (thumbs / f"{EDITION}.webp").write_bytes(b"dressed thumb")


def test_harvest_invalidates_archived_still(tmp_path, monkeypatch):
    _seed_archive(tmp_path, monkeypatch)
    session = ef.HarvestSession(owner="rUser", character=_char(), burnable=True)
    _run(ef.run_harvest(session, _deps(_conn(), tmp_path / "rec")))

    assert session.state == ef.DONE, session.error
    assert image_archive.local_image(config.XRPL_NETWORK, EDITION) is None
    assert image_archive.local_thumb(config.XRPL_NETWORK, EDITION) is None


def test_assemble_invalidates_archived_still(tmp_path, monkeypatch):
    _seed_archive(tmp_path, monkeypatch)
    session = ef.AssembleSession(
        owner="rUser",
        character=_char(blank=True),
        body_value="Straight Blue",
        body_class="male",
        chosen=dict.fromkeys(NON_BODY, "None"),
    )
    _run(ef.run_assemble(session, _deps(_conn(blank=True), tmp_path / "rec")))

    assert session.state == ef.DONE, session.error
    assert image_archive.local_image(config.XRPL_NETWORK, EDITION) is None
    assert image_archive.local_thumb(config.XRPL_NETWORK, EDITION) is None


def test_split_network_deployment_never_touches_the_other_chains_archive(tmp_path, monkeypatch):
    """Characters on mainnet + economy on testnet is a documented topology.
    Edition numbers collide across networks, so an economy op must NOT delete
    the served archive's same-numbered (unrelated) art."""
    monkeypatch.setenv("IMAGES_DIR", str(tmp_path))
    monkeypatch.setattr(config, "XRPL_NETWORK", "mainnet")
    monkeypatch.setattr(config, "ECONOMY_NETWORK", "testnet")
    (tmp_path / f"{EDITION}.png").write_bytes(b"\x89PNG unrelated mainnet art")

    session = ef.HarvestSession(owner="rUser", character=_char(), burnable=True)
    _run(ef.run_harvest(session, _deps(_conn(), tmp_path / "rec")))

    assert session.state == ef.DONE, session.error
    assert image_archive.local_image("mainnet", EDITION) is not None


def test_assemble_invalidates_even_when_only_the_db_mirror_fails(tmp_path, monkeypatch):
    """ClosetMirrorError returns early with the character modify + Closet debit
    COMMITTED on-chain — the new art is live, so the archived still must
    already be gone. Regression: the invalidation used to sit after this
    branch's early return and was skipped."""
    _seed_archive(tmp_path, monkeypatch)
    conn = _conn(blank=True)
    deps = _deps(conn, tmp_path / "rec")

    async def _mirror_fail(*a, **k):
        raise ef.bt.ClosetMirrorError("db mirror down", tx_hash="SYNCHASH")

    monkeypatch.setattr(ef, "_sync_then_persist", _mirror_fail)
    session = ef.AssembleSession(
        owner="rUser",
        character=_char(blank=True),
        body_value="Straight Blue",
        body_class="male",
        chosen=dict.fromkeys(NON_BODY, "None"),
    )
    _run(ef.run_assemble(session, deps))

    assert session.state == ef.DONE
    assert session.mirror_pending is True
    assert image_archive.local_image(config.XRPL_NETWORK, EDITION) is None
    assert image_archive.local_thumb(config.XRPL_NETWORK, EDITION) is None
