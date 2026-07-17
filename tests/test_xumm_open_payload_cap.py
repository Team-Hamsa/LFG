# XUMM open-payload cap (2026-07-17 incident): the platform rejected every
# payload create with HTTP 400 {"error": {"code": 429, "message": "Max
# payloads of N exceeded"}} because ~95 unsigned payloads with no expiry had
# accumulated. Fixes under test: (1) that embedded-429 body is treated as
# rate limiting (cooldown, no token-less retry), (2) every payload builder
# sets an expire so abandoned payloads drain instead of piling up for 24 h,
# (3) a cancel helper + uuid logging so a future backlog can be cleared.

import asyncio
import os

os.environ.setdefault("BUNNY_PULL_ZONE", "test")
os.environ.setdefault("LAYER_SOURCE", "local")

from lfg_core import xumm_ops


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _Resp:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code = status
        self._body = body or {}
        self.headers = headers or {}

    def json(self):
        return self._body


_CREATE_OK = {
    "refs": {"qr_png": "q"},
    "next": {"always": "n"},
    "uuid": "11111111-2222-3333-4444-555555555555",
    "pushed": False,
}

_MAX_PAYLOADS_BODY = {
    "error": {"reference": "x", "code": 429, "message": "Max payloads of 93 exceeded"}
}


def _reset_cooldown(monkeypatch):
    monkeypatch.setattr(xumm_ops, "_rate_limited_until", 0.0)


# --- embedded-429 ("Max payloads") handling ---------------------------------


def test_max_payloads_400_treated_as_rate_limited(monkeypatch):
    _reset_cooldown(monkeypatch)
    calls = []

    def fake_post(url, json, headers, timeout):
        calls.append(json)
        return _Resp(status=400, body=_MAX_PAYLOADS_BODY)

    monkeypatch.setattr(xumm_ops.requests, "post", fake_post)
    result = _run(xumm_ops._create_xumm_payload({"TransactionType": "SignIn"}, user_token="tok"))
    assert result is None
    # ONE call only: the embedded 429 must not trigger the token-less retry.
    assert len(calls) == 1
    assert xumm_ops.rate_limited()


def test_plain_400_does_not_arm_cooldown(monkeypatch):
    _reset_cooldown(monkeypatch)
    monkeypatch.setattr(
        xumm_ops.requests,
        "post",
        lambda *a, **k: _Resp(status=400, body={"error": {"code": 400, "message": "bad tx"}}),
    )
    result = _run(xumm_ops._create_xumm_payload({"TransactionType": "SignIn"}))
    assert result is None
    assert not xumm_ops.rate_limited()


# --- every builder sets an expire so payloads can't pile up ------------------


def _capture_create(monkeypatch):
    sent = []

    def fake_post(url, json, headers, timeout):
        sent.append(json)
        return _Resp(body=_CREATE_OK)

    monkeypatch.setattr(xumm_ops.requests, "post", fake_post)
    return sent


def test_accept_offer_payload_sets_expire(monkeypatch):
    _reset_cooldown(monkeypatch)
    sent = _capture_create(monkeypatch)
    _run(xumm_ops.create_accept_offer_payload("OFFERID"))
    assert sent[0]["options"]["expire"] == xumm_ops.DEFAULT_EXPIRE_MINUTES


def test_sell_offer_payload_sets_expire(monkeypatch):
    _reset_cooldown(monkeypatch)
    sent = _capture_create(monkeypatch)
    _run(xumm_ops.create_sell_offer_payload("rACCOUNT", "NFTID", "1000000"))
    assert sent[0]["options"]["expire"] == xumm_ops.DEFAULT_EXPIRE_MINUTES


def test_cancel_offer_payload_sets_expire(monkeypatch):
    _reset_cooldown(monkeypatch)
    sent = _capture_create(monkeypatch)
    _run(xumm_ops.create_cancel_offer_payload("rACCOUNT", "OFFERIDX"))
    assert sent[0]["options"]["expire"] == xumm_ops.DEFAULT_EXPIRE_MINUTES


def test_signin_payload_sets_expire(monkeypatch):
    _reset_cooldown(monkeypatch)
    sent = _capture_create(monkeypatch)
    _run(xumm_ops.create_signin_payload())
    assert sent[0]["options"]["expire"] == xumm_ops.DEFAULT_EXPIRE_MINUTES


def test_expire_does_not_clobber_return_url(monkeypatch):
    _reset_cooldown(monkeypatch)
    sent = _capture_create(monkeypatch)
    ru = {"app": "discord://-/channels/1/2", "web": "https://discord.com/channels/1/2"}
    _run(xumm_ops.create_signin_payload(return_url=ru))
    assert sent[0]["options"]["expire"] == xumm_ops.DEFAULT_EXPIRE_MINUTES
    assert sent[0]["options"]["return_url"] == ru


def test_payment_payload_keeps_its_own_expire(monkeypatch):
    _reset_cooldown(monkeypatch)
    sent = _capture_create(monkeypatch)
    _run(xumm_ops.create_payment_payload("rDEST", expire_minutes=7))
    assert sent[0]["options"]["expire"] == 7


# --- cancel helper + uuid logging so a backlog is clearable ------------------


def test_cancel_xumm_payload_deletes(monkeypatch):
    calls = []

    def fake_delete(url, headers, timeout):
        calls.append(url)
        return _Resp(body={"result": {"cancelled": True, "reason": "OK"}})

    monkeypatch.setattr(xumm_ops.requests, "delete", fake_delete)
    ok = _run(xumm_ops.cancel_xumm_payload("uuid-1"))
    assert ok is True
    assert calls == [f"{xumm_ops.config.XUMM_API_URL}/uuid-1"]


def test_cancel_xumm_payload_already_resolved(monkeypatch):
    monkeypatch.setattr(
        xumm_ops.requests,
        "delete",
        lambda *a, **k: _Resp(body={"result": {"cancelled": False, "reason": "ALREADY_RESOLVED"}}),
    )
    assert _run(xumm_ops.cancel_xumm_payload("uuid-2")) is False


def test_cancel_xumm_payload_transport_error(monkeypatch):
    def boom(*a, **k):
        raise xumm_ops.requests.ConnectionError("down")

    monkeypatch.setattr(xumm_ops.requests, "delete", boom)
    assert _run(xumm_ops.cancel_xumm_payload("uuid-3")) is False


def test_create_logs_payload_uuid(monkeypatch, caplog):
    # The 2026-07-17 backlog was uncancellable because no uuid was ever
    # persisted; every create must leave the uuid in the logs.
    import logging

    _reset_cooldown(monkeypatch)
    _capture_create(monkeypatch)
    with caplog.at_level(logging.INFO):
        _run(xumm_ops.create_signin_payload())
    assert "11111111-2222-3333-4444-555555555555" in caplog.text
