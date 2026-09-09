# lfg_core/signing/proof.py
# Wallet-ownership proof for WalletConnect sign-in and linking (#447).
#
# Joey exposes no signMessage, and (verified live 2026-09-09 + confirmed by the
# Joey developer) its signing pipeline refuses the classic "unsubmittable
# pseudo-tx" shape (Fee "0" / Sequence 0) outright. Joey's supported ownership
# proof is a WELL-FORMED, NEVER-SUBMITTED 1-drop Payment to the NAME-reservation
# blackhole address — Joey autofills Fee/Sequence/LastLedgerSequence, signs with
# `submit: false`, and hands the blob back. We never submit it; even if the blob
# leaked and someone else submitted it, it expires with its autofilled
# LastLedgerSequence and costs the signer at most 1 drop + the fee.
#
# verify_proof re-derives the signing account from SigningPubKey and checks the
# signature locally with xrpl-py; the allowlist of fields is CLOSED and the
# Destination/Amount are pinned so a real, spendable transaction can never be
# smuggled in as a "proof".
from __future__ import annotations

import re
from typing import Any

from xrpl.core.binarycodec import encode_for_signing
from xrpl.core.keypairs import derive_classic_address, is_valid_message
from xrpl.utils import str_to_hex

from lfg_core import config, memos
from lfg_core.signing import provenance

NONCE_MEMO_TYPE = "lfg/nonce"
SIGNIN_TTL = 300

# The XRPL "NAME reservation" blackhole — no known key, nothing can ever spend
# from it. Joey's own convention for ownership proofs.
PROOF_DESTINATION = "rrrrrrrrrrrrrrrrrNAMEtxvNvQ"
PROOF_AMOUNT = "1"  # drops
# Joey autofills the Fee; cap what we will accept so a hostile "proof" can't
# carry a ruinous fee if it ever were submitted. 1 XRP is orders of magnitude
# above any sane autofill.
MAX_PROOF_FEE_DROPS = 1_000_000

# tfFullyCanonicalSig — legacy-harmless flag some signers set on everything.
_TF_FULLY_CANONICAL = 0x80000000

# The only two actions a proof may carry. Anything else is a real app action and
# has no business being signed as a never-submitted proof transaction.
_PROOF_ACTIONS = (memos.ACTION_SIGNIN, memos.ACTION_LINK)

_HEX_RE = re.compile(r"[0-9A-Fa-f]+")

# CLOSED allowlist. Destination and Amount are pinned by value below;
# Fee/Sequence/LastLedgerSequence are Joey-autofilled and range-checked. A
# DestinationTag, SendMax, Paths — anything else — and the "proof" could be a
# meaningfully different transaction.
_ALLOWED = {
    "TransactionType",
    "Account",
    "Destination",
    "Amount",
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
    """The canonical unsigned proof transaction the wallet is asked to sign.

    Deliberately WITHOUT Fee/Sequence/LastLedgerSequence: Joey autofills those
    (`autofill: true`) and refuses zeroed placeholders. The signed result is
    still never submitted by anyone.
    """
    if action not in _PROOF_ACTIONS:
        raise ValueError(f"not a proof action: {action!r}")
    tx: dict[str, Any] = {
        "TransactionType": "Payment",
        "Account": wallet,
        "Destination": PROOF_DESTINATION,
        "Amount": PROOF_AMOUNT,
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
    if action not in _PROOF_ACTIONS:
        raise ProofError("action")
    # Transaction type first: a wholesale swap to another tx type should read as
    # "type", not as whatever extra field that type happens to require.
    if tx_json.get("TransactionType") != "Payment":
        raise ProofError("type")
    if set(tx_json) - _ALLOWED:
        raise ProofError("extra_field")
    if tx_json.get("Destination") != PROOF_DESTINATION:
        raise ProofError("destination")
    if tx_json.get("Amount") != PROOF_AMOUNT:
        raise ProofError("amount")
    fee = tx_json.get("Fee")
    if not (isinstance(fee, str) and fee.isdigit() and 0 < int(fee) <= MAX_PROOF_FEE_DROPS):
        raise ProofError("fee")
    seq = tx_json.get("Sequence")
    if not (isinstance(seq, int) and not isinstance(seq, bool) and seq > 0):
        raise ProofError("sequence")
    # LastLedgerSequence is REQUIRED: it is what makes a leaked blob expire in
    # seconds if anyone ever tried to submit it.
    lls = tx_json.get("LastLedgerSequence")
    if not (isinstance(lls, int) and not isinstance(lls, bool) and lls > 0):
        raise ProofError("last_ledger")
    if tx_json.get("SourceTag") != config.SOURCE_TAG:
        raise ProofError("source_tag")
    if "Flags" in tx_json and tx_json["Flags"] not in (0, _TF_FULLY_CANONICAL):
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
    # The provenance block must be EXACTLY what build_proof_tx asks for — no
    # extra memos, no re-pointed initiator/platform. Derived from the canonical
    # builder so a schema change can't silently loosen this.
    expected = dict(
        memos.decode_memos(
            memos.build_memos_json(memos.INITIATOR_USER, memos.PLATFORM_WEBAPP, action)
        )
        or {}
    )
    expected[NONCE_MEMO_TYPE] = nonce
    if decoded != expected:
        raise ProofError("memos")

    account = tx_json.get("Account")
    pub = tx_json.get("SigningPubKey")
    sig = tx_json.get("TxnSignature")
    if not (isinstance(account, str) and isinstance(pub, str) and isinstance(sig, str)):
        raise ProofError("shape")
    if not (account and pub and sig):
        raise ProofError("shape")
    if account == PROOF_DESTINATION:
        raise ProofError("account")
    # Joey-style clients may emit lower-case hex; `keypairs.is_valid_message`
    # dispatches on the literal "ED" prefix, so normalise once here (after
    # proving both fields really are hex) rather than at each use site.
    if not (_HEX_RE.fullmatch(pub) and _HEX_RE.fullmatch(sig)):
        raise ProofError("shape")
    pub = pub.upper()
    sig = sig.upper()
    try:
        derived = derive_classic_address(pub)
    except Exception as e:  # malformed pubkey
        raise ProofError("pubkey") from e
    if derived != account:
        raise ProofError("pubkey_account")
    if wallet_hint is not None and wallet_hint != account:
        raise ProofError("wallet_hint")

    unsigned = {k: v for k, v in tx_json.items() if k != "TxnSignature"}
    unsigned["SigningPubKey"] = pub
    try:
        blob = bytes.fromhex(encode_for_signing(unsigned))
        ok = is_valid_message(blob, bytes.fromhex(sig), pub)
    except Exception as e:
        raise ProofError("signature") from e
    if not ok:
        raise ProofError("signature")
    return account
