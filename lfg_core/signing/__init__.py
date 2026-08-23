# lfg_core/signing/__init__.py
# Signing-provider registry (#399).
#
# Providers are resolved lazily by name. The lazy import is load-bearing, not
# style: `xumm_ops` imports `signing.provenance` for its stamping, so importing
# `xaman` (which imports `xumm_ops`) at package-import time would close a cycle.
# Keeping the registry lazy also means `lfg_core.signing.provenance` stays
# importable by anything, with no transitive XUMM dependency.

from __future__ import annotations

from lfg_core.signing.base import BaseSigningProvider, SigningProvider
from lfg_core.signing.provenance import ProvenanceError, stamp, stamp_and_validate, validate
from lfg_core.signing.types import SignHandle, SignRequest, SignStatus

#: The provider used unless a caller asks for another. Every surface signs
#: through Xaman today; #399 adds WalletConnect for web sign-in only.
DEFAULT_PROVIDER = "xaman"

_PROVIDERS: dict[str, BaseSigningProvider] = {}


def get_provider(name: str | None = None) -> BaseSigningProvider:
    """The signing provider registered under `name` (default: Xaman).

    Returns `BaseSigningProvider`, NOT the structural `SigningProvider`
    protocol, and the isinstance check below is the reason. A class that merely
    matches the protocol's shape would satisfy the type checker while
    implementing its own `create()` — bypassing the stamp-and-validate this
    whole seam exists to guarantee. Enforcement that a second provider can opt
    out of by accident is not enforcement, so the registry only hands out
    providers that inherit the template.

    Instances are cached: `XamanProvider` is stateless, and the flows resolve a
    provider per payload."""
    key = (name or DEFAULT_PROVIDER).lower()
    cached = _PROVIDERS.get(key)
    if cached is not None:
        return cached
    if key == "xaman":
        from lfg_core.signing.xaman import XamanProvider

        provider: BaseSigningProvider = XamanProvider()
    else:
        raise ValueError(f"unknown signing provider: {name!r}")
    if not isinstance(provider, BaseSigningProvider):
        raise TypeError(
            f"signing provider {key!r} does not inherit BaseSigningProvider, so its "
            "transactions would skip SourceTag/memo enforcement"
        )
    _PROVIDERS[key] = provider
    return provider


__all__ = [
    "DEFAULT_PROVIDER",
    "BaseSigningProvider",
    "ProvenanceError",
    "SignHandle",
    "SignRequest",
    "SignStatus",
    "SigningProvider",
    "get_provider",
    "stamp",
    "stamp_and_validate",
    "validate",
]
