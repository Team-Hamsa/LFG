# lfg_core/signing/types.py
# Provider-neutral shapes for "get this transaction signed" (#399).
#
# Deliberately thin. The app's session dicts and API responses already speak
# XUMM's vocabulary (`qr_url`, `xumm_url`, `uuid`) across 48 Python and 19
# JavaScript call sites, and renaming those is a cross-surface API break with
# no user-visible benefit. So a handle carries the neutral fields a second
# provider can also supply AND keeps the provider-native payload verbatim in
# `raw`, letting existing callers keep reading exactly what they read today.

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class SignRequest:
    """A transaction to be signed by a human, before provenance is applied."""

    txjson: dict[str, Any]
    memos_json: list[dict[str, Any]] | None = None
    options: dict[str, Any] | None = None
    # Per-user push token, if the provider supports out-of-band delivery.
    # XUMM-specific today; a provider without push simply ignores it.
    user_token: str | None = None


@dataclass(frozen=True)
class SignHandle:
    """A pending signature request: something to poll, and ways to reach the
    signer. `push` is "sent" | "failed" | None, matching what the surfaces
    already render."""

    id: str
    sign_url: str | None = None
    qr_url: str | None = None
    push: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SignStatus:
    """The outcome of a signature request.

    `signed` is three-way on purpose, matching the rest of this codebase: True
    (signed), False (declined/expired — terminal), None (not yet, or the lookup
    failed). Callers must never read None as "declined"."""

    signed: bool | None = None
    resolved: bool = False
    txid: str | None = None
    signer: str | None = None
    # A token the provider issued for future push delivery, if any (#135/#212).
    user_token: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)
