# Tests for the signed-pseudo-transaction wallet-ownership proof (#447).
#
# The proof is signed at the binary-codec level rather than via
# `xrpl.transaction.sign`, because several negative cases deliberately mutate
# the transaction into shapes xrpl-py's typed models reject outright (a
# `Destination` on an `AccountSet`, a zero `Fee`). `encode_for_signing` +
# `keypairs.sign` is exactly what a wallet does, and it will encode any
# well-known field regardless of transaction type.
import pytest
from xrpl.core import keypairs
from xrpl.core.binarycodec import encode_for_signing
from xrpl.utils import str_to_hex
from xrpl.wallet import Wallet

from lfg_core import config, memos
from lfg_core.signing import proof

NONCE = "a" * 64


def _signed(wallet=None, nonce=NONCE, action=memos.ACTION_SIGNIN, mutate=None):
    w = wallet or Wallet.create()
    tx = proof.build_proof_tx(w.classic_address, nonce, action)
    if mutate:
        mutate(tx)
    tx["SigningPubKey"] = w.public_key
    tx["TxnSignature"] = keypairs.sign(bytes.fromhex(encode_for_signing(tx)), w.private_key)
    return w, tx


def test_build_is_canonical_and_unsubmittable():
    tx = proof.build_proof_tx("rN7n7otQDd6FczFgLdSqtcsAUxDkw6fzRH", NONCE, memos.ACTION_SIGNIN)
    assert tx["TransactionType"] == "AccountSet"
    assert tx["Fee"] == "0" and tx["Sequence"] == 0 and tx["LastLedgerSequence"] == 0
    assert tx["SourceTag"] == config.SOURCE_TAG
    decoded = memos.decode_memos(tx["Memos"])
    assert decoded["action"] == "signin"
    assert any(m["Memo"]["MemoType"] == str_to_hex("lfg/nonce") for m in tx["Memos"])


def test_valid_proof_returns_the_account():
    w, tx = _signed()
    assert (
        proof.verify_proof(tx, wallet_hint=None, nonce=NONCE, action=memos.ACTION_SIGNIN)
        == w.classic_address
    )


def test_link_action_round_trips():
    w, tx = _signed(action=memos.ACTION_LINK)
    assert (
        proof.verify_proof(tx, wallet_hint=None, nonce=NONCE, action=memos.ACTION_LINK)
        == w.classic_address
    )


@pytest.mark.parametrize(
    "mutate,reason",
    [
        (lambda t: t.update(Fee="10"), "fee"),
        (lambda t: t.update(Sequence=5), "sequence"),
        (lambda t: t.update(LastLedgerSequence=99), "last_ledger"),
        (lambda t: t.update(Destination="rN7n7otQDd6FczFgLdSqtcsAUxDkw6fzRH"), "extra_field"),
        (
            lambda t: t.update(
                TransactionType="Payment",
                Destination="rN7n7otQDd6FczFgLdSqtcsAUxDkw6fzRH",
                Amount="1",
            ),
            "type",
        ),
        (lambda t: t.update(SourceTag=1), "source_tag"),
        (lambda t: t.update(Flags=2147483648), "flags"),
    ],
)
def test_noncanonical_fields_reject(mutate, reason):
    _, tx = _signed(mutate=mutate)
    with pytest.raises(proof.ProofError) as ei:
        proof.verify_proof(tx, wallet_hint=None, nonce=NONCE, action=memos.ACTION_SIGNIN)
    assert ei.value.reason == reason
    assert ei.value.code == "bad_proof"


def test_zero_flags_is_allowed():
    w, tx = _signed(mutate=lambda t: t.update(Flags=0))
    assert (
        proof.verify_proof(tx, wallet_hint=None, nonce=NONCE, action=memos.ACTION_SIGNIN)
        == w.classic_address
    )


def test_wrong_nonce_rejects():
    _, tx = _signed(nonce="b" * 64)
    with pytest.raises(proof.ProofError) as ei:
        proof.verify_proof(tx, wallet_hint=None, nonce=NONCE, action=memos.ACTION_SIGNIN)
    assert ei.value.reason == "nonce"


def test_missing_nonce_memo_rejects():
    def _drop_nonce(t):
        want = str_to_hex(proof.NONCE_MEMO_TYPE)
        t["Memos"] = [m for m in t["Memos"] if m["Memo"]["MemoType"] != want]

    _, tx = _signed(mutate=_drop_nonce)
    with pytest.raises(proof.ProofError) as ei:
        proof.verify_proof(tx, wallet_hint=None, nonce=NONCE, action=memos.ACTION_SIGNIN)
    assert ei.value.reason == "nonce"


def test_wrong_action_memo_rejects():
    _, tx = _signed(action=memos.ACTION_LINK)
    with pytest.raises(proof.ProofError) as ei:
        proof.verify_proof(tx, wallet_hint=None, nonce=NONCE, action=memos.ACTION_SIGNIN)
    assert ei.value.reason == "action"


def test_tampered_signature_rejects():
    _, tx = _signed()
    tx["TxnSignature"] = tx["TxnSignature"][:-2] + (
        "00" if tx["TxnSignature"][-2:] != "00" else "11"
    )
    with pytest.raises(proof.ProofError) as ei:
        proof.verify_proof(tx, wallet_hint=None, nonce=NONCE, action=memos.ACTION_SIGNIN)
    assert ei.value.reason == "signature"


def test_pubkey_must_derive_the_account():
    """A RegularKey-signed proof (pubkey != Account) is rejected in v1."""
    other = Wallet.create()
    _, tx = _signed()
    tx["Account"] = other.classic_address
    with pytest.raises(proof.ProofError) as ei:
        proof.verify_proof(tx, wallet_hint=None, nonce=NONCE, action=memos.ACTION_SIGNIN)
    assert ei.value.reason == "pubkey_account"


def test_wallet_hint_must_match():
    _, tx = _signed()
    with pytest.raises(proof.ProofError) as ei:
        proof.verify_proof(
            tx,
            wallet_hint="rN7n7otQDd6FczFgLdSqtcsAUxDkw6fzRH",
            nonce=NONCE,
            action=memos.ACTION_SIGNIN,
        )
    assert ei.value.reason == "wallet_hint"


def test_missing_signature_rejects():
    _, tx = _signed()
    del tx["TxnSignature"]
    with pytest.raises(proof.ProofError) as ei:
        proof.verify_proof(tx, wallet_hint=None, nonce=NONCE, action=memos.ACTION_SIGNIN)
    assert ei.value.reason == "shape"


def test_non_dict_input_rejects():
    with pytest.raises(proof.ProofError):
        proof.verify_proof(["nope"], wallet_hint=None, nonce=NONCE, action=memos.ACTION_SIGNIN)  # type: ignore[arg-type]


def test_network_id_allowed_off_mainnet_only(monkeypatch):
    w, tx = _signed(mutate=lambda t: t.update(NetworkID=21338))
    monkeypatch.setattr(config, "XRPL_NETWORK", "testnet")
    assert (
        proof.verify_proof(tx, wallet_hint=None, nonce=NONCE, action=memos.ACTION_SIGNIN)
        == w.classic_address
    )
    monkeypatch.setattr(config, "XRPL_NETWORK", "mainnet")
    with pytest.raises(proof.ProofError) as ei:
        proof.verify_proof(tx, wallet_hint=None, nonce=NONCE, action=memos.ACTION_SIGNIN)
    assert ei.value.reason == "network_id"
