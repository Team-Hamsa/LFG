# tests/test_market_listener.py
# Env-guard preamble: importing lfg_core.config freezes its constants (e.g.
# IMG_PROXY_ALLOWED_BASES, LAYER_SOURCE) at import time; set the same defaults
# test_smoke.py uses so collection order can't strand them. (Copy the block
# verbatim from tests/test_server_identity_wiring.py — same keys/values.)
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

import sqlite3  # noqa: E402

from xrpl.core import addresscodec  # noqa: E402

from lfg_core import config, economy_store, market_store, nft_index, nft_listener  # noqa: E402

# --- fixture helpers -------------------------------------------------------


def _conn() -> sqlite3.Connection:
    """One in-memory conn carrying onchain_nfts + trait_tokens + market_listings,
    same as the production listener's shared onchain_<net>.db."""
    c = sqlite3.connect(":memory:")
    c.executescript(nft_index._SCHEMA)
    economy_store.init_economy_schema(c)
    market_store.init_db(c)
    return c


def _foreign_addr() -> str:
    return addresscodec.encode_classic_address(b"\xaa" * 20)


def _our_nft_id(seq: int = 1) -> str:
    """A 64-hex NFTokenID embedding OUR issuer's AccountID at hex chars 8..48
    (config.SWAP_ISSUER_ADDRESS), like the real ledger does."""
    acct = addresscodec.decode_classic_address(config.SWAP_ISSUER_ADDRESS).hex().upper()
    return f"000A0000{acct}00000000{seq:08X}"


def _foreign_nft_id(seq: int = 1) -> str:
    acct = addresscodec.decode_classic_address(_foreign_addr()).hex().upper()
    return f"000A0000{acct}00000000{seq:08X}"


def _seed_character(conn: sqlite3.Connection, nft_id: str, owner: str = "rOwner") -> None:
    conn.execute(
        "INSERT INTO onchain_nfts (nft_id, nft_number, owner, is_burned, mutable, uri_hex, body) "
        "VALUES (?, 1, ?, 0, 0, '', NULL)",
        (nft_id, owner),
    )
    conn.commit()


def _seed_trait(
    conn: sqlite3.Connection,
    nft_id: str,
    owner: str = "rOwner",
    slot: str = "Hat",
    value: str = "Cap",
) -> None:
    economy_store.upsert_trait_token(conn, nft_id, owner, slot, value)


def _sell_offer_create_tx(
    nft_id: str,
    *,
    seller: str = "rSeller",
    offer_index: str = "OFFER1",
    amount: str | dict = "1000000",
    destination: str | None = None,
    sell: bool = True,
) -> dict:
    flags = 1 if sell else 0
    new_fields: dict = {
        "NFTokenID": nft_id,
        "Flags": flags,
        "Amount": amount,
    }
    if destination is not None:
        new_fields["Destination"] = destination
    return {
        "TransactionType": "NFTokenCreateOffer",
        "Account": seller,
        "NFTokenID": nft_id,
        "ledger_index": 555,
        "date": 800000000,
        "meta": {
            "AffectedNodes": [
                {
                    "CreatedNode": {
                        "LedgerEntryType": "NFTokenOffer",
                        "LedgerIndex": offer_index,
                        "NewFields": new_fields,
                    }
                }
            ]
        },
    }


def _offer_cancel_tx(offer_indexes: list[str]) -> dict:
    return {
        "TransactionType": "NFTokenCancelOffer",
        "Account": "rSeller",
        "meta": {
            "AffectedNodes": [
                {
                    "DeletedNode": {
                        "LedgerEntryType": "NFTokenOffer",
                        "LedgerIndex": idx,
                        "FinalFields": {},
                    }
                }
                for idx in offer_indexes
            ]
        },
    }


def _accept_tx(
    *,
    nft_id: str,
    offer_index: str,
    seller: str,
    sell_flags: int = 1,
    buyer_offer: dict | None = None,
) -> dict:
    nodes = [
        {
            "DeletedNode": {
                "LedgerEntryType": "NFTokenOffer",
                "LedgerIndex": offer_index,
                "FinalFields": {
                    "NFTokenID": nft_id,
                    "Owner": seller,
                    "Flags": sell_flags,
                    "Amount": "1000000",
                },
            }
        }
    ]
    if buyer_offer is not None:
        nodes.append({"DeletedNode": buyer_offer})
    return {
        "TransactionType": "NFTokenAcceptOffer",
        "Account": "rBuyer",
        "meta": {"nftoken_id": nft_id, "AffectedNodes": nodes},
    }


def _run(coro):
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# --- classify_tx: new kinds + regression ------------------------------------


def test_classify_tx_market_kinds():
    create = {"TransactionType": "NFTokenCreateOffer", "meta": {}}
    cancel = {"TransactionType": "NFTokenCancelOffer", "meta": {}}
    assert nft_listener.classify_tx(create) == "offer_create"
    assert nft_listener.classify_tx(cancel) == "offer_cancel"


def test_classify_tx_regression_existing_kinds_unchanged():
    mint = {"TransactionType": "NFTokenMint", "meta": {"nftoken_id": "X"}}
    accept = {"TransactionType": "NFTokenAcceptOffer", "meta": {"nftoken_id": "X"}}
    burn = {"TransactionType": "NFTokenBurn", "NFTokenID": "X", "meta": {}}
    modify = {"TransactionType": "NFTokenModify", "NFTokenID": "X", "meta": {}}
    payment = {"TransactionType": "Payment", "meta": {}}
    assert nft_listener.classify_tx(mint) == "mint"
    assert nft_listener.classify_tx(accept) == "accept"
    assert nft_listener.classify_tx(burn) == "burn"
    assert nft_listener.classify_tx(modify) == "modify"
    assert nft_listener.classify_tx(payment) is None




def _brix_amount(value="10"):
    # #239: trait listings are BRIX-denominated on the token currency/issuer.
    from lfg_core import config

    return {
        "currency": config.TOKEN_CURRENCY_HEX,
        "issuer": config.TOKEN_ISSUER_ADDRESS,
        "value": value,
    }

# --- offer_create: membership + filtering -----------------------------------


def test_offer_create_character_upserts_live_row():
    conn = _conn()
    nft_id = _our_nft_id(1)
    _seed_character(conn, nft_id, owner="rSeller")
    tx = _sell_offer_create_tx(nft_id, seller="rSeller", offer_index="OFF_CHAR")

    _run(nft_listener.apply_market_tx(conn, tx))

    rows = conn.execute("SELECT * FROM market_listings WHERE offer_index='OFF_CHAR'").fetchall()
    assert len(rows) == 1
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM market_listings WHERE offer_index='OFF_CHAR'").fetchone()
    assert row["nft_id"] == nft_id
    assert row["kind"] == "character"
    assert row["seller"] == "rSeller"
    assert row["amount_drops"] == 1000000
    assert row["is_live"] == 1
    assert row["slot"] is None and row["value"] is None


def test_offer_create_trait_copies_slot_value():
    conn = _conn()
    nft_id = _our_nft_id(2)
    _seed_trait(conn, nft_id, owner="rSeller", slot="Hat", value="Wizard Hat")
    tx = _sell_offer_create_tx(
        nft_id, seller="rSeller", offer_index="OFF_TRAIT", amount=_brix_amount("10.5")
    )

    _run(nft_listener.apply_market_tx(conn, tx))

    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM market_listings WHERE offer_index='OFF_TRAIT'").fetchone()
    assert row is not None
    assert row["kind"] == "trait"
    assert row["slot"] == "Hat"
    assert row["value"] == "Wizard Hat"
    assert row["amount_brix"] == "10.5"
    assert row["amount_drops"] is None


def test_offer_create_foreign_issuer_produces_zero_rows():
    conn = _conn()
    nft_id = _foreign_nft_id(3)
    # Even if (hypothetically) indexed, foreign-issuer nft_ids must never
    # produce a row — the issuer pre-filter must short-circuit first.
    tx = _sell_offer_create_tx(nft_id, seller="rSeller", offer_index="OFF_FOREIGN")

    _run(nft_listener.apply_market_tx(conn, tx))

    assert conn.execute("SELECT COUNT(*) FROM market_listings").fetchone()[0] == 0


def test_offer_create_iou_amount_produces_zero_rows():
    conn = _conn()
    nft_id = _our_nft_id(4)
    _seed_character(conn, nft_id, owner="rSeller")
    iou_amount = {"currency": "USD", "issuer": "rIssuer", "value": "10"}
    tx = _sell_offer_create_tx(nft_id, seller="rSeller", offer_index="OFF_IOU", amount=iou_amount)

    _run(nft_listener.apply_market_tx(conn, tx))

    assert conn.execute("SELECT COUNT(*) FROM market_listings").fetchone()[0] == 0


def test_offer_create_our_issuer_unindexed_is_ignored():
    conn = _conn()
    nft_id = _our_nft_id(5)  # our issuer bytes, but never seeded into either table
    tx = _sell_offer_create_tx(nft_id, seller="rSeller", offer_index="OFF_UNINDEXED")

    _run(nft_listener.apply_market_tx(conn, tx))

    assert conn.execute("SELECT COUNT(*) FROM market_listings").fetchone()[0] == 0


def test_offer_create_buy_offer_ignored():
    conn = _conn()
    nft_id = _our_nft_id(6)
    _seed_character(conn, nft_id, owner="rSeller")
    tx = _sell_offer_create_tx(nft_id, seller="rSeller", offer_index="OFF_BUY", sell=False)

    _run(nft_listener.apply_market_tx(conn, tx))

    assert conn.execute("SELECT COUNT(*) FROM market_listings").fetchone()[0] == 0


# --- offer_cancel ------------------------------------------------------------


def test_offer_cancel_closes_cancelled():
    conn = _conn()
    nft_id = _our_nft_id(7)
    _seed_character(conn, nft_id, owner="rSeller")
    _run(nft_listener.apply_market_tx(conn, _sell_offer_create_tx(nft_id, offer_index="OFF_CX")))

    _run(nft_listener.apply_market_tx(conn, _offer_cancel_tx(["OFF_CX"])))

    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM market_listings WHERE offer_index='OFF_CX'").fetchone()
    assert row["is_live"] == 0
    assert row["closed_reason"] == "cancelled"


def test_offer_cancel_one_bad_entry_does_not_abort_the_rest(monkeypatch):
    """Per-item error isolation (#130): one NFTokenCancelOffer tx can delete
    many offers; a failure closing one of them must not abort processing of
    the remaining deleted offers (mirrors apply_market_tx's own per-tx
    isolation, one level down)."""
    conn = _conn()
    nft_id = _our_nft_id(7)
    _seed_character(conn, nft_id, owner="rSeller")
    _run(nft_listener.apply_market_tx(conn, _sell_offer_create_tx(nft_id, offer_index="OFF_A")))
    _run(
        nft_listener.apply_market_tx(
            conn, _sell_offer_create_tx(nft_id, seller="rSeller2", offer_index="OFF_B")
        )
    )

    real_close = market_store.close_listing

    def flaky_close(c, offer_index, reason, buyer=None):
        if offer_index == "OFF_A":
            raise RuntimeError("boom on OFF_A")
        return real_close(c, offer_index, reason, buyer=buyer)

    monkeypatch.setattr(nft_listener.market_store, "close_listing", flaky_close)
    _run(nft_listener.apply_market_tx(conn, _offer_cancel_tx(["OFF_A", "OFF_B"])))

    row = conn.execute(
        "SELECT is_live, closed_reason FROM market_listings WHERE offer_index='OFF_B'"
    ).fetchone()
    assert row == (0, "cancelled")


def test_offer_cancel_unknown_offer_index_is_harmless_noop():
    conn = _conn()
    # Never had this offer_index indexed at all.
    _run(nft_listener.apply_market_tx(conn, _offer_cancel_tx(["NEVER_SEEN"])))
    assert conn.execute("SELECT COUNT(*) FROM market_listings").fetchone()[0] == 0


# --- accept: character sold, trait sold+settled, stale delist ---------------


def test_accept_character_closes_sold():
    conn = _conn()
    nft_id = _our_nft_id(8)
    _seed_character(conn, nft_id, owner="rSeller")
    _run(
        nft_listener.apply_market_tx(
            conn, _sell_offer_create_tx(nft_id, seller="rSeller", offer_index="OFF_SOLD_CHAR")
        )
    )
    # Ownership moves to the buyer as part of the same real-world event.
    conn.execute("UPDATE onchain_nfts SET owner='rBuyer' WHERE nft_id=?", (nft_id,))
    conn.commit()

    _run(
        nft_listener.apply_market_tx(
            conn,
            _accept_tx(nft_id=nft_id, offer_index="OFF_SOLD_CHAR", seller="rSeller"),
        )
    )

    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM market_listings WHERE offer_index='OFF_SOLD_CHAR'").fetchone()
    assert row["is_live"] == 0
    assert row["closed_reason"] == "sold"
    assert row["settled"] is None  # characters never get a settled meaning


def test_accept_trait_closes_sold_with_settled_zero():
    conn = _conn()
    nft_id = _our_nft_id(9)
    _seed_trait(conn, nft_id, owner="rSeller", slot="Hat", value="Cap")
    _run(
        nft_listener.apply_market_tx(
            conn, _sell_offer_create_tx(
                nft_id, seller="rSeller", offer_index="OFF_SOLD_TRAIT", amount=_brix_amount()
            )
        )
    )
    economy_store.upsert_trait_token(conn, nft_id, "rBuyer", "Hat", "Cap")  # ownership moved

    _run(
        nft_listener.apply_market_tx(
            conn,
            _accept_tx(nft_id=nft_id, offer_index="OFF_SOLD_TRAIT", seller="rSeller"),
        )
    )

    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM market_listings WHERE offer_index='OFF_SOLD_TRAIT'"
    ).fetchone()
    assert row["is_live"] == 0
    assert row["closed_reason"] == "sold"
    assert row["settled"] == 0
    # The post-transfer owner is persisted as the durable buyer-of-record so
    # settlement stays recoverable after run_deposit deletes trait_tokens.
    assert row["buyer"] == "rBuyer"


def test_accept_persists_buyer_from_tx_even_when_owner_refresh_stale():
    """The durable buyer comes from the ACCEPT TX (tx.Account for a direct sell
    accept), not the local owner index. Even when the owner refresh failed/ran
    stale (trait_tokens.owner still == seller), the sold row must carry the real
    accepting account as buyer — never NULL (which would strand the settlement
    sweep) and never the seller (which would misdirect it)."""
    conn = _conn()
    nft_id = _our_nft_id(20)
    _seed_trait(conn, nft_id, owner="rSeller", slot="Hat", value="Cap")
    _run(
        nft_listener.apply_market_tx(
            conn, _sell_offer_create_tx(
                nft_id, seller="rSeller", offer_index="OFF_STALE_BUYER", amount=_brix_amount()
            )
        )
    )
    # NOTE: no ownership move — trait_tokens.owner still == seller (refresh stale)

    _run(
        nft_listener.apply_market_tx(
            conn,
            # _accept_tx sets tx.Account == "rBuyer" (the accepting account)
            _accept_tx(nft_id=nft_id, offer_index="OFF_STALE_BUYER", seller="rSeller"),
        )
    )

    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM market_listings WHERE offer_index='OFF_STALE_BUYER'"
    ).fetchone()
    assert row["is_live"] == 0
    assert row["closed_reason"] == "sold"
    assert row["buyer"] == "rBuyer"  # resolved from the tx, index-independent


def test_accept_brokered_persists_buy_offer_owner_not_broker():
    """In a brokered accept the tx.Account is the BROKER, not the buyer; the new
    owner is the buy offer's Owner. That account (not the broker) must be
    persisted as the durable buyer."""
    conn = _conn()
    nft_id = _our_nft_id(21)
    _seed_trait(conn, nft_id, owner="rSeller", slot="Hat", value="Cap")
    _run(
        nft_listener.apply_market_tx(
            conn, _sell_offer_create_tx(
                nft_id, seller="rSeller", offer_index="OFF_BROKERED", amount=_brix_amount()
            )
        )
    )
    buy_offer = {
        "LedgerEntryType": "NFTokenOffer",
        "LedgerIndex": "BUYOFF",
        "FinalFields": {
            "NFTokenID": nft_id,
            "Owner": "rRealBuyer",
            "Flags": 0,  # buy offer (sell bit unset)
            "Amount": "1000000",
        },
    }
    _run(
        nft_listener.apply_market_tx(
            conn,
            # tx.Account defaults to "rBuyer" here, standing in for the broker
            _accept_tx(
                nft_id=nft_id,
                offer_index="OFF_BROKERED",
                seller="rSeller",
                buyer_offer=buy_offer,
            ),
        )
    )

    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM market_listings WHERE offer_index='OFF_BROKERED'").fetchone()
    assert row["closed_reason"] == "sold"
    assert row["buyer"] == "rRealBuyer"  # buy offer Owner, not the broker


def test_accept_delists_other_live_rows_for_stale_seller():
    """A second live sell offer from the OLD owner for the same nft_id (e.g. a
    stale/duplicate listing) must be delisted once ownership moves on, even
    though it wasn't the offer actually accepted."""
    conn = _conn()
    nft_id = _our_nft_id(10)
    _seed_character(conn, nft_id, owner="rSeller")
    _run(
        nft_listener.apply_market_tx(
            conn, _sell_offer_create_tx(nft_id, seller="rSeller", offer_index="OFF_A")
        )
    )
    _run(
        nft_listener.apply_market_tx(
            conn, _sell_offer_create_tx(nft_id, seller="rSeller", offer_index="OFF_B")
        )
    )
    conn.execute("UPDATE onchain_nfts SET owner='rBuyer' WHERE nft_id=?", (nft_id,))
    conn.commit()

    _run(
        nft_listener.apply_market_tx(
            conn, _accept_tx(nft_id=nft_id, offer_index="OFF_A", seller="rSeller")
        )
    )

    conn.row_factory = sqlite3.Row
    sold = conn.execute("SELECT * FROM market_listings WHERE offer_index='OFF_A'").fetchone()
    other = conn.execute("SELECT * FROM market_listings WHERE offer_index='OFF_B'").fetchone()
    assert sold["is_live"] == 0 and sold["closed_reason"] == "sold"
    assert other["is_live"] == 0 and other["closed_reason"] == "stale"


def test_accept_does_not_delist_rows_still_matching_new_owner():
    """If another live row's seller happens to equal the NEW owner (edge case,
    e.g. re-listed instantly), it must be left alone."""
    conn = _conn()
    nft_id = _our_nft_id(11)
    _seed_character(conn, nft_id, owner="rSeller")
    _run(
        nft_listener.apply_market_tx(
            conn, _sell_offer_create_tx(nft_id, seller="rSeller", offer_index="OFF_C1")
        )
    )
    _run(
        nft_listener.apply_market_tx(
            conn, _sell_offer_create_tx(nft_id, seller="rBuyer", offer_index="OFF_C2")
        )
    )
    conn.execute("UPDATE onchain_nfts SET owner='rBuyer' WHERE nft_id=?", (nft_id,))
    conn.commit()

    _run(
        nft_listener.apply_market_tx(
            conn, _accept_tx(nft_id=nft_id, offer_index="OFF_C1", seller="rSeller")
        )
    )

    conn.row_factory = sqlite3.Row
    other = conn.execute("SELECT * FROM market_listings WHERE offer_index='OFF_C2'").fetchone()
    assert other["is_live"] == 1


# --- error isolation ----------------------------------------------------------


def test_apply_market_tx_ignores_non_market_kinds():
    conn = _conn()
    tx = {"TransactionType": "Payment", "meta": {}}
    _run(nft_listener.apply_market_tx(conn, tx))  # must not raise
    assert conn.execute("SELECT COUNT(*) FROM market_listings").fetchone()[0] == 0


# --- #239: per-kind denomination in offer_create ------------------------------


def test_offer_create_xrp_trait_offer_ignored():
    # An XRP-denominated sell offer on a TRAIT token is the wrong
    # denomination under #239 — the listener must not index it.
    conn = _conn()
    nft_id = _our_nft_id(30)
    _seed_trait(conn, nft_id, owner="rSeller", slot="Hat", value="Cap")
    tx = _sell_offer_create_tx(nft_id, seller="rSeller", offer_index="OFF_XRP_TRAIT")

    _run(nft_listener.apply_market_tx(conn, tx))

    assert conn.execute("SELECT COUNT(*) FROM market_listings").fetchone()[0] == 0


def test_offer_create_brix_character_offer_ignored():
    conn = _conn()
    nft_id = _our_nft_id(31)
    _seed_character(conn, nft_id, owner="rSeller")
    tx = _sell_offer_create_tx(
        nft_id, seller="rSeller", offer_index="OFF_BRIX_CHAR", amount=_brix_amount()
    )

    _run(nft_listener.apply_market_tx(conn, tx))

    assert conn.execute("SELECT COUNT(*) FROM market_listings").fetchone()[0] == 0


def test_offer_create_foreign_iou_trait_offer_ignored():
    conn = _conn()
    nft_id = _our_nft_id(32)
    _seed_trait(conn, nft_id, owner="rSeller", slot="Hat", value="Cap")
    tx = _sell_offer_create_tx(
        nft_id,
        seller="rSeller",
        offer_index="OFF_FOREIGN_IOU",
        amount={"currency": "USD", "issuer": "rIssuer", "value": "10"},
    )

    _run(nft_listener.apply_market_tx(conn, tx))

    assert conn.execute("SELECT COUNT(*) FROM market_listings").fetchone()[0] == 0
