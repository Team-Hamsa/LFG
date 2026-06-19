# Tests for lfg_core/nft_index.py (per-nft_id on-chain index).
import os
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


def _nft(nft_id, number=1, owner="rOwner", burned=False, attrs=None, body="male"):
    return nft_index.OnchainNft(
        nft_id=nft_id,
        nft_number=number,
        owner=owner,
        is_burned=burned,
        mutable=True,
        uri_hex="6868",
        body=body,
        attributes=attrs if attrs is not None else [{"trait_type": "Body", "value": "Straight"}],
        image="https://img/x.png",
        ledger_index=100,
    )


def test_index_db_path_per_network(monkeypatch):
    monkeypatch.delenv("ONCHAIN_DB_PATH", raising=False)
    assert nft_index.index_db_path("testnet").endswith("onchain_testnet.db")
    assert nft_index.index_db_path("mainnet").endswith("onchain_mainnet.db")
    monkeypatch.setenv("ONCHAIN_DB_PATH", "/tmp/custom.db")
    assert nft_index.index_db_path("testnet") == "/tmp/custom.db"


def test_init_db_creates_table(tmp_path):
    conn = nft_index.init_db(str(tmp_path / "x.db"))
    cols = {r[1] for r in conn.execute("PRAGMA table_info(onchain_nfts)")}
    assert {
        "nft_id",
        "nft_number",
        "owner",
        "is_burned",
        "mutable",
        "uri_hex",
        "body",
        "attributes_json",
        "image",
        "ledger_index",
        "last_synced_at",
    } <= cols


def test_upsert_inserts_then_updates(tmp_path):
    conn = nft_index.init_db(str(tmp_path / "x.db"))
    nft_index.upsert(conn, _nft("AAA", owner="rOld"))
    nft_index.upsert(conn, _nft("AAA", owner="rNew"))  # same id -> update, not a new row
    rows = conn.execute("SELECT owner FROM onchain_nfts WHERE nft_id='AAA'").fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "rNew"


def test_live_nfts_excludes_burned_and_roundtrips_attributes(tmp_path):
    conn = nft_index.init_db(str(tmp_path / "x.db"))
    attrs = [{"trait_type": "Clothing", "value": "Wonder"}]
    nft_index.upsert(conn, _nft("LIVE", attrs=attrs))
    nft_index.upsert(conn, _nft("DEAD", burned=True))
    live = nft_index.live_nfts(conn)
    assert [n.nft_id for n in live] == ["LIVE"]
    assert live[0].attributes == attrs


def test_token_record_with_metadata():
    token = {"nft_id": "AAA", "owner": "rO", "is_burned": False, "flags": 0x10, "uri_hex": "6868"}
    meta = {
        "name": "Let's Effing Go! #3547",
        "image": "https://img/x.png",
        "attributes": [
            {"trait_type": "Body", "value": "Curved Green"},
            {"trait_type": "Clothing", "value": "Wonder"},
        ],
    }
    rec = nft_index.token_record(token, meta)
    assert rec.nft_id == "AAA"
    assert rec.nft_number == 3547
    assert rec.body == "female"  # Curved -> female
    assert rec.mutable is True  # flag 0x10 set
    assert {"trait_type": "Clothing", "value": "Wonder"} in rec.attributes


def test_token_record_without_metadata_is_recorded_not_dropped():
    token = {"nft_id": "BBB", "owner": "rO", "is_burned": True, "flags": 0, "uri_hex": "6868"}
    rec = nft_index.token_record(token, None)
    assert rec.nft_id == "BBB"
    assert rec.attributes == []
    assert rec.body == ""
    assert rec.is_burned is True
    assert rec.mutable is False
