# lfg_core/signing/base.py
# The signing seam (#399): one interface every human-signature path goes
# through, with Make Waves attribution enforced by the base class rather than
# trusted to each implementation.
#
# The enforcement is the point. Before this, "every transaction carries the
# SourceTag" was true because there happened to be one door
# (`xumm_ops._create_xumm_payload`) — a property of the codebase's shape, not
# an invariant. Adding a second signer would have made it a convention again.
# Here `create()` is final: it stamps and validates, then hands the frozen
# txjson to the implementation. A provider cannot skip it without overriding
# the method the registry calls.

from __future__ import annotations

import abc
from typing import Any, Protocol, runtime_checkable

from lfg_core.signing.provenance import stamp_and_validate
from lfg_core.signing.types import SignHandle, SignRequest, SignStatus


@runtime_checkable
class SigningProvider(Protocol):
    """What a wallet integration must offer to be usable by the flows."""

    name: str

    async def create(self, request: SignRequest) -> SignHandle | None:
        """Build a signature request. None means "not created" (rate-limited,
        transport failure) — never an exception the flows must catch."""
        ...

    async def status(self, handle_id: str) -> SignStatus:
        """Where a signature request stands. Never raises."""
        ...

    async def cancel(self, handle_id: str) -> bool:
        """Withdraw a pending request. False if it could not be withdrawn."""
        ...


class BaseSigningProvider(abc.ABC):
    """Template implementation. Subclasses supply transport; the base supplies
    the guarantee."""

    name: str = "base"

    #: Whether a provider's transactions must carry provenance memos as well as
    #: the SourceTag. Off by default because several legitimate builders (the
    #: CLI economy drivers) have no surface to attribute to; the SourceTag is
    #: required unconditionally either way.
    require_memos: bool = False

    async def create(self, request: SignRequest) -> SignHandle | None:
        """Stamp, prove it stuck, then delegate. Do not override — override
        `_create`."""
        txjson: dict[str, Any] = dict(request.txjson)
        stamp_and_validate(txjson, request.memos_json, require_memos=self.require_memos)
        return await self._create(
            SignRequest(
                txjson=txjson,
                memos_json=request.memos_json,
                options=request.options,
                user_token=request.user_token,
            )
        )

    @abc.abstractmethod
    async def _create(self, request: SignRequest) -> SignHandle | None:
        """Transport-specific creation. `request.txjson` is already stamped and
        validated."""

    @abc.abstractmethod
    async def status(self, handle_id: str) -> SignStatus: ...

    @abc.abstractmethod
    async def cancel(self, handle_id: str) -> bool: ...
