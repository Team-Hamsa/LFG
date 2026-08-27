# #445: wallet <-> XUMM issued_user_token correlation (User Profiles epic #444).
#
# The token is scoped per XUMM app + Xaman USER (same across every r-address
# in that install), so wallets observed sharing a token belong to one human.
# It also rotates (30-day inactivity expiry), so correlation is by recorded
# co-observation — never live-token equality — and it is a push credential,
# so only a sha256 hash ever lands in the correlation table.

import hashlib
import sqlite3

from lfg_service import identity

RAW_TOKEN = "3f1a0000-aaaa-bbbb-cccc-000000000001"
RAW_TOKEN_2 = "3f1a0000-aaaa-bbbb-cccc-000000000002"
W_A = "rWalletAAAAAAAAAAAAAAAAAAAAAAAAAA"
W_B = "rWalletBBBBBBBBBBBBBBBBBBBBBBBBBB"
W_C = "rWalletCCCCCCCCCCCCCCCCCCCCCCCCCC"


def _fresh_db(tmp_path, monkeypatch):
    db = tmp_path / "ids.db"
    monkeypatch.setattr(identity, "DATABASE", str(db))
    identity.ensure_identities_table()
    return db


def _rows(db):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(
            "SELECT token_hash, wallet, first_seen, last_seen FROM wallet_token_links"
        ).fetchall()
    finally:
        conn.close()


# --- Task 1: schema + observe_token ---------------------------------------


def test_table_created(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path, monkeypatch)
    conn = sqlite3.connect(db)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(wallet_token_links)")}
    conn.close()
    assert {"token_hash", "wallet", "first_seen", "last_seen"} <= cols


def test_observe_stores_hash_never_raw(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path, monkeypatch)
    identity.observe_token(W_A, RAW_TOKEN)
    rows = _rows(db)
    assert len(rows) == 1
    expected = hashlib.sha256(RAW_TOKEN.encode()).hexdigest()
    assert rows[0][0] == expected
    assert rows[0][1] == W_A
    # the raw token must not appear anywhere in the DB file
    assert RAW_TOKEN.encode() not in (tmp_path / "ids.db").read_bytes()


def test_reobserve_is_appendonly_upsert(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path, monkeypatch)
    identity.observe_token(W_A, RAW_TOKEN)
    first = _rows(db)[0]
    identity.observe_token(W_A, RAW_TOKEN)
    rows = _rows(db)
    assert len(rows) == 1  # PK (token_hash, wallet): no duplicate rows
    assert rows[0][2] == first[2]  # first_seen preserved
    assert rows[0][3] is not None  # last_seen stamped


def test_falsy_token_or_wallet_is_noop(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path, monkeypatch)
    identity.observe_token(W_A, None)
    identity.observe_token(W_A, "")
    identity.observe_token("", RAW_TOKEN)
    identity.observe_token(None, RAW_TOKEN)
    assert _rows(db) == []


def test_observe_never_raises(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    monkeypatch.setattr(identity, "DATABASE", str(tmp_path / "nonexistent-dir" / "x.db"))
    identity.observe_token(W_A, RAW_TOKEN)  # best-effort: must not raise


# --- Task 2: seed from identities.user_token -------------------------------


def test_seed_from_existing_identities(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path, monkeypatch)
    identity.link("discord", "111", "alice", W_A)
    identity.set_user_token("discord", "111", RAW_TOKEN)
    identity.link("web", W_B, "bob", W_B)  # no token: must not seed
    identity.ensure_identities_table()  # boot-time seed
    rows = _rows(db)
    assert [(r[0], r[1]) for r in rows] == [(hashlib.sha256(RAW_TOKEN.encode()).hexdigest(), W_A)]


def test_seed_is_idempotent(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path, monkeypatch)
    identity.link("discord", "111", "alice", W_A)
    identity.set_user_token("discord", "111", RAW_TOKEN)
    identity.ensure_identities_table()
    first = _rows(db)
    identity.ensure_identities_table()
    assert _rows(db) == first


# --- Task 3: bucket walk over token edges ----------------------------------


def test_shared_token_buckets_two_wallets(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    # two web identities (wallet IS the platform_user_id, #240) — disjoint today
    identity.link("web", W_A, "a", W_A)
    identity.link("web", W_B, "b", W_B)
    identity.observe_token(W_A, RAW_TOKEN)
    identity.observe_token(W_B, RAW_TOKEN)
    b = identity.bucket_for_wallet(W_A)
    assert b is not None
    assert b["wallets"] == sorted([W_A, W_B])
    assert b == identity.bucket_for_wallet(W_B)


def test_token_rotation_does_not_split_bucket(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    for w in (W_A, W_B, W_C):
        identity.link("web", w, w[:6], w)
    # hash1 saw A+B; token rotated; hash2 saw B+C -> one bucket A,B,C
    identity.observe_token(W_A, RAW_TOKEN)
    identity.observe_token(W_B, RAW_TOKEN)
    identity.observe_token(W_B, RAW_TOKEN_2)
    identity.observe_token(W_C, RAW_TOKEN_2)
    b = identity.bucket_for_wallet(W_A)
    assert b["wallets"] == sorted([W_A, W_B, W_C])


def test_token_edges_union_with_identity_edges(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    identity.link("discord", "111", "alice", W_A)  # discord identity -> wallet A
    identity.link("web", W_B, "b", W_B)
    identity.observe_token(W_A, RAW_TOKEN)
    identity.observe_token(W_B, RAW_TOKEN)
    b = identity.bucket_for("discord", "111")
    assert b is not None
    assert b["wallets"] == sorted([W_A, W_B])
    ids = {(i["platform"], i["platform_user_id"]) for i in b["identities"]}
    assert ("discord", "111") in ids and ("web", W_B) in ids
    # same bucket id from the wallet-keyed entry point
    assert identity.bucket_for_wallet(W_B)["bucket_id"] == b["bucket_id"]


def test_bucket_for_wallet_unknown_is_none(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    assert identity.bucket_for_wallet(W_A) is None


def test_bucket_for_wallet_token_only_wallet(tmp_path, monkeypatch):
    # a wallet seen ONLY via token observation (no wallet_links row) still
    # resolves, and the wallets-only bucket id is deterministic
    _fresh_db(tmp_path, monkeypatch)
    identity.observe_token(W_A, RAW_TOKEN)
    identity.observe_token(W_B, RAW_TOKEN)
    b = identity.bucket_for_wallet(W_B)
    assert b is not None
    assert b["wallets"] == sorted([W_A, W_B])
    assert b["identities"] == []
    import json

    assert b["bucket_id"] == json.dumps(["wallet", sorted([W_A, W_B])[0]])


def test_unrelated_tokens_stay_separate(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    identity.observe_token(W_A, RAW_TOKEN)
    identity.observe_token(W_B, RAW_TOKEN_2)
    a = identity.bucket_for_wallet(W_A)
    b = identity.bucket_for_wallet(W_B)
    assert a["wallets"] == [W_A]
    assert b["wallets"] == [W_B]
    assert a["bucket_id"] != b["bucket_id"]


def test_bucket_for_wallet_fails_closed(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    monkeypatch.setattr(identity, "DATABASE", str(tmp_path / "gone" / "x.db"))
    import pytest

    with pytest.raises(identity.BucketLookupError):
        identity.bucket_for_wallet(W_A)


# --- Task 4: capture-site wiring (set_user_token signer_wallet) ------------


def test_set_user_token_with_signer_wallet_observes(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path, monkeypatch)
    identity.link("discord", "111", "alice", W_A)
    identity.set_user_token("discord", "111", RAW_TOKEN, signer_wallet=W_A)
    rows = _rows(db)
    assert [(r[0], r[1]) for r in rows] == [(hashlib.sha256(RAW_TOKEN.encode()).hexdigest(), W_A)]
    # push token itself still stored raw on the identity row
    assert identity.user_token_for("discord", "111") == RAW_TOKEN


def test_set_user_token_without_signer_wallet_unchanged(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path, monkeypatch)
    identity.link("discord", "111", "alice", W_A)
    identity.set_user_token("discord", "111", RAW_TOKEN)
    assert _rows(db) == []


def test_persist_issued_user_token_passes_session_wallet(tmp_path, monkeypatch):
    # the #212 every-payload recapture must thread the session wallet through
    # so the observation is recorded against the verified signer
    import asyncio
    from types import SimpleNamespace

    from lfg_service import app as service_app

    def _run(coro):  # suite convention — asyncio.run() would clear the
        # thread's event loop (set_event_loop(None)) and poison later tests
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    calls = []
    monkeypatch.setattr(
        service_app.identity_store,
        "set_user_token",
        lambda *a, **k: calls.append((a, k)),
    )
    session = SimpleNamespace(issued_user_token=RAW_TOKEN, wallet=W_A)
    user = {"id": "111", "platform": "discord", "name": "alice"}
    _run(service_app._persist_issued_user_token(user, session))
    assert len(calls) == 1
    assert calls[0][0][2] == RAW_TOKEN
    assert calls[0][1] == {"signer_wallet": W_A}
    assert session.issued_user_token is None
