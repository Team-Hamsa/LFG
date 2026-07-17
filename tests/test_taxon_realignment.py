# Task 9 (#217): taxon realignment tests.
#
# T1 flipped the base config: TRAIT_TAXON default = 176 (was 1763),
# ASSEMBLE_TAXON (new) default = 1760. Regular /letsgo mints stay NFT_TAXON (0).
# This file locks in the follow-on wiring:
#   1. a taxon-176 mint from our issuer is classified as a trait token, never
#      character/closet.
#   2. a taxon-1760 mint from our issuer flows into the character (onchain_nfts)
#      index, and is NOT misclassified into the closet/trait economy tables.
#   3. run_assemble's mint site (scripts/_economy_deps.py char_mint_fn) mints at
#      config.ASSEMBLE_TAXON specifically (not just "whatever SWAP_TAXON is").

import asyncio
import os
import sqlite3
import sys

# Env-guard preamble (tests/test_env_guard_convention pattern) so importing
# lfg_core at module scope doesn't strand frozen config constants when this
# file runs inside the full suite.
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

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "scripts"))

from lfg_core import (  # noqa: E402
    config,
    nft_index,
    nft_listener,
    trait_token,  # noqa: E402
)
from lfg_core import economy_store as es  # noqa: E402
from lfg_core import trait_economy as te  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _econ_conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    es.init_economy_schema(c)
    return c


# --- 1. taxon 176 (TRAIT_TAXON) mint from our issuer -> trait_tokens row ----


def test_taxon_176_mint_upserts_trait_token():
    assert config.TRAIT_TAXON == 176  # locks in the flipped default (T1)
    conn = _econ_conn()
    meta = trait_token.build_trait_metadata("Hat", "Cap", "https://example.com/img.png")

    async def fetch_token(nft_id):
        return {
            "nft_id": "TRAIT176",
            "owner": "rUser",
            "taxon": config.TRAIT_TAXON,
            "uri_hex": "AA",
            "issuer": config.SWAP_ISSUER_ADDRESS,
        }

    async def fetch_meta(uri_hex):
        return meta

    tx = {
        "TransactionType": "NFTokenMint",
        "meta": {"TransactionResult": "tesSUCCESS", "nftoken_id": "TRAIT176"},
    }
    _run(
        nft_listener.apply_economy_tx(
            conn,
            tx,
            fetch_token_fn=fetch_token,
            fetch_meta_fn=fetch_meta,
            genesis=te.Genesis(trait_counts={}, edition_bodies={}),
        )
    )
    rows = es.read_trait_tokens(conn)
    assert len(rows) == 1
    nft_id, owner, slot, value = rows[0]
    assert (nft_id, owner, slot, value) == ("TRAIT176", "rUser", "Hat", "Cap")


# --- 2. taxon 1760 (ASSEMBLE_TAXON) mint from our issuer ---------------------


def test_taxon_1760_mint_is_not_classified_as_trait_or_closet():
    """apply_economy_tx must not route a taxon-1760 (character) mint into the
    closet/trait economy tables — those are gated to CLOSET_TAXON/LEGACY_BUCKET_TAXON
    and TRAIT_TAXON specifically."""
    assert config.ASSEMBLE_TAXON == 1760
    conn = _econ_conn()

    async def fetch_token(nft_id):
        return {
            "nft_id": "CHAR1760",
            "owner": "rUser",
            "taxon": config.ASSEMBLE_TAXON,
            "uri_hex": "CD",
            "issuer": config.SWAP_ISSUER_ADDRESS,
        }

    async def fetch_meta(uri_hex):
        return {
            "name": "LFG #7",
            "attributes": [{"trait_type": "Body", "value": "Straight Blue"}],
        }

    tx = {
        "TransactionType": "NFTokenMint",
        "meta": {"TransactionResult": "tesSUCCESS", "nftoken_id": "CHAR1760"},
    }
    genesis = te.Genesis(trait_counts={}, edition_bodies={7: ("Straight Blue", "male")})
    _run(
        nft_listener.apply_economy_tx(
            conn, tx, fetch_token_fn=fetch_token, fetch_meta_fn=fetch_meta, genesis=genesis
        )
    )
    assert es.read_trait_tokens(conn) == []
    assert es.read_closet_assets(conn) == []
    assert es.read_closet_bodies(conn) == []


def test_taxon_1760_mint_flows_into_character_index():
    """apply_tx (the main onchain_nfts index) must accept a taxon-1760 mint from
    our issuer when is_ours scopes on that taxon — Assemble rebirths must land
    in the character index, not be silently dropped."""
    conn = nft_index.init_db(":memory:")

    async def fetch_token(nft_id):
        return {
            "nft_id": "CHAR1760B",
            "owner": "rUser",
            "flags": 0x19,
            "uri_hex": "EE",
            "is_burned": False,
            "issuer": config.SWAP_ISSUER_ADDRESS,
            "taxon": config.ASSEMBLE_TAXON,
        }

    async def fetch_meta(uri_hex):
        return {"name": "LFG #99", "attributes": []}

    def is_ours(token):
        return (
            token.get("issuer") == config.SWAP_ISSUER_ADDRESS
            and int(token.get("taxon") or -1) == config.ASSEMBLE_TAXON
        )

    tx = {
        "TransactionType": "NFTokenMint",
        "meta": {"TransactionResult": "tesSUCCESS", "nftoken_id": "CHAR1760B"},
    }
    _run(nft_listener.apply_tx(conn, tx, fetch_token, fetch_meta, is_ours=is_ours))

    row = conn.execute(
        "SELECT nft_id FROM onchain_nfts WHERE nft_id = ?", ("CHAR1760B",)
    ).fetchone()
    assert row is not None


# --- 3. run_assemble's mint site (scripts/_economy_deps.py) mints at ASSEMBLE_TAXON


def test_assemble_char_mint_fn_uses_assemble_taxon(monkeypatch):
    """char_mint_fn (wired in scripts/_economy_deps.build_economy_deps, the fn
    run_assemble calls to mint the rebirth) must mint at config.ASSEMBLE_TAXON,
    not an incidental alias — pins the taxon so a future SWAP_TAXON change can't
    silently retarget Assemble mints."""
    import _economy_deps as deps

    from lfg_core import memos, xrpl_ops

    # SWAP_TAXON and ASSEMBLE_TAXON both default to 1760, which would let a
    # wiring bug (char_mint_fn reading config.SWAP_TAXON) pass this test by
    # numeric coincidence. Diverge them so only reading ASSEMBLE_TAXON passes.
    monkeypatch.setattr(config, "SWAP_TAXON", 9999)
    assert config.ASSEMBLE_TAXON != config.SWAP_TAXON

    captured: dict[str, object] = {}

    async def fake_mint(url, taxon, issuer, flags=None, action=memos.ACTION_MINT, **kw):
        captured["taxon"] = taxon
        return "NFTID"

    monkeypatch.setattr(xrpl_ops, "mint_nft", fake_mint)
    conn = sqlite3.connect(":memory:")
    es.init_economy_schema(conn)
    d = deps.build_economy_deps(conn)
    _run(d.char_mint_fn("https://x/m.json"))
    assert captured["taxon"] == config.ASSEMBLE_TAXON
