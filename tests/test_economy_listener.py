# Listener applies economy events: rebuild a bucket from its token metadata,
# and log supply growth on an unknown-edition character mint.

import asyncio
import sqlite3

from lfg_core import closet_token as bt
from lfg_core import config, nft_listener, trait_token
from lfg_core import economy_store as es
from lfg_core import trait_economy as te
from tests.closet_archive_helpers import closet_uri, closet_version_tx

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
    meta = bt.build_closet_metadata("rUser", [("Head", "None", 2), ("Eyes", "Blue", 1)], [])

    async def fetch_token(nft_id):
        return {
            "nft_id": "CLOSET",
            "owner": "rUser",
            "taxon": config.CLOSET_TAXON,
            "uri_hex": "AB",
            "issuer": config.SWAP_ISSUER_ADDRESS,
        }

    async def fetch_meta(uri_hex):
        return meta

    tx = {
        "TransactionType": "NFTokenModify",
        "NFTokenID": "CLOSET",
        "meta": {"TransactionResult": "tesSUCCESS"},
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
    assets = {(s, v): n for o, s, v, n in es.read_closet_assets(conn)}
    assert assets == {("Head", "None"): 2, ("Eyes", "Blue"): 1}
    # schema v2: build_closet_metadata never writes legacy body editions.
    assert es.read_closet_bodies(conn) == []
    assert es.get_closet_token(conn, "rUser") == ("CLOSET", "AB")


def test_unknown_edition_mint_logs_growth():
    conn = _conn()

    async def fetch_token(nft_id):
        return {
            "nft_id": "CHAR",
            "owner": "rUser",
            "taxon": config.SWAP_TAXON,
            "uri_hex": "CD",
            "issuer": config.SWAP_ISSUER_ADDRESS,
        }

    async def fetch_meta(uri_hex):
        return _char_meta(3536)

    tx = {
        "TransactionType": "NFTokenMint",
        "meta": {"TransactionResult": "tesSUCCESS", "nftoken_id": "CHAR"},
    }
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
        return {
            "nft_id": "CHAR",
            "owner": "rUser",
            "taxon": config.SWAP_TAXON,
            "uri_hex": "CD",
            "issuer": config.SWAP_ISSUER_ADDRESS,
        }

    async def fetch_meta(uri_hex):
        return _char_meta(7)

    tx = {
        "TransactionType": "NFTokenMint",
        "meta": {"TransactionResult": "tesSUCCESS", "nftoken_id": "CHAR"},
    }
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
            "issuer": config.SWAP_ISSUER_ADDRESS,
        }

    async def fetch_meta(uri_hex):
        return meta

    tx = {
        "TransactionType": "NFTokenAcceptOffer",
        "meta": {"TransactionResult": "tesSUCCESS", "nftoken_id": "CLOSET_ACC"},
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
    record = es.get_closet_record(conn, "rUser")
    assert record is not None
    assert record[2] == bt.ACTIVE


def test_closet_mint_records_nothing_for_the_issuer():
    """An NFTokenMint of a CLOSET_TAXON token is issuer-held until the user
    accepts, so the listener's owner-of-record is the ISSUER — not the user the
    offer targets, whom this snapshot cannot identify.

    This test used to assert the opposite (that a PENDING_ACCEPT row was written
    under the issuer). That row was the #383 bug: it shadowed the real user's
    row, pointing at the same token. `ensure_closet` already recorded the
    pending Closet under the real owner before the mint was ever streamed, so
    there is nothing here for the listener to add."""
    conn = _conn()
    meta = bt.build_closet_metadata(config.SWAP_ISSUER_ADDRESS, [], [])

    async def fetch_token(nft_id):
        return {
            "nft_id": "CLOSET_MINT",
            "owner": config.SWAP_ISSUER_ADDRESS,
            "taxon": config.CLOSET_TAXON,
            "uri_hex": "GH",
            "issuer": config.SWAP_ISSUER_ADDRESS,
        }

    async def fetch_meta(uri_hex):
        return meta

    tx = {
        "TransactionType": "NFTokenMint",
        "meta": {"TransactionResult": "tesSUCCESS", "nftoken_id": "CLOSET_MINT"},
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
    assert es.get_closet_record(conn, config.SWAP_ISSUER_ADDRESS) is None


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
            "issuer": config.SWAP_ISSUER_ADDRESS,
        }

    async def fetch_meta(uri_hex):
        return meta

    # Drive a modify through the listener (simulates an NFTokenModify event on the Closet).
    tx = {
        "TransactionType": "NFTokenModify",
        "NFTokenID": "CLOSET1",
        "meta": {"TransactionResult": "tesSUCCESS"},
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
        "issuer": config.SWAP_ISSUER_ADDRESS,
    }


def test_trait_mint_inserts_row():
    """A TRAIT_TAXON NFTokenMint with valid metadata should insert a trait_tokens row."""
    conn = _conn()
    meta = _trait_meta("Hat", "Cap")

    async def fetch_token(nft_id):
        return _trait_token_dict("TRAIT1", "rUser")

    async def fetch_meta(uri_hex):
        return meta

    tx = {
        "TransactionType": "NFTokenMint",
        "meta": {"TransactionResult": "tesSUCCESS", "nftoken_id": "TRAIT1"},
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

    tx = {
        "TransactionType": "NFTokenMint",
        "meta": {"TransactionResult": "tesSUCCESS", "nftoken_id": "TRAITNG"},
    }
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
            "issuer": config.SWAP_ISSUER_ADDRESS,
        }

    async def fetch_meta(uri_hex):
        return meta

    tx = {
        "TransactionType": "NFTokenAcceptOffer",
        "meta": {"TransactionResult": "tesSUCCESS", "nftoken_id": "TRAIT1"},
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

    tx = {
        "TransactionType": "NFTokenBurn",
        "NFTokenID": "TRAIT1",
        "meta": {"TransactionResult": "tesSUCCESS"},
    }

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

    tx = {
        "TransactionType": "NFTokenBurn",
        "NFTokenID": "TRAIT_GONE",
        "meta": {"TransactionResult": "tesSUCCESS"},
    }

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

    tx = {
        "TransactionType": "NFTokenBurn",
        "NFTokenID": "UNKNOWN_NFT",
        "meta": {"TransactionResult": "tesSUCCESS"},
    }

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


# --- Issuer gate (#178): only tokens minted by OUR issuer may write economy rows ---

FOREIGN_ISSUER = "rForgedIssuerAcct000000000000000000"


def test_forged_closet_from_foreign_issuer_writes_nothing():
    """A taxon-1762 mint carrying forged lfg_closet metadata but minted by a
    FOREIGN issuer must NOT create closet_tokens/closet_assets rows — the taxon
    is attacker-controlled, so only the issuer identity can be trusted."""
    conn = _conn()
    meta = bt.build_closet_metadata("rAttacker", [("Head", "None", 2), ("Eyes", "Blue", 1)], [42])

    async def fetch_token(nft_id):
        return {
            "nft_id": "FORGED_CLOSET",
            "owner": "rAttacker",
            "taxon": config.CLOSET_TAXON,
            "uri_hex": "AB",
            "issuer": FOREIGN_ISSUER,
        }

    async def fetch_meta(uri_hex):
        return meta

    tx = {
        "TransactionType": "NFTokenMint",
        "meta": {"TransactionResult": "tesSUCCESS", "nftoken_id": "FORGED_CLOSET"},
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
    assert es.read_closet_assets(conn) == []
    assert es.read_closet_bodies(conn) == []
    assert es.get_closet_record(conn, "rAttacker") is None


def test_forged_trait_token_from_foreign_issuer_writes_nothing():
    """A taxon-1763 mint from a FOREIGN issuer must NOT create a trait_tokens row
    (which would otherwise be listable on our marketplace for real XRP)."""
    conn = _conn()
    meta = _trait_meta("Hat", "Cap")

    async def fetch_token(nft_id):
        return {
            "nft_id": "FORGED_TRAIT",
            "owner": "rAttacker",
            "taxon": config.TRAIT_TAXON,
            "uri_hex": "AA",
            "issuer": FOREIGN_ISSUER,
        }

    async def fetch_meta(uri_hex):
        return meta

    tx = {
        "TransactionType": "NFTokenMint",
        "meta": {"TransactionResult": "tesSUCCESS", "nftoken_id": "FORGED_TRAIT"},
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
    assert es.read_trait_tokens(conn) == []


def test_foreign_issuer_named_mint_logs_no_growth():
    """A #N-named character mint from a FOREIGN issuer must NOT record a
    supply_changes row — this is exactly what poisoned onchain_mainnet.db."""
    conn = _conn()

    async def fetch_token(nft_id):
        return {
            "nft_id": "FORGED_CHAR",
            "owner": "rAttacker",
            "taxon": config.SWAP_TAXON,
            "uri_hex": "CD",
            "issuer": FOREIGN_ISSUER,
        }

    async def fetch_meta(uri_hex):
        return _char_meta(9999)

    tx = {
        "TransactionType": "NFTokenMint",
        "meta": {"TransactionResult": "tesSUCCESS", "nftoken_id": "FORGED_CHAR"},
    }
    genesis = te.Genesis(trait_counts={}, edition_bodies={})  # 9999 unknown
    _run(
        nft_listener.apply_economy_tx(
            conn, tx, fetch_token_fn=fetch_token, fetch_meta_fn=fetch_meta, genesis=genesis
        )
    )
    assert es.read_supply_changes(conn) == []


# --- Task 9: genesis wiring for legacy Closet metadata + blank-model Modify ---


def test_closet_rebuild_converts_legacy_integer_bodies_with_genesis():
    """A legacy-schema Closet token whose metadata still carries an integer
    `bodies` list (pre-migration) must convert to a ("Body", value) asset row
    on listener rebuild when the effective genesis is forwarded in."""
    conn = _conn()
    # build_closet_metadata (schema v2) never writes a non-empty "bodies" list
    # (every caller passes []), so a legacy pre-migration token is constructed
    # by hand here to exercise the conversion path.
    meta = {
        "lfg_closet": {
            "assets": [],
            "bodies": [3],
        }
    }

    async def fetch_token(nft_id):
        return {
            "nft_id": "CLOSET_LEGACY",
            "owner": "rUser",
            "taxon": config.CLOSET_TAXON,
            "uri_hex": "AB",
            "issuer": config.SWAP_ISSUER_ADDRESS,
        }

    async def fetch_meta(uri_hex):
        return meta

    tx = {
        "TransactionType": "NFTokenModify",
        "NFTokenID": "CLOSET_LEGACY",
        "meta": {"TransactionResult": "tesSUCCESS"},
    }
    genesis = te.Genesis(trait_counts={}, edition_bodies={3: ("Milady", "milady")})
    _run(
        nft_listener.apply_economy_tx(
            conn,
            tx,
            fetch_token_fn=fetch_token,
            fetch_meta_fn=fetch_meta,
            genesis=genesis,
        )
    )
    assets = {(s, v): n for o, s, v, n in es.read_closet_assets(conn)}
    assert assets.get(("Body", "Milady")) == 1
    # legacy bodies list must not also land in closet_bodies once converted.
    assert es.read_closet_bodies(conn) == []


def test_character_modify_to_blank_writes_no_economy_rows():
    """A character NFTokenModify (blank-harvest strip-in-place) is not a Closet
    or trait-token taxon, so it must leave closet_assets/trait_tokens
    completely untouched -- no spurious economy writes."""
    conn = _conn()

    async def fetch_token(nft_id):
        return {
            "nft_id": "CHAR_BLANKED",
            "owner": "rUser",
            "taxon": config.SWAP_TAXON,
            "uri_hex": "CD",
            "issuer": config.SWAP_ISSUER_ADDRESS,
        }

    async def fetch_meta(uri_hex):
        # Blanked character metadata: no trait attributes of interest.
        return {"name": "LFG #42", "attributes": []}

    tx = {
        "TransactionType": "NFTokenModify",
        "NFTokenID": "CHAR_BLANKED",
        "meta": {"TransactionResult": "tesSUCCESS"},
    }
    genesis = te.Genesis(trait_counts={}, edition_bodies={42: ("Straight Blue", "male")})
    _run(
        nft_listener.apply_economy_tx(
            conn,
            tx,
            fetch_token_fn=fetch_token,
            fetch_meta_fn=fetch_meta,
            genesis=genesis,
        )
    )
    assert es.read_closet_assets(conn) == []
    assert es.read_closet_bodies(conn) == []
    assert es.read_trait_tokens(conn) == []
    assert es.read_supply_changes(conn) == []


def test_genuine_issuer_named_mint_still_logs_growth():
    """Positive control: the same #N-named mint from OUR issuer still records
    growth, so the gate scopes to issuer identity and nothing more."""
    conn = _conn()

    async def fetch_token(nft_id):
        return {
            "nft_id": "REAL_CHAR",
            "owner": "rUser",
            "taxon": config.SWAP_TAXON,
            "uri_hex": "CD",
            "issuer": config.SWAP_ISSUER_ADDRESS,
        }

    async def fetch_meta(uri_hex):
        return _char_meta(9999)

    tx = {
        "TransactionType": "NFTokenMint",
        "meta": {"TransactionResult": "tesSUCCESS", "nftoken_id": "REAL_CHAR"},
    }
    genesis = te.Genesis(trait_counts={}, edition_bodies={})  # 9999 unknown
    _run(
        nft_listener.apply_economy_tx(
            conn, tx, fetch_token_fn=fetch_token, fetch_meta_fn=fetch_meta, genesis=genesis
        )
    )
    rows = es.read_supply_changes(conn)
    assert len(rows) == 1
    assert rows[0]["kind"] == "mint" and rows[0]["edition"] == 9999


def _accept_tx(nft_id: str, *, account: str, deleted: list[dict]) -> dict:
    return {
        "TransactionType": "NFTokenAcceptOffer",
        "Account": account,
        "meta": {
            "TransactionResult": "tesSUCCESS",
            "nftoken_id": nft_id,
            "AffectedNodes": [
                {"DeletedNode": {"LedgerEntryType": "NFTokenOffer", **w}} for w in deleted
            ],
        },
    }


def _offer(nft_id: str, owner: str, *, sell: bool) -> dict:
    return {
        "LedgerIndex": f"{owner}-{'S' if sell else 'B'}",
        "FinalFields": {"NFTokenID": nft_id, "Owner": owner, "Flags": 1 if sell else 0},
    }


def _apply_accept(conn, tx, clio_owner: str) -> None:
    async def fetch_token(nft_id):
        return _trait_token_dict(nft_id, clio_owner)

    async def fetch_meta(uri_hex):
        return _trait_meta("Background", "Pastel Green")

    _run(
        nft_listener.apply_economy_tx(
            conn, tx, fetch_token_fn=fetch_token, fetch_meta_fn=fetch_meta, genesis=None
        )
    )


def test_trait_accept_owner_comes_from_tx_not_stale_nft_info():
    """Extract delivery: the user accepts the issuer's sell offer while clio
    nft_info still reports the issuer. The row must record the accepting user."""
    conn = _conn()
    issuer = config.SWAP_ISSUER_ADDRESS
    es.upsert_trait_token(conn, "TRAITX", issuer, "Background", "Pastel Green")
    tx = _accept_tx("TRAITX", account="rUser", deleted=[_offer("TRAITX", issuer, sell=True)])
    _apply_accept(conn, tx, clio_owner=issuer)
    assert es.read_trait_tokens(conn) == [("TRAITX", "rUser", "Background", "Pastel Green")]


def test_trait_brokered_accept_owner_is_buy_offer_owner():
    conn = _conn()
    es.upsert_trait_token(conn, "TRAITB", "rSeller", "Hat", "Cap")
    tx = _accept_tx(
        "TRAITB",
        account="rBroker",
        deleted=[_offer("TRAITB", "rSeller", sell=True), _offer("TRAITB", "rBuyer", sell=False)],
    )
    _apply_accept(conn, tx, clio_owner="rSeller")
    assert es.read_trait_tokens(conn)[0][1] == "rBuyer"


def test_trait_bid_accept_owner_is_bidder():
    """The holder accepts a buy offer directly: tx.Account is the SELLER."""
    conn = _conn()
    es.upsert_trait_token(conn, "TRAITD", "rHolder", "Hat", "Cap")
    tx = _accept_tx("TRAITD", account="rHolder", deleted=[_offer("TRAITD", "rBidder", sell=False)])
    _apply_accept(conn, tx, clio_owner="rHolder")
    assert es.read_trait_tokens(conn)[0][1] == "rBidder"


# --- #522: the Closet mirror follows the URI each tx set, never a lagging nft_info ---
#
# A Closet's contents live in the JSON behind its token URI, and every version
# gets its own URL, so a version is identified by its URI. On 2026-09-11 the
# listener handled a deposit's Closet modify while clio nft_info still served
# the previous URI, rebuilt the mirror one version back, and the owner's next
# deposit full-overwrote the token from that mirror — erasing a credit on-chain
# (10 such lost versions since 08-07, #522).

CLOSET_ID = "00100000D1AE1BC312BEF9C68233FB0C8CF6A338F7C227BE3E5B27B704943DBA"


VERSION_A = closet_uri("a")
VERSION_B = closet_uri("b")
VERSION_C = closet_uri("c")
# What the CDN serves at each version's URL.
_CLOSET_VERSIONS = {
    VERSION_A: [("Body", "Ape Xray", 2)],
    VERSION_B: [("Body", "Ape Xray", 3)],
    VERSION_C: [("Body", "Ape Xray", 3), ("Head", "Pepe Hat", 1)],
}


def _closet_tx(kind: str, uri_hex: str, ledger_index: int) -> dict:
    return closet_version_tx(kind, CLOSET_ID, uri_hex, ledger_index)


def _apply_closet_tx(
    conn, tx: dict, *, clio_uri: str, clio_owner: str = "rUser", unreadable: tuple = ()
) -> None:
    async def fetch_token(nft_id):  # clio nft_info: may still serve another version
        return {
            "nft_id": nft_id,
            "owner": clio_owner,
            "taxon": config.CLOSET_TAXON,
            "uri_hex": clio_uri,
            "issuer": config.SWAP_ISSUER_ADDRESS,
        }

    async def fetch_meta(uri_hex):
        if uri_hex in unreadable:  # CDN/timeout failure: what _bounded returns
            return None
        return bt.build_closet_metadata("rUser", _CLOSET_VERSIONS[uri_hex], [])

    _run(
        nft_listener.apply_economy_tx(
            conn, tx, fetch_token_fn=fetch_token, fetch_meta_fn=fetch_meta, genesis=None
        )
    )


def _closet_contents(conn, owner: str = "rUser") -> dict:
    return {(s, v): n for o, s, v, n in es.read_closet_assets(conn) if o == owner}


def test_closet_modify_rebuilds_from_tx_uri_not_lagging_nft_info():
    conn = _conn()
    es.set_closet_token(conn, "rUser", CLOSET_ID, VERSION_A, status=bt.ACTIVE)
    es.set_closet_contents(conn, "rUser", _CLOSET_VERSIONS[VERSION_A], [])

    # The modify at ledger 1000 set version B; clio still answers with A.
    _apply_closet_tx(conn, _closet_tx("modify", VERSION_B, 1000), clio_uri=VERSION_A)

    assert _closet_contents(conn) == {("Body", "Ape Xray"): 3}
    assert es.get_closet_token(conn, "rUser") == (CLOSET_ID, VERSION_B)
    assert es.get_closet_applied_ledger(conn, "rUser") == 1000


def test_closet_mint_applies_first_version_from_tx_uri():
    """The mint is a Closet's first version. Reached only after the owner
    already holds the token (clio then serves a later URI), it still mirrors
    the version it minted, stamped with its own ledger; the later modifies
    advance the mirror from there."""
    conn = _conn()
    es.set_closet_token(
        conn, "rUser", CLOSET_ID, VERSION_A, status=bt.PENDING_ACCEPT, offer_id="OF1"
    )

    _apply_closet_tx(conn, _closet_tx("mint", VERSION_A, 990), clio_uri=VERSION_C)

    assert _closet_contents(conn) == {("Body", "Ape Xray"): 2}
    assert es.get_closet_record(conn, "rUser") == (CLOSET_ID, VERSION_A, bt.ACTIVE, "OF1")
    assert es.get_closet_applied_ledger(conn, "rUser") == 990


def test_older_closet_modify_never_regresses_mirror():
    conn = _conn()
    es.set_closet_token(conn, "rUser", CLOSET_ID, VERSION_A, status=bt.ACTIVE)
    # The mirror already holds version C, set by the modify at ledger 1005.
    _apply_closet_tx(conn, _closet_tx("modify", VERSION_C, 1005), clio_uri=VERSION_C)

    # The OLDER modify (version B, ledger 1000) is handled afterwards, and the
    # clio node answering still serves B.
    _apply_closet_tx(conn, _closet_tx("modify", VERSION_B, 1000), clio_uri=VERSION_B)

    assert _closet_contents(conn) == {("Body", "Ape Xray"): 3, ("Head", "Pepe Hat"): 1}
    assert es.get_closet_token(conn, "rUser") == (CLOSET_ID, VERSION_C)
    assert es.get_closet_applied_ledger(conn, "rUser") == 1005


def test_unreadable_closet_version_is_not_claimed_by_the_mirror():
    """Metadata for the new version cannot be fetched, so the contents stay at
    the old version (fail closed). The URI and ledger stamp must stay with them:
    recording the new version over old contents would make a mirror that is
    behind the chain look current."""
    conn = _conn()
    es.set_closet_token(conn, "rUser", CLOSET_ID, VERSION_A, status=bt.ACTIVE)
    _apply_closet_tx(conn, _closet_tx("modify", VERSION_A, 1000), clio_uri=VERSION_A)

    _apply_closet_tx(
        conn, _closet_tx("modify", VERSION_B, 1005), clio_uri=VERSION_B, unreadable=(VERSION_B,)
    )

    assert _closet_contents(conn) == {("Body", "Ape Xray"): 2}
    assert es.get_closet_record(conn, "rUser") == (CLOSET_ID, VERSION_A, bt.ACTIVE, None)
    assert es.get_closet_applied_ledger(conn, "rUser") == 1000


def test_listener_lag_never_regresses_a_flow_written_closet():
    """Flows write the mirror themselves as soon as their Closet modify
    validates. A lagging listener can still be working through an OLDER Closet
    modify for the same owner, whose tx carries an older URI. The flow's write is
    stamped with its own ledger, so that tx is skipped instead of rolling the
    mirror back under the owner's next flow."""
    from lfg_core import economy_flow as ef

    conn = _conn()
    es.set_closet_token(conn, "rUser", CLOSET_ID, VERSION_A, status=bt.ACTIVE)
    _apply_closet_tx(conn, _closet_tx("modify", VERSION_A, 1980), clio_uri=VERSION_A)

    def flow_deps(name: str, ledger_index: int) -> ef.EconomyDeps:
        async def upload(meta):
            return f"https://cdn/closets/{name}.json"

        async def modify(nft_id, owner, url):
            return bt.ModifyReceipt(tx_hash=f"FLOW{ledger_index}", ledger_index=ledger_index)

        return ef.EconomyDeps(
            conn=conn,
            closet_upload_fn=upload,
            closet_mint_fn=None,
            closet_offer_fn=None,
            closet_accept_fn=None,
            closet_modify_fn=modify,
            char_compose_fn=None,
            char_mint_fn=None,
            char_modify_fn=None,
            char_burn_fn=None,
            char_offer_fn=None,
            char_accept_fn=None,
        )

    def contents(version: str) -> dict:
        return {(s, v): n for s, v, n in _CLOSET_VERSIONS[version]}

    # Two flows back to back: version B at ledger 1990, then C at ledger 2000.
    _run(ef._sync_then_persist(flow_deps("b", 1990), "rUser", contents(VERSION_B)))
    _run(ef._sync_then_persist(flow_deps("c", 2000), "rUser", contents(VERSION_C)))

    # Only now does the listener reach the first flow's modify (version B).
    _apply_closet_tx(conn, _closet_tx("modify", VERSION_B, 1990), clio_uri=VERSION_C)

    assert _closet_contents(conn) == {("Body", "Ape Xray"): 3, ("Head", "Pepe Hat"): 1}
    assert es.get_closet_token(conn, "rUser") == (CLOSET_ID, VERSION_C)
    assert es.get_closet_applied_ledger(conn, "rUser") == 2000
