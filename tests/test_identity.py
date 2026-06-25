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
