"""Which keys may sign for an account, per its AccountRoot (agent users spec §2).

Pure: `xrpl_ops.key_authority` does the validated-ledger read and parses it with
`from_account_info`; `signing.proof.verify_proof` applies the rule.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: AccountRoot flag lsfDisableMaster: the master key may no longer sign.
LSF_DISABLE_MASTER = 0x00100000


@dataclass(frozen=True)
class KeyAuthority:
    """One `account_info` read of an account's signing keys.

    `lookup_ok` False means the read failed or was inconclusive, and the other
    two fields say nothing. An account the ledger doesn't know yet reads as found,
    with no RegularKey and its master key enabled (`NOT_FOUND`).
    """

    regular_key: str | None
    master_disabled: bool
    lookup_ok: bool


LOOKUP_FAILED = KeyAuthority(regular_key=None, master_disabled=False, lookup_ok=False)
NOT_FOUND = KeyAuthority(regular_key=None, master_disabled=False, lookup_ok=True)


def from_account_info(result: dict[str, Any]) -> KeyAuthority:
    """Parse a successful `account_info` result.

    The decoded `account_flags` object wins; older rippled builds omit it, so
    fall back to the raw `Flags` bit, as `xrpl_ops.disallows_incoming_nft_offers`
    does for its flag. No flags at all is inconclusive.
    """
    data = result.get("account_data")
    if not isinstance(data, dict):
        return LOOKUP_FAILED
    flags = result.get("account_flags")
    raw = data.get("Flags")
    if isinstance(flags, dict) and "disableMasterKey" in flags:
        disabled = bool(flags["disableMasterKey"])
    elif isinstance(raw, int):
        disabled = bool(raw & LSF_DISABLE_MASTER)
    else:
        return LOOKUP_FAILED
    regular = data.get("RegularKey")
    return KeyAuthority(
        regular_key=regular if isinstance(regular, str) and regular else None,
        master_disabled=disabled,
        lookup_ok=True,
    )
