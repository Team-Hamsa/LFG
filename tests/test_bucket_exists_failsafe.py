# nft_exists must distinguish a definitive on-ledger absence from a transient
# lookup failure, and _bucket_exists must be fail-safe: re-mint only on a
# definitive absence, never on a network blip (which would orphan a live Bucket).

import asyncio
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

from lfg_core import xrpl_ops  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _FakeWS:
    """Minimal async-context-manager stand-in for AsyncWebsocketClient."""

    def __init__(self, result=None, raises=False):
        self._result = result
        self._raises = raises

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def request(self, _req):
        if self._raises:
            raise OSError("websocket boom")

        class _Resp:
            result = self._result

        return _Resp()


async def _no_sleep(*_a, **_k):
    return None


def _patch_ws(monkeypatch, *, result=None, raises=False):
    monkeypatch.setattr(xrpl_ops, "AsyncWebsocketClient", lambda endpoint: _FakeWS(result, raises))
    monkeypatch.setattr(xrpl_ops.asyncio, "sleep", _no_sleep)


def test_nft_exists_true_when_present(monkeypatch):
    _patch_ws(monkeypatch, result={"nft_id": "N", "owner": "rA"})
    assert _run(xrpl_ops.nft_exists("N", attempts=1)) is True


def test_nft_exists_false_on_definitive_absence(monkeypatch):
    _patch_ws(monkeypatch, result={"error": "objectNotFound"})
    assert _run(xrpl_ops.nft_exists("N", attempts=1)) is False


def test_nft_exists_none_on_transient_failure(monkeypatch):
    _patch_ws(monkeypatch, raises=True)
    assert _run(xrpl_ops.nft_exists("N", attempts=2)) is None


def test_nft_exists_none_on_unknown_error(monkeypatch):
    # A non-notfound error is indeterminate, not a definitive absence.
    _patch_ws(monkeypatch, result={"error": "tooBusy"})
    assert _run(xrpl_ops.nft_exists("N", attempts=2)) is None


def test_bucket_exists_failsafe_mapping(monkeypatch):
    import _economy_deps as deps

    async def fake(nft_id, value):
        return value

    # present -> exists; definitive absence -> stale; transient -> assume exists.
    monkeypatch.setattr(xrpl_ops, "nft_exists", lambda nft_id: fake(nft_id, True))
    assert _run(deps._bucket_exists("N")) is True
    monkeypatch.setattr(xrpl_ops, "nft_exists", lambda nft_id: fake(nft_id, False))
    assert _run(deps._bucket_exists("N")) is False
    monkeypatch.setattr(xrpl_ops, "nft_exists", lambda nft_id: fake(nft_id, None))
    assert _run(deps._bucket_exists("N")) is True  # fail-safe: do NOT re-mint
