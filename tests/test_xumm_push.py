# tests/test_xumm_push.py
# XUMM push delivery via user_token (issue #135): payload builders must forward
# a stored user_token as a top-level payload field so XUMM push-delivers the
# sign request to the user's Xaman app; the create response's `pushed` flag and
# the payload-status `issued_user_token` must be surfaced to callers. A missing
# token must never appear in the request body (so an empty string can't be
# misread as a token) and never block the flow.
#
# Env-guard preamble: importing lfg_core.config freezes its constants at import
# time; set the same defaults test_smoke.py uses so collection order can't
# strand them. (Copy the block verbatim from tests/test_market_ops.py.)
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

import pytest  # noqa: E402

from lfg_core import xumm_ops  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _CreateResp:
    def __init__(self, pushed=False):
        self._pushed = pushed

    def json(self):
        return {
            "refs": {"qr_png": "q"},
            "next": {"always": "n"},
            "uuid": "u",
            "pushed": self._pushed,
        }


def _capture_post(monkeypatch, pushed=False):
    captured = {}

    def fake_post(url, json, headers, timeout):
        captured["payload"] = json
        return _CreateResp(pushed=pushed)

    monkeypatch.setattr(xumm_ops.requests, "post", fake_post)
    return captured


# --- create-side: user_token is sent, and only when present ---


def test_user_token_sent_as_top_level_field(monkeypatch):
    captured = _capture_post(monkeypatch)
    _run(xumm_ops.create_accept_offer_payload("OFFER1", user_token="tok-123"))
    # user_token is a top-level payload field, NOT nested under options/txjson.
    assert captured["payload"]["user_token"] == "tok-123"
    assert "user_token" not in captured["payload"]["txjson"]


def test_no_user_token_omits_the_field(monkeypatch):
    captured = _capture_post(monkeypatch)
    _run(xumm_ops.create_accept_offer_payload("OFFER1"))
    assert "user_token" not in captured["payload"]


def test_empty_user_token_omits_the_field(monkeypatch):
    # An empty string must never be sent — XUMM would reject/ignore it and it
    # signals "no token" just like None.
    captured = _capture_post(monkeypatch)
    _run(xumm_ops.create_accept_offer_payload("OFFER1", user_token=""))
    assert "user_token" not in captured["payload"]


@pytest.mark.parametrize(
    "call",
    [
        lambda: xumm_ops.create_payment_payload("rDest", value="1", user_token="tok"),
        lambda: xumm_ops.create_accept_offer_payload("OFFER1", user_token="tok"),
        lambda: xumm_ops.create_sell_offer_payload("rAcct", "NFT1", "1000000", user_token="tok"),
        lambda: xumm_ops.create_cancel_offer_payload("rAcct", "OFFERIDX", user_token="tok"),
    ],
)
def test_every_builder_forwards_user_token(monkeypatch, call):
    captured = _capture_post(monkeypatch)
    _run(call())
    assert captured["payload"]["user_token"] == "tok"
    # SourceTag discipline is unchanged by push delivery.
    assert captured["payload"]["txjson"]["SourceTag"] == xumm_ops.config.SOURCE_TAG


def test_create_returns_pushed_true(monkeypatch):
    _capture_post(monkeypatch, pushed=True)
    result = _run(xumm_ops.create_accept_offer_payload("OFFER1", user_token="tok"))
    assert result["pushed"] is True
    # QR/deep link are always present as the universal fallback.
    assert result["qr_url"] == "q"
    assert result["xumm_url"] == "n"


def test_create_returns_pushed_false_when_absent(monkeypatch):
    # A stale token yields pushed:false (or no key at all) → caller falls back.
    _capture_post(monkeypatch, pushed=False)
    result = _run(xumm_ops.create_accept_offer_payload("OFFER1", user_token="stale"))
    assert result["pushed"] is False


# --- status-side: issued_user_token is captured ---


class _StatusResp:
    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


_UUID = "11111111-2222-3333-4444-555555555555"


def _capture_status(monkeypatch, body):
    monkeypatch.setattr(xumm_ops.requests, "get", lambda url, headers, timeout: _StatusResp(body))


def test_status_surfaces_issued_user_token(monkeypatch):
    _capture_status(
        monkeypatch,
        {
            "meta": {"signed": True, "opened": True, "expired": False},
            "response": {"account": "rSIGNER", "txid": "HASH"},
            "application": {"issued_user_token": "issued-tok-abc"},
        },
    )
    s = _run(xumm_ops.get_payload_status(_UUID))
    assert s["user_token"] == "issued-tok-abc"
    assert s["signed"] is True
    assert s["account"] == "rSIGNER"


def test_status_user_token_none_when_no_application_block(monkeypatch):
    _capture_status(
        monkeypatch,
        {"meta": {"signed": False}, "response": {}},
    )
    s = _run(xumm_ops.get_payload_status(_UUID))
    assert s["user_token"] is None


# --- #212: UI-facing push state on the create response ---


def test_push_state_sent_when_pushed(monkeypatch):
    _capture_post(monkeypatch, pushed=True)
    result = _run(xumm_ops.create_accept_offer_payload("OFFER1", user_token="tok"))
    assert result["push"] == "sent"


def test_push_state_failed_when_token_sent_but_not_pushed(monkeypatch):
    # The #212 failure mode: XUMM accepts the token as valid but its push
    # attempt no-ops. The payload still shows in Xaman's Events list, so the
    # UI needs to distinguish this from a plain no-token QR sign.
    _capture_post(monkeypatch, pushed=False)
    result = _run(xumm_ops.create_accept_offer_payload("OFFER1", user_token="tok"))
    assert result["push"] == "failed"


def test_push_state_none_without_token(monkeypatch):
    _capture_post(monkeypatch, pushed=False)
    result = _run(xumm_ops.create_accept_offer_payload("OFFER1"))
    assert result["push"] is None


# --- #212: a bad stored token must never block payload creation ---


def test_create_retries_without_token_on_failure(monkeypatch):
    """First POST (with user_token) blows up; the retry must go out WITHOUT
    the token and its result is returned as a plain QR sign (push=None)."""
    attempts = []

    def fake_post(url, json, headers, timeout):
        attempts.append(dict(json))
        if "user_token" in json:
            raise RuntimeError("XUMM rejected the token")
        return _CreateResp(pushed=False)

    monkeypatch.setattr(xumm_ops.requests, "post", fake_post)
    result = _run(xumm_ops.create_accept_offer_payload("OFFER1", user_token="bad-tok"))
    assert result is not None
    assert result["push"] is None
    assert len(attempts) == 2
    assert "user_token" in attempts[0] and "user_token" not in attempts[1]


def test_create_returns_none_when_tokenless_create_fails(monkeypatch):
    # No token → no retry: a hard XUMM failure is still a hard failure.
    attempts = []

    def fake_post(url, json, headers, timeout):
        attempts.append(dict(json))
        raise RuntimeError("XUMM down")

    monkeypatch.setattr(xumm_ops.requests, "post", fake_post)
    assert _run(xumm_ops.create_accept_offer_payload("OFFER1")) is None
    assert len(attempts) == 1


def test_create_does_not_retry_on_timeout(monkeypatch):
    # A timeout is AMBIGUOUS — XUMM may have already created (and pushed) the
    # payload. Retrying would mint a duplicate the user could sign while the
    # flow polls the other uuid, so a timeout fails outright.
    attempts = []

    def fake_post(url, json, headers, timeout):
        attempts.append(dict(json))
        raise xumm_ops.requests.Timeout("read timed out")

    monkeypatch.setattr(xumm_ops.requests, "post", fake_post)
    assert _run(xumm_ops.create_accept_offer_payload("OFFER1", user_token="tok")) is None
    assert len(attempts) == 1


# --- #212: flows capture the rotated token off signed payloads ---


def _signed_status(account="rWALLET", user_token="fresh-tok", txid="HASH"):
    return {
        "opened": True,
        "signed": True,
        "expired": False,
        "account": account,
        "txid": txid,
        "user_token": user_token,
    }


def test_cancel_session_captures_issued_token(monkeypatch):
    from lfg_core import market_flow

    session = market_flow.CancelSession(
        discord_id="u1", wallet_address="rWALLET", offer_index="OFF", network="testnet"
    )
    session.payload_uuid = _UUID

    async def fake_status(uuid):
        return _signed_status()

    assert _run(market_flow.advance_cancel_session(session, get_payload_status=fake_status))
    assert session.issued_user_token == "fresh-tok"


def test_capture_skips_foreign_signer(monkeypatch):
    # A shared QR signed by a DIFFERENT wallet must never overwrite this
    # user's stored token.
    from lfg_core import market_flow

    session = market_flow.CancelSession(
        discord_id="u1", wallet_address="rWALLET", offer_index="OFF", network="testnet"
    )
    session.payload_uuid = _UUID

    async def fake_status(uuid):
        return _signed_status(account="rSOMEONE_ELSE")

    _run(market_flow.advance_cancel_session(session, get_payload_status=fake_status))
    assert session.issued_user_token is None


def test_mint_accept_poll_captures_issued_token(monkeypatch):
    from lfg_core import mint_flow

    session = mint_flow.MintSession(discord_id="u1", wallet_address="rWALLET")
    session.state = mint_flow.OFFER_READY
    session.accept_uuid = _UUID

    async def fake_status(uuid):
        return _signed_status()

    monkeypatch.setattr(mint_flow.xumm_ops, "get_payload_status", fake_status)
    _run(mint_flow.update_scan_state(session))
    assert session.accept_signed is True
    assert session.issued_user_token == "fresh-tok"
    # The session's own token is refreshed too, so the LATER accept payload of
    # the same session is already built with the rotated token.
    assert session.push_user_token == "fresh-tok"


def test_mint_payment_poll_continues_past_opened(monkeypatch):
    """The payment payload must keep being polled after `opened` — the
    signature (and the rotated token it carries) lands later. Polling stops
    only once signed (or the session leaves AWAITING_PAYMENT)."""
    from lfg_core import mint_flow

    session = mint_flow.MintSession(discord_id="u1", wallet_address="rWALLET")
    session.payment_uuid = _UUID
    statuses = [
        {
            "opened": True,
            "signed": False,
            "expired": False,
            "account": None,
            "txid": None,
            "user_token": None,
        },
        _signed_status(),
    ]

    async def fake_status(uuid):
        return statuses.pop(0)

    monkeypatch.setattr(mint_flow.xumm_ops, "get_payload_status", fake_status)
    _run(mint_flow.update_scan_state(session))  # opened only
    assert session.qr_scanned is True and session.payment_signed is False
    assert session.issued_user_token is None
    _run(mint_flow.update_scan_state(session))  # signed → capture
    assert session.payment_signed is True
    assert session.issued_user_token == "fresh-tok"
    assert not statuses  # both polls actually hit the (fake) API


def test_trait_sell_list_payload_gets_push_token(monkeypatch):
    """The trait-sell wizard's List step (signature 2) must forward the stored
    push token — it was the last QR-only Activity payload site."""
    from lfg_core import market_flow

    class _ExtractDone:
        state = "done"
        error = None
        nft_id = "N" * 64
        accept = {"qr_url": "q", "xumm_url": "x", "uuid": _UUID, "push": "sent"}

    session = market_flow.TraitSellSession(
        discord_id="u1",
        wallet_address="rWALLET",
        slot="Hat",
        value="Wizard Hat",
        amount_drops=1_000_000,
        extract_session=_ExtractDone(),
        push_user_token="stored-tok",
    )
    # EXTRACT_PENDING -> EXTRACT_DONE picks up the accept payload's push state.
    _run(market_flow.advance_trait_sell_session(session))
    assert session.state == market_flow.EXTRACT_DONE
    assert session.extract_push == "sent"

    captured = {}

    async def fake_status(uuid):
        return _signed_status()

    async def fake_sell_payload(wallet, nft_id, drops, user_token=None, platform=None):
        captured["user_token"] = user_token
        return {"qr_url": "q2", "xumm_url": "x2", "uuid": _UUID, "push": "sent"}

    _run(
        market_flow.advance_trait_sell_session(
            session,
            get_payload_status=fake_status,
            create_sell_offer_payload=fake_sell_payload,
        )
    )
    assert session.state == market_flow.LIST_PENDING
    # Signature 1's signed status rotated the token; signature 2's payload must
    # already use the FRESH one, not the stale stored one.
    assert captured["user_token"] == "fresh-tok"
    assert session.push_user_token == "fresh-tok"
    assert session.list_push == "sent"
    # ...and the rotated token is staged for the service to persist.
    assert session.issued_user_token == "fresh-tok"
