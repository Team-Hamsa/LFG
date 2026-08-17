# tests/test_nft_index_video.py
# #204 residual: the onchain_nfts index must carry the metadata `video` URL so
# roster records synthesized on a metadata-cache miss (lfg_service.app
# _index_roster) keep their animation instead of silently degrading to the
# static PNG thumbnail. Additive column; self-migrating for existing DBs.
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DISCORD_BOT_TOKEN", "x")
os.environ.setdefault("XUMM_API_KEY", "x")
os.environ.setdefault("XUMM_API_SECRET", "x")
os.environ.setdefault("BUNNY_CDN_ACCESS_KEY", "x")
os.environ.setdefault("BUNNY_CDN_STORAGE_ZONE", "x")
os.environ.setdefault("SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2")  # dummy testnet seed
os.environ.setdefault("TOKEN_ISSUER_ADDRESS", "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
os.environ.setdefault("TOKEN_CURRENCY_HEX", "4C46474F00000000000000000000000000000000")
os.environ.setdefault("XRPL_NETWORK", "testnet")
os.environ.setdefault("BUNNY_PULL_ZONE", "nft.pullzone.example")

from lfg_core import nft_index  # noqa: E402

_MP4 = "https://nft.pullzone.example/nfts/12_3.mp4"


def _nft(nft_id, video="", attrs=None, image="https://img/x.png"):
    return nft_index.OnchainNft(
        nft_id=nft_id,
        nft_number=12,
        owner="rOwner",
        is_burned=False,
        mutable=True,
        uri_hex="6868",
        body="milady",
        attributes=(
            attrs if attrs is not None else [{"trait_type": "Body", "value": "Irridescent"}]
        ),
        image=image,
        ledger_index=100,
        video=video,
    )


def test_video_round_trips_through_upsert(tmp_path):
    conn = nft_index.init_db(str(tmp_path / "x.db"))
    nft_index.upsert(conn, _nft("A" * 64, video=_MP4))
    rec = nft_index.nft_by_id(conn, "A" * 64)
    assert rec is not None and rec.video == _MP4


def test_video_defaults_empty(tmp_path):
    conn = nft_index.init_db(str(tmp_path / "x.db"))
    nft_index.upsert(conn, _nft("B" * 64))
    rec = nft_index.nft_by_id(conn, "B" * 64)
    assert rec is not None and rec.video == ""


def test_init_db_migrates_legacy_schema_without_video_column(tmp_path):
    """A pre-#204 DB (no video column) must be migrated in place, not crash."""
    path = str(tmp_path / "legacy.db")
    legacy = sqlite3.connect(path)
    legacy.execute(
        "CREATE TABLE onchain_nfts ("
        " nft_id TEXT PRIMARY KEY, nft_number INTEGER, owner TEXT,"
        " is_burned INTEGER DEFAULT 0, mutable INTEGER, uri_hex TEXT,"
        " body TEXT, attributes_json TEXT, image TEXT,"
        " ledger_index INTEGER, last_synced_at TIMESTAMP)"
    )
    legacy.execute(
        "INSERT INTO onchain_nfts (nft_id, nft_number, owner, attributes_json, image)"
        " VALUES (?, 7, 'rOld', '[]', 'https://img/7.png')",
        ("C" * 64,),
    )
    legacy.commit()
    legacy.close()
    conn = nft_index.init_db(path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(onchain_nfts)")}
    assert "video" in cols
    rec = nft_index.nft_by_id(conn, "C" * 64)
    assert rec is not None and rec.video == ""
    nft_index.upsert(conn, _nft("C" * 64, video=_MP4))
    rec = nft_index.nft_by_id(conn, "C" * 64)
    assert rec is not None and rec.video == _MP4


def test_failed_metadata_fetch_never_clobbers_video(tmp_path):
    """Empty attributes mean 'metadata fetch failed' — video must survive a
    []-write the same way body/image/attributes do."""
    conn = nft_index.init_db(str(tmp_path / "x.db"))
    nft_index.upsert(conn, _nft("D" * 64, video=_MP4))
    nft_index.upsert(conn, _nft("D" * 64, video="", attrs=[], image=""))
    rec = nft_index.nft_by_id(conn, "D" * 64)
    assert rec is not None and rec.video == _MP4


def test_good_metadata_without_video_clears_stale_video(tmp_path):
    """A modify to all-static art writes real attributes and no video — the
    stale animation URL must not stick to the row."""
    conn = nft_index.init_db(str(tmp_path / "x.db"))
    nft_index.upsert(conn, _nft("E" * 64, video=_MP4))
    nft_index.upsert(conn, _nft("E" * 64, video=""))
    rec = nft_index.nft_by_id(conn, "E" * 64)
    assert rec is not None and rec.video == ""


def test_token_record_derives_video_from_metadata():
    token = {"nft_id": "F" * 64, "owner": "rX", "flags": 16, "uri_hex": "6868"}
    meta = {
        "name": "LFGO #12",
        "image": "https://img/12.png",
        "video": _MP4,
        "attributes": [{"trait_type": "Body", "value": "Irridescent"}],
    }
    assert nft_index.token_record(token, meta).video == _MP4
    assert nft_index.token_record(token, {**meta, "video": None}).video == ""
    del meta["video"]
    assert nft_index.token_record(token, meta).video == ""
    assert nft_index.token_record(token, None).video == ""


def test_migration_race_duplicate_column_is_tolerated(tmp_path, monkeypatch):
    """Two processes racing the check-then-ALTER on a legacy DB: the loser's
    duplicate-column error must not break initialization."""
    path = str(tmp_path / "race.db")
    nft_index.init_db(path).close()  # column now exists

    class RacedConn:
        """Proxy that hides `video` from PRAGMA table_info (stale view), so
        init_db takes the ALTER branch against a DB that already has it."""

        def __init__(self, real):
            self._real = real

        def execute(self, sql, *a):
            cur = self._real.execute(sql, *a)
            if sql.startswith("PRAGMA table_info"):
                rows = [r for r in cur.fetchall() if r[1] != "video"]
                return iter(rows)
            return cur

        def __getattr__(self, name):
            return getattr(self._real, name)

    real_connect = nft_index.sqlite3.connect
    monkeypatch.setattr(
        nft_index.sqlite3, "connect", lambda p, *a, **k: RacedConn(real_connect(p, *a, **k))
    )
    conn = nft_index.init_db(path)  # would raise "duplicate column" unguarded
    cols = {r[1] for r in conn._real.execute("PRAGMA table_info(onchain_nfts)")}
    assert "video" in cols
    conn._real.close()
