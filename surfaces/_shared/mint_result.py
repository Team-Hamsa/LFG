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
}


def friendly_error(err: ServiceError) -> str:
    """Return a user-facing string for a ServiceError from the mint flow."""
    code = (err.code or "").lower()
    message = (err.message or "").lower()
    if isinstance(err, BadRequest) and ("wallet" in code or "wallet" in message):
        return "Please register your wallet first using /register."
    if err.status == 409 or "in_progress" in code or "already" in message:
        return "You already have a mint in progress — finish or wait for it to time out."
    return err.message or "The mint service is unavailable. Please try again shortly."
