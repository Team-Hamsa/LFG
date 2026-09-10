# Tests for the signed, never-submitted wallet-ownership proof (#447).
#
# The proof is a 1-drop Payment to the NAME-reservation blackhole, autofilled
# and signed by the wallet with `submit: false` (Joey's supported pattern —
# its pipeline refuses zeroed pseudo-tx placeholders). Tests sign at the
# binary-codec level rather than via `xrpl.transaction.sign`, because several
# negative cases deliberately mutate the transaction into shapes xrpl-py's
# typed models reject outright. `encode_for_signing` + `keypairs.sign` is
# exactly what a wallet does, and it will encode any well-known field
# regardless of transaction type.
import pytest
from xrpl.core import keypairs
from xrpl.core.binarycodec import encode_for_signing
from xrpl.utils import str_to_hex
from xrpl.wallet import Wallet

from lfg_core import config, memos
from lfg_core.signing import proof

NONCE = "a" * 64


def _autofill(tx):
    """What Joey does before signing: fill Fee/Sequence/LastLedgerSequence."""
    tx.setdefault("Fee", "12")
    tx.setdefault("Sequence", 42)
    tx.setdefault("LastLedgerSequence", 99_000_000)
    return tx


def _signed(wallet=None, nonce=NONCE, action=memos.ACTION_SIGNIN, mutate=None):
    w = wallet or Wallet.create()
    tx = _autofill(proof.build_proof_tx(w.classic_address, nonce, action))
    if mutate:
        mutate(tx)
    tx["SigningPubKey"] = w.public_key
    tx["TxnSignature"] = keypairs.sign(bytes.fromhex(encode_for_signing(tx)), w.private_key)
    return w, tx


def test_build_is_canonical_and_wallet_autofillable():
    tx = proof.build_proof_tx("rN7n7otQDd6FczFgLdSqtcsAUxDkw6fzRH", NONCE, memos.ACTION_SIGNIN)
    assert tx["TransactionType"] == "Payment"
    assert tx["Destination"] == proof.PROOF_DESTINATION
    assert tx["Amount"] == "1"
    # Left for Joey's autofill — its pipeline refuses zeroed placeholders.
    assert "Fee" not in tx and "Sequence" not in tx and "LastLedgerSequence" not in tx
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
        (lambda t: t.update(Fee="0"), "fee"),
        (lambda t: t.update(Fee="20000"), "fee"),
        (lambda t: t.update(Sequence=0), "sequence"),
        (lambda t: t.pop("LastLedgerSequence"), "last_ledger"),
        (lambda t: t.update(LastLedgerSequence=0), "last_ledger"),
        (lambda t: t.update(Destination="rN7n7otQDd6FczFgLdSqtcsAUxDkw6fzRH"), "destination"),
        (lambda t: t.update(Amount="1000000"), "amount"),
        (
            lambda t: t.update(
                Amount={"currency": "USD", "issuer": proof.PROOF_DESTINATION, "value": "1"}
            ),
            "amount",
        ),
        (lambda t: t.update(DestinationTag=7), "extra_field"),
        (lambda t: t.update(SendMax="1000000"), "extra_field"),
        (
            lambda t: (
                t.update(TransactionType="AccountSet"),
                t.pop("Destination"),
                t.pop("Amount"),
            ),
            "type",
        ),
        (lambda t: t.update(SourceTag=1), "source_tag"),
        (lambda t: t.update(Flags=131072), "flags"),
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


def test_lowercase_hex_fields_still_verify():
    """Joey-style clients may emit lower-case hex; the proof must still verify."""
    w, tx = _signed()
    tx["SigningPubKey"] = tx["SigningPubKey"].lower()
    tx["TxnSignature"] = tx["TxnSignature"].lower()
    assert (
        proof.verify_proof(tx, wallet_hint=None, nonce=NONCE, action=memos.ACTION_SIGNIN)
        == w.classic_address
    )


def test_non_hex_signature_rejects_as_shape():
    _, tx = _signed()
    tx["TxnSignature"] = "zz" + tx["TxnSignature"][2:]
    with pytest.raises(proof.ProofError) as ei:
        proof.verify_proof(tx, wallet_hint=None, nonce=NONCE, action=memos.ACTION_SIGNIN)
    assert ei.value.reason == "shape"


def test_only_signin_and_link_are_proof_actions():
    with pytest.raises(ValueError):
        proof.build_proof_tx("rN7n7otQDd6FczFgLdSqtcsAUxDkw6fzRH", NONCE, memos.ACTION_MINT)
    _, tx = _signed()
    with pytest.raises(proof.ProofError) as ei:
        proof.verify_proof(tx, wallet_hint=None, nonce=NONCE, action=memos.ACTION_MINT)
    assert ei.value.reason == "action"


def test_error_message_leaks_no_key_material():
    _, tx = _signed()
    tx["TxnSignature"] = tx["TxnSignature"][:-2] + (
        "00" if tx["TxnSignature"][-2:] != "00" else "11"
    )
    with pytest.raises(proof.ProofError) as ei:
        proof.verify_proof(tx, wallet_hint=None, nonce=NONCE, action=memos.ACTION_SIGNIN)
    rendered = str(ei.value)
    assert tx["TxnSignature"] not in rendered
    assert tx["SigningPubKey"] not in rendered
    assert rendered == "bad proof: signature"


def test_changed_platform_memo_rejects():
    """The memo set must be EXACTLY the one build_proof_tx asks for: a proof
    re-pointed at another platform is not the proof we issued."""

    def _repoint(t):
        want = str_to_hex("platform")
        for m in t["Memos"]:
            if m["Memo"]["MemoType"] == want:
                m["Memo"]["MemoData"] = str_to_hex(memos.PLATFORM_TELEGRAM)

    _, tx = _signed(mutate=_repoint)
    with pytest.raises(proof.ProofError) as ei:
        proof.verify_proof(tx, wallet_hint=None, nonce=NONCE, action=memos.ACTION_SIGNIN)
    assert ei.value.reason == "memos"


def test_extra_memo_rejects():
    def _add(t):
        t["Memos"].append(
            {"Memo": {"MemoType": str_to_hex("lfg/extra"), "MemoData": str_to_hex("x")}}
        )

    _, tx = _signed(mutate=_add)
    with pytest.raises(proof.ProofError) as ei:
        proof.verify_proof(tx, wallet_hint=None, nonce=NONCE, action=memos.ACTION_SIGNIN)
    assert ei.value.reason == "memos"


def test_fully_canonical_sig_flag_is_allowed():
    w, tx = _signed(mutate=lambda t: t.update(Flags=2147483648))
    assert (
        proof.verify_proof(tx, wallet_hint=None, nonce=NONCE, action=memos.ACTION_SIGNIN)
        == w.classic_address
    )


def test_blackhole_cannot_prove_itself():
    # Account == the pinned Destination is nonsense; nobody holds that key, but
    # reject the shape outright rather than relying on signature failure.
    _, tx = _signed(mutate=lambda t: t.update(Account=proof.PROOF_DESTINATION))
    with pytest.raises(proof.ProofError):
        proof.verify_proof(tx, wallet_hint=None, nonce=NONCE, action=memos.ACTION_SIGNIN)


def test_lls_beyond_window_rejects():
    _, tx = _signed(mutate=lambda t: t.update(LastLedgerSequence=4294967295))
    with pytest.raises(proof.ProofError) as ei:
        proof.verify_proof(
            tx,
            wallet_hint=None,
            nonce=NONCE,
            action=memos.ACTION_SIGNIN,
            max_last_ledger=1000 + proof.PROOF_LLS_WINDOW,
        )
    assert ei.value.reason == "last_ledger"


def test_lls_within_window_verifies():
    w, tx = _signed(mutate=lambda t: t.update(LastLedgerSequence=1500))
    assert (
        proof.verify_proof(
            tx,
            wallet_hint=None,
            nonce=NONCE,
            action=memos.ACTION_SIGNIN,
            max_last_ledger=1000 + proof.PROOF_LLS_WINDOW,
        )
        == w.classic_address
    )


def test_response_artifacts_are_tolerated():
    """A wallet's signed-tx response may echo unsigned artifacts inside
    tx_json (hash/ctid/date/meta etc. — the shapes rippled itself uses); they
    are not smuggled fields (observed live with Joey mobile 2026-09-09)."""
    w, tx = _signed()
    tx.update(
        hash="A" * 64,
        ctid="C0000001",
        date=800000000,
        ledger_index=99000123,
        validated=True,
        meta={"TransactionResult": "tesSUCCESS"},
        close_time_iso="2026-09-09T23:30:00Z",
        status="success",
        DeliverMax=tx["Amount"],
    )
    assert (
        proof.verify_proof(tx, wallet_hint=None, nonce=NONCE, action=memos.ACTION_SIGNIN)
        == w.classic_address
    )


def test_deliver_max_mismatch_rejects():
    _, tx = _signed()
    tx["DeliverMax"] = "1000000"
    with pytest.raises(proof.ProofError) as ei:
        proof.verify_proof(tx, wallet_hint=None, nonce=NONCE, action=memos.ACTION_SIGNIN)
    assert ei.value.reason == "amount"


def test_extra_field_names_surface_in_detail_only():
    _, tx = _signed(mutate=lambda t: t.update(SendMax="1000000"))
    with pytest.raises(proof.ProofError) as ei:
        proof.verify_proof(tx, wallet_hint=None, nonce=NONCE, action=memos.ACTION_SIGNIN)
    assert ei.value.reason == "extra_field"
    assert ei.value.detail == "SendMax"


def test_deliver_max_only_normalises_to_amount():
    """API v2 may carry DeliverMax INSTEAD of Amount; the signed bytes always
    contain Amount, so it is normalised back before signature verification."""
    w, tx = _signed()
    tx["DeliverMax"] = tx.pop("Amount")
    assert (
        proof.verify_proof(tx, wallet_hint=None, nonce=NONCE, action=memos.ACTION_SIGNIN)
        == w.classic_address
    )


def test_deliver_max_only_with_wrong_value_rejects():
    _, tx = _signed()
    tx.pop("Amount")
    tx["DeliverMax"] = "1000000"
    with pytest.raises(proof.ProofError) as ei:
        proof.verify_proof(tx, wallet_hint=None, nonce=NONCE, action=memos.ACTION_SIGNIN)
    assert ei.value.reason == "amount"
