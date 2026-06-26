# surfaces/_shared/signin_result.py
# Surface-agnostic outcome message for the NON-signed branches of the Xaman
# sign-in /register flow (signed is handled inline with the verified wallet).
# Shared by the Discord + Telegram adapters so wording stays identical; each
# surface wraps the returned string in its own embed/caption.

_EXPIRED = "Sign-in expired before you approved it — run /register again to try once more."


def signin_outcome(state: str) -> str:
    # Only "signed" is success (handled by the caller with the wallet address);
    # every other terminal/timeout state is reported as a retry prompt.
    if state == "signed":
        return "Signed in."
    return _EXPIRED
