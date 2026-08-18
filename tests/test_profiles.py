# #207: user profiles — first-class entity above per-platform identities.
# Foundation only: nothing live reads profiles yet. Fail-closed like #206
# buckets (ProfileError / BucketLookupError on infra failure).

import sqlite3

import pytest

import lfg_service.identity as identity


def _fresh_db(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    monkeypatch.setattr(identity, "DATABASE", str(db))
    identity.ensure_identities_table()
    return str(db)


def test_profiles_table_created(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path, monkeypatch)
    conn = sqlite3.connect(db)
    try:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    assert "profiles" in names


def test_account_id_migrated_onto_old_table_shape(tmp_path, monkeypatch):
    # A pre-#207 identities table without account_id gains the column and
    # existing rows survive with account_id NULL.
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE identities ("
        "platform TEXT NOT NULL, platform_user_id TEXT NOT NULL, "
        "platform_username TEXT, wallet TEXT NOT NULL, "
        "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, "
        "PRIMARY KEY (platform, platform_user_id))"
    )
    conn.execute(
        "INSERT INTO identities (platform, platform_user_id, platform_username, wallet) "
        "VALUES ('discord', '1', 'bob', 'rW1')"
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(identity, "DATABASE", str(db))
    identity.ensure_identities_table()
    conn = sqlite3.connect(db)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(identities)")}
        assert "account_id" in cols
        row = conn.execute(
            "SELECT account_id FROM identities WHERE platform='discord' AND platform_user_id='1'"
        ).fetchone()
        assert row == (None,)
    finally:
        conn.close()
    # and the migrated row can now be given a profile
    p = identity.ensure_profile_for("discord", "1")
    assert p["id"] >= 1


def test_create_on_first_ensure_and_idempotent(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    identity.link("discord", "1", "bob", "rW1")
    p1 = identity.ensure_profile_for("discord", "1")
    assert p1["display_name"] == "bob"
    assert p1["preferences"] == {}
    p2 = identity.ensure_profile_for("discord", "1")
    assert p2["id"] == p1["id"]


def test_unknown_identity_raises(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    with pytest.raises(identity.ProfileError):
        identity.ensure_profile_for("discord", "nope")


def test_attach_to_existing_via_bucket(tmp_path, monkeypatch):
    # Same wallet on two platforms => one bucket => second ensure attaches to
    # the first identity's profile instead of creating a new one.
    _fresh_db(tmp_path, monkeypatch)
    identity.link("discord", "1", "bob", "rW1")
    p1 = identity.ensure_profile_for("discord", "1")
    identity.link("telegram", "9", "bob_tg", "rW1")
    p2 = identity.ensure_profile_for("telegram", "9")
    assert p2["id"] == p1["id"]
    view = identity.profile_for("telegram", "9")
    assert {(m["platform"], m["platform_user_id"]) for m in view["identities"]} == {
        ("discord", "1"),
        ("telegram", "9"),
    }


def test_merge_on_new_link(tmp_path, monkeypatch):
    # Two identities create separate profiles, then a shared wallet joins
    # their buckets: merge is deterministic (older/smaller id wins),
    # idempotent, and moves the loser's identities.
    _fresh_db(tmp_path, monkeypatch)
    identity.link("discord", "1", "bob", "rW1")
    identity.link("telegram", "9", "eve", "rW2")
    pa = identity.ensure_profile_for("discord", "1")
    pb = identity.ensure_profile_for("telegram", "9")
    assert pa["id"] != pb["id"]
    identity.link("telegram", "9", "eve", "rW1")  # joins the buckets
    report = identity.merge_profiles(pb["id"], pa["id"])  # arg order irrelevant
    assert report["winner"] == min(pa["id"], pb["id"])
    assert report["loser"] == max(pa["id"], pb["id"])
    assert report["moved_identities"] == 1
    # winner keeps its fields; the differing loser display_name is reported
    assert report["conflicts"]["display_name"] == {"kept": "bob", "discarded": "eve"}
    # identities moved
    view = identity.profile_for("telegram", "9")
    assert view["profile"]["id"] == report["winner"]
    assert view["profile"]["display_name"] == "bob"
    assert len(view["identities"]) == 2
    # idempotent: re-merge is a no-op
    again = identity.merge_profiles(pa["id"], pb["id"])
    assert again["winner"] == report["winner"]
    assert again["loser"] is None
    assert again["moved_identities"] == 0


def test_ensure_merges_multi_profile_bucket(tmp_path, monkeypatch):
    # A third identity ensuring into a bucket that spans two profiles first
    # unifies them, then attaches to the surviving (older) profile.
    _fresh_db(tmp_path, monkeypatch)
    identity.link("discord", "1", "bob", "rW1")
    identity.link("telegram", "9", "eve", "rW2")
    pa = identity.ensure_profile_for("discord", "1")
    pb = identity.ensure_profile_for("telegram", "9")
    identity.link("web", "rW3", "w", "rW1")
    identity.link("web", "rW3", "w", "rW2")  # web bridges both buckets
    p = identity.ensure_profile_for("web", "rW3")
    assert p["id"] == min(pa["id"], pb["id"])
    view = identity.profile_for("discord", "1")
    assert len(view["identities"]) == 3


def test_lookup_shape_and_wallets(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    identity.link("discord", "1", "bob", "rW1")
    identity.link("discord", "1", "bob", "rW2")  # re-link keeps history
    identity.ensure_profile_for("discord", "1")
    view = identity.profile_for("discord", "1")
    assert set(view) == {"profile", "identities", "wallets"}
    assert set(view["profile"]) == {
        "id",
        "display_name",
        "avatar_url",
        "preferences",
        "created_at",
    }
    assert view["wallets"] == ["rW1", "rW2"]
    # no-profile identity: None, never auto-created
    identity.link("telegram", "9", "eve", "rW9")
    assert identity.profile_for("telegram", "9") is None


def test_db_error_fails_closed(tmp_path, monkeypatch):
    # Point DATABASE at a directory so every query fails: profile ops must
    # raise, never silently return "no profile" / create one.
    monkeypatch.setattr(identity, "DATABASE", str(tmp_path))
    with pytest.raises((identity.ProfileError, identity.BucketLookupError)):
        identity.ensure_profile_for("discord", "1")
    with pytest.raises((identity.ProfileError, identity.BucketLookupError)):
        identity.profile_for("discord", "1")
    with pytest.raises(identity.ProfileError):
        identity.merge_profiles(1, 2)
