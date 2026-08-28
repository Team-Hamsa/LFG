# The WalletConnect (Joey Wallet) signing provider (#447). Transport is the
# user's browser: the request is stored, the client signs+submits via Joey
# and posts the hash back, and lfg_service verifies it on-ledger
# (handle_sign_result) before the row ever reads "signed".
from __future__ import annotations

import logging
from typing import Any

from lfg_core.signing import store
from lfg_core.signing.base import BaseSigningProvider
from lfg_core.signing.types import SignHandle, SignRequest, SignStatus

TX_TTL = 900
_TERMINAL_NOT_SIGNED = ("expired", "rejected", "failed", "mismatch", "cancelled")


def is_wc_id(value: Any) -> bool:
    """True for a sign-request id this provider owns. The `wc-` prefix is what
    lets the XUMM chokepoints route without threading a provider argument
    through every call site."""
    return isinstance(value, str) and value.startswith("wc-")


def handle_dict(request_id: str) -> dict[str, Any]:
    """The xumm-shaped create response the surfaces already know how to read.
    `qr_url` is None (nothing to scan — the browser signs) and `xumm_url` is
    the client-side scheme the Activity intercepts."""
    return {
        "uuid": request_id,
        "xumm_url": f"lfg-wc://{request_id}",
        "qr_url": None,
        "pushed": False,
        "push": None,
        "sign_mode": "walletconnect",
    }


class WalletConnectProvider(BaseSigningProvider):
    name = "walletconnect"

    async def _create(self, request: SignRequest) -> SignHandle | None:
        wallet = request.txjson.get("Account")
        if not isinstance(wallet, str) or not wallet:
            # Fail closed: without an Account there is no wallet to bind the
            # row to, so the result could never be verified against a signer.
            logging.error("walletconnect create refused: txjson has no Account")
            return None
        row = store.create(
            wallet=wallet,
            purpose="tx",
            txjson=request.txjson,
            nonce=None,
            ttl_seconds=TX_TTL,
        )
        raw = handle_dict(row["id"])
        logging.info(
            f"WC sign request {row['id']} ({request.txjson.get('TransactionType')}) for {wallet}"
        )
        return SignHandle(id=row["id"], sign_url=raw["xumm_url"], qr_url=None, push=None, raw=raw)

    @staticmethod
    def status_dict(request_id: str) -> dict[str, Any] | None:
        """The xumm-shaped status dict `get_payload_status` returns, so the
        polling flows need no per-provider branch. None for an unknown id."""
        row = store.get(request_id)
        if row is None:
            return None
        if row["state"] == "pending" and store.expire_stale():
            row = store.get(request_id) or row
        state = row["state"]
        return {
            "opened": state != "pending",
            "signed": state == "signed",
            "expired": state in _TERMINAL_NOT_SIGNED,
            "account": row["wallet"],
            "txid": row.get("txid"),
            "user_token": None,
            "sign_mode": "walletconnect",
            "state": state,
        }

    async def status(self, handle_id: str) -> SignStatus:
        raw = self.status_dict(handle_id)
        if raw is None:
            return SignStatus(signed=None, resolved=False, raw={})
        return SignStatus(
            signed=raw["signed"],
            resolved=raw["signed"] or raw["expired"],
            txid=raw["txid"],
            signer=raw["account"],
            user_token=None,
            raw=raw,
        )

    async def cancel(self, handle_id: str) -> bool:
        return store.set_state(handle_id, "cancelled")
