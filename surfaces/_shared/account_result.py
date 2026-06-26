# surfaces/_shared/account_result.py
# Surface-agnostic rendering of the #90 "linked another surface" confirmation.
# Shared by the Discord + Telegram adapters so the wording stays identical and
# the Telegram package never imports discord. Each surface wraps the returned
# string in its own embed/caption.
from typing import Any

# Pretty platform labels for the confirmation text.
_LABELS = {"discord": "Discord", "telegram": "Telegram", "x": "X"}


def _label(platform: str) -> str:
    return _LABELS.get(platform, platform.capitalize())


def other_surfaces(
    account: dict[str, Any], *, current_platform: str, current_user_id: str
) -> list[tuple[str, str]]:
    """The (platform, display_handle) of every identity on the account EXCEPT
    the caller's own. display_handle falls back to platform_username then ""."""
    out: list[tuple[str, str]] = []
    for ident in account.get("identities", []):
        if ident.get("platform") == current_platform and str(ident.get("platform_user_id")) == str(
            current_user_id
        ):
            continue
        handle = ident.get("display_handle") or ident.get("platform_username") or ""
        out.append((ident.get("platform", ""), handle))
    return out


def linked_summary(account: dict[str, Any], *, current_platform: str, current_user_id: str) -> str:
    """Plain-text confirmation: lists the OTHER surfaces this wallet is on, or a
    'no other surfaces yet' note when this is the only identity."""
    others = other_surfaces(
        account, current_platform=current_platform, current_user_id=current_user_id
    )
    wallet = account.get("wallet", "")
    if not others:
        return (
            f"Linked to your account ({wallet}). "
            "No other surfaces are linked yet — sign in on another surface with the "
            "same wallet to link it."
        )
    parts = ", ".join(
        f"{_label(platform)} ({handle})" if handle else _label(platform)
        for platform, handle in others
    )
    return f"Linked to your account ({wallet}). Also on: {parts}."
