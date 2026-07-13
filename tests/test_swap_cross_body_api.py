# tests/test_swap_cross_body_api.py
# Task 14 (#30): cross-body swap-matrix enforcement in handle_swap_start.
# Exercises the POST /api/swap handler directly via aiohttp's
# make_mocked_request, mirroring webapp/test_smoke.py's
# test_equip_missing_body_field_returns_400 pattern (dev-mode auth bypass +
# a stubbed request.json()) — there's no full aiohttp TestClient fixture for
# this route in the repo, so this is the established way app.py handlers are
# exercised directly.
#
# Env-guard preamble (verbatim from tests/test_seasons.py lines 1-18): importing
# lfg_core.config freezes its constants (e.g. IMG_PROXY_ALLOWED_BASES,
# LAYER_SOURCE) at import time; set the same defaults test_smoke.py uses so
# collection order can't strand them.
import os

os.environ.setdefault("XUMM_API_KEY", "test")
os.environ.setdefault("XUMM_API_SECRET", "test")
os.environ.setdefault("SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2")
os.environ.setdefault("TOKEN_ISSUER_ADDRESS", "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
os.environ.setdefault("TOKEN_CURRENCY_HEX", "4C46474F00000000000000000000000000000000")
os.environ.setdefault("BUNNY_CDN_ACCESS_KEY", "test")
os.environ.setdefault("BUNNY_CDN_STORAGE_ZONE", "test")
os.environ.setdefault("LAYER_SOURCE", "local")
os.environ.setdefault("BUNNY_PULL_ZONE", "nft.pullzone.example")

import asyncio  # noqa: E402
import json  # noqa: E402
from typing import Any  # noqa: E402

import pytest  # noqa: E402
from aiohttp.test_utils import make_mocked_request  # noqa: E402
from aiohttp.web import BaseRequest  # noqa: E402

from lfg_core import trait_config  # noqa: E402
from lfg_service import app as server  # noqa: E402


def _nft(
    nft_id: str, gender: str, name: str, none_slots: frozenset[str] = frozenset()
) -> dict[str, Any]:
    """Minimal normalized-NFT record: the fields handle_swap_start and
    SwapSession.to_dict() read (nft_id for the by_id lookup, gender for the
    swap-matrix gate, name/image for the response body) plus a full normalized
    `attributes` list (every slot filled) so the None-swap guard has data to
    read. `none_slots` marks trait types whose value is 'None' (a faceless
    ape's Eyes/Eyebrows/Mouth)."""
    from lfg_core import swap_meta

    attributes = [
        {"trait_type": t, "value": ("None" if t in none_slots else f"{t}-val")}
        for t in swap_meta.TRAIT_ORDER
    ]
    return {
        "nft_id": nft_id,
        "gender": gender,
        "name": name,
        "image": f"https://example.test/{nft_id}.png",
        "attributes": attributes,
    }


def _make_swap_request(nft1_id: str, nft2_id: str, traits: list[str]) -> BaseRequest:
    req = make_mocked_request("POST", "/api/swap")

    async def _json() -> dict[str, Any]:
        return {"nft1_id": nft1_id, "nft2_id": nft2_id, "traits": traits}

    req.json = _json  # type: ignore[method-assign]
    return req


def _stub_wallet_nfts(monkeypatch: pytest.MonkeyPatch, nfts: list[dict[str, Any]]) -> None:
    async def _fake_load_wallet_nfts(
        wallet: str, get_account_nfts: Any, meta_cache: Any = None
    ) -> list[dict[str, Any]]:
        return nfts

    monkeypatch.setattr(server.swap_meta, "load_wallet_nfts", _fake_load_wallet_nfts)


@pytest.fixture(autouse=True)
def _isolate_trait_config():
    # Defensive: some test modules point the global trait_config singleton at
    # a fixture YAML and reset it afterward, but don't rely on ordering —
    # force a clean load of the real repo trait_config.yaml for this module.
    trait_config.reset_config()
    yield
    trait_config.reset_config()


@pytest.fixture(autouse=True)
def _dev_mode_and_clean_sessions(monkeypatch: pytest.MonkeyPatch):
    # require_wallet/require_auth's dev-mode bypass gives a fixed user id
    # ("dev") and wallet (mock_economy.DEV_OWNER) without needing an
    # Authorization header — the same shortcut webapp/test_smoke.py uses for
    # its dev-mode auth tests.
    monkeypatch.setattr(server.config, "WEBAPP_DEV_MODE", True)
    # swap_sessions is a module-level dict keyed by session id, but
    # _active_session() looks up by discord_id ("dev" for every test here),
    # so a session left over from a prior test would spuriously 409 the next
    # one. Clear it before and after.
    server.swap_sessions.clear()
    yield
    server.swap_sessions.clear()


@pytest.fixture(autouse=True)
def _stub_run_swap_session(monkeypatch: pytest.MonkeyPatch):
    # handle_swap_start fires swap_flow.run_swap_session in a background
    # task it never awaits; stub it to a no-op so no XRPL/XUMM/network calls
    # happen and the task completes instantly instead of running unstubbed
    # I/O after the test function returns.
    async def _noop(session: Any) -> None:
        return None

    monkeypatch.setattr(server.swap_flow, "run_swap_session", _noop)


@pytest.fixture
def ape_skeleton_nfts() -> list[dict[str, Any]]:
    return [
        _nft("APE1", "ape", "LFG #1"),
        _nft("SKEL1", "skeleton", "LFG #2"),
    ]


@pytest.fixture
def same_body_nfts() -> list[dict[str, Any]]:
    return [
        _nft("M1", "male", "LFG #3"),
        _nft("M2", "male", "LFG #4"),
    ]


def test_cross_body_swap_permitted_layers_pass(monkeypatch, ape_skeleton_nfts):
    """ape+skeleton is a configured swap_matrix pair scoped to Head/Clothing
    (trait_config.yaml) — swapping exactly those layers must proceed."""
    _stub_wallet_nfts(monkeypatch, ape_skeleton_nfts)
    req = _make_swap_request("APE1", "SKEL1", ["Head", "Clothing"])

    resp = asyncio.get_event_loop().run_until_complete(server.handle_swap_start(req))

    assert resp.status == 200


def test_cross_body_swap_blocked_layer_rejected(monkeypatch, ape_skeleton_nfts):
    """Eyes is outside the ape/skeleton pair's allowed layer set, so it must
    be rejected with a 400 naming the blocked layer and the two bodies —
    even though Head (also requested) would be allowed on its own."""
    _stub_wallet_nfts(monkeypatch, ape_skeleton_nfts)
    req = _make_swap_request("APE1", "SKEL1", ["Head", "Eyes"])

    resp = asyncio.get_event_loop().run_until_complete(server.handle_swap_start(req))

    assert resp.status == 400
    body = json.loads(resp.body)
    assert "Eyes" in body["error"]
    assert "Head" not in body["error"]
    assert "ape" in body["error"]
    assert "skeleton" in body["error"]


def test_same_body_swap_unaffected(monkeypatch, same_body_nfts):
    """Same-body pairs must remain wholly unaffected by the cross-body
    matrix — swap_allowed() short-circuits to True whenever body_a == body_b."""
    _stub_wallet_nfts(monkeypatch, same_body_nfts)
    req = _make_swap_request("M1", "M2", ["Clothing", "Eyes"])

    resp = asyncio.get_event_loop().run_until_complete(server.handle_swap_start(req))

    assert resp.status == 200


def test_noop_swaps_helper_flags_only_both_empty_slots():
    """Pure helper: a one-sided empty slot IS swappable — 'None' is a real,
    expected trait value (shirtless/bald/no-accessory), and moving it onto the
    other NFT is a legitimate exchange, not a deletion. Only a slot that is
    empty on BOTH sides is a no-op (nothing would change) and is flagged."""
    from lfg_core import swap_meta

    faceless = [{"trait_type": "Eyes", "value": "None"}, {"trait_type": "Head", "value": "Crown"}]
    faced = [{"trait_type": "Eyes", "value": "Creepy"}, {"trait_type": "Head", "value": "Cap"}]
    # one-sided None is now allowed (the halo/None exchange the user wants)
    assert swap_meta.noop_swaps(faceless, faced, ["Eyes"]) == []
    assert swap_meta.noop_swaps(faced, faceless, ["Eyes"]) == []  # other side None
    assert swap_meta.noop_swaps(faced, faced, ["Eyes", "Head"]) == []  # both filled
    # both empty is a pure no-op
    assert swap_meta.noop_swaps(faceless, faceless, ["Eyes"]) == ["Eyes"]
    # "" (the original generator's empty spelling) counts as empty too
    empty = [{"trait_type": "Eyes", "value": ""}, {"trait_type": "Head", "value": "Crown"}]
    assert swap_meta.noop_swaps(empty, faced, ["Eyes"]) == []  # one-sided, allowed
    assert swap_meta.noop_swaps(empty, faceless, ["Eyes"]) == ["Eyes"]  # ""+None both empty
    # a slot absent from one attribute list is empty; still one-sided → allowed
    bare = [{"trait_type": "Head", "value": "Cap"}]
    assert swap_meta.noop_swaps(bare, faced, ["Eyes"]) == []


def test_swap_one_sided_none_slot_allowed(monkeypatch):
    """Same-body pair where one NFT has Eyes=None: requesting an Eyes swap is
    now allowed — the None moves onto the partner (an expected empty image),
    the partner's real Eyes moves back. Must NOT 400 on the None guard."""
    nfts = [
        _nft("M1", "male", "LFG #10", none_slots=frozenset({"Eyes"})),
        _nft("M2", "male", "LFG #11"),
    ]
    _stub_wallet_nfts(monkeypatch, nfts)
    req = _make_swap_request("M1", "M2", ["Eyes"])

    resp = asyncio.get_event_loop().run_until_complete(server.handle_swap_start(req))

    assert resp.status == 200


def test_swap_both_empty_slot_rejected(monkeypatch):
    """A slot empty on BOTH NFTs is a pure no-op — the swap would change
    nothing, so it 400s rather than burn a signature/fee for identity."""
    nfts = [
        _nft("M1", "male", "LFG #10", none_slots=frozenset({"Eyes"})),
        _nft("M2", "male", "LFG #11", none_slots=frozenset({"Eyes"})),
    ]
    _stub_wallet_nfts(monkeypatch, nfts)
    req = _make_swap_request("M1", "M2", ["Eyes"])

    resp = asyncio.get_event_loop().run_until_complete(server.handle_swap_start(req))

    assert resp.status == 400
    assert "Eyes" in json.loads(resp.body)["error"]


def test_swap_filled_slots_still_ok(monkeypatch):
    """The guard must not block swaps where both sides have real values."""
    nfts = [
        _nft("M1", "male", "LFG #10", none_slots=frozenset({"Eyes"})),
        _nft("M2", "male", "LFG #11"),
    ]
    _stub_wallet_nfts(monkeypatch, nfts)
    req = _make_swap_request("M1", "M2", ["Head"])  # both have Head-val

    resp = asyncio.get_event_loop().run_until_complete(server.handle_swap_start(req))

    assert resp.status == 200


def test_nfts_payload_includes_swap_matrix(monkeypatch, ape_skeleton_nfts):
    """Task 15 (#30): handle_nfts serializes trait_config's swap matrix so
    the client can mirror swap_allowed() and filter offered traits per the
    selected NFT pair's bodies, instead of only enforcing it server-side."""
    _stub_wallet_nfts(monkeypatch, ape_skeleton_nfts)

    async def _no_fee(wallet: str, amount: str) -> tuple[str, str]:
        raise RuntimeError("no fee quote available in this test")

    monkeypatch.setattr(server.swap_flow, "detect_swap_payment", _no_fee)
    req = make_mocked_request("GET", "/api/nfts")

    resp = asyncio.get_event_loop().run_until_complete(server.handle_nfts(req))

    assert resp.status == 200
    body = json.loads(resp.body)
    matrix = body["swap_matrix"]
    assert matrix["universal_layers"] == ["Accessory", "Back"]
    pairs = {frozenset(p["bodies"]): p for p in matrix["pairs"]}
    ape_skel = pairs[frozenset({"ape", "skeleton"})]
    assert ape_skel["layers"] == ["Clothing", "Head"]
    assert ape_skel["layers_except"] is None
    male_female = pairs[frozenset({"female", "male"})]
    assert male_female["layers"] is None
    assert male_female["layers_except"] == ["Clothing"]
