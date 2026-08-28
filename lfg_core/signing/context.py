# Ambient signing-provider selection (#447).
#
# The session token names the provider a user signed in with. Instead of
# threading that through every session dataclass and the ~60 builder/poll
# call sites in lfg_service/app.py, require_auth sets it here for the
# request; asyncio.create_task copies the context, so the background flow
# tasks a handler launches inherit it. Startup-resumed jobs and the sweeps
# run with the defaults (xaman, no wallet).
from __future__ import annotations

import contextvars
from collections.abc import Iterator
from contextlib import contextmanager

_provider: contextvars.ContextVar[str] = contextvars.ContextVar(
    "lfg_sign_provider", default="xaman"
)
_wallet: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "lfg_sign_wallet", default=None
)


def current_provider() -> str:
    return _provider.get()


def current_wallet() -> str | None:
    """The session wallet a WalletConnect session can sign AS. None outside a
    web/Telegram request, or for Xaman sessions (where it is not needed)."""
    return _wallet.get()


@contextmanager
def use(provider: str, wallet: str | None) -> Iterator[None]:
    t1 = _provider.set(provider)
    t2 = _wallet.set(wallet)
    try:
        yield
    finally:
        _wallet.reset(t2)
        _provider.reset(t1)
