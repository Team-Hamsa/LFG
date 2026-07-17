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

from lfg_core import economy_flow, economy_store, layer_store, nft_index, trait_config  # noqa: E402
from lfg_core.nft_index import OnchainNft  # noqa: E402
from webapp import economy_api  # noqa: E402


class _PermissiveLayerStore:
    """Stub layer store whose resolve() always succeeds. Task 16's body-affinity
    gate (economy_api._require_body_affinity) calls swap_compose.resolve_layer,
    which needs a real layer file on disk to return non-None. Tests below that
    are about scheduling/connection-lifecycle plumbing (not about affinity
    itself) use synthetic closet assets like ("Head", "Halo") that don't
    correspond to any real art file, so they patch the store to this stub —
    mirrors the `lambda: object()` stub webapp/test_smoke.py uses for flows
    that don't care about layer resolution, except this one actually gets
    called and must resolve."""

    async def resolve(self, body: str, trait_type: str, value: str) -> str:
        return f"/fake/{body}/{trait_type}/{value}.png"


def _stub_permissive_layer_store(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(economy_api.layer_store, "get_layer_store", lambda: _PermissiveLayerStore())


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
    economy_store.set_closet_contents(conn, "rOwner", [("Head", "Halo", 2)], [42])
    return conn


def test_read_economy_state_shape():
    conn = _seed_conn()
    state = economy_api.read_economy_state(conn, "rOwner")
    assert state["characters"][0]["edition"] == 3537
    assert state["characters"][0]["attributes"][0]["value"] == "Crown"
    assert state["characters"][0]["image_url"] == "https://cdn.example/3537.png"
    assert state["characters"][0]["mutable"] is True
    assert state["closet"]["assets"][0] == {"slot": "Head", "value": "Halo", "count": 2}
    assert state["closet"]["bodies"] == [42]
    assert state["trait_order"][0] == "Background"
    assert "Body" not in state["slots"]


def test_read_economy_state_excludes_other_owners():
    conn = _seed_conn()
    state = economy_api.read_economy_state(conn, "rNobody")
    assert state["characters"] == []
    assert state["closet"]["assets"] == []
    assert state["closet"]["bodies"] == []


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
    _stub_permissive_layer_store(monkeypatch)

    captured = {}

    async def fake_run_equip(session, deps):
        captured["ran"] = True
        session.state = economy_flow.DONE
        session.displaced_value = "Crown"

    monkeypatch.setattr(economy_flow, "run_equip", fake_run_equip)
    # Stub the real deps builder so no XRPL/CDN is touched.
    from scripts import _economy_deps

    monkeypatch.setattr(_economy_deps, "build_economy_deps", lambda c, user_token=None: object())

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
    economy_store.set_closet_contents(tracked, "rOwner", [("Head", "Halo", 2)], [42])

    monkeypatch.setattr(economy_api, "open_conn", lambda: tracked)
    _stub_permissive_layer_store(monkeypatch)

    async def fake_run_equip(session, deps):
        session.state = economy_flow.DONE
        session.displaced_value = "Crown"

    monkeypatch.setattr(economy_flow, "run_equip", fake_run_equip)
    from scripts import _economy_deps

    monkeypatch.setattr(_economy_deps, "build_economy_deps", lambda c, user_token=None: object())

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


def test_run_and_close_marks_session_failed_on_runner_crash(monkeypatch):
    """Regression: if the runner raises unexpectedly the inner session must reach
    a terminal (FAILED) state so it is never left as a zombie RUNNING session."""

    class _TrackingConn(sqlite3.Connection):
        """sqlite3.Connection subclass that counts close() calls."""

        close_count: int

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.close_count = 0

        def close(self) -> None:  # type: ignore[override]
            self.close_count += 1
            super().close()

    # Build a seeded tracking connection equivalent to _seed_conn().
    tracked = sqlite3.connect(":memory:", factory=_TrackingConn)
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
    economy_store.set_closet_contents(tracked, "rOwner", [("Head", "Halo", 2)], [42])

    monkeypatch.setattr(economy_api, "open_conn", lambda: tracked)
    _stub_permissive_layer_store(monkeypatch)

    async def crashing_runner(session, deps):
        raise RuntimeError("simulated unexpected crash")

    monkeypatch.setattr(economy_flow, "run_equip", crashing_runner)
    from scripts import _economy_deps

    monkeypatch.setattr(_economy_deps, "build_economy_deps", lambda c, user_token=None: object())

    async def go():
        ws = await economy_api.start_equip("123", "rOwner", "A", "Head", "Halo")
        # Two ticks: first starts the task body, second runs the except/finally
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return ws

    ws = asyncio.get_event_loop().run_until_complete(go())
    # The inner session must be in a terminal FAILED state (not stuck RUNNING)
    assert ws.inner.state == economy_flow.FAILED
    assert "internal error" in (ws.inner.error or "")
    # The conn must be closed regardless of the crash
    assert tracked.close_count == 1


# --- Task 6: closet token status in read_economy_state ---


def test_economy_state_reports_closet_status_none():
    """read_economy_state includes a closet.token block: status='none' when no record."""
    conn = _seed_conn()  # no closet_tokens row
    state = economy_api.read_economy_state(conn, "rOwner")
    assert "token" in state["closet"], "closet block must include 'token' key"
    assert state["closet"]["token"]["status"] == "none"
    assert state["closet"]["token"]["nft_id"] is None


def test_economy_state_reports_closet_status_active():
    """read_economy_state includes closet.token.status='active' when a token row exists."""
    conn = _seed_conn()
    economy_store.set_closet_token(conn, "rOwner", "NFT_ABC", "deadbeef", status="active")
    state = economy_api.read_economy_state(conn, "rOwner")
    assert state["closet"]["token"]["status"] == "active"
    assert state["closet"]["token"]["nft_id"] == "NFT_ABC"


def test_economy_state_reports_closet_status_pending_accept():
    """read_economy_state reports pending_accept when the token is minted but not claimed."""
    conn = _seed_conn()
    economy_store.set_closet_token(
        conn, "rOwner", "NFT_XYZ", "cafebabe", status="pending_accept", offer_id="OFFER1"
    )
    state = economy_api.read_economy_state(conn, "rOwner")
    assert state["closet"]["token"]["status"] == "pending_accept"
    assert state["closet"]["token"]["nft_id"] == "NFT_XYZ"


# --- Task 6: harvest/assemble gated on active Closet ---


def test_start_harvest_rejects_without_active_closet(monkeypatch):
    """start_harvest raises EconomyError if the owner has no active Closet."""
    conn = _seed_conn()  # no closet_tokens row -> no active closet
    monkeypatch.setattr(economy_api, "open_conn", lambda: conn)

    async def go():
        with pytest.raises(economy_api.EconomyError, match="Closet"):
            await economy_api.start_harvest("123", "rOwner", "A")

    asyncio.get_event_loop().run_until_complete(go())


def test_start_assemble_rejects_without_active_closet(monkeypatch):
    """start_assemble raises EconomyError if the owner has no active Closet."""
    conn = _seed_conn()  # no closet_tokens row -> no active closet
    # Seed genesis so the edition lookup doesn't fail before the closet gate
    from lfg_core import trait_economy

    economy_store.freeze_genesis(
        conn,
        trait_economy.Genesis(
            trait_counts={("Head", "Crown"): 1},
            edition_bodies={3537: ("male", "male")},
        ),
        {},
    )
    monkeypatch.setattr(economy_api, "open_conn", lambda: conn)

    async def go():
        with pytest.raises(economy_api.EconomyError, match="Closet"):
            await economy_api.start_assemble("123", "rOwner", 3537, {"Head": "Crown"})

    asyncio.get_event_loop().run_until_complete(go())


# --- Task 6: start_closet returns session-like dict ---


def test_start_closet_returns_status_dict(monkeypatch):
    """start_closet returns a dict with status, nft_id, and accept link."""
    conn = _seed_conn()
    monkeypatch.setattr(economy_api, "open_conn", lambda: conn)

    async def fake_ensure_closet(conn, owner, **kw):
        from lfg_core.closet_token import ClosetRef

        return ClosetRef(
            nft_id="NFT_NEW",
            uri_hex="aabbcc",
            status="pending_accept",
            accept_payload={"xumm_url": "https://xaman/pay"},
        )

    import lfg_core.closet_token as ct

    monkeypatch.setattr(ct, "ensure_closet", fake_ensure_closet)

    async def go():
        return await economy_api.start_closet("123", "rOwner")

    result = asyncio.get_event_loop().run_until_complete(go())
    assert result["status"] == "pending_accept"
    assert result["nft_id"] == "NFT_NEW"
    assert result["accept"] == "https://xaman/pay"


# --- Task 8: trait_tokens in read_economy_state ---


def test_read_economy_state_includes_trait_tokens_filtered_to_wallet():
    """read_economy_state includes a trait_tokens list filtered to the requesting wallet."""
    conn = _seed_conn()
    # Seed two trait_tokens rows: one for rOwner, one for rOther
    economy_store.upsert_trait_token(conn, "TOK1", "rOwner", "Head", "Crown")
    economy_store.upsert_trait_token(conn, "TOK2", "rOther", "Eyes", "Shades")

    state = economy_api.read_economy_state(conn, "rOwner")

    assert "trait_tokens" in state, "read_economy_state must include trait_tokens key"
    tokens = state["trait_tokens"]
    assert len(tokens) == 1, f"expected 1 trait_token for rOwner, got {len(tokens)}"
    tok = tokens[0]
    assert tok["nft_id"] == "TOK1"
    assert tok["slot"] == "Head"
    assert tok["value"] == "Crown"


# --- Task 8: start_extract gated on active Closet ---


def test_start_extract_rejects_without_active_closet(monkeypatch):
    """start_extract raises EconomyError('Create and claim your Closet first.') when no active closet."""
    conn = _seed_conn()  # no closet_tokens row -> no active closet
    monkeypatch.setattr(economy_api, "open_conn", lambda: conn)

    async def go():
        with pytest.raises(economy_api.EconomyError, match="Closet"):
            await economy_api.start_extract("123", "rOwner", {"slot": "Head", "value": "Crown"})

    asyncio.get_event_loop().run_until_complete(go())


# --- Task 8: start_deposit gated on active Closet ---


def test_start_deposit_rejects_without_active_closet(monkeypatch):
    """start_deposit raises EconomyError('Create and claim your Closet first.') when no active closet."""
    conn = _seed_conn()  # no closet_tokens row -> no active closet
    monkeypatch.setattr(economy_api, "open_conn", lambda: conn)

    async def go():
        with pytest.raises(economy_api.EconomyError, match="Closet"):
            await economy_api.start_deposit("123", "rOwner", {"nft_id": "TOK1"})

    asyncio.get_event_loop().run_until_complete(go())


# --- Fix 1: body validation before open_conn (no connection leak on malformed body) ---


def test_start_extract_missing_field_does_not_leak_conn(monkeypatch):
    """start_extract must raise KeyError BEFORE opening a connection when body fields are missing."""
    open_count = {"n": 0}
    real_open_conn = economy_api.open_conn

    def counting_open_conn():
        open_count["n"] += 1
        return real_open_conn()

    monkeypatch.setattr(economy_api, "open_conn", counting_open_conn)

    async def go():
        with pytest.raises(KeyError):
            await economy_api.start_extract("123", "rOwner", {})  # missing slot + value

    asyncio.get_event_loop().run_until_complete(go())
    assert open_count["n"] == 0, (
        f"open_conn must NOT be called when body fields are missing, got {open_count['n']} calls"
    )


def test_start_deposit_missing_field_does_not_leak_conn(monkeypatch):
    """start_deposit must raise KeyError BEFORE opening a connection when body fields are missing."""
    open_count = {"n": 0}
    real_open_conn = economy_api.open_conn

    def counting_open_conn():
        open_count["n"] += 1
        return real_open_conn()

    monkeypatch.setattr(economy_api, "open_conn", counting_open_conn)

    async def go():
        with pytest.raises(KeyError):
            await economy_api.start_deposit("123", "rOwner", {})  # missing nft_id

    asyncio.get_event_loop().run_until_complete(go())
    assert open_count["n"] == 0, (
        f"open_conn must NOT be called when body fields are missing, got {open_count['n']} calls"
    )


def test_start_extract_pending_closet_is_gated(monkeypatch):
    """start_extract raises EconomyError when the Closet exists but is pending_accept (not active)."""
    conn = _seed_conn()
    # Insert a closet_tokens row with status='pending_accept' (not 'active')
    economy_store.set_closet_token(
        conn, "rOwner", "NFT_PENDING", "deadbeef", status="pending_accept"
    )
    monkeypatch.setattr(economy_api, "open_conn", lambda: conn)

    async def go():
        with pytest.raises(economy_api.EconomyError, match="Closet"):
            await economy_api.start_extract("123", "rOwner", {"slot": "Head", "value": "Crown"})

    asyncio.get_event_loop().run_until_complete(go())


def test_start_deposit_pending_closet_is_gated(monkeypatch):
    """start_deposit raises EconomyError when the Closet exists but is pending_accept (not active)."""
    conn = _seed_conn()
    # Insert a closet_tokens row with status='pending_accept' (not 'active')
    economy_store.set_closet_token(
        conn, "rOwner", "NFT_PENDING", "deadbeef", status="pending_accept"
    )
    monkeypatch.setattr(economy_api, "open_conn", lambda: conn)

    async def go():
        with pytest.raises(economy_api.EconomyError, match="Closet"):
            await economy_api.start_deposit("123", "rOwner", {"nft_id": "TOK1"})

    asyncio.get_event_loop().run_until_complete(go())


# --- Task 16 (#30): body-affinity gating on equip / assemble ---
#
# Deposit is intentionally NOT gated here: run_deposit (lfg_core/economy_flow.py)
# only ever reads nft_id/slot/value — it never resolves or checks a character's
# body, and the trait token being deposited isn't tied to any character. Gating
# it would be inventing a constraint the implementation doesn't have.

_BODY_GATE_CFG = """
version: 1
layers:
  - {name: Background, z: 10}
  - {name: Back, z: 20}
  - {name: Body, z: 30}
  - {name: Clothing, z: 40}
  - {name: Mouth, z: 50}
  - {name: Eyebrows, z: 60}
  - {name: Eyes, z: 70}
  - {name: Head, z: 80}
  - {name: Accessory, z: 90}
swap_matrix:
  pairs:
    - {bodies: [ape, skeleton], layers: [Head]}
affinity:
  Clothing:
    "Summer Dress": [female]
  Head:
    "Spikey Black": [skeleton]
"""


def _mk_body_gate_layers(tmp_path):
    for body, trait_type, values in [
        ("male", "Clothing", ["Hoodie"]),
        ("female", "Clothing", ["Hoodie", "Summer Dress"]),
        # Spikey Black exists ONLY in the skeleton dir; the ape+skeleton
        # matrix pair (Head) makes it resolvable on an ape through the
        # foreign branch of swap_compose.resolve_layer.
        ("skeleton", "Head", ["Spikey Black"]),
    ]:
        d = tmp_path / "layers" / body / trait_type
        d.mkdir(parents=True, exist_ok=True)
        for v in values:
            (d / f"{v}.png").write_bytes(b"x")
    return str(tmp_path / "layers")


@pytest.fixture
def body_gate_store(tmp_path):
    """Fixture trait_config ('Summer Dress' is female-only) + a matching
    local layer tree, mirroring tests/test_traits_affinity.py's pattern."""
    cfg_path = tmp_path / "trait_config.yaml"
    cfg_path.write_text(_BODY_GATE_CFG)
    trait_config.reset_config()
    trait_config.get_config(str(cfg_path))
    store = layer_store.LocalLayerStore(_mk_body_gate_layers(tmp_path))
    try:
        yield store
    finally:
        trait_config.reset_config()


def test_start_equip_rejects_incompatible_body_value(monkeypatch, body_gate_store):
    """A female-only Clothing value ('Summer Dress') cannot be equipped onto
    a male character, even though the Closet holds the asset: resolve_layer
    finds no male file and male<->female Clothing isn't matrix-permitted."""
    conn = _seed_conn()  # owner rOwner holds a male character (edition 3537, nft_id "A")
    economy_store.set_closet_contents(
        conn, "rOwner", [("Head", "Halo", 2), ("Clothing", "Summer Dress", 1)], [42]
    )
    monkeypatch.setattr(economy_api, "open_conn", lambda: conn)
    monkeypatch.setattr(economy_api.layer_store, "get_layer_store", lambda: body_gate_store)

    async def go():
        with pytest.raises(economy_api.EconomyError, match="does not fit"):
            await economy_api.start_equip("123", "rOwner", "A", "Clothing", "Summer Dress")

    asyncio.get_event_loop().run_until_complete(go())


def test_start_equip_compatible_value_still_starts(monkeypatch, body_gate_store):
    """A value that's both affinity-allowed and file-resolvable on the
    character's body must still schedule the equip session (no regression)."""
    conn = _seed_conn()
    economy_store.set_closet_contents(
        conn, "rOwner", [("Head", "Halo", 2), ("Clothing", "Hoodie", 1)], [42]
    )
    monkeypatch.setattr(economy_api, "open_conn", lambda: conn)
    monkeypatch.setattr(economy_api.layer_store, "get_layer_store", lambda: body_gate_store)

    captured = {}

    async def fake_run_equip(session, deps):
        captured["ran"] = True
        session.state = economy_flow.DONE

    monkeypatch.setattr(economy_flow, "run_equip", fake_run_equip)
    from scripts import _economy_deps

    monkeypatch.setattr(_economy_deps, "build_economy_deps", lambda c, user_token=None: object())

    async def go():
        ws = await economy_api.start_equip("123", "rOwner", "A", "Clothing", "Hoodie")
        await asyncio.sleep(0)
        return ws

    ws = asyncio.get_event_loop().run_until_complete(go())
    assert ws.kind == "equip"
    assert captured.get("ran") is True


def test_start_equip_accepts_matrix_permitted_foreign_value(monkeypatch, body_gate_store):
    """Spec §5 parity with the swap path: a value with foreign-only affinity
    ('Spikey Black' Head, skeleton-affinity, file only in the skeleton dir)
    that IS matrix-permitted (ape+skeleton pair covers Head) must be
    ACCEPTED on an ape character — exactly the placement a cross-body swap
    legally produces. A target-body value_allowed() term would wrongly
    reject this."""
    conn = nft_index.init_db(":memory:")
    economy_store.init_economy_schema(conn)
    nft_index.upsert(
        conn,
        OnchainNft(
            nft_id="APE1",
            nft_number=77,
            owner="rOwner",
            is_burned=False,
            mutable=True,
            uri_hex="",
            body="ape",
            attributes=[{"trait_type": "Head", "value": "None"}],
            image="",
            ledger_index=1,
        ),
    )
    economy_store.set_closet_contents(conn, "rOwner", [("Head", "Spikey Black", 1)], [])
    monkeypatch.setattr(economy_api, "open_conn", lambda: conn)
    monkeypatch.setattr(economy_api.layer_store, "get_layer_store", lambda: body_gate_store)

    captured = {}

    async def fake_run_equip(session, deps):
        captured["ran"] = True
        session.state = economy_flow.DONE

    monkeypatch.setattr(economy_flow, "run_equip", fake_run_equip)
    from scripts import _economy_deps

    monkeypatch.setattr(_economy_deps, "build_economy_deps", lambda c, user_token=None: object())

    async def go():
        ws = await economy_api.start_equip("123", "rOwner", "APE1", "Head", "Spikey Black")
        await asyncio.sleep(0)
        return ws

    ws = asyncio.get_event_loop().run_until_complete(go())
    assert ws.kind == "equip"
    assert captured.get("ran") is True


def test_start_assemble_rejects_incompatible_asset_in_set(monkeypatch, body_gate_store):
    """assemble must gate EVERY asset in the requested set: a female-only
    Clothing value in the chosen set for a male-bodied edition is rejected,
    even though every other slot in the set is the legal 'None' asset."""
    from lfg_core import trait_economy

    conn = nft_index.init_db(":memory:")
    economy_store.init_economy_schema(conn)
    edition = 99
    economy_store.freeze_genesis(
        conn,
        trait_economy.Genesis(trait_counts={}, edition_bodies={edition: ("male", "male")}),
        {},
    )
    economy_store.set_closet_token(conn, "rOwner", "NFT_CLOSET", "deadbeef", status="active")
    chosen = dict.fromkeys(trait_economy.NON_BODY_SLOTS, "None")
    chosen["Clothing"] = "Summer Dress"
    economy_store.set_closet_contents(
        conn, "rOwner", [(slot, value, 1) for slot, value in chosen.items()], [edition]
    )
    monkeypatch.setattr(economy_api, "open_conn", lambda: conn)
    monkeypatch.setattr(economy_api.layer_store, "get_layer_store", lambda: body_gate_store)

    async def go():
        with pytest.raises(economy_api.EconomyError, match="does not fit"):
            await economy_api.start_assemble("123", "rOwner", edition, chosen)

    asyncio.get_event_loop().run_until_complete(go())


# --- Fix (review, PR #125): conn must close on every reject between
# open_conn() and _schedule(), including the body-affinity gate ---


class _TrackingConn(sqlite3.Connection):
    """sqlite3.Connection subclass that counts close() calls."""

    close_count: int

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.close_count = 0

    def close(self) -> None:  # type: ignore[override]
        self.close_count += 1
        super().close()


def _tracked_char_conn():
    """A _TrackingConn seeded with the same schema/data as _seed_conn()."""
    tracked = sqlite3.connect(":memory:", factory=_TrackingConn)
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
    return tracked


def test_start_equip_closes_conn_on_body_affinity_reject(monkeypatch, body_gate_store):
    """Regression: _require_body_affinity rejecting AFTER open_conn() but
    BEFORE _schedule() hands the conn to _run_and_close() must still close
    the conn -- this leaked before the try/except wrap in start_equip."""
    tracked = _tracked_char_conn()
    economy_store.set_closet_contents(
        tracked, "rOwner", [("Head", "Halo", 2), ("Clothing", "Summer Dress", 1)], [42]
    )
    monkeypatch.setattr(economy_api, "open_conn", lambda: tracked)
    monkeypatch.setattr(economy_api.layer_store, "get_layer_store", lambda: body_gate_store)

    async def go():
        with pytest.raises(economy_api.EconomyError, match="does not fit"):
            await economy_api.start_equip("123", "rOwner", "A", "Clothing", "Summer Dress")

    asyncio.get_event_loop().run_until_complete(go())
    assert tracked.close_count == 1, (
        f"expected conn.close() called exactly once on reject, got {tracked.close_count}"
    )


def test_start_assemble_closes_conn_on_body_affinity_reject(monkeypatch, body_gate_store):
    """Regression: the per-slot _require_body_affinity loop rejecting an
    assemble request AFTER open_conn() but BEFORE _schedule() must still
    close the conn -- this leaked before the try/except wrap in
    start_assemble."""
    from lfg_core import trait_economy

    tracked = _tracked_char_conn()
    edition = 99
    economy_store.freeze_genesis(
        tracked,
        trait_economy.Genesis(trait_counts={}, edition_bodies={edition: ("male", "male")}),
        {},
    )
    economy_store.set_closet_token(tracked, "rOwner", "NFT_CLOSET", "deadbeef", status="active")
    chosen = dict.fromkeys(trait_economy.NON_BODY_SLOTS, "None")
    chosen["Clothing"] = "Summer Dress"
    economy_store.set_closet_contents(
        tracked, "rOwner", [(slot, value, 1) for slot, value in chosen.items()], [edition]
    )
    monkeypatch.setattr(economy_api, "open_conn", lambda: tracked)
    monkeypatch.setattr(economy_api.layer_store, "get_layer_store", lambda: body_gate_store)

    async def go():
        with pytest.raises(economy_api.EconomyError, match="does not fit"):
            await economy_api.start_assemble("123", "rOwner", edition, chosen)

    asyncio.get_event_loop().run_until_complete(go())
    assert tracked.close_count == 1, (
        f"expected conn.close() called exactly once on reject, got {tracked.close_count}"
    )


# --- Assemble prefill (server-side auto-fill for the Build panel's + tile) ---
# The client cannot know body affinity (that lives in trait_config +
# swap_compose.resolve_layer), so it asks the server which edition/asset set
# to propose instead of blindly picking the first asset per slot.


def _prefill_conn(edition_bodies, assets, bodies):
    from lfg_core import trait_economy

    conn = nft_index.init_db(":memory:")
    economy_store.init_economy_schema(conn)
    economy_store.freeze_genesis(
        conn,
        trait_economy.Genesis(trait_counts={}, edition_bodies=edition_bodies),
        {},
    )
    economy_store.set_closet_token(conn, "rOwner", "NFT_CLOSET", "deadbeef", status="active")
    economy_store.set_closet_contents(conn, "rOwner", assets, bodies)
    return conn


def test_assemble_prefill_skips_incompatible_asset(monkeypatch, body_gate_store):
    """Prefill must skip a female-only Clothing asset when filling a
    male-bodied edition and fall through to the next compatible asset --
    the exact bug behind 'Davy Jones Beard does not fit a skeleton body'."""
    from lfg_core import trait_economy

    edition = 99
    assets = [(s, "None", 1) for s in trait_economy.NON_BODY_SLOTS if s != "Clothing"]
    # Incompatible asset first: the naive "first asset with count>0" pick fails.
    assets += [("Clothing", "Summer Dress", 1), ("Clothing", "Hoodie", 1)]
    conn = _prefill_conn({edition: ("male", "male")}, assets, [edition])
    monkeypatch.setattr(economy_api.layer_store, "get_layer_store", lambda: body_gate_store)

    pre = asyncio.get_event_loop().run_until_complete(economy_api.assemble_prefill(conn, "rOwner"))
    assert pre["edition"] == edition
    assert pre["body"] == "male"
    assert pre["chosen"]["Clothing"] == "Hoodie"
    assert pre["missing"] == []
    # The proposed set must actually pass the commit-path gate.
    from lfg_core.trait_economy import can_assemble, effective_genesis

    genesis = effective_genesis(
        economy_store.read_genesis(conn), economy_store.read_supply_changes(conn)
    )
    owned = {(s, v): c for (o, s, v, c) in economy_store.read_closet_assets(conn) if o == "rOwner"}
    assert can_assemble(edition, pre["chosen"], {edition}, owned, set(), genesis).ok


def test_assemble_prefill_reports_missing_slot(monkeypatch, body_gate_store):
    """When no compatible asset exists for a slot, prefill reports the slot
    as missing instead of proposing a set the server would reject."""
    from lfg_core import trait_economy

    edition = 99
    assets = [(s, "None", 1) for s in trait_economy.NON_BODY_SLOTS if s != "Clothing"]
    assets += [("Clothing", "Summer Dress", 1)]  # female-only; male body can't use it
    conn = _prefill_conn({edition: ("male", "male")}, assets, [edition])
    monkeypatch.setattr(economy_api.layer_store, "get_layer_store", lambda: body_gate_store)

    pre = asyncio.get_event_loop().run_until_complete(economy_api.assemble_prefill(conn, "rOwner"))
    assert pre["missing"] == ["Clothing"]
    assert pre["body"] == "male"


def test_assemble_prefill_prefers_fully_fillable_body(monkeypatch, body_gate_store):
    """With two closet bodies, prefill returns the first edition whose set
    fills completely -- here the female body, whose dir holds Summer Dress --
    rather than stopping at the male body with a missing Clothing slot."""
    from lfg_core import trait_economy

    assets = [(s, "None", 1) for s in trait_economy.NON_BODY_SLOTS if s != "Clothing"]
    assets += [("Clothing", "Summer Dress", 1)]
    conn = _prefill_conn({99: ("male", "male"), 100: ("female", "female")}, assets, [99, 100])
    monkeypatch.setattr(economy_api.layer_store, "get_layer_store", lambda: body_gate_store)

    pre = asyncio.get_event_loop().run_until_complete(economy_api.assemble_prefill(conn, "rOwner"))
    assert pre["edition"] == 100
    assert pre["chosen"]["Clothing"] == "Summer Dress"
    assert pre["missing"] == []


def test_assemble_prefill_no_bodies_raises(monkeypatch, body_gate_store):
    conn = _prefill_conn({}, [("Head", "Halo", 1)], [])
    monkeypatch.setattr(economy_api.layer_store, "get_layer_store", lambda: body_gate_store)

    async def go():
        with pytest.raises(economy_api.EconomyError, match="[Nn]o bodies"):
            await economy_api.assemble_prefill(conn, "rOwner")

    asyncio.get_event_loop().run_until_complete(go())
