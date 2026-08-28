# lfg_core/signing/proof.py
# Wallet-ownership proof for WalletConnect sign-in and linking (#447).
#
# Joey exposes no signMessage, so the proof is a SIGNED, NEVER-SUBMITTED
# pseudo-transaction: an AccountSet with Fee "0", Sequence 0 and
# LastLedgerSequence 0 — unsubmittable on any XRPL network — carrying our
# provenance memos plus a server-issued nonce. verify_proof re-derives the
# signing account from SigningPubKey and checks the signature locally with
# xrpl-py; the allowlist of fields is CLOSED so a real transaction can never
# be smuggled in as a "proof".
from __future__ import annotations

from typing import Any

from xrpl.core.binarycodec import encode_for_signing
from xrpl.core.keypairs import derive_classic_address, is_valid_message
from xrpl.utils import str_to_hex

from lfg_core import config, memos
from lfg_core.signing import provenance

NONCE_MEMO_TYPE = "lfg/nonce"
SIGNIN_TTL = 300

# CLOSED allowlist. Anything else — a Destination, an Amount, a TicketSequence
# — and the "proof" could be a real, submittable transaction.
_ALLOWED = {
    "TransactionType",
    "Account",
    "Fee",
    "Sequence",
    "LastLedgerSequence",
    "SourceTag",
    "Memos",
    "SigningPubKey",
    "TxnSignature",
    "Flags",
    "NetworkID",
}


class ProofError(Exception):
    """A submitted proof is not a valid, canonical wallet-ownership proof."""

    code = "bad_proof"

    def __init__(self, reason: str):
        super().__init__(f"bad proof: {reason}")
        self.reason = reason


def build_proof_tx(wallet: str, nonce: str, action: str) -> dict[str, Any]:
    """The canonical unsigned proof transaction the wallet is asked to sign."""
    tx: dict[str, Any] = {
        "TransactionType": "AccountSet",
        "Account": wallet,
        "Fee": "0",
        "Sequence": 0,
        "LastLedgerSequence": 0,
    }
    provenance.stamp_and_validate(
        tx,
        memos.build_memos_json(memos.INITIATOR_USER, memos.PLATFORM_WEBAPP, action),
        require_memos=True,
    )
    tx["Memos"].append(
        {
            "Memo": {
                "MemoType": str_to_hex(NONCE_MEMO_TYPE),
                "MemoData": str_to_hex(nonce),
            }
        }
    )
    return tx


def _nonce_from(memos_list: Any) -> str | None:
    if not isinstance(memos_list, list):
        return None
    want = str_to_hex(NONCE_MEMO_TYPE).upper()
    for entry in memos_list:
        body = entry.get("Memo") if isinstance(entry, dict) else None
        if isinstance(body, dict) and str(body.get("MemoType", "")).upper() == want:
            try:
                return bytes.fromhex(str(body.get("MemoData", ""))).decode()
            except (ValueError, UnicodeDecodeError):
                return None
    return None


def verify_proof(tx_json: Any, *, wallet_hint: str | None, nonce: str, action: str) -> str:
    """Return the classic address proven by `tx_json`, or raise `ProofError`."""
    if not isinstance(tx_json, dict):
        raise ProofError("shape")
    # Transaction type first: a wholesale swap to a real tx type should read as
    # "type", not as whatever extra field that type happens to require.
    if tx_json.get("TransactionType") != "AccountSet":
        raise ProofError("type")
    if set(tx_json) - _ALLOWED:
        raise ProofError("extra_field")
    if tx_json.get("Fee") != "0":
        raise ProofError("fee")
    if tx_json.get("Sequence") != 0:
        raise ProofError("sequence")
    if tx_json.get("LastLedgerSequence") != 0:
        raise ProofError("last_ledger")
    if tx_json.get("SourceTag") != config.SOURCE_TAG:
        raise ProofError("source_tag")
    if "Flags" in tx_json and tx_json["Flags"] != 0:
        raise ProofError("flags")
    if "NetworkID" in tx_json and (
        config.XRPL_NETWORK == "mainnet" or not isinstance(tx_json["NetworkID"], int)
    ):
        raise ProofError("network_id")

    decoded = memos.decode_memos(tx_json.get("Memos")) or {}
    if decoded.get("action") != action:
        raise ProofError("action")
    if _nonce_from(tx_json.get("Memos")) != nonce:
        raise ProofError("nonce")

    account = tx_json.get("Account")
    pub = tx_json.get("SigningPubKey")
    sig = tx_json.get("TxnSignature")
    if not (isinstance(account, str) and isinstance(pub, str) and isinstance(sig, str)):
        raise ProofError("shape")
    if not (account and pub and sig):
        raise ProofError("shape")
    try:
        derived = derive_classic_address(pub)
    except Exception as e:  # malformed pubkey
        raise ProofError("pubkey") from e
    if derived != account:
        raise ProofError("pubkey_account")
    if wallet_hint is not None and wallet_hint != account:
        raise ProofError("wallet_hint")

    unsigned = {k: v for k, v in tx_json.items() if k != "TxnSignature"}
    try:
        blob = bytes.fromhex(encode_for_signing(unsigned))
        ok = is_valid_message(blob, bytes.fromhex(sig), pub)
    except Exception as e:
        raise ProofError("signature") from e
    if not ok:
        raise ProofError("signature")
    return account
