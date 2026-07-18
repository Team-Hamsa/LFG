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


def test_nft_by_number_returns_live_token(tmp_path):
    conn = nft_index.init_db(str(tmp_path / "x.db"))
    nft_index.upsert(conn, _nft("LIVE", number=42))
    rec = nft_index.nft_by_number(conn, 42)
    assert rec is not None
    assert rec.nft_id == "LIVE"


def test_nft_by_number_returns_none_for_unknown_number(tmp_path):
    conn = nft_index.init_db(str(tmp_path / "x.db"))
    nft_index.upsert(conn, _nft("LIVE", number=42))
    assert nft_index.nft_by_number(conn, 9999) is None


def test_nft_by_number_excludes_burned(tmp_path):
    # A Harvest burn (dress-up economy) or any other burn leaves the token
    # is_burned=1 in the index; nft_by_number must treat that as "not live"
    # even though the row still exists (#41 OG-card liveness check).
    conn = nft_index.init_db(str(tmp_path / "x.db"))
    nft_index.upsert(conn, _nft("DEAD", number=7, burned=True))
    assert nft_index.nft_by_number(conn, 7) is None


def test_nft_by_number_multi_live_picks_highest_ledger_index(tmp_path):
    # Trait-swap/reminting duplicates can leave >1 live token at one edition
    # number (a data anomaly, see collection_anomalies()'s multi_live) —
    # nft_by_number must deterministically pick the most-recently-synced one.
    conn = nft_index.init_db(str(tmp_path / "x.db"))
    older = _nft("OLDER", number=5)
    older.ledger_index = 100
    newer = _nft("NEWER", number=5)
    newer.ledger_index = 200
    nft_index.upsert(conn, older)
    nft_index.upsert(conn, newer)
    rec = nft_index.nft_by_number(conn, 5)
    assert rec is not None
    assert rec.nft_id == "NEWER"


def test_metadata_urls_skips_ipfs_entirely():
    # IPFS fetches are banned: gateway flakiness fed the []-clobber cycle
    # (unreadable-live 1 -> 483 over a month of re-runs). Bithomp CSV import
    # is the mainnet metadata source for ipfs:// tokens.
    uri_hex = b"ipfs://bafyCID/meta.json".hex()
    assert nft_index._metadata_urls(uri_hex) == []


def test_upsert_empty_attributes_never_clobber_nonempty(tmp_path):
    conn = nft_index.init_db(str(tmp_path / "x.db"))
    attrs = [{"trait_type": "Clothing", "value": "Wonder"}]
    nft_index.upsert(conn, _nft("AAA", owner="rOld", attrs=attrs))
    # A re-scan whose metadata fetch failed: empty attributes, no body/image.
    failed = _nft("AAA", owner="rNew", attrs=[], body="")
    failed.image = ""
    nft_index.upsert(conn, failed)
    row = conn.execute(
        "SELECT owner, attributes_json, body, image FROM onchain_nfts WHERE nft_id='AAA'"
    ).fetchone()
    assert row[0] == "rNew"  # ledger facts still update
    assert row[1] != "[]"  # metadata survives the failed fetch
    assert row[2] == "male"
    assert row[3] == "https://img/x.png"


def test_upsert_nonempty_attributes_still_overwrite(tmp_path):
    conn = nft_index.init_db(str(tmp_path / "x.db"))
    nft_index.upsert(conn, _nft("AAA", attrs=[{"trait_type": "Head", "value": "Old"}]))
    new_attrs = [{"trait_type": "Head", "value": "New"}]
    nft_index.upsert(conn, _nft("AAA", attrs=new_attrs))
    live = nft_index.live_nfts(conn)
    assert live[0].attributes == new_attrs


def test_upsert_empty_attributes_fill_empty_row(tmp_path):
    conn = nft_index.init_db(str(tmp_path / "x.db"))
    nft_index.upsert(conn, _nft("AAA", attrs=[], body=""))
    attrs = [{"trait_type": "Head", "value": "New"}]
    nft_index.upsert(conn, _nft("AAA", attrs=attrs))
    live = nft_index.live_nfts(conn)
    assert live[0].attributes == attrs
    assert live[0].body == "male"


def test_metadata_urls_passes_through_http():
    uri_hex = b"https://lfgo.b-cdn.net/x.json".hex()
    assert nft_index._metadata_urls(uri_hex) == ["https://lfgo.b-cdn.net/x.json"]


def test_retryable_unreadable_query(tmp_path):
    conn = nft_index.init_db(str(tmp_path / "x.db"))
    # empty attributes + uri -> retryable; empty attrs + no uri -> not; has attrs -> not
    nft_index.upsert(conn, _nft("RETRY", attrs=[], body=""))
    nft_index.upsert(conn, _nft("NOURI", attrs=[], body=""))
    conn.execute("UPDATE onchain_nfts SET uri_hex='' WHERE nft_id='NOURI'")
    nft_index.upsert(conn, _nft("OK", attrs=[{"trait_type": "Body", "value": "Straight"}]))
    conn.commit()
    ids = [n.nft_id for n in nft_index.retryable_unreadable(conn)]
    assert ids == ["RETRY"]


def test_collection_anomalies():
    recs = [
        _nft("A", number=1),
        _nft("B", number=2),
        _nft("C", number=2),  # edition 2 has two live tokens
        _nft("D", number=9),  # out of range (max=5)
        _nft("E", number=None),  # unparsed name
    ]
    a = nft_index.collection_anomalies(recs, max_edition=5)
    assert a["missing"] == [3, 4, 5]  # 1,2 present; 3,4,5 absent
    assert a["multi_live"] == {2: 2}
    assert a["out_of_range"] == ["D"]
    assert a["unparsed"] == ["E"]


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


# --- enumerate_tokens fails closed on an error response (#190) ---


def test_enumerate_tokens_raises_on_error_response(monkeypatch):
    import asyncio

    class _Resp:
        def __init__(self, ok, result):
            self._ok = ok
            self.result = result

        def is_successful(self):
            return self._ok

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def request(self, req):
            # An error response mid-enumeration must abort, not truncate to
            # end-of-list (empty nfts + no marker) and silently return [].
            return _Resp(False, {"error": "backendOverloaded"})

    monkeypatch.setattr(nft_index, "AsyncWebsocketClient", lambda url: _FakeClient())

    async def _go():
        return await nft_index.enumerate_tokens("wss://x", "rIssuer", 1763)

    import pytest

    with pytest.raises(RuntimeError, match="nfts_by_issuer failed"):
        asyncio.new_event_loop().run_until_complete(_go())


def test_upsert_never_resurrects_burned_token(tmp_path):
    # XRPL burns are irreversible. A stale source (e.g. a Bithomp CSV exported
    # before the burn) re-upserting the token as live must not flip is_burned
    # back to 0 — that resurrected 18 burned mainnet tokens on 2026-07-15 and
    # made every burn-reminted edition show as a duplicate.
    conn = nft_index.init_db(str(tmp_path / "x.db"))
    nft_index.upsert(conn, _nft("AAA", burned=True))
    nft_index.upsert(conn, _nft("AAA", burned=False))  # stale re-import
    row = conn.execute("SELECT is_burned FROM onchain_nfts WHERE nft_id='AAA'").fetchone()
    assert row[0] == 1
