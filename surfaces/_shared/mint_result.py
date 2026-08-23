# surfaces/_shared/mint_result.py
# Surface-agnostic mint-result helpers shared by Discord, Telegram, and any
# future surface adapter.  Nothing here imports discord or any surface SDK.

from surfaces._client.errors import BadRequest, ServiceError

# End-states from lfg_core.mint_flow that represent success.
MINT_OK_STATES: frozenset[str] = frozenset({"offer_ready", "done"})

# Human-readable messages for known bad terminal states.
BAD_STATE_MESSAGES: dict[str, str] = {
    "payment_timeout": "Payment request timed out. Please try again.",
    "failed": "The mint failed. Please try again or contact an admin.",
    "cancelled": "The mint was cancelled.",
}


# Refusal codes whose service-supplied `error` text is already the right thing
# to show a user, and which must NOT be swallowed by the generic rules below.
# The destination pre-flight (#388/#408) returns 409 with an actionable message
# per failure mode; without this set every one of them rendered as "you already
# have a mint in progress", which is both wrong and unactionable.
PASS_THROUGH_ERROR_CODES: frozenset[str] = frozenset(
    {
        "wallet_unfunded",
        "wallet_blocks_nft_offers",
        "wallet_reserve_short",
    }
)


def friendly_error(err: ServiceError) -> str:
    """Return a user-facing string for a ServiceError from the mint flow."""
    code = (err.code or "").lower()
    message = (err.message or "").lower()
    if code in PASS_THROUGH_ERROR_CODES and err.message:
        return err.message
    if isinstance(err, BadRequest) and ("wallet" in code or "wallet" in message):
        return "Please register your wallet first using /register."
    if err.status == 409 or "in_progress" in code or "already" in message:
        return "You already have a mint in progress — finish or wait for it to time out."
    return err.message or "The mint service is unavailable. Please try again shortly."
