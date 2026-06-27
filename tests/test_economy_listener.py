# Listener applies economy events: rebuild a bucket from its token metadata,
# and log supply growth on an unknown-edition character mint.

import asyncio
import sqlite3

from lfg_core import closet_token as bt
from lfg_core import config, nft_listener, trait_token
from lfg_core import economy_store as es
from lfg_core import trait_economy as te

NON_BODY = te.NON_BODY_SLOTS


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


def _char_meta(edition: int, body: str = "Straight Blue") -> dict:
    attrs = [{"trait_type": "Body", "value": body}]
    attrs += [{"trait_type": s, "value": "None"} for s in NON_BODY]
    return {"name": f"LFG #{edition}", "attributes": attrs}


def test_closet_modify_rebuilds_tables():
    conn = _conn()
    meta = bt.build_closet_metadata("rUser", [("Head", "None", 2), ("Eyes", "Blue", 1)], [3536])

    async def fetch_token(nft_id):
        return {"nft_id": "CLOSET", "owner": "rUser", "taxon": config.CLOSET_TAXON, "uri_hex": "AB"}

    async def fetch_meta(uri_hex):
        return meta

    tx = {"TransactionType": "NFTokenModify", "NFTokenID": "CLOSET"}
    _run(
        nft_listener.apply_economy_tx(
            conn,
            tx,
            fetch_token_fn=fetch_token,
            fetch_meta_fn=fetch_meta,
            genesis=te.Genesis(trait_counts={}, edition_bodies={}),
        )
    )
    assets = {(s, v): n for o, s, v, n in es.read_closet_assets(conn)}
    assert assets == {("Head", "None"): 2, ("Eyes", "Blue"): 1}
    assert es.read_closet_bodies(conn) == [("rUser", 3536)]
    assert es.get_closet_token(conn, "rUser") == ("CLOSET", "AB")


def test_unknown_edition_mint_logs_growth():
    conn = _conn()

    async def fetch_token(nft_id):
        return {"nft_id": "CHAR", "owner": "rUser", "taxon": config.SWAP_TAXON, "uri_hex": "CD"}

    async def fetch_meta(uri_hex):
        return _char_meta(3536)

    tx = {"TransactionType": "NFTokenMint", "meta": {"nftoken_id": "CHAR"}}
    genesis = te.Genesis(trait_counts={}, edition_bodies={})  # 3536 unknown
    _run(
        nft_listener.apply_economy_tx(
            conn, tx, fetch_token_fn=fetch_token, fetch_meta_fn=fetch_meta, genesis=genesis
        )
    )
    rows = es.read_supply_changes(conn)
    assert len(rows) == 1
    assert rows[0]["kind"] == "mint" and rows[0]["edition"] == 3536
    assert rows[0]["trait_deltas"]["Head|None"] == 1


def test_known_edition_mint_logs_nothing():
    conn = _conn()

    async def fetch_token(nft_id):
        return {"nft_id": "CHAR", "owner": "rUser", "taxon": config.SWAP_TAXON, "uri_hex": "CD"}

    async def fetch_meta(uri_hex):
        return _char_meta(7)

    tx = {"TransactionType": "NFTokenMint", "meta": {"nftoken_id": "CHAR"}}
    genesis = te.Genesis(trait_counts={}, edition_bodies={7: ("Straight Blue", "male")})
    _run(
        nft_listener.apply_economy_tx(
            conn, tx, fetch_token_fn=fetch_token, fetch_meta_fn=fetch_meta, genesis=genesis
        )
    )
    assert es.read_supply_changes(conn) == []


def test_closet_accept_marks_active():
    """An NFTokenAcceptOffer for a CLOSET_TAXON token whose post-transfer owner is
    a user (not the issuer) should record status == ACTIVE."""
    conn = _conn()
    meta = bt.build_closet_metadata("rUser", [], [])

    async def fetch_token(nft_id):
        return {
            "nft_id": "CLOSET_ACC",
            "owner": "rUser",
            "taxon": config.CLOSET_TAXON,
            "uri_hex": "EF",
        }

    async def fetch_meta(uri_hex):
        return meta

    tx = {"TransactionType": "NFTokenAcceptOffer", "meta": {"nftoken_id": "CLOSET_ACC"}}
    _run(
        nft_listener.apply_economy_tx(
            conn,
            tx,
            fetch_token_fn=fetch_token,
            fetch_meta_fn=fetch_meta,
            genesis=te.Genesis(trait_counts={}, edition_bodies={}),
        )
    )
    record = es.get_closet_record(conn, "rUser")
    assert record is not None
    assert record[2] == bt.ACTIVE


def test_closet_mint_marks_pending():
    """An NFTokenMint of a CLOSET_TAXON token owned by the issuer should record
    status == PENDING_ACCEPT (the token hasn't been transferred to the user yet)."""
    conn = _conn()
    meta = bt.build_closet_metadata(config.SWAP_ISSUER_ADDRESS, [], [])

    async def fetch_token(nft_id):
        return {
            "nft_id": "CLOSET_MINT",
            "owner": config.SWAP_ISSUER_ADDRESS,
            "taxon": config.CLOSET_TAXON,
            "uri_hex": "GH",
        }

    async def fetch_meta(uri_hex):
        return meta

    tx = {"TransactionType": "NFTokenMint", "meta": {"nftoken_id": "CLOSET_MINT"}}
    _run(
        nft_listener.apply_economy_tx(
            conn,
            tx,
            fetch_token_fn=fetch_token,
            fetch_meta_fn=fetch_meta,
            genesis=te.Genesis(trait_counts={}, edition_bodies={}),
        )
    )
    record = es.get_closet_record(conn, config.SWAP_ISSUER_ADDRESS)
    assert record is not None
    assert record[2] == bt.PENDING_ACCEPT


def test_closet_listener_preserves_offer_id():
    """_apply_closet must not overwrite a stored offer_id with None (I1 fix).
    The offer_id is set by ensure_closet (not on-chain), so a subsequent
    listener mint/modify/accept must pass the existing offer_id through rather
    than defaulting to None and clobbering it."""
    conn = _conn()
    # Seed a closet record with a non-null offer_id (as ensure_closet would do).
    es.set_closet_token(conn, "rUser", "CLOSET1", "AB", status=bt.PENDING_ACCEPT, offer_id="OF1")

    meta = bt.build_closet_metadata("rUser", [], [])

    async def fetch_token(nft_id):
        return {
            "nft_id": "CLOSET1",
            "owner": "rUser",
            "taxon": config.CLOSET_TAXON,
            "uri_hex": "AB",
        }

    async def fetch_meta(uri_hex):
        return meta

    # Drive a modify through the listener (simulates an NFTokenModify event on the Closet).
    tx = {"TransactionType": "NFTokenModify", "NFTokenID": "CLOSET1"}
    _run(
        nft_listener.apply_economy_tx(
            conn,
            tx,
            fetch_token_fn=fetch_token,
            fetch_meta_fn=fetch_meta,
            genesis=te.Genesis(trait_counts={}, edition_bodies={}),
        )
    )
    record = es.get_closet_record(conn, "rUser")
    assert record is not None
    assert record[3] == "OF1", f"offer_id was clobbered; got {record[3]!r}"


# --- Trait-token listener tests (Task 6) ---


def _trait_meta(slot: str = "Hat", value: str = "Cap") -> dict:
    return trait_token.build_trait_metadata(slot, value, "https://example.com/img.png")


def _trait_token_dict(nft_id: str = "TRAIT1", owner: str = "rUser") -> dict:
    return {
        "nft_id": nft_id,
        "owner": owner,
        "taxon": config.TRAIT_TAXON,
        "uri_hex": "AA",
    }


def test_trait_mint_inserts_row():
    """A TRAIT_TAXON NFTokenMint with valid metadata should insert a trait_tokens row."""
    conn = _conn()
    meta = _trait_meta("Hat", "Cap")

    async def fetch_token(nft_id):
        return _trait_token_dict("TRAIT1", "rUser")

    async def fetch_meta(uri_hex):
        return meta

    tx = {"TransactionType": "NFTokenMint", "meta": {"nftoken_id": "TRAIT1"}}
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
    assert nft_id == "TRAIT1"
    assert owner == "rUser"
    assert slot == "Hat"
    assert value == "Cap"


def test_trait_mint_applies_without_frozen_genesis():
    """Trait-token mirror maintenance must NOT depend on a frozen genesis: with
    genesis=None (no genesis frozen) a TRAIT_TAXON mint still inserts its row.
    Only the supply-growth path needs genesis."""
    conn = _conn()
    meta = _trait_meta("Hat", "Cap")

    async def fetch_token(nft_id):
        return _trait_token_dict("TRAITNG", "rUser")

    async def fetch_meta(uri_hex):
        return meta

    tx = {"TransactionType": "NFTokenMint", "meta": {"nftoken_id": "TRAITNG"}}
    _run(
        nft_listener.apply_economy_tx(
            conn,
            tx,
            fetch_token_fn=fetch_token,
            fetch_meta_fn=fetch_meta,
            genesis=None,
        )
    )
    rows = es.read_trait_tokens(conn)
    assert ("TRAITNG", "rUser", "Hat", "Cap") in rows


def test_trait_transfer_updates_owner():
    """A TRAIT_TAXON NFTokenAcceptOffer whose post-transfer owner is rNew should
    update the existing trait_tokens row's owner field."""
    conn = _conn()
    # Seed a pre-existing row for TRAIT1 owned by rOld.
    es.upsert_trait_token(conn, "TRAIT1", "rOld", "Hat", "Cap")

    meta = _trait_meta("Hat", "Cap")

    async def fetch_token(nft_id):
        # Post-transfer: owner is rNew.
        return {
            "nft_id": "TRAIT1",
            "owner": "rNew",
            "taxon": config.TRAIT_TAXON,
            "uri_hex": "AA",
        }

    async def fetch_meta(uri_hex):
        return meta

    tx = {"TransactionType": "NFTokenAcceptOffer", "meta": {"nftoken_id": "TRAIT1"}}
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
    assert nft_id == "TRAIT1"
    assert owner == "rNew", f"expected rNew, got {owner!r}"
    assert slot == "Hat"
    assert value == "Cap"


def test_trait_burn_deletes_row():
    """A TRAIT_TAXON NFTokenBurn should remove the row from trait_tokens."""
    conn = _conn()
    # Seed a pre-existing trait_tokens row.
    es.upsert_trait_token(conn, "TRAIT1", "rUser", "Hat", "Cap")
    assert len(es.read_trait_tokens(conn)) == 1

    tx = {"TransactionType": "NFTokenBurn", "NFTokenID": "TRAIT1"}

    # For a burn, apply_economy_tx must not call fetch_token (it short-circuits on kind).
    # We do provide fetchers that return TRAIT_TAXON data so if the code incorrectly
    # tries to fetch and then upsert it would fail the assertion below.
    async def fetch_token(nft_id):
        return {
            "nft_id": "TRAIT1",
            "owner": "rUser",
            "taxon": config.TRAIT_TAXON,
            "uri_hex": "AA",
            "is_burned": True,
        }

    async def fetch_meta(uri_hex):
        return _trait_meta("Hat", "Cap")

    _run(
        nft_listener.apply_economy_tx(
            conn,
            tx,
            fetch_token_fn=fetch_token,
            fetch_meta_fn=fetch_meta,
            genesis=te.Genesis(trait_counts={}, edition_bodies={}),
        )
    )
    assert es.read_trait_tokens(conn) == [], "Row was not deleted on burn"


def test_trait_burn_deletes_row_even_when_token_fetch_returns_none():
    """Regression guard for the latent bug: when nft_info returns None for a
    burned token (it's gone from the ledger), the trait_tokens row must still be
    deleted. Old code hit `if not token: continue` before taxon dispatch, so the
    row was never deleted — a silent inconsistency."""
    conn = _conn()
    # Seed a pre-existing trait_tokens row.
    es.upsert_trait_token(conn, "TRAIT_GONE", "rUser", "Hat", "Cap")
    assert len(es.read_trait_tokens(conn)) == 1

    tx = {"TransactionType": "NFTokenBurn", "NFTokenID": "TRAIT_GONE"}

    async def fetch_token(nft_id):
        # Simulate nft_info returning None for a token already purged from the ledger.
        return None

    async def fetch_meta(uri_hex):
        return _trait_meta("Hat", "Cap")

    _run(
        nft_listener.apply_economy_tx(
            conn,
            tx,
            fetch_token_fn=fetch_token,
            fetch_meta_fn=fetch_meta,
            genesis=te.Genesis(trait_counts={}, edition_bodies={}),
        )
    )
    assert es.read_trait_tokens(conn) == [], "Row was not deleted when token fetch returned None"


def test_trait_burn_of_unknown_nft_id_is_idempotent():
    """A burn for an nft_id with no trait_tokens row must not error — delete is
    a no-op for non-trait (or already-deleted) tokens."""
    conn = _conn()
    # No rows seeded: table is empty.
    assert es.read_trait_tokens(conn) == []

    tx = {"TransactionType": "NFTokenBurn", "NFTokenID": "UNKNOWN_NFT"}

    async def fetch_token(nft_id):
        return None  # gone from ledger

    async def fetch_meta(uri_hex):
        return None

    # Must complete without raising.
    _run(
        nft_listener.apply_economy_tx(
            conn,
            tx,
            fetch_token_fn=fetch_token,
            fetch_meta_fn=fetch_meta,
            genesis=te.Genesis(trait_counts={}, edition_bodies={}),
        )
    )
    # Table still empty — no spurious inserts.
    assert es.read_trait_tokens(conn) == []
