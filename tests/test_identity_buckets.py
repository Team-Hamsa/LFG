# #206: identity buckets — connected components over the append-only
# wallet_links identity—wallet graph. Leaky by design (same-wallet linkage
# only); see the module docstring block in lfg_service/identity.py.

import sqlite3

import lfg_service.identity as identity


def _fresh_db(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    monkeypatch.setattr(identity, "DATABASE", str(db))
    identity.ensure_identities_table()
    return str(db)


def test_wallet_links_table_created(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path, monkeypatch)
    conn = sqlite3.connect(db)
    try:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    assert "wallet_links" in names


def test_shared_wallet_same_bucket(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    identity.link("discord", "1", "bob", "rW1")
    identity.link("telegram", "9", "bob_tg", "rW1")
    b = identity.bucket_for("discord", "1")
    assert b is not None
    assert b["bucket_id"] == identity.bucket_for("telegram", "9")["bucket_id"]
    assert {(m["platform"], m["platform_user_id"]) for m in b["identities"]} == {
        ("discord", "1"),
        ("telegram", "9"),
    }
    assert b["wallets"] == ["rW1"]
    assert identity.same_bucket(("discord", "1"), ("telegram", "9"))


def test_transitive_linking_one_bucket(tmp_path, monkeypatch):
    # A—w1—B, B—w2—C  =>  A, B, C are one bucket.
    _fresh_db(tmp_path, monkeypatch)
    identity.link("discord", "A", "a", "rW1")
    identity.link("telegram", "B", "b", "rW1")
    identity.link("telegram", "B", "b", "rW2")  # B re-links a new wallet
    identity.link("web", "C", "c", "rW2")
    b = identity.bucket_for("discord", "A")
    assert b is not None
    assert len(b["identities"]) == 3
    assert b["wallets"] == ["rW1", "rW2"]
    assert identity.same_bucket(("discord", "A"), ("web", "C"))


def test_unlinked_identities_separate_buckets(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    identity.link("discord", "1", "bob", "rW1")
    identity.link("telegram", "2", "eve", "rW2")
    b1 = identity.bucket_for("discord", "1")
    b2 = identity.bucket_for("telegram", "2")
    assert b1["bucket_id"] != b2["bucket_id"]
    assert len(b1["identities"]) == 1
    assert not identity.same_bucket(("discord", "1"), ("telegram", "2"))


def test_append_only_history_survives_relink(tmp_path, monkeypatch):
    # identities.wallet is an upsert; wallet_links must keep the old wallet so
    # a later same-wallet link on another platform still merges the buckets.
    _fresh_db(tmp_path, monkeypatch)
    identity.link("discord", "1", "bob", "rOLD")
    identity.link("discord", "1", "bob", "rNEW")
    assert identity.resolve("discord", "1") == "rNEW"
    identity.link("telegram", "9", "bob_tg", "rOLD")  # links via the OLD wallet
    assert identity.same_bucket(("discord", "1"), ("telegram", "9"))
    b = identity.bucket_for("discord", "1")
    assert b["wallets"] == ["rNEW", "rOLD"]


def test_unknown_identity_returns_none(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    assert identity.bucket_for("discord", "nope") is None
    assert not identity.same_bucket(("discord", "nope"), ("discord", "nope"))


def test_bucket_id_deterministic(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    identity.link("telegram", "9", "b", "rW1")
    identity.link("discord", "1", "b", "rW1")
    # lexicographically smallest member key, regardless of query entry point;
    # JSON-encoded so separator characters in either field cannot collide
    assert identity.bucket_for("telegram", "9")["bucket_id"] == '["discord", "1"]'
    assert identity.bucket_for("discord", "1")["bucket_id"] == '["discord", "1"]'


def test_bucket_id_separator_collision(tmp_path, monkeypatch):
    # ("a:b", "c") and ("a", "b:c") must NOT share a bucket_id when they have
    # no shared wallet (a ":"-joined id would collide as "a:b:c").
    _fresh_db(tmp_path, monkeypatch)
    identity.link("a:b", "c", "x", "rW1")
    identity.link("a", "b:c", "y", "rW2")
    b1 = identity.bucket_for("a:b", "c")
    b2 = identity.bucket_for("a", "b:c")
    assert b1["bucket_id"] != b2["bucket_id"]
    assert not identity.same_bucket(("a:b", "c"), ("a", "b:c"))


def test_db_failure_fails_closed(tmp_path, monkeypatch):
    # A database failure must raise BucketLookupError — never read as
    # "unknown identity" / not-same-bucket, which would fail a gate open.
    _fresh_db(tmp_path, monkeypatch)
    identity.link("discord", "1", "bob", "rW1")
    monkeypatch.setattr(identity, "DATABASE", str(tmp_path))  # a directory: queries fail
    import pytest

    with pytest.raises(identity.BucketLookupError):
        identity.bucket_for("discord", "1")
    with pytest.raises(identity.BucketLookupError):
        identity.same_bucket(("discord", "1"), ("telegram", "9"))
    with pytest.raises(identity.BucketLookupError):
        identity.bucket_overlaps("discord", "1", identities={("x", "y")})


def test_seed_backfill_from_existing_identities(tmp_path, monkeypatch):
    # Identities that predate wallet_links get seeded on ensure_identities_table.
    db = tmp_path / "test.db"
    monkeypatch.setattr(identity, "DATABASE", str(db))
    identity.ensure_identities_table()
    conn = sqlite3.connect(str(db))
    conn.execute("DROP TABLE wallet_links")
    conn.execute(
        "INSERT INTO identities (platform, platform_user_id, platform_username, wallet) "
        "VALUES ('discord', '1', 'old', 'rW1')"
    )
    conn.commit()
    conn.close()
    identity.ensure_identities_table()  # recreates + seeds
    assert identity.bucket_for("discord", "1")["wallets"] == ["rW1"]


def test_bucket_overlaps_gating(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    identity.link("discord", "1", "bob", "rW1")
    identity.link("telegram", "9", "bob_tg", "rW1")
    identity.link("web", "2", "eve", "rW2")
    claimed_ids = {("discord", "1")}
    # Same human on another platform is denied under B2 semantics...
    assert identity.bucket_overlaps("telegram", "9", identities=claimed_ids)
    # ...a different human is not.
    assert not identity.bucket_overlaps("web", "2", identities=claimed_ids)
    # Wallet-set gating: any bucket wallet in the consumed set denies.
    assert identity.bucket_overlaps("telegram", "9", wallets={"rW1"})
    assert not identity.bucket_overlaps("web", "2", wallets={"rW1"})
    # Unknown identity only matches itself in the identity set (B1 fallback).
    assert identity.bucket_overlaps("x", "u", identities={("x", "u")})
    assert not identity.bucket_overlaps("x", "u", wallets={"rW1"})


def test_migrate_users_seeds_wallet_links(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path, monkeypatch)
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE Users (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "discord_id TEXT NOT NULL UNIQUE, discord_name TEXT NOT NULL, wallet TEXT NOT NULL)"
    )
    conn.execute("INSERT INTO Users (discord_id, discord_name, wallet) VALUES ('7', 'n', 'rW7')")
    conn.commit()
    conn.close()
    assert identity.migrate_users_to_identities() == 1
    assert identity.bucket_for("discord", "7")["wallets"] == ["rW7"]


def test_migrate_records_legacy_wallet_for_existing_identity(tmp_path, monkeypatch):
    # Pre-upgrade user: identities row already exists with a NEWER wallet than
    # the legacy Users row. The migration must still record the legacy wallet
    # into wallet_links (append-only, idempotent) even though the identity
    # upsert is skipped — otherwise the bucket graph loses the W1 linkage.
    db = _fresh_db(tmp_path, monkeypatch)
    identity.link("discord", "5", "bob", "rW2")  # current identity wallet
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE Users (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "discord_id TEXT NOT NULL UNIQUE, discord_name TEXT NOT NULL, wallet TEXT NOT NULL)"
    )
    conn.execute("INSERT INTO Users (discord_id, discord_name, wallet) VALUES ('5', 'bob', 'rW1')")
    conn.commit()
    conn.close()
    assert identity.migrate_users_to_identities() == 0  # no new identity row
    b = identity.bucket_for("discord", "5")
    assert b["wallets"] == ["rW1", "rW2"]
    # And the legacy wallet links buckets: another platform on rW1 merges.
    identity.link("telegram", "9", "bob_tg", "rW1")
    assert identity.same_bucket(("discord", "5"), ("telegram", "9"))
