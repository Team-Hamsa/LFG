"""Share-link mint attribution (#273): ref validation, referrer recording,
bulk-job threading, and the conversion metrics query."""

import os
import sqlite3

from lfg_core import bulk_mint_flow, share_clicks
from lfg_core.db_helpers import record_nft_mint
from lfg_service.app import _mint_referrer

SHARER = "rHb9CJAWyB4rj91VRWn96DkukG4bwdtyTh"  # valid classic address
MINTER = "rN7n7otQDd6FczFgLdSqtcsAUxDkw6fzRH"  # valid, distinct


# --- _mint_referrer validation -------------------------------------------


def test_valid_ref_accepted():
    assert _mint_referrer({"ref": SHARER}, MINTER) == SHARER


def test_ref_whitespace_stripped():
    assert _mint_referrer({"ref": f"  {SHARER} "}, MINTER) == SHARER


def test_invalid_address_dropped():
    assert _mint_referrer({"ref": "not-an-address"}, MINTER) is None


def test_self_referral_rejected():
    assert _mint_referrer({"ref": MINTER}, MINTER) is None


def test_missing_and_non_string_refs_dropped():
    assert _mint_referrer({}, MINTER) is None
    assert _mint_referrer({"ref": None}, MINTER) is None
    assert _mint_referrer({"ref": 42}, MINTER) is None
    assert _mint_referrer({"ref": ""}, MINTER) is None
    assert _mint_referrer("junk", MINTER) is None


# --- record_nft_mint referrer column -------------------------------------


def _mint_row(db_file, nft_number, referrer=None):
    assert record_nft_mint(
        nft_number=nft_number,
        nft_id=f"ID{nft_number}",
        discord_id="u1",
        owner_address=MINTER,
        metadata_url="https://cdn/x.json",
        image_url="https://cdn/x.png",
        traits={},
        network="testnet",
        body_type="male",
        db_path=db_file,
        referrer=referrer,
    )


def test_referrer_recorded_and_null_without(tmp_path):
    db = str(tmp_path / "app.db")
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE LFG (nft_number INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()

    _mint_row(db, 1, referrer=SHARER)
    _mint_row(db, 2, referrer=None)

    conn = sqlite3.connect(db)
    rows = dict(conn.execute("SELECT nft_number, referrer FROM LFG").fetchall())
    conn.close()
    assert rows == {1: SHARER, 2: None}


def test_referrer_column_self_migrates(tmp_path):
    """A pre-#273 LFG table (no referrer column) gains it on the next mint."""
    db = str(tmp_path / "app.db")
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE LFG (nft_number INTEGER PRIMARY KEY, nft_id TEXT)")
    conn.commit()
    conn.close()

    _mint_row(db, 5, referrer=SHARER)
    conn = sqlite3.connect(db)
    cols = {c[1] for c in conn.execute("PRAGMA table_info(LFG)")}
    val = conn.execute("SELECT referrer FROM LFG WHERE nft_number=5").fetchone()[0]
    conn.close()
    assert "referrer" in cols
    assert val == SHARER


# --- bulk job carries referrer -------------------------------------------


def test_bulk_job_referrer_survives_serialize_roundtrip():
    job = bulk_mint_flow.BulkMintJob("u1", MINTER, 3, referrer=SHARER)
    restored = bulk_mint_flow.BulkMintJob.from_serialized(job.serialize())
    assert restored.referrer == SHARER


def test_bulk_job_referrer_defaults_none():
    job = bulk_mint_flow.BulkMintJob("u1", MINTER, 1)
    assert job.referrer is None
    # legacy on-disk records (pre-#273) have no key -> None, not KeyError
    d = job.serialize()
    del d["referrer"]
    assert bulk_mint_flow.BulkMintJob.from_serialized(d).referrer is None


# --- conversion metrics query --------------------------------------------


def test_conversion_rows_shape(tmp_path):
    db = str(tmp_path / "app.db")
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE LFG (nft_number INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()
    # clicks: 2 human + 1 bot for SHARER; mints: 1 attributed to SHARER,
    # 1 to MINTER (no clicks logged for MINTER).
    share_clicks.record_click(db, 1, SHARER, False, "ua")
    share_clicks.record_click(db, 1, SHARER, False, "ua")
    share_clicks.record_click(db, 1, SHARER, True, "twitterbot")
    _mint_row(db, 10, referrer=SHARER)
    _mint_row(db, 11, referrer=MINTER)
    _mint_row(db, 12, referrer=None)

    rows = share_clicks.conversion_rows(db, "testnet")
    # deterministic ordering: mints desc, then clicks desc; bot clicks and
    # unattributed mints excluded
    assert rows == [
        {"wallet": SHARER, "clicks": 2, "mints": 1},
        {"wallet": MINTER, "clicks": 0, "mints": 1},
    ]


def test_conversion_rows_empty_db(tmp_path):
    db = str(tmp_path / "empty.db")
    assert share_clicks.conversion_rows(db, "testnet") == []


def test_conversion_rows_wrong_network_excluded(tmp_path):
    db = str(tmp_path / "app.db")
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE LFG (nft_number INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()
    _mint_row(db, 20, referrer=SHARER)  # network="testnet"
    rows = share_clicks.conversion_rows(db, "mainnet")
    assert rows == []


# --- client stash window (static assertions, no JS runtime — same style as
# --- the other webapp/client tests) ---------------------------------------


def _app_js() -> str:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "webapp", "client", "app.js")) as f:
        return f.read()


def test_client_ref_stash_has_attribution_window():
    """Greptile P1s (PR #393): the stash must carry a timestamp, expire on a
    TTL, and be consumed only on an observed MINTED outcome — a failed/
    cancelled/timed-out attempt keeps the click's attribution for the retry,
    while a recorded mint stops later mints re-attributing the same click."""
    src = _app_js()
    assert "REF_TTL_MS" in src
    assert "JSON.stringify({ ref: refParam, ts: Date.now() })" in src
    assert "function consumeRef()" in src
    # consumed on the minted signal of BOTH poll paths (single + bulk),
    # never at mint start (early consume loses attribution on failure)
    assert src.count("consumeRef(); // one attribution per click: mint record written") == 2
    assert "consumeRef(); // one attribution per click: clear on successful start" not in src
    # legacy plain-string stashes (no ts) are treated as expired, not eternal
    assert "typeof ts !== 'number'" in src
