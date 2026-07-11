# Legacy apes get faces rolled on their first pass through the swapper (#168):
# run_swap_session fills None face slots after swap application, before the
# layer pre-check, for ape bodies only.
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DISCORD_BOT_TOKEN", "x")
os.environ.setdefault("XUMM_API_KEY", "x")
os.environ.setdefault("XUMM_API_SECRET", "x")
os.environ.setdefault("BUNNY_CDN_ACCESS_KEY", "x")
os.environ.setdefault("BUNNY_CDN_STORAGE_ZONE", "x")
os.environ.setdefault("SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2")
os.environ.setdefault("TOKEN_ISSUER_ADDRESS", "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
os.environ.setdefault("TOKEN_CURRENCY_HEX", "4C46474F00000000000000000000000000000000")
os.environ.setdefault("BUNNY_PULL_ZONE", "nft.pullzone.example")
os.environ.setdefault("LAYER_SOURCE", "local")

import asyncio  # noqa: E402

from lfg_core import swap_flow, swap_meta  # noqa: E402


def _nft(number, gender, attributes, mutable=True):
    return {
        "number": number,
        "gender": gender,
        "attributes": swap_meta.normalize_attributes(attributes),
        "mutable": mutable,
        "nft_id": f"ID{number}",
        "burn_count": 1,
    }


def test_swap_session_rolls_faces_for_apes_only(monkeypatch):
    captured = {}

    async def fake_fill(store, body, attributes, **kwargs):
        captured.setdefault("calls", []).append((body, [dict(a) for a in attributes]))
        if body != "ape":
            return False
        for a in attributes:
            if a["trait_type"] in ("Eyes", "Eyebrows", "Mouth"):
                a["value"] = "ROLLED"
        return True

    async def fake_missing(attributes, body, store):
        # Capture what the pre-check sees: rolled faces must already be there.
        captured.setdefault("prechecked", []).append((body, [dict(a) for a in attributes]))
        return ["stop/here/now"]  # fail the session before any payment/on-chain step

    monkeypatch.setattr(swap_flow.traits, "fill_missing_face_traits", fake_fill)
    monkeypatch.setattr(swap_flow.swap_compose, "missing_layers", fake_missing)
    monkeypatch.setattr(swap_flow.layer_store, "get_layer_store", lambda: object())

    ape = _nft(
        814,
        "ape",
        [{"trait_type": "Body", "value": "Xray"}, {"trait_type": "Accessory", "value": "Scythe"}],
    )
    skel = _nft(
        59,
        "skeleton",
        [{"trait_type": "Body", "value": "White"}, {"trait_type": "Accessory", "value": "Bible"}],
    )
    session = swap_flow.SwapSession(
        discord_id="1",
        wallet_address="rUser",
        nft1=ape,
        nft2=skel,
        traits_to_swap=["Accessory"],
    )
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(swap_flow.run_swap_session(session))
    finally:
        loop.close()

    assert session.state == swap_flow.FAILED  # stopped at the layer pre-check
    bodies = [b for b, _ in captured["calls"]]
    assert sorted(bodies) == ["ape", "skeleton"]
    ape_precheck = next(a for b, a in captured["prechecked"] if b == "ape")
    skel_precheck = next(a for b, a in captured["prechecked"] if b == "skeleton")
    for slot in ("Eyes", "Eyebrows", "Mouth"):
        assert swap_meta.get_attr(ape_precheck, slot) == "ROLLED"
        assert swap_meta.get_attr(skel_precheck, slot) != "ROLLED"
