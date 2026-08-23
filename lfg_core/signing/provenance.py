# lfg_core/signing/provenance.py
# The one place a transaction acquires — and is then held to — its Make Waves
# SourceTag and provenance memos (#54, #61, #399).
#
# Until now this lived inside `xumm_ops._create_xumm_payload`, which made the
# guarantee "every payload is stamped" true only for as long as XUMM was the
# only door. #399 adds a second signer (WalletConnect/Joey), and a WalletConnect
# payload is relayed through the USER'S BROWSER before it reaches the wallet —
# so stamping on the way out is not enough there. Something has to check what
# came back.
#
# Hence stamp AND validate, in that order, in one function every provider is
# required to route through. Deliberately dependency-light (config + memos
# only) so `xumm_ops` can import it without a cycle through the provider
# registry.

from __future__ import annotations

from typing import Any

from lfg_core import config, memos

# A pseudo-transaction: it proves key control and never reaches the ledger, so
# it carries neither a SourceTag nor memos. Every validator must carve it out or
# sign-in breaks on every surface.
SIGN_IN = "SignIn"


class ProvenanceError(ValueError):
    """A transaction reached a signer without valid Make Waves attribution.

    Raised rather than silently corrected: a payload that lost its SourceTag or
    memos in transit is evidence of tampering or of a code path that bypassed
    the seam, and hackathon volume credit is lost either way. Fail loudly.
    """


def stamp(txjson: dict[str, Any], memos_json: list[dict[str, Any]] | None) -> dict[str, Any]:
    """Apply the SourceTag and provenance memos to `txjson`, in place.

    `setdefault`, never assignment — a caller that deliberately pre-set either
    field keeps its value, exactly as `_create_xumm_payload` has always
    behaved. `validate` is what makes sure the result is still correct.
    """
    if txjson.get("TransactionType") != SIGN_IN:
        txjson.setdefault("SourceTag", config.SOURCE_TAG)
        if memos_json:
            txjson.setdefault("Memos", memos_json)
    return txjson


def validate(txjson: dict[str, Any], *, require_memos: bool = True) -> None:
    """Raise `ProvenanceError` unless `txjson` carries our SourceTag and a
    well-formed provenance memo triple.

    `require_memos=False` covers the builders that legitimately have no memo
    set to offer yet (the CLI drivers pass none); the SourceTag is never
    optional for a real transaction.
    """
    if txjson.get("TransactionType") == SIGN_IN:
        if "SourceTag" in txjson or "Memos" in txjson:
            raise ProvenanceError(
                "SignIn is a pseudo-transaction: it must carry neither a SourceTag nor Memos"
            )
        return

    tag = txjson.get("SourceTag")
    if tag != config.SOURCE_TAG:
        raise ProvenanceError(
            f"SourceTag must be {config.SOURCE_TAG} (Make Waves attribution), got {tag!r}"
        )

    raw = txjson.get("Memos")
    if raw is None:
        if require_memos:
            raise ProvenanceError("transaction carries no provenance Memos")
        return
    decoded = memos.decode_memos(raw)
    if decoded is None:
        raise ProvenanceError("provenance Memos are malformed or carry duplicate keys")
    missing = [k for k in ("initiator", "platform", "action") if k not in decoded]
    if missing:
        raise ProvenanceError(f"provenance Memos are missing {', '.join(missing)}")
    try:
        memos.assert_valid_values(decoded)
    except ValueError as e:
        raise ProvenanceError(f"provenance Memos carry a value outside the closed enum: {e}") from e


def stamp_and_validate(
    txjson: dict[str, Any],
    memos_json: list[dict[str, Any]] | None = None,
    *,
    require_memos: bool = False,
) -> dict[str, Any]:
    """Stamp, then prove it stuck. Every signing provider goes through here."""
    stamp(txjson, memos_json)
    validate(txjson, require_memos=require_memos)
    return txjson
