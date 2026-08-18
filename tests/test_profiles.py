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


def test_ensure_surfaces_merge_conflicts(tmp_path, monkeypatch):
    # Unifying a multi-profile bucket must not discard conflict info: the
    # reports come back on the ensure result (and are logged at WARNING).
    _fresh_db(tmp_path, monkeypatch)
    identity.link("discord", "1", "bob", "rW1")
    identity.link("telegram", "9", "eve", "rW2")
    pa = identity.ensure_profile_for("discord", "1")
    pb = identity.ensure_profile_for("telegram", "9")
    assert pa["merge_reports"] == []
    identity.link("web", "rW3", "w", "rW1")
    identity.link("web", "rW3", "w", "rW2")  # bridges both buckets
    p = identity.ensure_profile_for("web", "rW3")
    assert len(p["merge_reports"]) == 1
    report = p["merge_reports"][0]
    assert report["winner"] == min(pa["id"], pb["id"])
    assert report["loser"] == max(pa["id"], pb["id"])
    assert report["conflicts"]["display_name"] == {"kept": "bob", "discarded": "eve"}


def test_profiled_identity_joining_profiled_bucket_reconciles(tmp_path, monkeypatch):
    # An ALREADY-profiled identity whose bucket gained a second profile via a
    # new wallet link is reconciled on ensure (no early-return split bucket).
    _fresh_db(tmp_path, monkeypatch)
    identity.link("discord", "1", "bob", "rW1")
    identity.link("telegram", "9", "eve", "rW2")
    pa = identity.ensure_profile_for("discord", "1")
    pb = identity.ensure_profile_for("telegram", "9")
    identity.link("discord", "1", "bob", "rW2")  # joins the buckets
    p = identity.ensure_profile_for("discord", "1")  # caller already profiled
    winner = min(pa["id"], pb["id"])
    assert p["id"] == winner
    assert len(p["merge_reports"]) == 1
    # both identities now resolve to the single surviving profile
    assert identity.profile_for("discord", "1")["profile"]["id"] == winner
    assert identity.profile_for("telegram", "9")["profile"]["id"] == winner
    assert len(identity.profile_for("telegram", "9")["identities"]) == 2


def test_double_ensure_converges_single_profile(tmp_path, monkeypatch):
    # Find-or-create is serialized (BEGIN IMMEDIATE): overlapping ensures for
    # two members of one profile-less bucket converge on ONE profile. True
    # concurrency is untestable deterministically; assert the convergence
    # property the transaction guarantees — the later ensure re-reads the
    # bucket inside the write lock and attaches instead of inserting, leaving
    # exactly one profile row.
    db = _fresh_db(tmp_path, monkeypatch)
    identity.link("discord", "1", "bob", "rW1")
    identity.link("telegram", "9", "bob_tg", "rW1")  # same bucket
    p1 = identity.ensure_profile_for("discord", "1")
    p2 = identity.ensure_profile_for("telegram", "9")
    p3 = identity.ensure_profile_for("discord", "1")
    assert p1["id"] == p2["id"] == p3["id"]
    conn = sqlite3.connect(db)
    try:
        live = conn.execute("SELECT COUNT(*) FROM profiles WHERE merged_into IS NULL").fetchone()[0]
        total = conn.execute("SELECT COUNT(*) FROM profiles").fetchone()[0]
    finally:
        conn.close()
    assert live == 1
    assert total == 1  # attach, not insert-then-merge


def test_ensure_blocks_on_concurrent_writer(tmp_path, monkeypatch):
    # The write lock is real: with another connection holding BEGIN IMMEDIATE,
    # ensure cannot sneak its read-decide-write through — it fails closed
    # (ProfileError on lock timeout) rather than double-inserting.
    db = _fresh_db(tmp_path, monkeypatch)
    identity.link("discord", "1", "bob", "rW1")
    blocker = sqlite3.connect(db, isolation_level=None)
    try:
        blocker.execute("BEGIN IMMEDIATE")
        real_connect = sqlite3.connect
        monkeypatch.setattr(
            identity.sqlite3,
            "connect",
            lambda *a, **k: real_connect(*a, timeout=0.1, **k),
        )
        with pytest.raises(identity.ProfileError):
            identity.ensure_profile_for("discord", "1")
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
    # and once the lock is released, ensure succeeds normally
    p = identity.ensure_profile_for("discord", "1")
    assert p["id"] >= 1


def test_bucket_snapshot_inside_lock_sees_concurrent_link(tmp_path, monkeypatch):
    # TOCTOU regression: the bucket snapshot is taken INSIDE the write lock.
    # A wallet link committed while ensure waits for the lock must be seen —
    # ensure merges the newly-joined profiles instead of operating on a stale
    # pre-lock bucket.
    import threading

    db = _fresh_db(tmp_path, monkeypatch)
    identity.link("discord", "1", "bob", "rW1")
    identity.link("telegram", "9", "eve", "rW2")
    pa = identity.ensure_profile_for("discord", "1")
    pb = identity.ensure_profile_for("telegram", "9")
    blocker = sqlite3.connect(db, isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")
    # the joining link commits only when the blocker releases the lock —
    # i.e. strictly after ensure has started but before it can proceed
    blocker.execute(
        "INSERT OR IGNORE INTO wallet_links (platform, platform_user_id, wallet) "
        "VALUES ('discord', '1', 'rW2')"
    )
    result: dict[str, dict] = {}
    t = threading.Thread(
        target=lambda: result.update(p=identity.ensure_profile_for("discord", "1"))
    )
    t.start()
    t.join(timeout=0.5)  # ensure is parked on BEGIN IMMEDIATE
    assert t.is_alive(), "ensure should be blocked on the write lock"
    blocker.execute("COMMIT")
    blocker.close()
    t.join(timeout=10)
    assert not t.is_alive()
    p = result["p"]
    winner = min(pa["id"], pb["id"])
    assert p["id"] == winner  # the just-committed link was seen and merged
    assert len(p["merge_reports"]) == 1
    assert identity.profile_for("telegram", "9")["profile"]["id"] == winner
