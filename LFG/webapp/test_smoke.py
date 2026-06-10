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

from lfg_core import mint_flow, xumm_ops, traits, swap_meta, swap_flow  # noqa: E402
from webapp import server  # noqa: E402


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


def test_format_trait_name():
    assert traits.format_trait_name("12 cool hat") == "Cool Hat"
    assert traits.format_trait_name("blue eyes") == "Blue Eyes"


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

    def fake_compose(layers_dir, selected, output_path):
        with open(output_path, "wb") as f:
            f.write(b"\x89PNG fake")
        return output_path

    monkeypatch.setattr(mint_flow.xrpl_ops, "wait_for_payment", paid)
    monkeypatch.setattr(mint_flow, "_upload_to_bunny", fake_upload)
    monkeypatch.setattr(mint_flow.xrpl_ops, "mint_nft", fake_mint)
    monkeypatch.setattr(mint_flow.xrpl_ops, "create_nft_offer", fake_offer)
    monkeypatch.setattr(mint_flow.xumm_ops, "create_accept_offer_payload", fake_accept)
    monkeypatch.setattr(mint_flow.traits, "compose_image", fake_compose)
    monkeypatch.setattr(mint_flow.traits, "select_random_traits",
                        lambda d: {"1 background": "blue.png"})
    monkeypatch.setattr(mint_flow, "get_next_nft_number", lambda: 9999)
    monkeypatch.setattr(mint_flow, "record_nft_mint", lambda **kw: True)
    monkeypatch.chdir(tmp_path)

    session = mint_flow.MintSession(discord_id="1", wallet_address="rTest")
    asyncio.get_event_loop().run_until_complete(mint_flow.run_mint_session(session))

    assert session.state == mint_flow.OFFER_READY
    assert session.nft_id == "NFTID123"
    assert session.accept_deeplink == "https://xumm.test/sign"
    assert session.image_url == "https://cdn.test/lfg_9999.png"

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
    monkeypatch.setattr(swap_flow.xrpl_ops, "burn_nft", fake_burn)
    monkeypatch.setattr(swap_flow.swap_compose, "missing_layers",
                        lambda attrs, gender, d: ["male/Eyes/Eyes20"])

    session = _swap_session()
    asyncio.get_event_loop().run_until_complete(swap_flow.run_swap_session(session))
    assert session.state == swap_flow.FAILED
    assert "Missing trait layer" in session.error
    assert burned == []


def test_swap_session_happy_path(monkeypatch, tmp_path):
    async def fake_upload(path_on_cdn, data, content_type):
        return f"https://cdn.test/LFGO/{path_on_cdn}"

    def fake_compose(attrs, gender, number, burn, layers_dir, out_dir="generated"):
        p = tmp_path / f"{number}_{burn}.png"
        p.write_bytes(b"\x89PNG fake")
        return str(p), False

    burned = []
    async def fake_burn(nft_id, owner):
        burned.append(nft_id)
        return "HASH"

    minted = []
    async def fake_mint(**kwargs):
        minted.append(kwargs["metadata_cdn_url"])
        return f"NEW{len(minted)}"

    async def fake_offer(nft_id, destination, amount=None):
        assert amount is not None  # swap offers are BRIX-priced
        return f"OFFER_{nft_id}"

    async def fake_accept(offer_id):
        return {"qr_url": "https://xumm.test/qr.png",
                "xumm_url": f"https://xumm.test/{offer_id}", "uuid": "u"}

    monkeypatch.setattr(swap_flow.swap_compose, "missing_layers",
                        lambda attrs, gender, d: [])
    monkeypatch.setattr(swap_flow.swap_compose, "compose_swapped_nft", fake_compose)
    monkeypatch.setattr(swap_flow, "_upload_swap_file", fake_upload)
    monkeypatch.setattr(swap_flow.xrpl_ops, "burn_nft", fake_burn)
    monkeypatch.setattr(swap_flow.xrpl_ops, "mint_nft", fake_mint)
    monkeypatch.setattr(swap_flow.xrpl_ops, "create_nft_offer", fake_offer)
    monkeypatch.setattr(swap_flow.xumm_ops, "create_accept_offer_payload", fake_accept)

    session = _swap_session()
    asyncio.get_event_loop().run_until_complete(swap_flow.run_swap_session(session))

    assert session.state == swap_flow.OFFERS_READY
    assert burned == ["OLD10", "OLD20"]
    assert len(session.results) == 2
    r = session.results[0]
    assert r["nft_id"] == "NEW1"
    assert r["image_url"] == "https://cdn.test/LFGO/10/10_1.png"
    assert r["metadata_url"].endswith("10/10_1.json")
    assert r["accept_deeplink"].startswith("https://xumm.test/OFFER_")
