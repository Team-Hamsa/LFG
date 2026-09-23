"""KeyAuthority parsing (agent users spec §2)."""

import asyncio

from lfg_core import xrpl_ops
from lfg_core.signing.key_authority import (
    LOOKUP_FAILED,
    LSF_DISABLE_MASTER,
    NOT_FOUND,
    KeyAuthority,
    from_account_info,
)


def test_decoded_account_flags_win_over_the_raw_bit():
    result = {
        "account_data": {"Flags": 0, "RegularKey": "rReg"},
        "account_flags": {"disableMasterKey": True},
    }
    assert from_account_info(result) == KeyAuthority("rReg", True, True)


def test_raw_flags_bit_when_account_flags_is_absent():
    assert from_account_info({"account_data": {"Flags": LSF_DISABLE_MASTER}}) == KeyAuthority(
        None, True, True
    )
    assert from_account_info({"account_data": {"Flags": 0}}) == KeyAuthority(None, False, True)


def test_no_flags_or_no_account_data_is_inconclusive():
    assert from_account_info({"account_data": {"RegularKey": "rReg"}}) == LOOKUP_FAILED
    assert from_account_info({}) == LOOKUP_FAILED


class _Resp:
    def __init__(self, ok, result):
        self._ok = ok
        self.result = result

    def is_successful(self):
        return self._ok


def _stub_client(monkeypatch, response=None, raises=None):
    class _Client:
        async def request(self, _req):
            if raises is not None:
                raise raises
            return response

    monkeypatch.setattr(xrpl_ops, "async_rpc_client", _Client)


def _lookup(address="rAccount"):
    return asyncio.get_event_loop().run_until_complete(xrpl_ops.key_authority(address))


def test_found_account_is_parsed(monkeypatch):
    _stub_client(
        monkeypatch,
        _Resp(True, {"account_data": {"Flags": 0, "RegularKey": "rReg"}}),
    )
    assert _lookup() == KeyAuthority("rReg", False, True)


def test_unknown_account_is_a_definite_answer(monkeypatch):
    _stub_client(monkeypatch, _Resp(False, {"error": "actNotFound"}))
    assert _lookup() == NOT_FOUND


def test_other_errors_and_exceptions_are_lookup_failures(monkeypatch):
    _stub_client(monkeypatch, _Resp(False, {"error": "tooBusy"}))
    assert _lookup() == LOOKUP_FAILED
    _stub_client(monkeypatch, raises=OSError("down"))
    assert _lookup() == LOOKUP_FAILED
