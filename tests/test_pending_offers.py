# tests/test_pending_offers.py
# Pending-offers tray (#218): pure claimability filter + source-assertion
# guards for the HTML/JS wiring (same posture as test_market_panel_dom.py —
# the webapp client has no JS execution harness for DOM code).
import os

# Set env vars before any lfg_core.config import so module-level constants
# are frozen with the correct values even when this file is collected before
# webapp/test_smoke.py (see tests/test_server_identity_wiring.py).
os.environ.setdefault("XUMM_API_KEY", "test")
os.environ.setdefault("XUMM_API_SECRET", "test")
os.environ.setdefault("SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2")
os.environ.setdefault("TOKEN_ISSUER_ADDRESS", "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
os.environ.setdefault("TOKEN_CURRENCY_HEX", "4C46474F00000000000000000000000000000000")
os.environ.setdefault("BUNNY_CDN_ACCESS_KEY", "test")
os.environ.setdefault("BUNNY_CDN_STORAGE_ZONE", "test")
os.environ.setdefault("LAYER_SOURCE", "local")
os.environ.setdefault("BUNNY_PULL_ZONE", "nft.pullzone.example")

import lfg_core.xrpl_ops as xrpl_ops  # noqa: E402

WALLET = "rUSERUSERUSERUSERUSERUSERUSERUSr"
OTHER = "rOTHEROTHEROTHEROTHEROTHEROTHEr"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLIENT = os.path.join(ROOT, "webapp", "client")


def _offer(**kw):
    base = {
        "offer_index": "OFF" + kw.pop("offer_index", "1"),
        "nft_id": "00081B58" + "0" * 56,
        "amount": "0",
        "destination": WALLET,
        "flags": xrpl_ops.LSF_SELL_NFTOKEN,
        "owner": "rISSUER",
        "expiration": None,
    }
    base.update(kw)
    return base


def test_claimable_keeps_unexpired_sell_offer_to_wallet():
    offers = [_offer()]
    assert xrpl_ops.filter_claimable_offers(offers, WALLET, 1_800_000_000) == offers


def test_claimable_drops_other_destinations_and_open_offers():
    offers = [
        _offer(offer_index="a", destination=OTHER),  # someone else's gift
        _offer(offer_index="b", destination=None),  # open (not destination-locked)
        _offer(offer_index="c"),
    ]
    kept = xrpl_ops.filter_claimable_offers(offers, WALLET, 1_800_000_000)
    assert [o["offer_index"] for o in kept] == ["OFFc"]


def test_claimable_drops_priced_offers():
    # The signing account also holds PRICED destination-locked sells (Trait
    # Shop #217: XRP-drops string or BRIX amount dict). Only free gifts
    # ("0" drops) are claimable — anything else would charge on accept.
    offers = [
        _offer(offer_index="xrp", amount="10000000"),
        _offer(offer_index="brix", amount={"currency": "4C46", "issuer": "rISS", "value": "5"}),
        _offer(offer_index="gift"),
    ]
    kept = xrpl_ops.filter_claimable_offers(offers, WALLET, 1_800_000_000)
    assert [o["offer_index"] for o in kept] == ["OFFgift"]


def test_claimable_drops_buy_offers():
    # A buy bid (no sell flag) destined to the wallet must never be claimable.
    offers = [_offer(flags=0)]
    assert xrpl_ops.filter_claimable_offers(offers, WALLET, 1_800_000_000) == []


def test_claimable_respects_expiration():
    now_unix = 1_800_000_000
    now_ripple = now_unix - xrpl_ops.RIPPLE_EPOCH_OFFSET
    offers = [
        _offer(offer_index="past", expiration=now_ripple - 60),
        _offer(offer_index="future", expiration=now_ripple + 60),
        _offer(offer_index="never", expiration=None),
    ]
    kept = xrpl_ops.filter_claimable_offers(offers, WALLET, now_unix)
    assert [o["offer_index"] for o in kept] == ["OFFfuture", "OFFnever"]


def _read(name: str) -> str:
    with open(os.path.join(CLIENT, name), encoding="utf-8") as f:
        return f.read()


def test_index_has_offers_panel_and_entry_button():
    html = _read("index.html")
    assert 'id="offers-panel"' in html
    assert 'id="offers-btn"' in html
    assert 'id="offers-list"' in html
    assert 'id="offers-back-btn"' in html


def test_app_js_wires_offers_tray():
    js = _read("app.js")
    assert "'offers-panel'" in js  # registered in ALL_PANELS
    assert "async function openOffers()" in js
    assert "/api/offers/pending" in js
    assert "/api/offers/accept" in js
    # Thumbnails must go through the same-origin proxy (Activity CSP).
    assert "imgUrl(o.image, THUMB_W)" in js
