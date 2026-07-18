# Smoke tests for the Activity webapp: module imports, route registration,
# session tokens, wallet validation, and the mint session state machine with
# XRPL/XUMM stubbed out. Run from repo root: python -m pytest webapp/test_smoke.py

import asyncio
import os
import sys
from decimal import Decimal

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
os.environ.setdefault("BUNNY_PULL_ZONE", "nft.pullzone.example")

from aiohttp import web  # noqa: E402

from lfg_core import (  # noqa: E402
    config,
    layer_store,
    mint_flow,
    nft_index,
    swap_flow,
    swap_meta,
    traits,
    user_db,  # noqa: E402
    xrpl_ops,
    xumm_ops,
)
from lfg_service import app as server  # noqa: E402


def test_routes_registered():
    app = server.create_app()
    paths = {getattr(r.resource, "canonical", "") for r in app.router.routes()}
    for expected in [
        "/api/config",
        "/api/token",
        "/api/me",
        "/api/register",
        "/api/mint",
        "/api/mint/active",
        "/api/mint/bulk",
        "/api/mint/bulk/active",
        "/api/mint/bulk/{session_id}/cancel",
        "/api/mint/{session_id}",
        "/api/nfts",
        "/api/swap",
        "/api/swap/{session_id}",
        "/api/swap/{session_id}/regenerate",
        "/api/swap/{session_id}/cancel",
        "/api/qr.png",
        "/",
        "/api/mint/{session_id}/regenerate",
        "/api/signin",
        "/api/signin/{payload_uuid}",
    ]:
        assert expected in paths, f"missing route {expected}"
    # New session-recovery routes: pin the HTTP method too, so a wrong-verb
    # registration can't pass on path presence alone.
    method_paths = {(r.method, getattr(r.resource, "canonical", "")) for r in app.router.routes()}
    for expected_pair in [
        ("GET", "/api/mint/active"),
        ("GET", "/api/mint/bulk/active"),
        ("POST", "/api/mint/bulk/{session_id}/cancel"),
        ("POST", "/api/swap/{session_id}/regenerate"),
        ("POST", "/api/swap/{session_id}/cancel"),
    ]:
        assert expected_pair in method_paths, f"missing route {expected_pair}"
    # Trustline endpoints were removed in v2: never user-facing again
    assert "/api/trustline" not in paths
    assert "/api/brix-trustline" not in paths


def test_session_token_roundtrip():
    token = server.make_session_token({"id": "123", "name": "josh"})
    payload = server.verify_session_token(token)
    assert payload["id"] == "123"
    assert payload["name"] == "josh"


def test_session_token_tamper_rejected():
    token = server.make_session_token({"id": "123", "name": "josh"})
    # flip the last signature char to one it isn't — replacing with a fixed
    # value was flaky (1/256 runs the signature already ended that way)
    flipped = "0" if token[-1] != "0" else "1"
    assert server.verify_session_token(token[:-1] + flipped) is None
    assert server.verify_session_token("garbage") is None


def test_payment_link_is_xaman_detect():
    # Last-resort fallback only (used when the XUMM API is down); the real
    # payment link is a XUMM sign-request payload — see the tests below.
    link = xumm_ops.generate_static_payment_link("rrrrrrrrrrrrrrrrrrrrrhoLvTp")
    assert link.startswith("https://xaman.app/detect/")


def _fake_xumm_api(monkeypatch, captured):
    class FakeResp:
        def json(self):
            return {
                "refs": {"qr_png": "https://xumm.test/qr.png"},
                "next": {"always": "https://xumm.app/sign/UUID1"},
                "uuid": "UUID1",
            }

    def fake_post(url, json=None, headers=None, timeout=None):
        captured.update(json)
        return FakeResp()

    monkeypatch.setattr(xumm_ops.requests, "post", fake_post)


def test_create_payment_payload_is_xumm_sign_request(monkeypatch):
    """The payment QR must encode a real XUMM sign-request URL — Xaman cannot
    parse the hand-rolled raw-JSON xaman.app/detect link (issue #8)."""
    captured = {}
    _fake_xumm_api(monkeypatch, captured)
    payload = asyncio.get_event_loop().run_until_complete(xumm_ops.create_payment_payload("rDest"))
    assert payload["xumm_url"] == "https://xumm.app/sign/UUID1"
    tx = captured["txjson"]
    assert tx["TransactionType"] == "Payment"
    assert tx["Destination"] == "rDest"
    assert tx["Amount"]["value"] == "1"
    assert captured["options"]["expire"] >= 1


def test_create_payment_payload_custom_currency(monkeypatch):
    captured = {}
    _fake_xumm_api(monkeypatch, captured)
    brix = "4252495800000000000000000000000000000000"
    asyncio.get_event_loop().run_until_complete(
        xumm_ops.create_payment_payload("rDest", value="20", currency=brix, issuer="rBrixIssuer")
    )
    assert captured["txjson"]["Amount"] == {
        "currency": brix,
        "value": "20",
        "issuer": "rBrixIssuer",
    }


def test_discord_return_url_builder():
    ru = xumm_ops.discord_return_url("970785471686905867", "970785471686905871")
    assert ru == {
        "app": "discord://-/channels/970785471686905867/970785471686905871",
        "web": "https://discord.com/channels/970785471686905867/970785471686905871",
    }
    # anything non-numeric (or missing) is rejected — these come from the client
    assert xumm_ops.discord_return_url("evil://x", "123") is None
    assert xumm_ops.discord_return_url("123", "") is None
    assert xumm_ops.discord_return_url(None, None) is None


def test_payment_payload_includes_return_url(monkeypatch):
    captured = {}
    _fake_xumm_api(monkeypatch, captured)
    ru = {"app": "discord://-/channels/1/2", "web": "https://discord.com/channels/1/2"}
    asyncio.get_event_loop().run_until_complete(
        xumm_ops.create_payment_payload("rDest", return_url=ru)
    )
    assert captured["options"]["return_url"] == ru


def test_accept_offer_payload_includes_return_url(monkeypatch):
    captured = {}
    _fake_xumm_api(monkeypatch, captured)
    ru = {"app": "discord://-/channels/1/2", "web": "https://discord.com/channels/1/2"}
    asyncio.get_event_loop().run_until_complete(
        xumm_ops.create_accept_offer_payload("OFFER", return_url=ru)
    )
    assert captured["options"]["return_url"] == ru


def test_xrp_payment_payload_uses_drops(monkeypatch):
    captured = {}
    _fake_xumm_api(monkeypatch, captured)
    asyncio.get_event_loop().run_until_complete(
        xumm_ops.create_payment_payload("rDest", value="10", currency="XRP")
    )
    assert captured["txjson"]["Amount"] == "10000000"  # drops string, not dict


def _stub_balance(monkeypatch, balance):
    async def fake_balance(address, currency, issuer):
        return balance

    monkeypatch.setattr(mint_flow.xrpl_ops, "get_trustline_balance", fake_balance)


def test_mint_session_threads_return_url(monkeypatch):
    seen = {}

    async def fake_payload(destination, **kw):
        seen.update(kw)
        return {"qr_url": "q", "xumm_url": "https://xumm.app/sign/PAY", "uuid": "u"}

    _stub_balance(monkeypatch, Decimal("5"))
    monkeypatch.setattr(mint_flow.xumm_ops, "create_payment_payload", fake_payload)
    ru = {"app": "discord://-/channels/1/2", "web": "https://discord.com/channels/1/2"}
    session = mint_flow.MintSession(discord_id="1", wallet_address="rTest", return_url=ru)
    asyncio.get_event_loop().run_until_complete(session.prepare_payment())
    assert seen["return_url"] == ru


def test_mint_session_prepare_uses_xumm_payload(monkeypatch):
    async def fake_payload(destination, **kw):
        return {"qr_url": "q", "xumm_url": "https://xumm.app/sign/PAY", "uuid": "u"}

    _stub_balance(monkeypatch, Decimal("5"))
    monkeypatch.setattr(mint_flow.xumm_ops, "create_payment_payload", fake_payload)
    session = mint_flow.MintSession(discord_id="1", wallet_address="rTest")
    asyncio.get_event_loop().run_until_complete(session.prepare_payment())
    assert session.pay_with == "LFGO"
    assert session.payment_link == "https://xumm.app/sign/PAY"


def test_mint_session_prepare_falls_back_to_static(monkeypatch):
    async def fail(destination, **kw):
        return None

    _stub_balance(monkeypatch, Decimal("5"))
    monkeypatch.setattr(mint_flow.xumm_ops, "create_payment_payload", fail)
    session = mint_flow.MintSession(discord_id="1", wallet_address="rTest")
    asyncio.get_event_loop().run_until_complete(session.prepare_payment())
    assert session.payment_link.startswith("https://xaman.app/detect/")


def test_mint_payment_path_detection(monkeypatch):
    """Silent path detection: LFGO trustline + balance pays LFGO to the
    issuer; everything else (no line, low balance, lookup failure) pays XRP
    to the bot wallet."""

    async def fake_payload(destination, **kw):
        return None

    monkeypatch.setattr(mint_flow.xumm_ops, "create_payment_payload", fake_payload)
    loop = asyncio.get_event_loop()
    for balance, expected in ((Decimal("1"), "LFGO"), (Decimal("0"), "XRP"), (None, "XRP")):
        _stub_balance(monkeypatch, balance)
        session = mint_flow.MintSession(discord_id="1", wallet_address="rTest")
        loop.run_until_complete(session.prepare_payment())
        assert session.pay_with == expected, f"balance={balance}"
    assert session.pay_amount == config.MINT_PRICE_XRP
    p = session._payment_params()
    assert p["currency"] == "XRP"
    assert p["destination"] == xrpl_ops.bot_wallet_address()


def test_mint_fallback_defaults_to_xrp_path():
    # prepare_payment cancelled/failed -> any wallet can still pay XRP
    session = mint_flow.MintSession(discord_id="1", wallet_address="rTest")
    session.ensure_payment_fallback()
    assert session.pay_with == "XRP"
    assert session.pay_amount == config.MINT_PRICE_XRP
    assert session.payment_link.startswith("https://xaman.app/detect/")


def test_qr_png():
    png = xumm_ops.generate_qr_png("https://example.com")
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


# --- Issue #19: branded QR codes (mascot composited in the center) ---


def test_qr_png_branded_with_logo(tmp_path, monkeypatch):
    import io

    from PIL import Image

    logo = tmp_path / "mascot.png"
    Image.new("RGBA", (64, 64), (216, 72, 48, 255)).save(logo)
    monkeypatch.setattr(xumm_ops, "QR_LOGO_PATH", str(logo))
    png = xumm_ops.generate_qr_png("https://example.com")
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    img = Image.open(io.BytesIO(png)).convert("RGB")
    w, h = img.size
    # the logo (solid red) sits dead-center over the QR modules
    assert img.getpixel((w // 2, h // 2)) == (216, 72, 48)


def test_qr_png_missing_logo_falls_back_plain(monkeypatch):
    monkeypatch.setattr(xumm_ops, "QR_LOGO_PATH", "/nonexistent/mascot.png")
    png = xumm_ops.generate_qr_png("https://example.com")
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


# --- Issues #22/#24: XUMM payload status lookup ---


def _fake_xumm_get(monkeypatch, meta=None, response=None):
    class FakeResp:
        def json(self):
            return {
                "meta": {"opened": False, "signed": False, "expired": False, **(meta or {})},
                "response": {"account": None, **(response or {})},
            }

    def fake_get(url, headers=None, timeout=None):
        fake_get.url = url
        return FakeResp()

    monkeypatch.setattr(xumm_ops.requests, "get", fake_get)
    return fake_get


VALID_UUID = "01234567-89ab-cdef-0123-456789abcdef"


def test_get_payload_status(monkeypatch):
    fake = _fake_xumm_get(
        monkeypatch, meta={"opened": True, "signed": True}, response={"account": "rSigner"}
    )
    s = asyncio.get_event_loop().run_until_complete(xumm_ops.get_payload_status(VALID_UUID))
    assert fake.url.endswith(f"/{VALID_UUID}")
    assert s == {
        "opened": True,
        "signed": True,
        "expired": False,
        "cancelled": False,
        "resolved": False,
        "account": "rSigner",
        "txid": None,
        # #135: the push token XUMM issues on a signed payload; None here since
        # this fixture has no `application` block.
        "user_token": None,
    }


def test_get_payload_status_extracts_txid(monkeypatch):
    # The marketplace list/buy finalize flow (#44 Task 8) needs the signed
    # transaction's hash to fetch it by `tx` and confirm the ledger outcome —
    # XUMM's payload status yields the txid only, never the tx meta itself.
    _fake_xumm_get(
        monkeypatch,
        meta={"opened": True, "signed": True},
        response={"account": "rSigner", "txid": "ABCDEF0123456789"},
    )
    s = asyncio.get_event_loop().run_until_complete(xumm_ops.get_payload_status(VALID_UUID))
    assert s["txid"] == "ABCDEF0123456789"


def test_get_payload_status_error_returns_none(monkeypatch):
    def boom(url, headers=None, timeout=None):
        raise RuntimeError("xumm down")

    monkeypatch.setattr(xumm_ops.requests, "get", boom)
    assert (
        asyncio.get_event_loop().run_until_complete(xumm_ops.get_payload_status(VALID_UUID)) is None
    )


def test_get_payload_status_rejects_malformed_uuid(monkeypatch):
    def boom(url, headers=None, timeout=None):
        raise AssertionError("must not be called for a malformed uuid")

    monkeypatch.setattr(xumm_ops.requests, "get", boom)
    loop = asyncio.get_event_loop()
    for bad in ("../admin", "UUID9", "", None):
        assert loop.run_until_complete(xumm_ops.get_payload_status(bad)) is None


# --- Issue #24: Xaman Sign In payload for registration ---


def test_create_signin_payload(monkeypatch):
    captured = {}
    _fake_xumm_api(monkeypatch, captured)
    ru = {"app": "discord://-/channels/1/2", "web": "https://discord.com/channels/1/2"}
    payload = asyncio.get_event_loop().run_until_complete(
        xumm_ops.create_signin_payload(return_url=ru)
    )
    assert payload["xumm_url"] == "https://xumm.app/sign/UUID1"
    assert payload["uuid"] == "UUID1"
    assert captured["txjson"] == {"TransactionType": "SignIn"}
    assert captured["options"]["return_url"] == ru


# --- Issue #22: QR scan tracking + regenerate ---


def test_mint_session_tracks_payment_uuid(monkeypatch):
    async def fake_payload(destination, **kw):
        return {"qr_url": "q", "xumm_url": "https://xumm.app/sign/PAY", "uuid": "PAYUUID"}

    _stub_balance(monkeypatch, Decimal("5"))
    monkeypatch.setattr(mint_flow.xumm_ops, "create_payment_payload", fake_payload)
    session = mint_flow.MintSession(discord_id="1", wallet_address="rTest")
    asyncio.get_event_loop().run_until_complete(session.prepare_payment())
    assert session.payment_uuid == "PAYUUID"
    assert session.to_dict()["qr_scanned"] is False


def test_update_scan_state_marks_payment_scanned(monkeypatch):
    session = mint_flow.MintSession(discord_id="1", wallet_address="rTest")
    session.payment_uuid = "PAYUUID"
    calls = []
    signed = False

    async def fake_status(uuid):
        calls.append(uuid)
        return {"opened": True, "signed": signed, "expired": False, "account": None}

    monkeypatch.setattr(mint_flow.xumm_ops, "get_payload_status", fake_status)
    loop = asyncio.get_event_loop()
    loop.run_until_complete(mint_flow.update_scan_state(session))
    assert session.to_dict()["qr_scanned"] is True
    # opened is not enough to stop polling (#212: the signature — and the
    # rotated push token it carries — lands after the open)...
    signed = True
    loop.run_until_complete(mint_flow.update_scan_state(session))
    assert calls == ["PAYUUID", "PAYUUID"]
    # ...but once SIGNED, no further XUMM queries are made.
    loop.run_until_complete(mint_flow.update_scan_state(session))
    assert calls == ["PAYUUID", "PAYUUID"]


def test_update_scan_state_checks_accept_payload(monkeypatch):
    session = mint_flow.MintSession(discord_id="1", wallet_address="rTest")
    session.state = mint_flow.OFFER_READY
    session.accept_uuid = "ACCUUID"

    async def fake_status(uuid):
        assert uuid == "ACCUUID"
        return {"opened": True, "signed": True, "expired": False, "account": "rTest"}

    monkeypatch.setattr(mint_flow.xumm_ops, "get_payload_status", fake_status)
    asyncio.get_event_loop().run_until_complete(mint_flow.update_scan_state(session))
    d = session.to_dict()
    assert d["accept_scanned"] is True
    assert d["accept_signed"] is True


def test_mint_session_regenerate_payment(monkeypatch):
    links = iter(["https://xumm.app/sign/ONE", "https://xumm.app/sign/TWO"])
    uuids = iter(["U1", "U2"])

    async def fake_payload(destination, **kw):
        return {"qr_url": "q", "xumm_url": next(links), "uuid": next(uuids)}

    _stub_balance(monkeypatch, Decimal("5"))
    monkeypatch.setattr(mint_flow.xumm_ops, "create_payment_payload", fake_payload)
    session = mint_flow.MintSession(discord_id="1", wallet_address="rTest")
    loop = asyncio.get_event_loop()
    loop.run_until_complete(session.prepare_payment())
    session.qr_scanned = True
    loop.run_until_complete(session.regenerate_payment())
    assert session.payment_link == "https://xumm.app/sign/TWO"
    assert session.payment_uuid == "U2"
    assert session.qr_scanned is False


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
    "type": "transaction",
    "validated": True,
    "transaction": {
        "TransactionType": "Payment",
        "Account": "rSender",
        "Destination": "rDest",
        "Amount": {
            "currency": "4C46474F00000000000000000000000000000000",
            "issuer": "rIssuer",
            "value": "1",
        },
        "hash": "H1",
    },
    "meta": {
        "delivered_amount": {
            "currency": "4C46474F00000000000000000000000000000000",
            "issuer": "rIssuer",
            "value": "1",
        }
    },
}

V2_STREAM_MSG = {
    "type": "transaction",
    "validated": True,
    "tx_json": {
        "TransactionType": "Payment",
        "Account": "rSender",
        "Destination": "rDest",
        "DeliverMax": {
            "currency": "4C46474F00000000000000000000000000000000",
            "issuer": "rIssuer",
            "value": "1",
        },
        "hash": "H2",
    },
    "meta": {
        "delivered_amount": {
            "currency": "4C46474F00000000000000000000000000000000",
            "issuer": "rIssuer",
            "value": "1",
        }
    },
}

CUR = "4C46474F00000000000000000000000000000000"


def _matches(msg):
    tx, meta = xrpl_ops._extract_tx_and_meta(msg)
    return tx is not None and xrpl_ops._payment_matches(
        tx, meta, "rDest", "rSender", "1", CUR, "rIssuer"
    )


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


def test_payment_match_native_xrp():
    """The XRP mint/swap paths watch for native (drops string) payments."""
    msg = {
        "type": "transaction",
        "validated": True,
        "tx_json": {
            "TransactionType": "Payment",
            "Account": "rSender",
            "Destination": "rDest",
            "DeliverMax": "10000000",
            "hash": "H3",
        },
        "meta": {"delivered_amount": "10000000"},
    }
    tx, meta = xrpl_ops._extract_tx_and_meta(msg)

    def match(expected):
        return xrpl_ops._payment_matches(tx, meta, "rDest", "rSender", expected, "XRP", None)

    assert match("10")
    assert not match("10.5")  # short payment rejected
    # and a token payment never satisfies an XRP expectation
    tok, tok_meta = xrpl_ops._extract_tx_and_meta(V2_STREAM_MSG)
    assert not xrpl_ops._payment_matches(tok, tok_meta, "rDest", "rSender", "10", "XRP", None)


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
        xrpl_ops.wait_for_payment("rDest", "rSender", timeout_seconds=1)
    )
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

    paid = loop.run_until_complete(
        xrpl_ops.wait_for_payment("rDest", "rSender", timeout_seconds=1, not_before=tx_unix - 60)
    )
    assert paid is True

    # The same payment is too old for a session created after it -> no replay
    paid = loop.run_until_complete(
        xrpl_ops.wait_for_payment("rDest", "rSender", timeout_seconds=1, not_before=tx_unix + 60)
    )
    assert paid is False


def test_wait_for_payment_reconnects_after_stream_drop(monkeypatch):
    """A dropped websocket must reconnect (and re-check recent history)
    instead of reporting 'no payment' while time remains (issue #6)."""
    import copy

    entry = copy.deepcopy(V2_STREAM_MSG)
    entry["tx_json"]["date"] = 800000000
    connections = []

    class FakeWS:
        def __init__(self, url):
            connections.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def send(self, req):
            pass

        async def request(self, req):
            # The payment "lands" while the first connection is down
            class R:
                result = {"transactions": [entry] if len(connections) > 1 else []}

            return R()

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration  # stream closes immediately

    monkeypatch.setattr(xrpl_ops, "AsyncWebsocketClient", FakeWS)
    monkeypatch.setattr(xrpl_ops.config, "TOKEN_CURRENCY_HEX", CUR)
    monkeypatch.setattr(xrpl_ops.config, "TOKEN_ISSUER_ADDRESS", "rIssuer")
    tx_unix = 800000000 + xrpl_ops.RIPPLE_EPOCH_OFFSET
    paid = asyncio.get_event_loop().run_until_complete(
        xrpl_ops.wait_for_payment("rDest", "rSender", timeout_seconds=10, not_before=tx_unix - 60)
    )
    assert paid is True
    assert len(connections) >= 2


def test_mint_session_payment_timeout(monkeypatch):
    async def no_payment(**kwargs):
        return False

    monkeypatch.setattr(mint_flow.xrpl_ops, "wait_for_payment", no_payment)

    session = mint_flow.MintSession(discord_id="1", wallet_address="rTest")
    session.payment_uuid = "PAYUUID"  # #262: a real XUMM payload exists
    asyncio.get_event_loop().run_until_complete(mint_flow.run_mint_session(session))
    assert session.state == mint_flow.PAYMENT_TIMEOUT
    assert session.payment_link.startswith("https://xaman.app/detect/")


def test_mint_session_happy_path(monkeypatch, tmp_path):
    waited = {}

    async def paid(**kwargs):
        waited.update(kwargs)
        return True

    burned = []

    async def fake_buy_and_burn(currency, issuer, value, max_xrp=None):
        burned.append((currency, value, max_xrp))
        return "BURNHASH"

    monkeypatch.setattr(mint_flow.xrpl_ops, "buy_and_burn", fake_buy_and_burn)

    async def fake_upload(path_on_cdn, data, content_type):
        return f"https://cdn.test/{path_on_cdn}"

    async def fake_mint(**kwargs):
        return "NFTID123"

    async def fake_offer(nft_id, destination, **kwargs):
        return "OFFER456"

    async def fake_accept(offer_id, **kw):
        return {
            "qr_url": "https://xumm.test/qr.png",
            "xumm_url": "https://xumm.test/sign",
            "uuid": "u",
        }

    async def fake_select(store, body=None, **kw):
        return "male", [
            {"trait_type": "Background", "value": "Blue"},
            {"trait_type": "Back", "value": "Angel Wings"},
            {"trait_type": "Head", "value": "Crown"},
        ]

    async def fake_compose(attributes, body, store, basename, out_dir="generated"):
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
    session.payment_uuid = "PAYUUID"  # #262: a real XUMM payload exists
    asyncio.get_event_loop().run_until_complete(mint_flow.run_mint_session(session))

    assert session.state == mint_flow.OFFER_READY
    assert session.state in mint_flow.TERMINAL_STATES  # next mint not blocked
    # No prepare_payment ran, so the session fell back to the XRP path:
    # native-XRP watch on the bot wallet, then the LFGO buyback burn.
    assert session.pay_with == "XRP"
    assert waited["currency"] == "XRP"
    assert waited["destination"] == xrpl_ops.bot_wallet_address()
    assert waited["expected_amount"] == config.MINT_PRICE_XRP
    assert burned == [(config.TOKEN_CURRENCY_HEX, config.MINT_PRICE_LFGO, config.MINT_PRICE_XRP)]
    assert session.nft_id == "NFTID123"
    assert session.accept_deeplink == "https://xumm.test/sign"
    assert session.image_url == "https://cdn.test/9999/9999_0.png"
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
    a1 = swap_meta.normalize_attributes(
        [{"trait_type": "Eyes", "value": "Laser"}, {"trait_type": "Head", "value": "Crown"}]
    )
    a2 = swap_meta.normalize_attributes(
        [{"trait_type": "Eyes", "value": "Hypno"}, {"trait_type": "Head", "value": "Halo"}]
    )
    n1, n2 = swap_meta.swap_traits(a1, a2, ["Eyes"])
    assert swap_meta.get_attr(n1, "Eyes") == "Hypno"
    assert swap_meta.get_attr(n2, "Eyes") == "Laser"
    assert swap_meta.get_attr(n1, "Head") == "Crown"  # unswapped traits kept
    assert swap_meta.get_attr(n2, "Head") == "Halo"


def test_normalize_nft_and_season():
    meta = {
        "name": "Let's Effing Go! #800",
        "image": "ipfs://cid/800.png",
        "burnCount": 2,
        "attributes": [{"trait_type": "Body", "value": "Ape"}],
    }
    rec = swap_meta.normalize_nft("ID1", meta)
    assert rec["season"] == 2 and rec["gender"] == "ape" and rec["burn_count"] == 2
    assert rec["image"].startswith("https://cid.ipfs.dweb.link/")
    assert swap_meta.normalize_nft("ID2", {"name": "no number"}) is None
    # numbers above the configured collection cap are not swappable
    over_max = config.SWAP_MAX_NFT_NUMBER + 1
    assert swap_meta.normalize_nft("ID3", {"name": f"LFG #{over_max}", "attributes": []}) is None


def test_normalize_nft_tolerates_malformed_metadata():
    # External metadata is untrusted: junk shapes must not raise or leak in.
    rec = swap_meta.normalize_nft(
        "ID4",
        {
            "name": "LFG #5",
            "burnCount": "not-a-number",
            "attributes": [
                "junk",
                {"no_trait_type": 1},
                {"trait_type": "Eyes"},  # missing value
                {"trait_type": "Body", "value": "Ape"},
            ],
        },
    )
    assert rec["burn_count"] == 0
    assert swap_meta.get_attr(rec["attributes"], "Eyes") == "None"
    assert rec["gender"] == "ape"
    # Non-list attributes and non-string name are rejected, not crashed on
    assert swap_meta.normalize_nft("ID5", {"name": "LFG #5", "attributes": {"a": 1}})
    assert swap_meta.normalize_nft("ID6", {"name": 42}) is None


def _swap_session(mutable=(False, False)):
    def nft(i, mut):
        return {
            "nft_id": f"OLD{i}",
            "name": f"Let's Effing Go! #{i}",
            "number": i,
            "season": 1,
            "image": f"https://cdn.test/{i}.png",
            "video": None,
            "burn_count": 0,
            "gender": "male",
            "mutable": mut,
            "uri_hex": f"https://old/{i}.json".encode().hex().upper() if mut else "",
            "attributes": swap_meta.normalize_attributes(
                [
                    {"trait_type": "Eyes", "value": f"Eyes{i}"},
                    {"trait_type": "Body", "value": "Straight Light"},
                ]
            ),
        }

    return swap_flow.SwapSession(
        discord_id="1",
        wallet_address="rTest",
        nft1=nft(10, mutable[0]),
        nft2=nft(20, mutable[1]),
        traits_to_swap=["Eyes"],
    )


def test_swap_session_missing_layers_fails_before_burn(monkeypatch):
    burned = []

    async def fake_burn(nft_id, owner=None, **kwargs):
        burned.append(nft_id)
        return "HASH"

    async def missing(attrs, body, store):
        return ["male/Eyes/Eyes20"]

    monkeypatch.setattr(swap_flow.xrpl_ops, "burn_nft", fake_burn)
    monkeypatch.setattr(swap_flow.layer_store, "get_layer_store", lambda: object())
    monkeypatch.setattr(swap_flow.swap_compose, "missing_layers", missing)

    session = _swap_session()
    asyncio.get_event_loop().run_until_complete(swap_flow.run_swap_session(session))
    assert session.state == swap_flow.FAILED
    assert "Missing trait layer" in session.error
    assert burned == []


def _patch_swap_stubs(
    monkeypatch,
    tmp_path,
    events,
    burn_fails=(),
    mint_fails=(),
    modify_fails=(),
    offer_fails=(),
    exists_results=None,
    fee_paid=True,
    brix_balance=Decimal("100"),
    amm_cost=Decimal("12.5"),
):
    """Stub the swap flow's externals; `events` records on-chain call order.
    `exists_results` maps nft_id -> the tri-state nft_exists answer for the
    #211 guards (default True: present). A scalar answers every call the
    same; a list is consumed one element per call (last element repeats) so
    a test can pass the pre-fee check and trip the burn-time guard — e.g.
    [True, False] = present at session start, vanished by burn time. The
    on-chain index is routed to a temp DB so the post-burn persistence
    (#211) never touches the repo's real onchain_<net>.db."""
    monkeypatch.setenv("ONCHAIN_DB_PATH", str(tmp_path / "onchain_index.db"))
    exists_results = exists_results or {}

    async def fake_exists(nft_id, **kwargs):
        val = exists_results.get(nft_id, True)
        if isinstance(val, list):
            return val.pop(0) if len(val) > 1 else val[0]
        return val

    async def fake_sell_offers(nft_id, raise_on_error=False):
        return []  # #211 landed-offer recheck: nothing landed unless a test overrides

    monkeypatch.setattr(swap_flow.xrpl_ops, "nft_exists", fake_exists)
    monkeypatch.setattr(swap_flow.xrpl_ops, "get_nft_sell_offers", fake_sell_offers)
    # The landed-offer recheck's bounded retry must not slow the suite down.
    monkeypatch.setattr(swap_flow, "_LANDED_OFFER_DELAY_SECONDS", 0)

    async def fake_balance(address, currency, issuer):
        return brix_balance

    async def fake_amm_cost(currency, issuer, amount):
        return amm_cost

    async def fake_buy_and_burn(currency, issuer, value, max_xrp=None):
        events.append(f"burn_fee {value}")
        return "BURNHASH"

    monkeypatch.setattr(swap_flow.xrpl_ops, "get_trustline_balance", fake_balance)
    monkeypatch.setattr(swap_flow.xrpl_ops, "get_amm_xrp_cost", fake_amm_cost)
    monkeypatch.setattr(swap_flow.xrpl_ops, "buy_and_burn", fake_buy_and_burn)

    async def fake_upload(path_on_cdn, data, content_type):
        return f"https://cdn.test/LFGO/{path_on_cdn}"

    async def fake_compose(attrs, body, store, basename, out_dir="generated"):
        p = tmp_path / f"{basename}.png"
        p.write_bytes(b"\x89PNG fake")
        return str(p), False

    async def no_missing(attrs, body, store):
        return []

    async def fake_burn(nft_id, owner=None, **kwargs):
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

    offers = []

    async def fake_offer(nft_id, destination, amount=None, **kwargs):
        assert amount is not None  # swap offers are fee-priced, never free
        if nft_id in offer_fails:
            events.append(f"offer_failed {nft_id}")
            return None
        offers.append(amount)
        events.append(f"offer {nft_id}")
        return f"OFFER_{nft_id}"

    async def fake_accept(offer_id, **kw):
        return {
            "qr_url": "https://xumm.test/qr.png",
            "xumm_url": f"https://xumm.test/{offer_id}",
            "uuid": "u",
        }

    async def fake_modify(nft_id, owner, uri, **kwargs):
        if uri.startswith("https://old/"):  # rollback to the original URI
            events.append(f"revert {nft_id}")
            return "RHASH"
        if nft_id in modify_fails:
            events.append(f"modify_failed {nft_id}")
            return None
        events.append(f"modify {nft_id}")
        return "MHASH"

    async def fake_wait_for_payment(**kwargs):
        events.append(f"fee_requested {kwargs['expected_amount']}")
        return fee_paid

    async def fake_payment_payload(destination, value="1", **kw):
        return {
            "qr_url": "https://xumm.test/qr.png",
            "xumm_url": f"https://xumm.app/sign/PAY_{value}",
            "uuid": "u",
        }

    monkeypatch.setattr(swap_flow.xumm_ops, "create_payment_payload", fake_payment_payload)
    monkeypatch.setattr(swap_flow.xrpl_ops, "modify_nft", fake_modify)
    monkeypatch.setattr(swap_flow.xrpl_ops, "wait_for_payment", fake_wait_for_payment)
    monkeypatch.setattr(swap_flow.xrpl_ops, "bot_wallet_address", lambda: "rBotWallet")
    monkeypatch.setattr(swap_flow.config, "SWAP_RECORDS_DIR", str(tmp_path / "swap_records"))
    monkeypatch.setattr(swap_flow.layer_store, "get_layer_store", lambda: object())
    monkeypatch.setattr(swap_flow.swap_compose, "missing_layers", no_missing)
    monkeypatch.setattr(swap_flow.swap_compose, "compose_nft", fake_compose)
    monkeypatch.setattr(swap_flow, "_upload_swap_file", fake_upload)
    monkeypatch.setattr(swap_flow.xrpl_ops, "burn_nft", fake_burn)
    monkeypatch.setattr(swap_flow.xrpl_ops, "mint_nft", fake_mint)
    monkeypatch.setattr(swap_flow.xrpl_ops, "create_nft_offer", fake_offer)
    monkeypatch.setattr(swap_flow.xumm_ops, "create_accept_offer_payload", fake_accept)
    return offers


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


def test_swap_session_partial_burn_failure_delivers_first_replacement(monkeypatch, tmp_path):
    events = []
    _patch_swap_stubs(monkeypatch, tmp_path, events, burn_fails={"OLD20"})

    session = _swap_session()
    asyncio.get_event_loop().run_until_complete(swap_flow.run_swap_session(session))

    # Original #1 is gone, so its replacement MUST be offered; the second
    # half of the swap is cancelled (replacement burned, original kept).
    assert session.state == swap_flow.FAILED
    assert events == [
        "mint NEW1",
        "mint NEW2",
        "burn OLD10",
        "burn_failed OLD20",
        "burn NEW2",
        "offer NEW1",
    ]
    assert len(session.results) == 1
    assert session.results[0]["nft_id"] == "NEW1"
    assert "still in your wallet" in session.error


# --- #211: post-burn index persistence + stale-pointer guard ---


def _seed_swap_index(tmp_path):
    """Pre-seed the temp on-chain index with the two live originals the
    _swap_session roster rows would have come from."""
    conn = nft_index.init_db(str(tmp_path / "onchain_index.db"))
    try:
        for number in (10, 20):
            nft_index.upsert(
                conn,
                nft_index.OnchainNft(
                    nft_id=f"OLD{number}",
                    nft_number=number,
                    owner="rTest",
                    is_burned=False,
                    mutable=False,
                    uri_hex="",
                    body="male",
                    attributes=[{"trait_type": "Body", "value": "Straight Light"}],
                    image="",
                    ledger_index=100,
                ),
            )
    finally:
        conn.close()


def test_swap_session_offer_failure_still_persists_index(monkeypatch, tmp_path):
    """#211 incident shape: burns land, offer creation reports failure, and
    the landed-offer recheck finds nothing — the session still fails with
    failed_offers, but the index already carries the ledger truth (old
    burned, replacement live at the edition), so the roster can never feed
    the burned old_nft_id into a later session again."""
    events = []
    _patch_swap_stubs(monkeypatch, tmp_path, events, offer_fails={"NEW1"})
    _seed_swap_index(tmp_path)

    session = _swap_session()
    asyncio.get_event_loop().run_until_complete(swap_flow.run_swap_session(session))

    assert session.state == swap_flow.FAILED
    assert "offer failed" in session.error
    conn = nft_index.init_db(str(tmp_path / "onchain_index.db"))
    try:
        # The burn was the point of no return: both editions repointed even
        # though the first offer failed and the second was never attempted.
        assert nft_index.nft_by_number(conn, 10).nft_id == "NEW1"
        assert nft_index.nft_by_number(conn, 20).nft_id == "NEW2"
        burned = {
            row[0] for row in conn.execute("SELECT nft_id FROM onchain_nfts WHERE is_burned=1")
        }
    finally:
        conn.close()
    assert burned == {"OLD10", "OLD20"}
    import json as _json

    record = _json.loads(next((tmp_path / "swap_records").glob("*.json")).read_text())
    assert record["status"] == "failed_offers"


def test_swap_session_stale_pointer_precheck_fails_free(monkeypatch, tmp_path):
    """#211 pre-fee check: a stale pointer known at session start fails the
    session BEFORE any payment, compose, or on-chain work — nothing minted,
    nothing journaled — and heals the index so the next session's roster is
    truthful. This is what makes the refresh-and-retry error honest: no fee
    was consumed, so retrying really is free."""
    events = []
    _patch_swap_stubs(monkeypatch, tmp_path, events, exists_results={"OLD10": False})
    _seed_swap_index(tmp_path)

    session = _swap_session()
    asyncio.get_event_loop().run_until_complete(swap_flow.run_swap_session(session))

    assert session.state == swap_flow.FAILED
    assert "already swapped or replaced" in session.error
    assert events == []  # nothing reached the chain, no fee requested
    conn = nft_index.init_db(str(tmp_path / "onchain_index.db"))
    try:
        stale = conn.execute("SELECT is_burned FROM onchain_nfts WHERE nft_id='OLD10'").fetchone()
        live20 = nft_index.nft_by_number(conn, 20)
    finally:
        conn.close()
    assert stale[0] == 1  # self-healed: the roster stops serving OLD10
    assert live20 is not None and live20.nft_id == "OLD20"  # untouched
    # Nothing on-chain happened, so no journal record was written.
    assert not (tmp_path / "swap_records").exists()


def test_swap_session_stale_pointer_unwinds_and_heals_index(monkeypatch, tmp_path):
    """#211 burn-time guard (the final arbiter): the first original exists at
    the pre-fee check but clio definitively reports it absent by burn time
    (raced by a concurrent swap/burn) — the session unwinds its replacements
    without burning anything of the user's, surfaces the refresh-and-retry
    error, journals stale_pointer, and marks the stale token burned in the
    index so the roster stops offering it."""
    events = []
    _patch_swap_stubs(monkeypatch, tmp_path, events, exists_results={"OLD10": [True, False]})
    _seed_swap_index(tmp_path)

    session = _swap_session()
    asyncio.get_event_loop().run_until_complete(swap_flow.run_swap_session(session))

    assert session.state == swap_flow.FAILED
    assert "already swapped or replaced" in session.error
    # No fee was collected (burn-only swap), so no admin routing — retrying
    # is genuinely free.
    assert "administrator" not in session.error
    # Replacements minted then unwound; the stale original was never
    # burn-attempted (no "burn OLD10" event).
    assert events == ["mint NEW1", "mint NEW2", "burn NEW1", "burn NEW2"]
    conn = nft_index.init_db(str(tmp_path / "onchain_index.db"))
    try:
        stale = conn.execute("SELECT is_burned FROM onchain_nfts WHERE nft_id='OLD10'").fetchone()
        live20 = nft_index.nft_by_number(conn, 20)
    finally:
        conn.close()
    assert stale[0] == 1  # self-healed: the roster stops serving OLD10
    assert live20 is not None and live20.nft_id == "OLD20"  # untouched
    import json as _json

    record = _json.loads(next((tmp_path / "swap_records").glob("*.json")).read_text())
    assert record["status"] == "stale_pointer"


def test_swap_session_stale_second_item_delivers_first(monkeypatch, tmp_path):
    """#211 i!=0 stale-partial branch: the SECOND burn item goes stale
    mid-session after the first original already burned — its replacement
    MUST still reach the user (delivered-pending-offer), the second half
    unwinds, the stale token heals, and the journal gets the distinct
    "stale_pointer_partial" status (never plain "stale_pointer", which is
    reserved for the nothing-delivered full unwind)."""
    events = []
    _patch_swap_stubs(monkeypatch, tmp_path, events, exists_results={"OLD20": [True, False]})
    _seed_swap_index(tmp_path)

    session = _swap_session()
    asyncio.get_event_loop().run_until_complete(swap_flow.run_swap_session(session))

    assert session.state == swap_flow.FAILED
    assert "already swapped or replaced" in session.error
    assert "accept it below" in session.error  # edition 1 IS delivered
    # OLD20 was never burn-attempted; OLD10's swap is final and offered.
    assert events == ["mint NEW1", "mint NEW2", "burn OLD10", "burn NEW2", "offer NEW1"]
    assert len(session.results) == 1
    assert session.results[0]["nft_id"] == "NEW1"
    conn = nft_index.init_db(str(tmp_path / "onchain_index.db"))
    try:
        # Edition 10 repointed by the post-burn persist; OLD20 healed to
        # burned with no live token at the edition (the listener/backfill
        # restores whatever really lives there).
        assert nft_index.nft_by_number(conn, 10).nft_id == "NEW1"
        stale = conn.execute("SELECT is_burned FROM onchain_nfts WHERE nft_id='OLD20'").fetchone()
        assert stale[0] == 1
        assert nft_index.nft_by_number(conn, 20) is None
    finally:
        conn.close()
    import json as _json

    record = _json.loads(next((tmp_path / "swap_records").glob("*.json")).read_text())
    assert record["status"] == "stale_pointer_partial"
    by_number = {n["number"]: n for n in record["nfts"]}
    assert by_number[10]["burn_hash"] == "HASH"  # delivered half, offer live
    assert by_number[10]["offer_id"] == "OFFER_NEW1"
    assert by_number[20]["burn_hash"] is None  # stale half, nothing burned


def test_swap_session_stale_after_fee_routes_to_admin(monkeypatch, tmp_path):
    """#211 + fee fairness: in a mixed swap the modify fee is consumed before
    the burn stage, so a burn-time stale (raced past the pre-fee check) must
    NOT invite a plain retry — that would charge the fee twice for one
    effective swap. The error acknowledges the charged fee and routes to an
    administrator instead."""
    events = []
    _patch_swap_stubs(monkeypatch, tmp_path, events, exists_results={"OLD20": [True, False]})
    _seed_swap_index(tmp_path)

    session = _swap_session(mutable=(True, False))
    asyncio.get_event_loop().run_until_complete(swap_flow.run_swap_session(session))

    assert session.state == swap_flow.FAILED
    assert "already swapped or replaced" in session.error
    assert "fee was charged" in session.error
    assert "administrator" in session.error
    # Fee collected, replacement minted+unwound, modify reverted, no burn.
    assert events == [
        "fee_requested 10",
        "burn_fee 10",
        "mint NEW1",
        "modify OLD10",
        "revert OLD10",
        "burn NEW1",
    ]
    import json as _json

    record = _json.loads(next((tmp_path / "swap_records").glob("*.json")).read_text())
    assert record["status"] == "stale_pointer"  # i==0 full unwind, nothing delivered


def test_swap_session_exists_indeterminate_proceeds(monkeypatch, tmp_path):
    """nft_exists returning None (transient clio blip) must assume-present:
    the burn proceeds exactly as today — a blip never unwinds a session."""
    events = []
    _patch_swap_stubs(monkeypatch, tmp_path, events, exists_results={"OLD10": None, "OLD20": None})

    session = _swap_session()
    asyncio.get_event_loop().run_until_complete(swap_flow.run_swap_session(session))

    assert session.state == swap_flow.OFFERS_READY
    assert events[:4] == ["mint NEW1", "mint NEW2", "burn OLD10", "burn OLD20"]


# --- Dynamic NFTs (NFTokenModify path) ---


def test_normalize_nft_carries_mutability():
    meta = {"name": "LFG #800", "attributes": []}
    mutable = swap_meta.normalize_nft("ID1", meta, flags=0x18, uri_hex="AB")
    burnable = swap_meta.normalize_nft("ID2", meta, flags=0x9)
    assert mutable["mutable"] is True and mutable["uri_hex"] == "AB"
    assert burnable["mutable"] is False


def test_swap_fee_total():
    assert swap_flow.swap_fee_total(1) == "10"
    assert swap_flow.swap_fee_total(2) == "20"


def test_detect_swap_payment_paths(monkeypatch):
    """BRIX holders pay BRIX; everyone else the AMM XRP quote (+ buffer);
    no path at all (no BRIX, no AMM quote) raises."""
    state = {}

    async def fake_balance(address, currency, issuer):
        return state["balance"]

    async def fake_amm(currency, issuer, amount):
        return state["amm"]

    monkeypatch.setattr(swap_flow.xrpl_ops, "get_trustline_balance", fake_balance)
    monkeypatch.setattr(swap_flow.xrpl_ops, "get_amm_xrp_cost", fake_amm)
    loop = asyncio.get_event_loop()

    state.update(balance=Decimal("20"), amm=None)
    assert loop.run_until_complete(swap_flow.detect_swap_payment("rW", "20")) == ("BRIX", "20")

    state.update(balance=Decimal("5"), amm=Decimal("12.5"))  # insufficient BRIX
    pay_with, amount = loop.run_until_complete(swap_flow.detect_swap_payment("rW", "20"))
    assert pay_with == "XRP"
    assert Decimal(amount) == Decimal("12.5") * Decimal(config.SWAP_XRP_FEE_BUFFER)

    state.update(balance=None, amm=None)
    with pytest.raises(RuntimeError, match="unavailable"):
        loop.run_until_complete(swap_flow.detect_swap_payment("rW", "20"))


def test_swap_session_xrp_path_prices_fee_and_offers(monkeypatch, tmp_path):
    """No BRIX trustline: the modify fee is charged in XRP (AMM quote), the
    BRIX is bought + burned, and replacement offers are priced in drops."""
    events = []
    offers = _patch_swap_stubs(
        monkeypatch, tmp_path, events, brix_balance=None, amm_cost=Decimal("12.5")
    )

    session = _swap_session(mutable=(True, False))
    asyncio.get_event_loop().run_until_complete(swap_flow.run_swap_session(session))

    assert session.state == swap_flow.OFFERS_READY
    assert session.pay_with == "XRP"
    # 12.5 XRP for 20 BRIX, x1.05 buffer = 13.125 total -> 6.5625 per NFT
    assert session.fee_amount == "6.562500"
    assert events == [
        "fee_requested 6.562500",
        "burn_fee 10",
        "mint NEW1",
        "modify OLD10",
        "burn OLD20",
        "offer NEW1",
    ]
    assert offers == ["6562500"]  # drops string, not a BRIX amount


def test_payment_link_supports_custom_currency():
    import json as _json

    brix = "4252495800000000000000000000000000000000"
    link = xumm_ops.generate_static_payment_link(
        "rDest", value="20", currency=brix, issuer="rBrixIssuer"
    )
    tx = _json.loads(bytes.fromhex(link.split("/detect/")[1]))
    assert tx["Amount"] == {"currency": brix, "value": "20", "issuer": "rBrixIssuer"}


def test_swap_session_both_mutable_modifies_in_place(monkeypatch, tmp_path):
    events = []
    _patch_swap_stubs(monkeypatch, tmp_path, events)

    session = _swap_session(mutable=(True, True))
    asyncio.get_event_loop().run_until_complete(swap_flow.run_swap_session(session))

    assert session.state == swap_flow.OFFERS_READY
    # Fee charged upfront (2 × 10 BRIX); no mint/burn/offer at all
    assert events == ["fee_requested 20", "burn_fee 20", "modify OLD10", "modify OLD20"]
    assert session.fee_amount == "20"
    # Fee QR is a real XUMM sign request, not the unscannable detect link
    assert session.payment_link == "https://xumm.app/sign/PAY_20"
    assert all(r["modified"] for r in session.results)
    # NFTokenModify keeps the token IDs
    assert {r["nft_id"] for r in session.results} == {"OLD10", "OLD20"}
    assert all("accept_deeplink" not in r for r in session.results)


def test_swap_session_mixed_mints_modifies_then_burns(monkeypatch, tmp_path):
    events = []
    _patch_swap_stubs(monkeypatch, tmp_path, events)

    session = _swap_session(mutable=(True, False))
    asyncio.get_event_loop().run_until_complete(swap_flow.run_swap_session(session))

    assert session.state == swap_flow.OFFERS_READY
    # Reversible steps first, the irreversible burn last
    assert events == [
        "fee_requested 10",
        "burn_fee 10",
        "mint NEW1",
        "modify OLD10",
        "burn OLD20",
        "offer NEW1",
    ]
    modified = [r for r in session.results if r["modified"]]
    offered = [r for r in session.results if not r["modified"]]
    assert modified[0]["nft_id"] == "OLD10"
    assert offered[0]["nft_id"] == "NEW1"


def test_swap_session_payment_timeout_touches_nothing(monkeypatch, tmp_path):
    events = []
    _patch_swap_stubs(monkeypatch, tmp_path, events, fee_paid=False)

    session = _swap_session(mutable=(True, True))
    asyncio.get_event_loop().run_until_complete(swap_flow.run_swap_session(session))

    assert session.state == swap_flow.PAYMENT_TIMEOUT
    assert events == ["fee_requested 20"]  # nothing reached the chain


def test_swap_session_modify_failure_reverts(monkeypatch, tmp_path):
    events = []
    _patch_swap_stubs(monkeypatch, tmp_path, events, modify_fails={"OLD20"})

    session = _swap_session(mutable=(True, True))
    asyncio.get_event_loop().run_until_complete(swap_flow.run_swap_session(session))

    assert session.state == swap_flow.FAILED
    assert "untouched" in session.error
    assert events == [
        "fee_requested 20",
        "burn_fee 20",
        "modify OLD10",
        "modify_failed OLD20",
        "revert OLD10",
    ]


def test_swap_session_burn_failure_after_modify_unwinds_all(monkeypatch, tmp_path):
    events = []
    _patch_swap_stubs(monkeypatch, tmp_path, events, burn_fails={"OLD20"})

    session = _swap_session(mutable=(True, False))
    asyncio.get_event_loop().run_until_complete(swap_flow.run_swap_session(session))

    # The modify is rolled back and the orphaned replacement burned: the
    # user keeps both originals exactly as they were.
    assert session.state == swap_flow.FAILED
    assert events == [
        "fee_requested 10",
        "burn_fee 10",
        "mint NEW1",
        "modify OLD10",
        "burn_failed OLD20",
        "revert OLD10",
        "burn NEW1",
    ]
    assert session.results == []


# --- Unified layer store ---


def _make_layer_tree(root):
    for gender, traits_ in (
        ("male", {"Background": ["Blue"], "Body": ["Straight Light"], "Eyes": ["Laser", "Hypno"]}),
        ("ape", {"Background": ["Red"], "Body": ["Ape"]}),
    ):
        for trait, values in traits_.items():
            d = root / gender / trait
            d.mkdir(parents=True)
            for v in values:
                (d / f"{v}.png").write_bytes(b"\x89PNG fake")


def test_local_layer_store(tmp_path):
    _make_layer_tree(tmp_path)
    store = layer_store.LocalLayerStore(str(tmp_path))
    loop = asyncio.get_event_loop()
    assert loop.run_until_complete(store.list_bodies()) == ["ape", "male"]
    assert loop.run_until_complete(store.list_values("male", "Eyes")) == ["Hypno", "Laser"]
    path = loop.run_until_complete(store.resolve("male", "Eyes", "Laser"))
    assert path and path.endswith("Eyes/Laser.png")
    assert loop.run_until_complete(store.resolve("male", "Eyes", "Nope")) is None


def test_select_random_attributes_from_store(tmp_path):
    _make_layer_tree(tmp_path)
    store = layer_store.LocalLayerStore(str(tmp_path))
    loop = asyncio.get_event_loop()
    gender, attrs = loop.run_until_complete(traits.select_random_attributes(store, body="male"))
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


# --- XRPL_NETWORK flag: one switch for endpoints + collection/BRIX issuers ---


def _reload_config(monkeypatch, network):
    import importlib

    # .env must not leak back in when config re-runs load_dotenv()
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)
    for var in ("XRPL_JSON_RPC_URL", "XRPL_WS_URL", "SWAP_ISSUER_ADDRESS", "SWAP_OFFER_ISSUER"):
        monkeypatch.delenv(var, raising=False)
    # These reloads flip XRPL_NETWORK (incl. to mainnet) to check network URL
    # defaults, and are economy-agnostic. The suite conftest force-enables the
    # economy on testnet, so leaving ECONOMY_ENABLED=1 while the reload moves
    # XRPL_NETWORK to mainnet would trip validate_economy_config's network-match
    # assertion. Clear it so the reload uses the opt-in default (off).
    monkeypatch.delenv("ECONOMY_ENABLED", raising=False)
    monkeypatch.setenv("XRPL_NETWORK", network)
    return importlib.reload(config)


def test_xrpl_network_flag_testnet_uses_seed_wallet(monkeypatch):
    from xrpl.wallet import Wallet

    try:
        cfg = _reload_config(monkeypatch, "testnet")
        seed_addr = Wallet.from_seed(cfg.SEED).classic_address
        assert cfg.IS_TESTNET is True
        assert "altnet" in cfg.JSON_RPC_URL and "altnet" in cfg.WS_URL
        assert cfg.SWAP_ISSUER_ADDRESS == seed_addr
        assert cfg.SWAP_OFFER_ISSUER == seed_addr
    finally:
        monkeypatch.undo()
        import importlib

        importlib.reload(config)


def test_invalid_seed_on_testnet_raises_clear_error(monkeypatch):
    try:
        monkeypatch.setenv("SEED", "not-a-valid-seed")
        with pytest.raises(ValueError, match="SEED"):
            _reload_config(monkeypatch, "testnet")
    finally:
        monkeypatch.undo()
        import importlib

        importlib.reload(config)


def test_xrpl_network_flag_defaults_to_mainnet_addresses(monkeypatch):
    try:
        for network in ("mainnet", ""):  # flag off / unset → mainnet
            if network:
                cfg = _reload_config(monkeypatch, network)
            else:
                monkeypatch.delenv("XRPL_NETWORK", raising=False)
                cfg = _reload_config(monkeypatch, "")
                monkeypatch.delenv("XRPL_NETWORK", raising=False)
                import importlib

                cfg = importlib.reload(config)
            assert cfg.IS_TESTNET is False
            assert "altnet" not in cfg.JSON_RPC_URL
            assert cfg.SWAP_ISSUER_ADDRESS == "rLfgoMintj3KBcs4s2XKtquvDwEte2kYfJ"
            assert cfg.SWAP_OFFER_ISSUER == "rLfgoBriX5ZaMP32mtc7RUZJcjnisKh2Px"
    finally:
        monkeypatch.undo()
        import importlib

        importlib.reload(config)


def test_explicit_env_overrides_beat_network_flag(monkeypatch):
    try:
        monkeypatch.setenv("SWAP_OFFER_ISSUER", "rCustomIssuer111111111111111111111")
        cfg = _reload_config(monkeypatch, "testnet")
        # _reload_config cleared it; set and reload again to assert precedence
        monkeypatch.setenv("SWAP_OFFER_ISSUER", "rCustomIssuer111111111111111111111")
        import importlib

        cfg = importlib.reload(config)
        assert cfg.SWAP_OFFER_ISSUER == "rCustomIssuer111111111111111111111"
    finally:
        monkeypatch.undo()
        import importlib

        importlib.reload(config)


# --- same-origin image proxy (Activity CSP blocks cross-origin <img> loads) ---


def _img_request(query):
    from aiohttp.test_utils import make_mocked_request

    return make_mocked_request("GET", f"/api/img?{query}")


def test_img_proxy_route_registered():
    app = server.create_app()
    paths = {getattr(r.resource, "canonical", "") for r in app.router.routes()}
    assert "/api/img" in paths


def test_img_proxy_rejects_missing_and_foreign_urls():
    loop = asyncio.get_event_loop()
    from urllib.parse import quote

    for q in (
        "",
        "u=" + quote("https://evil.example/x.png", safe=""),
        # same prefix as the CDN base but a different host
        "u=" + quote(config.BUNNY_CDN_PUBLIC_BASE + ".evil.example/x.png", safe=""),
    ):
        resp = loop.run_until_complete(server.handle_img(_img_request(q)))
        assert resp.status == 400, f"query {q!r} should be rejected"


def test_img_proxy_streams_allowed_cdn_url(monkeypatch):
    fetched = []

    async def fake_fetch(url):
        fetched.append(url)
        return b"\x89PNG fake", "image/png"

    monkeypatch.setattr(server, "_fetch_cdn", fake_fetch)
    from urllib.parse import quote

    url = config.BUNNY_CDN_PUBLIC_BASE + "/LFGO/123.png"
    loop = asyncio.get_event_loop()
    resp = loop.run_until_complete(server.handle_img(_img_request("u=" + quote(url, safe=""))))
    assert resp.status == 200
    assert resp.body == b"\x89PNG fake"
    assert resp.content_type == "image/png"
    # images are immutable; they must opt out of the global no-store middleware
    assert "no-store" not in resp.headers.get("Cache-Control", "")
    assert fetched == [url]


def test_img_proxy_accepts_pull_zone_host(monkeypatch):
    """Legacy NFT metadata bakes in the BUNNY_PULL_ZONE custom domain (e.g.
    nft.letseffinggo.com) instead of BUNNY_CDN_PUBLIC_BASE; both point at the
    same pull zone and both must be proxyable."""

    async def fake_fetch(url):
        return b"\x89PNG fake", "image/png"

    monkeypatch.setattr(server, "_fetch_cdn", fake_fetch)
    from urllib.parse import quote

    url = "https://nft.pullzone.example/LFGO/3545/3545.png"
    loop = asyncio.get_event_loop()
    resp = loop.run_until_complete(server.handle_img(_img_request("u=" + quote(url, safe=""))))
    assert resp.status == 200
    # and a look-alike of the pull zone is still rejected
    bad = "https://nft.pullzone.example.evil.test/x.png"
    resp = loop.run_until_complete(server.handle_img(_img_request("u=" + quote(bad, safe=""))))
    assert resp.status == 400


def test_img_proxy_cdn_error_is_502(monkeypatch):
    async def fake_fetch(url):
        raise RuntimeError("CDN unreachable")

    monkeypatch.setattr(server, "_fetch_cdn", fake_fetch)
    from urllib.parse import quote

    url = config.BUNNY_CDN_PUBLIC_BASE + "/LFGO/123.png"
    loop = asyncio.get_event_loop()
    resp = loop.run_until_complete(server.handle_img(_img_request("u=" + quote(url, safe=""))))
    assert resp.status == 502


def test_no_cache_middleware_respects_handler_cache_header():
    """no_cache_mw must not clobber a Cache-Control the handler set (the image
    proxy marks responses cacheable)."""
    from aiohttp.test_utils import make_mocked_request

    async def handler(request):
        return web.Response(text="x", headers={"Cache-Control": "public, max-age=60"})

    loop = asyncio.get_event_loop()
    req = make_mocked_request("GET", "/x")
    resp = loop.run_until_complete(server.no_cache_mw(req, handler))
    assert resp.headers["Cache-Control"] == "public, max-age=60"


def test_economy_config_defaults():
    from lfg_core import config

    assert config.ECONOMY_NETWORK in ("testnet", "mainnet")
    assert isinstance(config.WEBAPP_DEV_MODE, bool)


def test_layer_route_registered():
    app = server.create_app()
    paths = {getattr(r.resource, "canonical", "") for r in app.router.routes()}
    assert "/api/layer" in paths


def test_layer_handler_bad_params(monkeypatch):
    from aiohttp.test_utils import make_mocked_request

    req = make_mocked_request("GET", "/api/layer")  # no query
    resp = asyncio.get_event_loop().run_until_complete(server.handle_layer(req))
    assert resp.status == 400


def test_economy_routes_registered():
    app = server.create_app()
    paths = {getattr(r.resource, "canonical", "") for r in app.router.routes()}
    for expected in [
        "/api/economy",
        "/api/equip",
        "/api/equip/{session_id}",
        "/api/harvest",
        "/api/harvest/{session_id}",
        "/api/assemble",
        "/api/assemble/{session_id}",
    ]:
        assert expected in paths, f"missing route {expected}"


@pytest.mark.filterwarnings("ignore::aiohttp.web_exceptions.NotAppKeyWarning")
def test_economy_dev_mode_read(monkeypatch):
    from aiohttp.test_utils import make_mocked_request

    from webapp import mock_economy

    monkeypatch.setattr(server.config, "WEBAPP_DEV_MODE", True)
    # require_wallet is bypassed in dev mode; handler reads the dev owner.
    req = make_mocked_request("GET", "/api/economy")
    req["user"] = {"id": "dev", "name": "dev"}
    req["wallet"] = mock_economy.DEV_OWNER
    resp = asyncio.get_event_loop().run_until_complete(server.handle_economy(req))
    assert resp.status == 200


def test_require_auth_dev_bypass(monkeypatch):
    from aiohttp.test_utils import make_mocked_request

    monkeypatch.setattr(server.config, "WEBAPP_DEV_MODE", True)

    @server.require_wallet
    async def probe(request):
        return server.web.json_response({"wallet": request["wallet"]})

    req = make_mocked_request("GET", "/x")  # no Authorization header
    resp = asyncio.get_event_loop().run_until_complete(probe(req))
    assert resp.status == 200


def test_config_reports_dev_mode(monkeypatch):
    from aiohttp.test_utils import make_mocked_request

    monkeypatch.setattr(server.config, "WEBAPP_DEV_MODE", True)
    req = make_mocked_request("GET", "/api/config")
    resp = asyncio.get_event_loop().run_until_complete(server.handle_config(req))
    import json

    assert json.loads(resp.body)["dev_mode"] is True


def test_config_reports_public_share_base_url_when_set(monkeypatch):
    # #41 T9: the client must learn PUBLIC_SHARE_BASE_URL through this same
    # existing /api/config delivery path — never from location.origin, which
    # inside the Activity is Discord's *.discordsays.com sandbox proxy, not
    # our public host.
    from aiohttp.test_utils import make_mocked_request

    monkeypatch.setattr(server.config, "PUBLIC_SHARE_BASE_URL", "https://share.example/lfg")
    req = make_mocked_request("GET", "/api/config")
    resp = asyncio.get_event_loop().run_until_complete(server.handle_config(req))
    import json

    assert json.loads(resp.body)["public_share_base_url"] == "https://share.example/lfg"


def test_config_reports_empty_public_share_base_url_when_unset(monkeypatch):
    from aiohttp.test_utils import make_mocked_request

    monkeypatch.setattr(server.config, "PUBLIC_SHARE_BASE_URL", "")
    req = make_mocked_request("GET", "/api/config")
    resp = asyncio.get_event_loop().run_until_complete(server.handle_config(req))
    import json

    assert json.loads(resp.body)["public_share_base_url"] == ""


def test_config_reports_bithomp_base_url_mainnet(monkeypatch):
    # Client-side fallback (no PUBLIC_SHARE_BASE_URL configured) needs a
    # bithomp NFT-page base without the client having to know XRPL_NETWORK
    # itself — the server hands over the already-network-resolved base.
    from aiohttp.test_utils import make_mocked_request

    monkeypatch.setattr(server.config, "IS_TESTNET", False)
    req = make_mocked_request("GET", "/api/config")
    resp = asyncio.get_event_loop().run_until_complete(server.handle_config(req))
    import json

    assert json.loads(resp.body)["bithomp_base_url"] == "https://bithomp.com"


def test_config_reports_bithomp_base_url_testnet(monkeypatch):
    from aiohttp.test_utils import make_mocked_request

    monkeypatch.setattr(server.config, "IS_TESTNET", True)
    req = make_mocked_request("GET", "/api/config")
    resp = asyncio.get_event_loop().run_until_complete(server.handle_config(req))
    import json

    assert json.loads(resp.body)["bithomp_base_url"] == "https://test.bithomp.com"


def test_dev_reload_route_404_when_off(monkeypatch):
    from aiohttp.test_utils import make_mocked_request

    monkeypatch.setattr(server.config, "WEBAPP_DEV_MODE", False)
    req = make_mocked_request("GET", "/__dev/reload")
    resp = asyncio.get_event_loop().run_until_complete(server.handle_dev_reload(req))
    assert resp.status == 404


def test_client_dir_mtime_is_float():
    assert isinstance(server._client_dir_mtime(), float)


# --- Fix 3 regression: missing body field → 400, not 502 ---


@pytest.mark.filterwarnings("ignore::aiohttp.web_exceptions.NotAppKeyWarning")
def test_equip_missing_body_field_returns_400(monkeypatch):
    """Regression: a POST body missing required fields (e.g. nft_id) must
    return 400 (bad request), not 502.  Exercises the new (KeyError, ValueError)
    catch in _economy_post for the non-dev code path."""
    from aiohttp.test_utils import make_mocked_request

    # Force non-dev mode so the real start_coro path is exercised.
    monkeypatch.setattr(server.config, "WEBAPP_DEV_MODE", False)

    # The lambda for handle_equip_start accesses b["nft_id"], b["slot"], b["value"].
    # Sending an empty body dict will raise KeyError on b["nft_id"] before any
    # DB/network call, so no other stubs are needed.
    req = make_mocked_request("POST", "/api/equip")
    req["user"] = {"id": "u1", "name": "test"}
    req["wallet"] = "rOwner"

    async def empty_json():
        return {}  # missing nft_id, slot, value

    req.json = empty_json  # type: ignore[method-assign]

    loop = asyncio.get_event_loop()
    resp = loop.run_until_complete(server.handle_equip_start(req))
    assert resp.status == 400, f"expected 400, got {resp.status}"
    import json

    body = json.loads(resp.body)
    assert "missing or invalid field" in body.get("error", "")
