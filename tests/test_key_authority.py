"""KeyAuthority parsing (agent users spec §2)."""

from lfg_core.signing.key_authority import (
    LOOKUP_FAILED,
    LSF_DISABLE_MASTER,
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
