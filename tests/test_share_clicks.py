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
