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
from typing import Any, Protocol, final, runtime_checkable

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
    the guarantee.

    `create` is final in both senses that matter. `@final` makes an override a
    type error, which the pre-push mypy gate catches — but only on a machine
    running the gate, and `typing.final` does not even set `__final__` at
    runtime before Python 3.11 (this project runs 3.10). So the check below
    refuses the subclass outright at class-definition time. A guarantee a
    future provider can opt out of by writing one method is not a guarantee.
    """

    name: str = "base"

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if "create" in cls.__dict__:
            raise TypeError(
                f"{cls.__name__} overrides BaseSigningProvider.create, which would skip "
                "SourceTag/memo stamping and validation. Implement _create instead."
            )

    #: Whether a provider's transactions must carry provenance memos as well as
    #: the SourceTag. ON, because #54 says memos ride on every transaction and
    #: every one of `xumm_ops`' eight non-`SignIn` builders already supplies
    #: them — including the ones the CLI economy drivers reach, which attribute
    #: to `platform=backend` rather than omitting attribution. A provider with
    #: a genuinely un-attributable path may lower it, and must say why.
    require_memos: bool = True

    @final
    async def create(self, request: SignRequest) -> SignHandle | None:
        """Stamp, prove it stuck, then delegate. Override `_create`, not this —
        `@final` makes that a type error rather than a convention."""
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
