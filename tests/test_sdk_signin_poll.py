import asyncio

from surfaces._client.client import SIGNIN_TERMINAL, LFGServiceClient


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_signin_terminal_set():
    assert SIGNIN_TERMINAL == frozenset({"signed", "expired"})


def test_wait_for_signin_returns_on_signed(monkeypatch):
    c = LFGServiceClient("http://svc", "tok", "telegram")
    states = [
        {"state": "pending"},
        {"state": "opened"},
        {"state": "signed", "wallet": "rXRPL"},
    ]

    async def fake_status(user_id, uuid):
        return states.pop(0)

    async def no_sleep(_):
        return None

    monkeypatch.setattr(c, "signin_status", fake_status)
    out = _run(c.wait_for_signin("55", "u1", interval=0, sleep=no_sleep))
    assert out["state"] == "signed" and out["wallet"] == "rXRPL"


def test_wait_for_signin_returns_last_on_timeout(monkeypatch):
    c = LFGServiceClient("http://svc", "tok", "telegram")

    async def fake_status(user_id, uuid):
        return {"state": "pending"}  # never terminal

    async def no_sleep(_):
        return None

    monkeypatch.setattr(c, "signin_status", fake_status)
    out = _run(c.wait_for_signin("55", "u1", interval=0, timeout=0, sleep=no_sleep))
    assert out["state"] == "pending"  # last non-terminal status on timeout
