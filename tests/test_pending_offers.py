# tests/test_pending_offers.py
# Pending-offers tray (#218): pure claimability filter + source-assertion
# guards for the HTML/JS wiring (same posture as test_market_panel_dom.py —
# the webapp client has no JS execution harness for DOM code).
import asyncio
import json
import os
from decimal import Decimal

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


def test_claimable_keeps_priced_offers_it_can_label(monkeypatch):
    # Priced destination-locked sells the tray can price honestly ARE
    # claimable — a swap-remint delivery offer is priced (the swap fee is
    # collected on the accept) and hiding it stranded the NFT at the issuer
    # (the 2026-08-30 Tinkerbell incident). The client renders the price on
    # the Accept button, so the user is never charged unknowingly.
    monkeypatch.setattr(
        xrpl_ops.config, "BRIX_CURRENCY_HEX", "4252495800000000000000000000000000000000"
    )
    monkeypatch.setattr(xrpl_ops.config, "BRIX_ISSUER", "rBRIXISSUER")
    offers = [
        _offer(offer_index="xrp", amount="10000000"),
        _offer(
            offer_index="brix",
            amount={
                "currency": "4252495800000000000000000000000000000000",
                "issuer": "rBRIXISSUER",
                "value": "10",
            },
        ),
        _offer(offer_index="gift"),
    ]
    kept = xrpl_ops.filter_claimable_offers(offers, WALLET, 1_800_000_000)
    assert [o["offer_index"] for o in kept] == ["OFFxrp", "OFFbrix", "OFFgift"]


def test_claimable_drops_unpriceable_currency_offers(monkeypatch):
    # An IOU amount that is not the configured BRIX pair cannot be rendered
    # honestly — fail closed and keep it out of the tray (the original
    # Greptile P1 concern: never let a user sign a charge they can't read).
    monkeypatch.setattr(
        xrpl_ops.config, "BRIX_CURRENCY_HEX", "4252495800000000000000000000000000000000"
    )
    monkeypatch.setattr(xrpl_ops.config, "BRIX_ISSUER", "rBRIXISSUER")
    offers = [
        _offer(offer_index="lfgo", amount={"currency": "4C46", "issuer": "rISS", "value": "5"}),
        _offer(
            offer_index="wrongissuer",
            amount={
                "currency": "4252495800000000000000000000000000000000",
                "issuer": "rSOMEONEELSE",
                "value": "5",
            },
        ),
        _offer(offer_index="gift"),
    ]
    kept = xrpl_ops.filter_claimable_offers(offers, WALLET, 1_800_000_000)
    assert [o["offer_index"] for o in kept] == ["OFFgift"]


def test_swap_delivery_priced_in_swap_offer_pair_is_labeled(monkeypatch):
    # Greptile P1 (PR #457, thread 3897184743): a deployment may configure
    # BRIX_CURRENCY_HEX/BRIX_ISSUER independently of SWAP_OFFER_CURRENCY_HEX/
    # SWAP_OFFER_ISSUER (CLAUDE.md's "Per-kind denomination" — BRIX_* defaults
    # to the SWAP_OFFER_* pair, so today's deployments have them equal, which
    # made this latent). swap_flow.py prices delivery offers in the
    # SWAP_OFFER pair specifically, so offer_price_label must recognize BOTH
    # configured pairs — not just BRIX_* — or a divergent deployment strands
    # the NFT exactly like the original bug this PR fixed.
    monkeypatch.setattr(
        xrpl_ops.config, "BRIX_CURRENCY_HEX", "4252495800000000000000000000000000000000"
    )
    monkeypatch.setattr(xrpl_ops.config, "BRIX_ISSUER", "rBRIXISSUER")
    monkeypatch.setattr(
        xrpl_ops.config, "SWAP_OFFER_CURRENCY_HEX", "ABABABABABABABABABABABABABABABABABABABAB"
    )
    monkeypatch.setattr(xrpl_ops.config, "SWAP_OFFER_ISSUER", "rSWAPOFFERISSUER")
    swap_amount = {
        "currency": "ABABABABABABABABABABABABABABABABABABABAB",
        "issuer": "rSWAPOFFERISSUER",
        "value": "10",
    }
    brix_amount = {
        "currency": "4252495800000000000000000000000000000000",
        "issuer": "rBRIXISSUER",
        "value": "5",
    }
    unknown_amount = {"currency": "4C46", "issuer": "rSOMEONEELSE", "value": "1"}
    offers = [
        _offer(offer_index="swap", amount=swap_amount),
        _offer(offer_index="brix", amount=brix_amount),
        _offer(offer_index="unknown", amount=unknown_amount),
    ]
    # An offer priced in the SWAP_OFFER pair is claimable and labeled.
    assert xrpl_ops.offer_price_label(swap_amount) == "10 BRIX"
    # An offer priced in the BRIX pair is claimable and labeled.
    assert xrpl_ops.offer_price_label(brix_amount) == "5 BRIX"
    # An unknown IOU is not claimable (fail closed, unchanged).
    assert xrpl_ops.offer_price_label(unknown_amount) is None
    kept = xrpl_ops.filter_claimable_offers(offers, WALLET, 1_800_000_000)
    assert [o["offer_index"] for o in kept] == ["OFFswap", "OFFbrix"]


def test_offer_price_label_forms(monkeypatch):
    # Free → None; XRP drops → trimmed "N XRP"; configured BRIX pair →
    # "N BRIX"; anything else → None (unpriceable, excluded by the filter).
    monkeypatch.setattr(
        xrpl_ops.config, "BRIX_CURRENCY_HEX", "4252495800000000000000000000000000000000"
    )
    monkeypatch.setattr(xrpl_ops.config, "BRIX_ISSUER", "rBRIXISSUER")
    assert xrpl_ops.offer_price_label("0") is None
    assert xrpl_ops.offer_price_label("10000000") == "10 XRP"
    assert xrpl_ops.offer_price_label("10500000") == "10.5 XRP"
    brix = {
        "currency": "4252495800000000000000000000000000000000",
        "issuer": "rBRIXISSUER",
        "value": "10",
    }
    assert xrpl_ops.offer_price_label(brix) == "10 BRIX"
    assert xrpl_ops.offer_price_label(dict(brix, value="2.50")) == "2.5 BRIX"
    assert xrpl_ops.offer_price_label(dict(brix, issuer="rX")) is None
    assert xrpl_ops.offer_price_label({"currency": "4C46", "issuer": "rX", "value": "1"}) is None
    assert xrpl_ops.offer_price_label("nonsense") is None


def test_pending_offer_row_carries_price_label(monkeypatch, tmp_path):
    # Every tray row exposes price_label so the client can render the cost on
    # the Accept button ("Accept — 10 BRIX"); free gifts carry None.
    import lfg_core.nft_index as nft_index
    from lfg_service import app

    monkeypatch.setattr(
        app.xrpl_ops.config, "BRIX_CURRENCY_HEX", "4252495800000000000000000000000000000000"
    )
    monkeypatch.setattr(app.xrpl_ops.config, "BRIX_ISSUER", "rBRIXISSUER")
    char_db = str(tmp_path / "char.db")
    econ_db = str(tmp_path / "econ.db")
    conn = nft_index.init_db(char_db)
    conn.execute(
        "INSERT INTO onchain_nfts (nft_id, nft_number, image, is_burned) VALUES (?, ?, ?, 0)",
        ("00081B58" + "0" * 56, 1125, "https://cdn.example/1125.png"),
    )
    conn.commit()
    conn.close()
    nft_index.init_db(econ_db).close()
    monkeypatch.setattr(
        app.nft_index, "index_db_path", lambda net: char_db if net == "MAINNET" else econ_db
    )
    brix = {
        "currency": "4252495800000000000000000000000000000000",
        "issuer": "rBRIXISSUER",
        "value": "10",
    }
    priced = {"offer_index": "OFFp", "nft_id": "00081B58" + "0" * 56, "amount": brix}
    free = {"offer_index": "OFFf", "nft_id": "00081B58" + "0" * 56, "amount": "0"}
    assert app._pending_offer_row(priced, "MAINNET", "TESTNET", None)["price_label"] == "10 BRIX"
    assert app._pending_offer_row(free, "MAINNET", "TESTNET", None)["price_label"] is None


def test_client_renders_price_label_on_accept():
    # Source-assertion guard (no JS harness): the tray's Accept button must
    # surface price_label so a priced accept is never mislabeled as free.
    with open(os.path.join(CLIENT, "app.js")) as f:
        src = f.read()
    assert "price_label" in src


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


def test_pending_offer_row_enriches_character(monkeypatch, tmp_path):
    # A claimable offer whose nft_id is a known character edition resolves to a
    # character row: nft_number + CDN image from onchain_nfts (unchanged).
    import lfg_core.nft_index as nft_index
    from lfg_service import app

    char_db = str(tmp_path / "char.db")
    econ_db = str(tmp_path / "econ.db")
    conn = nft_index.init_db(char_db)
    conn.execute(
        "INSERT INTO onchain_nfts (nft_id, nft_number, image, is_burned) VALUES (?, ?, ?, 0)",
        ("00081B58" + "0" * 56, 3536, "https://cdn.example/3536.png"),
    )
    conn.commit()
    conn.close()

    def _db(net):
        return char_db if net == "MAINNET" else econ_db

    monkeypatch.setattr(app.nft_index, "index_db_path", _db)
    o = {"offer_index": "OFFa", "nft_id": "00081B58" + "0" * 56, "amount": "0"}
    row = app._pending_offer_row(o, "MAINNET", "TESTNET", None)
    assert row["kind"] == "character"
    assert row["nft_number"] == 3536
    assert row["image"] == "https://cdn.example/3536.png"


def test_pending_offer_row_enriches_trait_token(monkeypatch, tmp_path):
    # An Extract-minted trait token is unknown to onchain_nfts but lives in
    # trait_tokens (economy net). The row must carry slot/value + a same-origin
    # /api/layer thumbnail so the tray shows the trait, not a raw nft_id (#).
    import lfg_core.economy_store as economy_store
    import lfg_core.nft_index as nft_index
    from lfg_service import app

    char_db = str(tmp_path / "char.db")
    econ_db = str(tmp_path / "econ.db")
    nft_index.init_db(char_db).close()  # empty character index
    econ = nft_index.init_db(econ_db)
    economy_store.init_economy_schema(econ)
    economy_store.upsert_trait_token(econ, "000900007D" + "0" * 54, WALLET, "Hat", "Wizard Hat")
    econ.commit()
    econ.close()

    def _db(net):
        return char_db if net == "MAINNET" else econ_db

    monkeypatch.setattr(app.nft_index, "index_db_path", _db)
    monkeypatch.setattr(
        app, "_trait_image_url", lambda cfg, slot, value: f"/api/layer?trait={slot}&value={value}"
    )
    o = {"offer_index": "OFFt", "nft_id": "000900007D" + "0" * 54, "amount": "0"}
    row = app._pending_offer_row(o, "MAINNET", "TESTNET", object())
    assert row["kind"] == "trait"
    assert row["nft_number"] is None
    assert row["slot"] == "Hat"
    assert row["value"] == "Wizard Hat"
    assert row["image_url"] == "/api/layer?trait=Hat&value=Wizard Hat"


def test_pending_offer_row_enriches_closet_token(monkeypatch, tmp_path):
    # A freshly minted soulbound Closet is in neither onchain_nfts nor
    # trait_tokens, so the tray rendered "0010000… Accept" with no context —
    # a user could not tell the offer was ours. Resolve it from closet_tokens.
    import lfg_core.economy_store as economy_store
    import lfg_core.nft_index as nft_index
    from lfg_service import app

    char_db = str(tmp_path / "char.db")
    econ_db = str(tmp_path / "econ.db")
    nft_index.init_db(char_db).close()
    econ = nft_index.init_db(econ_db)
    economy_store.init_economy_schema(econ)
    nft_id = "00100000D1" + "0" * 54
    econ.execute(
        "INSERT INTO closet_tokens (owner, nft_id, uri_hex, status, offer_id) VALUES (?,?,?,?,?)",
        (WALLET, nft_id, "", "pending_accept", "OFFc"),
    )
    econ.commit()
    econ.close()

    def _db(net):
        return char_db if net == "MAINNET" else econ_db

    monkeypatch.setattr(app.nft_index, "index_db_path", _db)
    o = {"offer_index": "OFFc", "nft_id": nft_id, "amount": "0"}
    row = app._pending_offer_row(o, "MAINNET", "TESTNET", object())
    assert row["kind"] == "closet"
    assert row["nft_number"] is None
    assert row["owner"] == WALLET


def test_pending_offers_config_failure_uses_availability_error_contract(monkeypatch):
    from lfg_service import app

    class _Request(dict):
        headers = {"Authorization": "Bearer test"}

    async def _offers(_wallet):
        return [_offer()]

    monkeypatch.setattr(app.config, "WEBAPP_DEV_MODE", False)
    monkeypatch.setattr(
        app, "verify_session_token", lambda _token: {"id": "user", "platform": "web"}
    )
    monkeypatch.setattr(
        app, "_resolve_wallet", lambda _platform, _user: asyncio.sleep(0, result=WALLET)
    )
    monkeypatch.setattr(app.xrpl_ops, "bot_wallet_address", lambda: "rISSUER")
    monkeypatch.setattr(app.xrpl_ops, "get_account_nft_offers", _offers)
    monkeypatch.setattr(
        app.trait_config, "get_config", lambda: (_ for _ in ()).throw(OSError("bad"))
    )
    response = asyncio.get_event_loop().run_until_complete(app.handle_pending_offers(_Request()))

    assert response.status == 503
    assert json.loads(response.body) == {
        "error": "offer lookup failed",
        "code": "pending_unavailable",
    }


def test_accept_priced_offer_insufficient_brix_409(monkeypatch):
    # Fail-closed like the marketplace buy (Buy is fail-closed via
    # verify_sell_offer): a BRIX-priced pending offer must never reach a
    # built payload when the caller can't afford it on-ledger — that would
    # cost them a doomed Xaman signature instead of an honest refusal.
    from lfg_service import app

    brix_currency = "4252495800000000000000000000000000000000"
    brix_issuer = "rBRIXISSUER"
    monkeypatch.setattr(app.xrpl_ops.config, "BRIX_CURRENCY_HEX", brix_currency)
    monkeypatch.setattr(app.xrpl_ops.config, "BRIX_ISSUER", brix_issuer)

    class _Request(dict):
        headers = {"Authorization": "Bearer test"}

        async def json(self):
            return {"offer_index": "OFFbrix"}

    priced_offer = _offer(
        offer_index="brix",
        amount={"currency": brix_currency, "issuer": brix_issuer, "value": "10"},
    )

    async def _offers(_wallet):
        return [priced_offer]

    async def _low_balance(_wallet, _currency, _issuer):
        return xrpl_ops.TrustlineState.PRESENT, Decimal("1")

    def _must_not_be_called(*_a, **_kw):
        raise AssertionError("no payload may be built when BRIX balance is insufficient")

    monkeypatch.setattr(app.config, "WEBAPP_DEV_MODE", False)
    monkeypatch.setattr(
        app, "verify_session_token", lambda _token: {"id": "user", "platform": "web"}
    )
    monkeypatch.setattr(
        app, "_resolve_wallet", lambda _platform, _user: asyncio.sleep(0, result=WALLET)
    )
    monkeypatch.setattr(app.xrpl_ops, "bot_wallet_address", lambda: "rISSUER")
    monkeypatch.setattr(app.xrpl_ops, "get_account_nft_offers", _offers)
    monkeypatch.setattr(app.xrpl_ops, "get_trustline_state", _low_balance)
    monkeypatch.setattr(app, "_request_return_url", _must_not_be_called)
    monkeypatch.setattr(app.xumm_ops, "create_accept_offer_payload", _must_not_be_called)

    response = asyncio.get_event_loop().run_until_complete(
        app.handle_pending_offer_accept(_Request())
    )

    assert response.status == 409
    assert json.loads(response.body)["code"] == "insufficient_brix"


def test_accept_priced_offer_missing_trustline_409(monkeypatch):
    # Fix round 1 (task review of PR #457): the stranded editions this PR
    # targets (#977, #985, #1217, #1866, #2058) are exactly the population
    # likely to have NO BRIX line at all yet, not merely an insufficient
    # balance. Falling through to build a payload for an ABSENT trustline
    # would let the user sign in Xaman and have the accept fail on-ledger
    # (tecNO_LINE class) — refuse up front with the same trustline_required
    # code the mint/market flows already use, before any payload is built.
    from lfg_service import app

    brix_currency = "4252495800000000000000000000000000000000"
    brix_issuer = "rBRIXISSUER"
    monkeypatch.setattr(app.xrpl_ops.config, "BRIX_CURRENCY_HEX", brix_currency)
    monkeypatch.setattr(app.xrpl_ops.config, "BRIX_ISSUER", brix_issuer)

    class _Request(dict):
        headers = {"Authorization": "Bearer test"}

        async def json(self):
            return {"offer_index": "OFFbrix"}

    priced_offer = _offer(
        offer_index="brix",
        amount={"currency": brix_currency, "issuer": brix_issuer, "value": "10"},
    )

    async def _offers(_wallet):
        return [priced_offer]

    async def _absent_trustline(_wallet, _currency, _issuer):
        return xrpl_ops.TrustlineState.ABSENT, None

    def _must_not_be_called(*_a, **_kw):
        raise AssertionError("no payload may be built when the caller has no BRIX trustline")

    monkeypatch.setattr(app.config, "WEBAPP_DEV_MODE", False)
    monkeypatch.setattr(
        app, "verify_session_token", lambda _token: {"id": "user", "platform": "web"}
    )
    monkeypatch.setattr(
        app, "_resolve_wallet", lambda _platform, _user: asyncio.sleep(0, result=WALLET)
    )
    monkeypatch.setattr(app.xrpl_ops, "bot_wallet_address", lambda: "rISSUER")
    monkeypatch.setattr(app.xrpl_ops, "get_account_nft_offers", _offers)
    monkeypatch.setattr(app.xrpl_ops, "get_trustline_state", _absent_trustline)
    monkeypatch.setattr(app, "_request_return_url", _must_not_be_called)
    monkeypatch.setattr(app.xumm_ops, "create_accept_offer_payload", _must_not_be_called)

    response = asyncio.get_event_loop().run_until_complete(
        app.handle_pending_offer_accept(_Request())
    )

    assert response.status == 409
    assert json.loads(response.body)["code"] == "trustline_required"


def test_accept_priced_offer_unknown_trustline_proceeds(monkeypatch):
    # UNKNOWN (the lookup itself failed) must fail OPEN, unlike ABSENT or a
    # confirmed-low balance: a transient RPC blip must never block a legitimate
    # accept the caller could otherwise complete — Xaman shows the real cost,
    # and a doomed accept (if funds truly are short) just costs a retry, not a
    # silent block on every future attempt.
    from lfg_service import app

    brix_currency = "4252495800000000000000000000000000000000"
    brix_issuer = "rBRIXISSUER"
    monkeypatch.setattr(app.xrpl_ops.config, "BRIX_CURRENCY_HEX", brix_currency)
    monkeypatch.setattr(app.xrpl_ops.config, "BRIX_ISSUER", brix_issuer)

    class _Request(dict):
        headers = {"Authorization": "Bearer test"}

        async def json(self):
            return {"offer_index": "OFFbrix"}

    priced_offer = _offer(
        offer_index="brix",
        amount={"currency": brix_currency, "issuer": brix_issuer, "value": "10"},
    )

    async def _offers(_wallet):
        return [priced_offer]

    async def _unknown_trustline(_wallet, _currency, _issuer):
        return xrpl_ops.TrustlineState.UNKNOWN, None

    async def _push_token(_user):
        return None

    async def _payload(offer_id, **_kw):
        assert offer_id == "OFFbrix"
        return {"qr_url": "QR", "xumm_url": "LINK", "uuid": "U", "pushed": False, "push": None}

    monkeypatch.setattr(app.config, "WEBAPP_DEV_MODE", False)
    monkeypatch.setattr(
        app, "verify_session_token", lambda _token: {"id": "user", "platform": "web"}
    )
    monkeypatch.setattr(
        app, "_resolve_wallet", lambda _platform, _user: asyncio.sleep(0, result=WALLET)
    )
    monkeypatch.setattr(app.xrpl_ops, "bot_wallet_address", lambda: "rISSUER")
    monkeypatch.setattr(app.xrpl_ops, "get_account_nft_offers", _offers)
    monkeypatch.setattr(app.xrpl_ops, "get_trustline_state", _unknown_trustline)
    monkeypatch.setattr(app, "_push_token", _push_token)
    monkeypatch.setattr(app.xumm_ops, "create_accept_offer_payload", _payload)

    response = asyncio.get_event_loop().run_until_complete(
        app.handle_pending_offer_accept(_Request())
    )

    assert response.status == 200
    body = json.loads(response.body)
    assert body == {"qr": "QR", "link": "LINK", "push": None}


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
    # Character thumbnails go through the same-origin CDN proxy (Activity CSP);
    # trait tokens (Extract) render their /api/layer art via traitLayerSrc.
    assert "imgUrl(o.image, THUMB_W)" in js
    assert "traitLayerSrc(o.image_url)" in js


def test_offer_accept_routes_missing_trustline_to_brix_flow():
    # Fix round 1 (task review of PR #457): the stranded editions this PR
    # targets (#977, #985, #1217, #1866, #2058) are exactly the population
    # likely to have no BRIX line yet, so the server now answers a priced
    # accept with 409 trustline_required (see handle_pending_offer_accept).
    # offerAccept must route that into the existing BRIX trustline flow
    # (same pattern as marketFlow's trustline_required handling), not just a
    # plain showError toast that leaves the user with no actionable next
    # step — a Source-assertion guard, since the client has no JS harness.
    js = _read("app.js")
    start = js.index("async function offerAccept(")
    end = js.index("\nfunction renderBulkJob(", start)
    body = js[start:end]
    assert "trustline_required" in body
    assert "startBrixTrustline" in body
