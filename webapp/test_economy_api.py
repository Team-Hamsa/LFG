import asyncio
import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("XUMM_API_KEY", "test")
os.environ.setdefault("XUMM_API_SECRET", "test")
os.environ.setdefault("SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2")
os.environ.setdefault("TOKEN_ISSUER_ADDRESS", "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
os.environ.setdefault("TOKEN_CURRENCY_HEX", "4C46474F00000000000000000000000000000000")
os.environ.setdefault("BUNNY_CDN_ACCESS_KEY", "test")
os.environ.setdefault("BUNNY_CDN_STORAGE_ZONE", "test")
os.environ.setdefault("LAYER_SOURCE", "local")
os.environ.setdefault("BUNNY_PULL_ZONE", "nft.pullzone.example")

from lfg_core import economy_flow, economy_store, nft_index  # noqa: E402
from lfg_core.nft_index import OnchainNft  # noqa: E402
from webapp import economy_api  # noqa: E402


def _char():
    return OnchainNft(
        nft_id="A",
        nft_number=1,
        owner="rOwner",
        is_burned=False,
        mutable=True,
        uri_hex="",
        body="male",
        attributes=[],
        image="",
        ledger_index=1,
    )


def _seed_conn():
    conn = nft_index.init_db(":memory:")
    economy_store.init_economy_schema(conn)
    nft_index.upsert(
        conn,
        OnchainNft(
            nft_id="A",
            nft_number=3537,
            owner="rOwner",
            is_burned=False,
            mutable=True,
            uri_hex="",
            body="male",
            attributes=[{"trait_type": "Head", "value": "Crown"}],
            image="https://cdn.example/3537.png",
            ledger_index=1,
        ),
    )
    economy_store.set_bucket_contents(conn, "rOwner", [("Head", "Halo", 2)], [42])
    return conn


def test_read_economy_state_shape():
    conn = _seed_conn()
    state = economy_api.read_economy_state(conn, "rOwner")
    assert state["characters"][0]["edition"] == 3537
    assert state["characters"][0]["attributes"][0]["value"] == "Crown"
    assert state["characters"][0]["image_url"] == "https://cdn.example/3537.png"
    assert state["characters"][0]["mutable"] is True
    assert state["bucket"]["assets"][0] == {"slot": "Head", "value": "Halo", "count": 2}
    assert state["bucket"]["bodies"] == [42]
    assert state["trait_order"][0] == "Background"
    assert "Body" not in state["slots"]


def test_read_economy_state_excludes_other_owners():
    conn = _seed_conn()
    state = economy_api.read_economy_state(conn, "rNobody")
    assert state["characters"] == []
    assert state["bucket"]["assets"] == []
    assert state["bucket"]["bodies"] == []


def test_equip_session_dict():
    s = economy_flow.EquipSession(
        owner="rOwner", character=_char(), slot="Head", incoming_value="Halo"
    )
    s.state = economy_flow.DONE
    s.displaced_value = "Crown"
    d = economy_api.economy_session_dict("equip", s)
    assert d["state"] == "done" and d["displaced"] == "Crown" and d["error"] is None


def test_assemble_session_dict_surfaces_accept_link():
    s = economy_flow.AssembleSession(
        owner="rOwner", edition=42, chosen={}, body_value="male", body_class="male"
    )
    s.results = [
        {
            "nft_id": "N",
            "image_url": "img",
            "metadata_url": "m",
            "accept": {"xumm_url": "https://xaman/abc"},
        }
    ]
    d = economy_api.economy_session_dict("assemble", s)
    assert d["accept"] == "https://xaman/abc" and d["nft_id"] == "N"


def test_web_session_delegates():
    s = economy_flow.EquipSession(
        owner="rOwner", character=_char(), slot="Head", incoming_value="Halo"
    )
    ws = economy_api.EconomyWebSession(discord_id="123", kind="equip", inner=s)
    assert ws.state == economy_flow.RUNNING
    assert ws.id == s.id
    assert ws.to_dict()["state"] == economy_flow.RUNNING
    assert isinstance(ws.created_at, float)


def test_start_equip_precheck_rejects_unowned(monkeypatch):
    conn = _seed_conn()  # owner rOwner holds edition 3537 (nft_id "A"), Bucket has Head=Halo
    monkeypatch.setattr(economy_api, "open_conn", lambda: conn)

    async def go():
        with pytest.raises(economy_api.EconomyError):
            # nft_id "A" is owned by rOwner, not rNobody -> precheck fails
            await economy_api.start_equip("123", "rNobody", "A", "Head", "Halo")

    asyncio.get_event_loop().run_until_complete(go())


def test_start_equip_happy_returns_session(monkeypatch):
    conn = _seed_conn()
    monkeypatch.setattr(economy_api, "open_conn", lambda: conn)

    captured = {}

    async def fake_run_equip(session, deps):
        captured["ran"] = True
        session.state = economy_flow.DONE
        session.displaced_value = "Crown"

    monkeypatch.setattr(economy_flow, "run_equip", fake_run_equip)
    # Stub the real deps builder so no XRPL/CDN is touched.
    from scripts import _economy_deps

    monkeypatch.setattr(_economy_deps, "build_economy_deps", lambda c: object())

    async def go():
        ws = await economy_api.start_equip("123", "rOwner", "A", "Head", "Halo")
        # give the scheduled task a tick to run
        await asyncio.sleep(0)
        return ws

    ws = asyncio.get_event_loop().run_until_complete(go())
    assert ws.kind == "equip" and ws.discord_id == "123"
    assert captured.get("ran") is True


def test_start_equip_closes_conn_after_task(monkeypatch):
    """Regression: the sqlite conn opened for the scheduled flow must be closed
    after the background task completes (no file-descriptor leak)."""

    class _TrackingConn(sqlite3.Connection):
        """sqlite3.Connection subclass that counts close() calls."""

        close_count: int

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.close_count = 0

        def close(self) -> None:  # type: ignore[override]
            self.close_count += 1
            super().close()

    # Build a _TrackingConn seeded with the same schema/data as _seed_conn().
    tracked = sqlite3.connect(":memory:", factory=_TrackingConn)
    # Replay what _seed_conn does, but on our tracking conn.
    tracked.executescript("""
        CREATE TABLE IF NOT EXISTS onchain_nfts (
            nft_id TEXT PRIMARY KEY,
            nft_number INTEGER,
            owner TEXT,
            is_burned INTEGER DEFAULT 0,
            mutable INTEGER,
            uri_hex TEXT,
            body TEXT,
            attributes_json TEXT,
            image TEXT,
            ledger_index INTEGER
        );
    """)
    economy_store.init_economy_schema(tracked)
    tracked.execute(
        "INSERT INTO onchain_nfts VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            "A",
            3537,
            "rOwner",
            0,
            1,
            "",
            "male",
            json.dumps([{"trait_type": "Head", "value": "Crown"}]),
            "https://cdn.example/3537.png",
            1,
        ),
    )
    tracked.commit()
    economy_store.set_bucket_contents(tracked, "rOwner", [("Head", "Halo", 2)], [42])

    monkeypatch.setattr(economy_api, "open_conn", lambda: tracked)

    async def fake_run_equip(session, deps):
        session.state = economy_flow.DONE
        session.displaced_value = "Crown"

    monkeypatch.setattr(economy_flow, "run_equip", fake_run_equip)
    from scripts import _economy_deps

    monkeypatch.setattr(_economy_deps, "build_economy_deps", lambda c: object())

    async def go():
        await economy_api.start_equip("123", "rOwner", "A", "Head", "Halo")
        # One sleep(0) tick lets the create_task coroutine begin; a second tick
        # ensures the finally-block runs before we assert.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.get_event_loop().run_until_complete(go())
    assert tracked.close_count == 1, (
        f"expected conn.close() called exactly once, got {tracked.close_count}"
    )
