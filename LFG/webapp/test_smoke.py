# Smoke tests for the Activity webapp: module imports, route registration,
# session tokens, wallet validation, and the mint session state machine with
# XRPL/XUMM stubbed out. Run from repo root: python -m pytest webapp/test_smoke.py

import os
import sys
import asyncio

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Provide dummy env so lfg_core.config import doesn't fail in CI/dev shells
os.environ.setdefault("XUMM_API_KEY", "test")
os.environ.setdefault("XUMM_API_SECRET", "test")
os.environ.setdefault("SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2")  # throwaway test seed
os.environ.setdefault("TOKEN_ISSUER_ADDRESS", "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
os.environ.setdefault("TOKEN_CURRENCY_HEX", "4C46474F00000000000000000000000000000000")
os.environ.setdefault("BUNNY_CDN_ACCESS_KEY", "test")
os.environ.setdefault("BUNNY_CDN_STORAGE_ZONE", "test")
os.environ.setdefault("LAYER_SOURCE", "local")

from lfg_core import mint_flow, xumm_ops, xrpl_ops, traits, swap_meta, swap_flow, layer_store  # noqa: E402
from webapp import server  # noqa: E402
import user_db  # noqa: E402


def test_routes_registered():
    app = server.create_app()
    paths = {getattr(r.resource, 'canonical', '') for r in app.router.routes()}
    for expected in ["/api/config", "/api/token", "/api/me", "/api/register",
                     "/api/trustline", "/api/mint", "/api/mint/{session_id}",
                     "/api/nfts", "/api/swap", "/api/swap/{session_id}",
                     "/api/qr.png", "/"]:
        assert expected in paths, f"missing route {expected}"


def test_session_token_roundtrip():
    token = server.make_session_token({"id": "123", "name": "josh"})
    payload = server.verify_session_token(token)
    assert payload["id"] == "123"
    assert payload["name"] == "josh"


def test_session_token_tamper_rejected():
    token = server.make_session_token({"id": "123", "name": "josh"})
    assert server.verify_session_token(token[:-2] + "ff") is None
    assert server.verify_session_token("garbage") is None


def test_payment_link_is_xaman_detect():
    link = xumm_ops.generate_static_payment_link("rrrrrrrrrrrrrrrrrrrrrhoLvTp")
    assert link.startswith("https://xaman.app/detect/")


def test_qr_png():
    png = xumm_ops.generate_qr_png("https://example.com")
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_register_user_upserts_wallet(tmp_path, monkeypatch):
    monkeypatch.setattr(user_db, "DATABASE", str(tmp_path / "test.db"))
    user_db.create_users_table()
    assert user_db.register_user("42", "josh", "rOldWallet")
    assert user_db.register_user("42", "josh", "rNewWallet")  # change wallet
    assert user_db.get_user("42")["address"] == "rNewWallet"


def test_success_states_are_terminal():
    # Non-terminal success states would 409-block users forever
    assert mint_flow.OFFER_READY in mint_flow.TERMINAL_STATES
    assert swap_flow.OFFERS_READY in swap_flow.TERMINAL_STATES


# --- Payment watching (rippled API v1 + v2 message shapes) ---

V1_STREAM_MSG = {
    "type": "transaction", "validated": True,
    "transaction": {
        "TransactionType": "Payment", "Account": "rSender", "Destination": "rDest",
        "Amount": {"currency": "4C46474F00000000000000000000000000000000",
                   "issuer": "rIssuer", "value": "1"},
        "hash": "H1",
    },
    "meta": {"delivered_amount": {
        "currency": "4C46474F00000000000000000000000000000000",
        "issuer": "rIssuer", "value": "1"}},
}

V2_STREAM_MSG = {
    "type": "transaction", "validated": True,
    "tx_json": {
        "TransactionType": "Payment", "Account": "rSender", "Destination": "rDest",
        "DeliverMax": {"currency": "4C46474F00000000000000000000000000000000",
                       "issuer": "rIssuer", "value": "1"},
        "hash": "H2",
    },
    "meta": {"delivered_amount": {
        "currency": "4C46474F00000000000000000000000000000000",
        "issuer": "rIssuer", "value": "1"}},
}

CUR = "4C46474F00000000000000000000000000000000"


def _matches(msg):
    tx, meta = xrpl_ops._extract_tx_and_meta(msg)
    return tx is not None and xrpl_ops._payment_matches(
        tx, meta, "rDest", "rSender", "1", CUR, "rIssuer")


def test_payment_matches_api_v1_and_v2_shapes():
    assert _matches(V1_STREAM_MSG)
    assert _matches(V2_STREAM_MSG)  # current xrpl-py subscribes with api_version 2


def test_payment_match_rejects_wrong_sender_and_partial():
    import copy
    wrong_sender = copy.deepcopy(V2_STREAM_MSG)
    wrong_sender["tx_json"]["Account"] = "rSomeoneElse"
    assert not _matches(wrong_sender)

    # Partial payment: DeliverMax says 1 but only 0.1 was delivered
    partial = copy.deepcopy(V2_STREAM_MSG)
    partial["meta"]["delivered_amount"]["value"] = "0.1"
    assert not _matches(partial)

    xrp_payment = copy.deepcopy(V1_STREAM_MSG)
    xrp_payment["transaction"]["Amount"] = "1000000"  # XRP drops, not LFGO
    del xrp_payment["meta"]
    assert not _matches(xrp_payment)


def test_wait_for_payment_times_out_with_no_traffic(monkeypatch):
    """The old code only checked the timeout when a message arrived, hanging
    forever on a quiet account."""
    class FakeWS:
        def __init__(self, url):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def send(self, req):
            pass

        async def request(self, req):
            class R:
                result = {"transactions": []}
            return R()

        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.Event().wait()  # silent account: never a message

    monkeypatch.setattr(xrpl_ops, "AsyncWebsocketClient", FakeWS)
    paid = asyncio.get_event_loop().run_until_complete(
        xrpl_ops.wait_for_payment("rDest", "rSender", timeout_seconds=1))
    assert paid is False


def test_wait_for_payment_backfills_missed_payment(monkeypatch):
    """A payment validated before the subscription went live must be found
    via account_tx — but only if it is newer than not_before."""
    import copy
    entry = copy.deepcopy(V2_STREAM_MSG)
    entry["tx_json"]["date"] = 800000000  # ripple epoch seconds

    class FakeWS:
        def __init__(self, url):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def send(self, req):
            pass

        async def request(self, req):
            class R:
                result = {"transactions": [entry]}
            return R()

        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.Event().wait()

    monkeypatch.setattr(xrpl_ops, "AsyncWebsocketClient", FakeWS)
    monkeypatch.setattr(xrpl_ops.config, "TOKEN_CURRENCY_HEX", CUR)
    monkeypatch.setattr(xrpl_ops.config, "TOKEN_ISSUER_ADDRESS", "rIssuer")
    loop = asyncio.get_event_loop()
    tx_unix = 800000000 + xrpl_ops.RIPPLE_EPOCH_OFFSET

    paid = loop.run_until_complete(xrpl_ops.wait_for_payment(
        "rDest", "rSender", timeout_seconds=1, not_before=tx_unix - 60))
    assert paid is True

    # The same payment is too old for a session created after it -> no replay
    paid = loop.run_until_complete(xrpl_ops.wait_for_payment(
        "rDest", "rSender", timeout_seconds=1, not_before=tx_unix + 60))
    assert paid is False


def test_mint_session_payment_timeout(monkeypatch):
    async def no_payment(**kwargs):
        return False
    monkeypatch.setattr(mint_flow.xrpl_ops, "wait_for_payment", no_payment)

    session = mint_flow.MintSession(discord_id="1", wallet_address="rTest")
    asyncio.get_event_loop().run_until_complete(mint_flow.run_mint_session(session))
    assert session.state == mint_flow.PAYMENT_TIMEOUT
    assert session.payment_link.startswith("https://xaman.app/detect/")


def test_mint_session_happy_path(monkeypatch, tmp_path):
    async def paid(**kwargs):
        return True

    async def fake_upload(path_on_cdn, data, content_type):
        return f"https://cdn.test/{path_on_cdn}"

    async def fake_mint(**kwargs):
        return "NFTID123"

    async def fake_offer(nft_id, destination):
        return "OFFER456"

    async def fake_accept(offer_id):
        return {"qr_url": "https://xumm.test/qr.png",
                "xumm_url": "https://xumm.test/sign", "uuid": "u"}

    async def fake_select(store, gender=None):
        return "male", [{"trait_type": "Background", "value": "Blue"},
                        {"trait_type": "Back", "value": "Angel Wings"},
                        {"trait_type": "Head", "value": "Crown"}]

    async def fake_compose(attributes, gender, store, basename, out_dir="generated"):
        p = tmp_path / f"{basename}.png"
        p.write_bytes(b"\x89PNG fake")
        return str(p), False

    recorded = {}
    def fake_record(**kw):
        recorded.update(kw)
        return True

    monkeypatch.setattr(mint_flow.xrpl_ops, "wait_for_payment", paid)
    monkeypatch.setattr(mint_flow, "_upload_to_bunny", fake_upload)
    monkeypatch.setattr(mint_flow.xrpl_ops, "mint_nft", fake_mint)
    monkeypatch.setattr(mint_flow.xrpl_ops, "create_nft_offer", fake_offer)
    monkeypatch.setattr(mint_flow.xumm_ops, "create_accept_offer_payload", fake_accept)
    monkeypatch.setattr(mint_flow.traits, "select_random_attributes", fake_select)
    monkeypatch.setattr(mint_flow.swap_compose, "compose_nft", fake_compose)
    monkeypatch.setattr(mint_flow.layer_store, "get_layer_store", lambda: object())
    monkeypatch.setattr(mint_flow, "get_next_nft_number", lambda: 9999)
    monkeypatch.setattr(mint_flow, "record_nft_mint", fake_record)
    monkeypatch.chdir(tmp_path)

    session = mint_flow.MintSession(discord_id="1", wallet_address="rTest")
    asyncio.get_event_loop().run_until_complete(mint_flow.run_mint_session(session))

    assert session.state == mint_flow.OFFER_READY
    assert session.state in mint_flow.TERMINAL_STATES  # next mint not blocked
    assert session.nft_id == "NFTID123"
    assert session.accept_deeplink == "https://xumm.test/sign"
    assert session.image_url == "https://cdn.test/lfg_9999.png"
    assert recorded["traits"]["Hat"] == "Crown"  # Head mapped to the Hat column
    assert "Head" not in recorded["traits"]
    assert recorded["traits"]["Back"] == "Angel Wings"  # Back persisted too

# --- Trait Swapper ---

def test_normalize_attributes():
    raw = [
        {"trait_type": "Accesory", "value": "Angel Wings"},  # typo + Back value
        {"trait_type": "Body", "value": "Curved Light"},
        {"trait_type": "Eyes", "value": "Hypno"},
    ]
    attrs = swap_meta.normalize_attributes(raw)
    assert [a["trait_type"] for a in attrs] == swap_meta.TRAIT_ORDER
    assert swap_meta.get_attr(attrs, "Back") == "Angel Wings"
    assert swap_meta.get_attr(attrs, "Accessory") == "None"
    assert swap_meta.get_attr(attrs, "Clothing") == "None"
    assert swap_meta.detect_gender(attrs) == "female"


def test_swap_traits_merge():
    a1 = swap_meta.normalize_attributes([
        {"trait_type": "Eyes", "value": "Laser"},
        {"trait_type": "Head", "value": "Crown"}])
    a2 = swap_meta.normalize_attributes([
        {"trait_type": "Eyes", "value": "Hypno"},
        {"trait_type": "Head", "value": "Halo"}])
    n1, n2 = swap_meta.swap_traits(a1, a2, ["Eyes"])
    assert swap_meta.get_attr(n1, "Eyes") == "Hypno"
    assert swap_meta.get_attr(n2, "Eyes") == "Laser"
    assert swap_meta.get_attr(n1, "Head") == "Crown"  # unswapped traits kept
    assert swap_meta.get_attr(n2, "Head") == "Halo"


def test_normalize_nft_and_season():
    meta = {"name": "Let's Effing Go! #800", "image": "ipfs://cid/800.png",
            "burnCount": 2, "attributes": [{"trait_type": "Body", "value": "Ape"}]}
    rec = swap_meta.normalize_nft("ID1", meta)
    assert rec["season"] == 2 and rec["gender"] == "ape" and rec["burn_count"] == 2
    assert rec["image"].startswith("https://cid.ipfs.dweb.link/")
    assert swap_meta.normalize_nft("ID2", {"name": "no number"}) is None
    assert swap_meta.normalize_nft("ID3", {"name": "LFG #9999", "attributes": []}) is None


def _swap_session():
    nft = lambda i: {  # noqa: E731
        "nft_id": f"OLD{i}", "name": f"Let's Effing Go! #{i}", "number": i,
        "season": 1, "image": f"https://cdn.test/{i}.png", "video": None,
        "burn_count": 0, "gender": "male",
        "attributes": swap_meta.normalize_attributes(
            [{"trait_type": "Eyes", "value": f"Eyes{i}"},
             {"trait_type": "Body", "value": "Straight Light"}]),
    }
    return swap_flow.SwapSession(discord_id="1", wallet_address="rTest",
                                 nft1=nft(10), nft2=nft(20),
                                 traits_to_swap=["Eyes"])


def test_swap_session_missing_layers_fails_before_burn(monkeypatch):
    burned = []
    async def fake_burn(nft_id, owner):
        burned.append(nft_id)
        return "HASH"
    async def missing(attrs, gender, store):
        return ["male/Eyes/Eyes20"]
    monkeypatch.setattr(swap_flow.xrpl_ops, "burn_nft", fake_burn)
    monkeypatch.setattr(swap_flow.layer_store, "get_layer_store", lambda: object())
    monkeypatch.setattr(swap_flow.swap_compose, "missing_layers", missing)

    session = _swap_session()
    asyncio.get_event_loop().run_until_complete(swap_flow.run_swap_session(session))
    assert session.state == swap_flow.FAILED
    assert "Missing trait layer" in session.error
    assert burned == []


def _patch_swap_stubs(monkeypatch, tmp_path, events,
                      burn_fails=(), mint_fails=()):
    """Stub the swap flow's externals; `events` records on-chain call order."""
    async def fake_upload(path_on_cdn, data, content_type):
        return f"https://cdn.test/LFGO/{path_on_cdn}"

    async def fake_compose(attrs, gender, store, basename, out_dir="generated"):
        p = tmp_path / f"{basename}.png"
        p.write_bytes(b"\x89PNG fake")
        return str(p), False

    async def no_missing(attrs, gender, store):
        return []

    async def fake_burn(nft_id, owner=None):
        if nft_id in burn_fails:
            events.append(f"burn_failed {nft_id}")
            return None
        events.append(f"burn {nft_id}")
        return "HASH"

    minted = []
    async def fake_mint(**kwargs):
        if len(minted) + 1 in mint_fails:
            minted.append(None)
            events.append("mint_failed")
            return None
        minted.append(kwargs["metadata_cdn_url"])
        events.append(f"mint NEW{len(minted)}")
        return f"NEW{len(minted)}"

    async def fake_offer(nft_id, destination, amount=None):
        assert amount is not None  # swap offers are BRIX-priced
        events.append(f"offer {nft_id}")
        return f"OFFER_{nft_id}"

    async def fake_accept(offer_id):
        return {"qr_url": "https://xumm.test/qr.png",
                "xumm_url": f"https://xumm.test/{offer_id}", "uuid": "u"}

    monkeypatch.setattr(swap_flow.config, "SWAP_RECORDS_DIR",
                        str(tmp_path / "swap_records"))
    monkeypatch.setattr(swap_flow.layer_store, "get_layer_store", lambda: object())
    monkeypatch.setattr(swap_flow.swap_compose, "missing_layers", no_missing)
    monkeypatch.setattr(swap_flow.swap_compose, "compose_nft", fake_compose)
    monkeypatch.setattr(swap_flow, "_upload_swap_file", fake_upload)
    monkeypatch.setattr(swap_flow.xrpl_ops, "burn_nft", fake_burn)
    monkeypatch.setattr(swap_flow.xrpl_ops, "mint_nft", fake_mint)
    monkeypatch.setattr(swap_flow.xrpl_ops, "create_nft_offer", fake_offer)
    monkeypatch.setattr(swap_flow.xumm_ops, "create_accept_offer_payload", fake_accept)


def test_swap_session_happy_path(monkeypatch, tmp_path):
    events = []
    _patch_swap_stubs(monkeypatch, tmp_path, events)

    session = _swap_session()
    asyncio.get_event_loop().run_until_complete(swap_flow.run_swap_session(session))

    assert session.state == swap_flow.OFFERS_READY
    # Replacements are minted BEFORE the originals are burned (fail-safe)
    assert events[:4] == ["mint NEW1", "mint NEW2", "burn OLD10", "burn OLD20"]
    assert len(session.results) == 2
    r = session.results[0]
    assert r["nft_id"] == "NEW1"
    assert r["image_url"] == "https://cdn.test/LFGO/10/10_1.png"
    assert r["metadata_url"].endswith("10/10_1.json")
    assert r["accept_deeplink"].startswith("https://xumm.test/OFFER_")
    # The on-chain journal is persisted for recovery
    records = list((tmp_path / "swap_records").glob("*.json"))
    assert len(records) == 1
    import json as _json
    record = _json.loads(records[0].read_text())
    assert record["status"] == "complete"
    assert {n["old_nft_id"] for n in record["nfts"]} == {"OLD10", "OLD20"}


def test_swap_session_mint_failure_keeps_originals(monkeypatch, tmp_path):
    events = []
    _patch_swap_stubs(monkeypatch, tmp_path, events, mint_fails={2})

    session = _swap_session()
    asyncio.get_event_loop().run_until_complete(swap_flow.run_swap_session(session))

    assert session.state == swap_flow.FAILED
    assert "No NFTs were lost" in session.error
    # No original was burned; the orphaned replacement was cleaned up
    assert events == ["mint NEW1", "mint_failed", "burn NEW1"]


def test_swap_session_partial_burn_failure_delivers_first_replacement(
        monkeypatch, tmp_path):
    events = []
    _patch_swap_stubs(monkeypatch, tmp_path, events, burn_fails={"OLD20"})

    session = _swap_session()
    asyncio.get_event_loop().run_until_complete(swap_flow.run_swap_session(session))

    # Original #1 is gone, so its replacement MUST be offered; the second
    # half of the swap is cancelled (replacement burned, original kept).
    assert session.state == swap_flow.FAILED
    assert events == ["mint NEW1", "mint NEW2", "burn OLD10",
                      "burn_failed OLD20", "burn NEW2", "offer NEW1"]
    assert len(session.results) == 1
    assert session.results[0]["nft_id"] == "NEW1"
    assert "still in your wallet" in session.error

# --- Unified layer store ---

def _make_layer_tree(root):
    for gender, traits_ in (("male", {"Background": ["Blue"], "Body": ["Straight Light"],
                                      "Eyes": ["Laser", "Hypno"]}),
                            ("ape", {"Background": ["Red"], "Body": ["Ape"]})):
        for trait, values in traits_.items():
            d = root / gender / trait
            d.mkdir(parents=True)
            for v in values:
                (d / f"{v}.png").write_bytes(b"\x89PNG fake")


def test_local_layer_store(tmp_path):
    _make_layer_tree(tmp_path)
    store = layer_store.LocalLayerStore(str(tmp_path))
    loop = asyncio.get_event_loop()
    assert loop.run_until_complete(store.list_genders()) == ["ape", "male"]
    assert loop.run_until_complete(store.list_values("male", "Eyes")) == ["Hypno", "Laser"]
    path = loop.run_until_complete(store.resolve("male", "Eyes", "Laser"))
    assert path and path.endswith("Eyes/Laser.png")
    assert loop.run_until_complete(store.resolve("male", "Eyes", "Nope")) is None


def test_select_random_attributes_from_store(tmp_path):
    _make_layer_tree(tmp_path)
    store = layer_store.LocalLayerStore(str(tmp_path))
    loop = asyncio.get_event_loop()
    gender, attrs = loop.run_until_complete(
        traits.select_random_attributes(store, gender="male"))
    assert gender == "male"
    by_type = {a["trait_type"]: a["value"] for a in attrs}
    assert by_type["Background"] == "Blue"
    assert by_type["Eyes"] in ("Laser", "Hypno")
    # attributes follow canonical layer order
    order = [a["trait_type"] for a in attrs]
    assert order == sorted(order, key=swap_meta.TRAIT_ORDER.index)


def test_cdn_layer_store_resolve_uses_cache(monkeypatch, tmp_path):
    store = layer_store.CdnLayerStore()
    store.cache_dir = str(tmp_path / "cache")
    downloads = []

    async def fake_list(rel_path):
        return [("Laser.png", False), ("Hypno.gif", False), ("sub", True)]

    async def fake_download(rel_path):
        downloads.append(rel_path)
        local = os.path.join(store.cache_dir, rel_path)
        os.makedirs(os.path.dirname(local), exist_ok=True)
        with open(local, "wb") as f:
            f.write(b"\x89PNG fake")
        return local

    monkeypatch.setattr(store, "_list_dir", fake_list)
    monkeypatch.setattr(store, "_download", fake_download)
    loop = asyncio.get_event_loop()
    assert loop.run_until_complete(store.list_values("male", "Eyes")) == ["Hypno", "Laser"]
    path = loop.run_until_complete(store.resolve("male", "Eyes", "Laser"))
    assert path.endswith("male/Eyes/Laser.png")
    assert downloads == ["male/Eyes/Laser.png"]
    assert loop.run_until_complete(store.resolve("male", "Eyes", "Missing")) is None
