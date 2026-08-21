import sqlite3

from lfg_core import share_clicks


def test_record_click_inserts_row(tmp_path):
    db = str(tmp_path / "app.db")
    ok = share_clicks.record_click(db, 42, "rrrrrrrrrrrrrrrrrrrrrhoLvTp", False, "Mozilla/5.0")
    assert ok is True
    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT nft_number, ref_wallet, is_bot, user_agent FROM share_clicks"
    ).fetchone()
    conn.close()
    assert row == (42, "rrrrrrrrrrrrrrrrrrrrrhoLvTp", 0, "Mozilla/5.0")


def test_record_click_null_ref_and_bot_flag(tmp_path):
    db = str(tmp_path / "app.db")
    assert share_clicks.record_click(db, 7, None, True, "Twitterbot/1.0") is True
    conn = sqlite3.connect(db)
    row = conn.execute("SELECT ref_wallet, is_bot FROM share_clicks").fetchone()
    conn.close()
    assert row == (None, 1)


def test_record_click_truncates_user_agent(tmp_path):
    db = str(tmp_path / "app.db")
    share_clicks.record_click(db, 1, None, False, "x" * 1000)
    conn = sqlite3.connect(db)
    (ua,) = conn.execute("SELECT user_agent FROM share_clicks").fetchone()
    conn.close()
    assert len(ua) == 256


def test_record_click_swallows_db_failure(tmp_path):
    # Unwritable path: a directory where the file should be.
    bad = str(tmp_path / "adir")
    import os

    os.mkdir(bad)
    assert share_clicks.record_click(bad, 1, None, False, "ua") is False


def test_record_click_stamps_clicked_at(tmp_path):
    db = str(tmp_path / "app.db")
    share_clicks.record_click(db, 1, None, False, "ua")
    conn = sqlite3.connect(db)
    (ts,) = conn.execute("SELECT clicked_at FROM share_clicks").fetchone()
    conn.close()
    assert ts  # non-empty ISO timestamp


# --- share_intents: the "Share on X" button beacon ---------------------------


def test_record_intent_inserts_row(tmp_path):
    db = str(tmp_path / "app.db")
    ok = share_clicks.record_intent(db, "rrrrrrrrrrrrrrrrrrrrrhoLvTp", "mint", 42, "discord")
    assert ok is True
    conn = sqlite3.connect(db)
    row = conn.execute("SELECT wallet, kind, nft_number, platform FROM share_intents").fetchone()
    assert conn.execute("SELECT clicked_at FROM share_intents").fetchone()[0]
    conn.close()
    assert row == ("rrrrrrrrrrrrrrrrrrrrrhoLvTp", "mint", 42, "discord")


def test_record_intent_null_nft_number(tmp_path):
    db = str(tmp_path / "app.db")
    assert share_clicks.record_intent(db, "rW", "swap", None, "web") is True
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT nft_number FROM share_intents").fetchone() == (None,)
    conn.close()


def test_record_intent_swallows_db_failure(tmp_path):
    import os

    bad = str(tmp_path / "adir")
    os.mkdir(bad)
    assert share_clicks.record_intent(bad, "rW", "mint", 1, "discord") is False


def test_record_intent_dedups_repeat_press_in_window(tmp_path):
    db = str(tmp_path / "app.db")
    assert share_clicks.record_intent(db, "rA", "mint", 1, "discord") is True
    assert share_clicks.record_intent(db, "rA", "mint", 1, "discord") is True  # repeat: ok, no row
    share_clicks.record_intent(db, "rA", "mint", 2, "discord")  # different nft: new row
    share_clicks.record_intent(db, "rA", "swap", None, "discord")
    share_clicks.record_intent(db, "rA", "swap", None, "discord")  # NULL nft dedups too
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM share_intents").fetchone() == (3,)
    conn.close()


def test_record_click_does_not_create_intents_table(tmp_path):
    # Greptile P2 on #420: the card-page path must not pay the intents DDL.
    db = str(tmp_path / "app.db")
    share_clicks.record_click(db, 1, None, False, "ua")
    conn = sqlite3.connect(db)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert "share_clicks" in names and "share_intents" not in names


def test_intent_rows_since_groups_by_wallet(tmp_path):
    db = str(tmp_path / "app.db")
    share_clicks.record_intent(db, "rA", "mint", 1, "discord")
    share_clicks.record_intent(db, "rA", "swap", 2, "discord")
    share_clicks.record_intent(db, "rB", "mint", 3, "web")
    rows = share_clicks.intent_rows_since(db, "2000-01-01T00:00:00Z")
    assert [(r["wallet"], r["shares"]) for r in rows] == [("rA", 2), ("rB", 1)]
    assert share_clicks.intent_rows_since(db, "2999-01-01T00:00:00Z") == []
