# One Closet NFToken belongs to exactly one owner, and that owner is never a
# project signing account (#383).
#
# On mainnet 2026-08-17 the listener wrote a second closet_tokens row keyed to
# the ISSUER for a Closet the real user's row already pointed at: a freshly
# minted Closet sits in the issuer's wallet until the user accepts the offer,
# so `nft_info`'s owner-of-record at NFTokenMint time IS the issuer.
# `scripts/backfill_economy._reconcile_closet` had already learned this (#190)
# and skips + scrubs issuer-held Closets; `nft_listener._apply_closet` had not.
#
# These tests pin both halves: the listener no longer forges the row, and the
# store refuses to write one even if some future caller tries.

import asyncio
import sqlite3

import pytest

from lfg_core import closet_token as ct
from lfg_core import config, nft_listener
from lfg_core import economy_store as es
from lfg_core import trait_economy as te

ISSUER = config.SWAP_ISSUER_ADDRESS
USER = "rET8NWdfFwoyqxDeoyq1RUaeqDKVA3m6Du"
CLOSET_ID = "EB03E1D904943DA0"


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


def _closet_token(owner: str, nft_id: str = CLOSET_ID, uri_hex: str = "AB") -> dict:
    return {
        "nft_id": nft_id,
        "owner": owner,
        "taxon": config.CLOSET_TAXON,
        "uri_hex": uri_hex,
        "issuer": config.SWAP_ISSUER_ADDRESS,
    }


def _apply(conn, tx, token, metadata):
    async def fetch_token(_nft_id):
        return token

    async def fetch_meta(_uri_hex):
        return metadata

    return _run(
        nft_listener.apply_economy_tx(
            conn,
            tx,
            fetch_token_fn=fetch_token,
            fetch_meta_fn=fetch_meta,
            genesis=te.Genesis(trait_counts={}, edition_bodies={}),
        )
    )


def _mint_tx(nft_id: str = CLOSET_ID) -> dict:
    return {
        "TransactionType": "NFTokenMint",
        "meta": {"TransactionResult": "tesSUCCESS", "nftoken_id": nft_id},
    }


# --- listener: never key a Closet row to the issuer -------------------------


def test_closet_mint_does_not_key_a_row_to_the_issuer():
    """The exact #383 shape: ensure_closet records the pending Closet under the
    real user, then ~37s later the listener sees the NFTokenMint whose
    owner-of-record is still the issuer. It must not write a second row."""
    conn = _conn()
    es.set_closet_token(conn, USER, CLOSET_ID, "AB", status=ct.PENDING_ACCEPT, offer_id="OF1")

    _apply(conn, _mint_tx(), _closet_token(ISSUER), ct.build_closet_metadata(ISSUER, [], []))

    assert es.get_closet_record(conn, ISSUER) is None
    # The real owner's row is untouched — same token, still pending, offer kept.
    assert es.get_closet_record(conn, USER) == (CLOSET_ID, "AB", ct.PENDING_ACCEPT, "OF1")


def test_closet_mint_does_not_write_issuer_assets():
    """set_closet_contents(issuer, …) is equally bogus: the issuer must own no
    closet_assets rows."""
    conn = _conn()
    meta = ct.build_closet_metadata(ISSUER, [("Hat", "Cap", 2)], [])

    _apply(conn, _mint_tx(), _closet_token(ISSUER), meta)

    assert [a for a in es.read_closet_assets(conn) if a[0] == ISSUER] == []


def test_closet_mint_scrubs_a_prior_bogus_issuer_row():
    """Mirrors backfill_economy's #190 scrub: seeing an issuer-held Closet is
    the moment to clean up whatever a previous buggy run recorded, so the real
    user's pending Closet stops being shadowed."""
    conn = _conn()
    es.set_closet_token(conn, USER, CLOSET_ID, "AB", status=ct.PENDING_ACCEPT, offer_id="OF1")
    # Simulate the row #383 found, written by the pre-fix listener onto a
    # pre-fix schema (the unique index is exactly what stops it existing now).
    conn.execute("DROP INDEX idx_closet_tokens_nft_id")
    conn.execute(
        "INSERT INTO closet_tokens (owner, nft_id, uri_hex, status) VALUES (?,?,?,?)",
        (ISSUER, CLOSET_ID, "AB", ct.PENDING_ACCEPT),
    )
    conn.execute(
        "INSERT INTO closet_assets (owner, slot, value, count) VALUES (?,?,?,?)",
        (ISSUER, "Hat", "Cap", 1),
    )
    conn.commit()

    _apply(conn, _mint_tx(), _closet_token(ISSUER), ct.build_closet_metadata(ISSUER, [], []))

    assert es.get_closet_record(conn, ISSUER) is None
    assert [a for a in es.read_closet_assets(conn) if a[0] == ISSUER] == []
    assert es.get_closet_record(conn, USER) == (CLOSET_ID, "AB", ct.PENDING_ACCEPT, "OF1")


def test_closet_accept_still_promotes_the_real_owner_to_active():
    """Regression guard: the valuable half of _apply_closet — promoting the
    user's row to ACTIVE once they accept — must keep working."""
    conn = _conn()
    es.set_closet_token(conn, USER, CLOSET_ID, "AB", status=ct.PENDING_ACCEPT, offer_id="OF1")

    accept = {
        "TransactionType": "NFTokenAcceptOffer",
        "NFTokenID": CLOSET_ID,
        "meta": {"TransactionResult": "tesSUCCESS"},
    }
    _apply(
        conn, accept, _closet_token(USER), ct.build_closet_metadata(USER, [("Hat", "Cap", 1)], [])
    )

    assert es.get_closet_record(conn, USER) == (CLOSET_ID, "AB", ct.ACTIVE, "OF1")
    assert [a for a in es.read_closet_assets(conn) if a[0] == USER] == [(USER, "Hat", "Cap", 1)]


# --- store: the invariant, enforced below every write site ------------------


def test_set_closet_token_refuses_a_project_signing_account():
    conn = _conn()
    with pytest.raises(es.ClosetOwnerError):
        es.set_closet_token(conn, ISSUER, CLOSET_ID, "AB")
    assert es.get_closet_record(conn, ISSUER) is None


def test_set_closet_contents_refuses_a_project_signing_account():
    conn = _conn()
    with pytest.raises(es.ClosetOwnerError):
        es.set_closet_contents(conn, ISSUER, [("Hat", "Cap", 1)], [])
    assert [a for a in es.read_closet_assets(conn) if a[0] == ISSUER] == []


def test_delete_closet_still_works_on_a_project_account():
    """The guard blocks writes, not the scrub — delete_closet(issuer) is how a
    bogus row gets removed."""
    conn = _conn()
    conn.execute(
        "INSERT INTO closet_tokens (owner, nft_id, uri_hex, status) VALUES (?,?,?,?)",
        (ISSUER, CLOSET_ID, "AB", ct.PENDING_ACCEPT),
    )
    conn.commit()
    es.delete_closet(conn, ISSUER)
    assert es.get_closet_record(conn, ISSUER) is None


def test_one_nft_id_cannot_belong_to_two_owners():
    """The DB itself refuses the duplicate, not just the write site."""
    conn = _conn()
    es.set_closet_token(conn, USER, CLOSET_ID, "AB")
    with pytest.raises(sqlite3.IntegrityError):
        es.set_closet_token(conn, "rOtherUser", CLOSET_ID, "AB")


def test_reassigning_the_same_owner_a_new_token_is_allowed():
    """ensure_closet re-mints a stale Closet for the SAME owner — the unique
    index must not block that (the old nft_id leaves the table with the update)."""
    conn = _conn()
    es.set_closet_token(conn, USER, "OLD", "AB")
    es.set_closet_token(conn, USER, "NEW", "CD")
    assert es.get_closet_token(conn, USER) == ("NEW", "CD")


# --- migration onto an already-broken database ------------------------------


def _legacy_db(tmp_path, rows):
    """A closet_tokens table as it existed before the unique index, seeded with
    `rows` of (owner, nft_id)."""
    path = str(tmp_path / "onchain_test.db")
    c = sqlite3.connect(path)
    es.init_economy_schema(c)
    c.execute("DROP INDEX IF EXISTS idx_closet_tokens_nft_id")
    for owner, nft_id in rows:
        c.execute(
            "INSERT INTO closet_tokens (owner, nft_id, uri_hex, status) VALUES (?,?,?,?)",
            (owner, nft_id, "AB", ct.PENDING_ACCEPT),
        )
    c.commit()
    c.close()
    return path


def _has_index(conn) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_closet_tokens_nft_id'"
        ).fetchone()
        is not None
    )


def test_migration_creates_the_index_on_a_clean_database(tmp_path):
    path = _legacy_db(tmp_path, [(USER, CLOSET_ID)])
    conn = sqlite3.connect(path)
    es.init_economy_schema(conn)
    assert _has_index(conn)


def test_migration_leaves_a_dirty_database_usable_and_warns(tmp_path, caplog):
    """A DB that already carries the #383 duplicate must still open: the index
    is skipped (loudly), the audit flags it, and the reconcile script repairs
    it. Refusing to boot the listener would be a worse failure."""
    path = _legacy_db(tmp_path, [(USER, CLOSET_ID), (ISSUER, CLOSET_ID)])
    conn = sqlite3.connect(path)
    with caplog.at_level("WARNING"):
        es.init_economy_schema(conn)
    assert not _has_index(conn)
    assert any("idx_closet_tokens_nft_id" in r.message for r in caplog.records)
    # Still fully usable — reads and non-conflicting writes work.
    assert es.get_closet_record(conn, USER) is not None
