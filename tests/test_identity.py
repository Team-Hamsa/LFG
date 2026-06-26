import sqlite3

import lfg_service.identity as identity


def _fresh_db(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    monkeypatch.setattr(identity, "DATABASE", str(db))
    return str(db)


def test_link_and_resolve(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    identity.ensure_identities_table()
    assert identity.resolve("telegram", "999") is None
    assert identity.link("telegram", "999", "alice", "rWALLET1") is True
    assert identity.resolve("telegram", "999") == "rWALLET1"


def test_link_upserts_wallet(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    identity.ensure_identities_table()
    identity.link("discord", "1", "bob", "rOLD")
    identity.link("discord", "1", "bob", "rNEW")
    assert identity.resolve("discord", "1") == "rNEW"


def test_same_user_id_different_platforms_are_distinct(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    identity.ensure_identities_table()
    identity.link("discord", "1", "bob", "rDISCORD")
    identity.link("telegram", "1", "bob", "rTELEGRAM")
    assert identity.resolve("discord", "1") == "rDISCORD"
    assert identity.resolve("telegram", "1") == "rTELEGRAM"


def _columns(db):
    conn = sqlite3.connect(db)
    try:
        return {r[1] for r in conn.execute("PRAGMA table_info(identities)")}
    finally:
        conn.close()


def test_ensure_adds_display_handle_column(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path, monkeypatch)
    # old-shape identities table (no display_handle / updated_at)
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE identities ("
        "platform TEXT NOT NULL, platform_user_id TEXT NOT NULL, platform_username TEXT, "
        "wallet TEXT NOT NULL, account_id INTEGER, "
        "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, "
        "PRIMARY KEY (platform, platform_user_id))"
    )
    conn.execute(
        "INSERT INTO identities (platform, platform_user_id, platform_username, wallet) "
        "VALUES ('discord', '1', 'oldname', 'rW')"
    )
    conn.commit()
    conn.close()

    identity.ensure_identities_table()

    cols = _columns(db)
    assert "display_handle" in cols
    assert "updated_at" in cols
    # existing row backfilled from platform_username
    conn = sqlite3.connect(db)
    try:
        row = conn.execute(
            "SELECT display_handle FROM identities WHERE platform='discord' AND platform_user_id='1'"
        ).fetchone()
    finally:
        conn.close()
    assert row[0] == "oldname"


def test_ensure_creates_wallet_index(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path, monkeypatch)
    identity.ensure_identities_table()
    conn = sqlite3.connect(db)
    try:
        indexes = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    finally:
        conn.close()
    assert "idx_identities_wallet" in indexes


def test_ensure_is_idempotent_twice(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path, monkeypatch)
    identity.ensure_identities_table()
    identity.ensure_identities_table()  # must not raise or duplicate
    cols = _columns(db)
    # exactly one of each new column
    assert sum(1 for c in cols if c == "display_handle") == 1
    assert sum(1 for c in cols if c == "updated_at") == 1


def _row(db, platform, uid):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(
            "SELECT display_handle, updated_at FROM identities "
            "WHERE platform=? AND platform_user_id=?",
            (platform, uid),
        ).fetchone()
    finally:
        conn.close()


def test_link_sets_display_handle_default(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path, monkeypatch)
    identity.ensure_identities_table()
    identity.link("discord", "1", "alice", "rW")
    handle, updated = _row(db, "discord", "1")
    assert handle == "alice"
    assert updated is not None


def test_link_explicit_display_handle(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path, monkeypatch)
    identity.ensure_identities_table()
    identity.link("discord", "1", "alice#123", "rW", display_handle="Alice")
    handle, _ = _row(db, "discord", "1")
    assert handle == "Alice"


def test_touch_handle_updates(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path, monkeypatch)
    identity.ensure_identities_table()
    identity.link("discord", "1", "alice", "rW")
    identity.touch_handle("discord", "1", "alice_renamed")
    handle, updated = _row(db, "discord", "1")
    assert handle == "alice_renamed"
    assert updated is not None
    # no-op on a missing row (must not raise)
    identity.touch_handle("discord", "nonexistent", "ghost")


def test_identities_for_wallet_returns_all_linked(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    identity.ensure_identities_table()
    identity.link("discord", "1", "alice", "rW")
    identity.link("telegram", "2", "alice_tg", "rW")
    identity.link("discord", "3", "bob", "rOTHER")

    rows = identity.identities_for_wallet("rW")
    assert {(r["platform"], r["platform_user_id"]) for r in rows} == {
        ("discord", "1"),
        ("telegram", "2"),
    }
    by_platform = {r["platform"]: r for r in rows}
    assert by_platform["discord"]["display_handle"] == "alice"
    assert by_platform["telegram"]["display_handle"] == "alice_tg"


def test_identities_for_wallet_empty(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    identity.ensure_identities_table()
    assert identity.identities_for_wallet("rUNKNOWN") == []


def test_identities_for_wallet_is_case_sensitive(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    identity.ensure_identities_table()
    identity.link("discord", "1", "alice", "rW")
    # rW and rw are distinct wallets — never case-folded
    assert len(identity.identities_for_wallet("rW")) == 1
    assert identity.identities_for_wallet("rw") == []


def test_migrate_users_is_idempotent(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path, monkeypatch)
    # seed a legacy Users table
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE Users (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "discord_id TEXT NOT NULL UNIQUE, discord_name TEXT NOT NULL, wallet TEXT NOT NULL)"
    )
    conn.execute("INSERT INTO Users (discord_id, discord_name, wallet) VALUES ('7','carol','rC')")
    conn.commit()
    conn.close()
    identity.ensure_identities_table()
    assert identity.migrate_users_to_identities() == 1
    assert identity.resolve("discord", "7") == "rC"
    # second run migrates nothing new and does not error
    assert identity.migrate_users_to_identities() == 0
    assert identity.resolve("discord", "7") == "rC"
