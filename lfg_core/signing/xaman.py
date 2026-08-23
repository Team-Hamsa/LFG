# lfg_core/signing/xaman.py
# The Xaman (XUMM) signing provider (#399).
#
# A thin adapter over `lfg_core.xumm_ops`, NOT a rewrite of it. xumm_ops holds a
# lot of hard-won behaviour that must not be disturbed while a seam is
# introduced around it: the post-429 cooldown that does NOT retry (the
# 2026-07-17 quota incident), the `requests.Timeout` path that does not retry
# either (a duplicate payload the user could sign while the flow polls the
# other uuid), the single token-less retry for a rotated app key (#212), the
# 15-minute `expire` that keeps us under the open-payload cap (#260), and the
# websocket watcher armed exactly once per payload. All of that stays where it
# is; this class just presents it through the common interface.
#
# Consequently every existing xumm_ops call site keeps working untouched. This
# provider is additive: new code (and the WalletConnect provider to come) talks
# to the interface, while the flows migrate at their own pace.

from __future__ import annotations

from typing import Any

from lfg_core import xumm_ops
from lfg_core.signing.base import BaseSigningProvider
from lfg_core.signing.types import SignHandle, SignRequest, SignStatus


class XamanProvider(BaseSigningProvider):
    name = "xaman"

    async def _create(self, request: SignRequest) -> SignHandle | None:
        result = await xumm_ops._create_xumm_payload(
            request.txjson,
            options=request.options,
            user_token=request.user_token,
            memos_json=request.memos_json,
        )
        if not result:
            return None
        return self.to_handle(result)

    @staticmethod
    def to_handle(result: dict[str, Any]) -> SignHandle:
        """Adapt a raw xumm_ops payload dict to a SignHandle, preserving it
        verbatim in `raw` so callers that read `qr_url`/`xumm_url` directly are
        unaffected."""
        return SignHandle(
            id=str(result.get("uuid") or ""),
            sign_url=result.get("xumm_url"),
            qr_url=result.get("qr_url"),
            push=result.get("push"),
            raw=result,
        )

    async def status(self, handle_id: str) -> SignStatus:
        raw = await xumm_ops.get_payload_status(handle_id)
        if not raw:
            # A failed lookup is NOT a decline. Three-way `signed` exists so a
            # transient XUMM error can never be read as "the user said no".
            return SignStatus(signed=None, resolved=False, raw={})
        return self.to_status(raw)

    @staticmethod
    def to_status(raw: dict[str, Any]) -> SignStatus:
        """`resolved` is DERIVED, not read: `get_payload_status` returns
        `{opened, signed, expired, account, txid, user_token}` and has no
        `resolved` key, so reading one would leave every status unresolved
        forever — including terminal ones. A payload is settled once it is
        signed or expired; anything else is still outstanding."""
        signed = raw.get("signed")
        expired = raw.get("expired")
        return SignStatus(
            signed=signed,
            resolved=bool(signed) or bool(expired),
            txid=raw.get("txid"),
            signer=raw.get("signer") or raw.get("account"),
            user_token=raw.get("user_token"),
            raw=raw,
        )

    async def cancel(self, handle_id: str) -> bool:
        return bool(await xumm_ops.cancel_xumm_payload(handle_id))
