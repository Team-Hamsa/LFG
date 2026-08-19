"""Deriving kind='claim' for daily-drip payouts (#48).

Env pins come from the repo-root conftest.py (#323) — no per-file preamble.
"""

from __future__ import annotations

from lfg_core.history_events import derive_brix_events

DISTRIBUTOR = "rDistributor"
BRIX_ISSUER = "rBrixIssuer"
BRIX_HEX = "4252495800000000000000000000000000000000"


def _memo(text: str) -> dict:
    return {"Memo": {"MemoData": text.encode().hex().upper()}}


def _payment(account: str, memos: list[dict] | None = None) -> dict:
    """A tesSUCCESS Payment that moves 5 BRIX from `account` to rAlice."""
    tx = {
        "TransactionType": "Payment",
        "Account": account,
        "hash": "TXHASH",
        "date": 800000000,
        "meta": {
            "TransactionResult": "tesSUCCESS",
            "AffectedNodes": [
                {
                    "ModifiedNode": {
                        "LedgerEntryType": "RippleState",
                        "FinalFields": {
                            "Balance": {"currency": BRIX_HEX, "value": "5"},
                            "LowLimit": {"issuer": BRIX_ISSUER},
                            "HighLimit": {"issuer": "rAlice"},
                        },
                        "PreviousFields": {"Balance": {"value": "0"}},
                    }
                }
            ],
        },
    }
    if memos:
        tx["Memos"] = memos
    return tx


def _kinds(tx):
    return {
        e["kind"]
        for e in derive_brix_events(
            tx, brix_issuer=BRIX_ISSUER, brix_hex=BRIX_HEX, distributor=DISTRIBUTOR
        )
    }


def test_distributor_payment_with_claim_memo_derives_claim():
    tx = _payment(DISTRIBUTOR, [_memo("lfg:brix_claim:42")])
    assert _kinds(tx) == {"claim"}


def test_distributor_payment_without_the_memo_stays_airdrop():
    """A plain distributor send is still an airdrop — the memo is what makes a
    payout a claim, so an unmemoed one must not be miscounted by the audit."""
    tx = _payment(DISTRIBUTOR)
    assert _kinds(tx) == {"airdrop"}


def test_non_distributor_payment_with_the_memo_stays_payment():
    """Memos are user-writable. Anyone can stamp `lfg:brix_claim:` on their own
    payment; only one sent BY the distributor is a real claim."""
    tx = _payment("rImpostor", [_memo("lfg:brix_claim:42")])
    assert _kinds(tx) == {"payment"}


def test_unrelated_memo_on_a_distributor_payment_stays_airdrop():
    tx = _payment(DISTRIBUTOR, [_memo("lfg:something-else:1")])
    assert _kinds(tx) == {"airdrop"}


def test_malformed_memo_hex_does_not_crash_derivation():
    tx = _payment(DISTRIBUTOR, [{"Memo": {"MemoData": "not-hex"}}])
    assert _kinds(tx) == {"airdrop"}


def test_non_dict_memos_entry_does_not_crash_derivation():
    """Firehose input: a wrong type here would raise before the decode guard
    and abort derivation for the whole transaction."""
    tx = _payment(DISTRIBUTOR)
    tx["Memos"] = ["not-a-dict"]
    assert _kinds(tx) == {"airdrop"}


def test_non_dict_inner_memo_does_not_crash_derivation():
    tx = _payment(DISTRIBUTOR)
    tx["Memos"] = [{"Memo": "not-a-dict"}]
    assert _kinds(tx) == {"airdrop"}


def test_numeric_memo_data_does_not_crash_derivation():
    tx = _payment(DISTRIBUTOR)
    tx["Memos"] = [{"Memo": {"MemoData": 12345}}]
    assert _kinds(tx) == {"airdrop"}
