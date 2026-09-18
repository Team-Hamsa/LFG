"""House Closet (#548): the project's trait stock sold as Closet asks.

The app wallet is the issuer and cannot own a Closet (#383), so the trait tokens
it has listed are burned into a separate house wallet's Closet and relisted there
as asks. scripts/house_closet.py drives it; the design is
docs/superpowers/specs/2026-09-18-house-closet-migration-design.md.
"""

from __future__ import annotations

from xrpl.core.addresscodec import is_valid_classic_address
from xrpl.wallet import Wallet

from lfg_core import config


class HouseConfigError(RuntimeError):
    """The house wallet config is missing or unsafe. Nothing was changed."""


def _system_wallets() -> set[str]:
    """Project wallets that must never double as the house: a Closet can't
    belong to the issuer (#383), and the others keep books of their own."""
    return {
        a
        for a in (
            config.SIGNING_ACCOUNT,
            config.SWAP_ISSUER_ADDRESS,
            config.BRIX_ISSUER,
            config.TOKEN_ISSUER_ADDRESS,
            config.BRIX_DISTRIBUTOR_ADDRESS,
            config.BRIX_AMM_ACCOUNT,
        )
        if a
    }


def house_address() -> str:
    """The configured house wallet, validated. Needs no seed."""
    address = config.CLOSET_HOUSE_WALLET
    if not address:
        raise HouseConfigError("CLOSET_HOUSE_WALLET is not set")
    if not is_valid_classic_address(address):
        raise HouseConfigError(f"CLOSET_HOUSE_WALLET is not a classic address: {address}")
    if address in _system_wallets():
        raise HouseConfigError(
            f"CLOSET_HOUSE_WALLET {address} is a system wallet (issuer, app, distributor "
            "or AMM); the house must be a wallet of its own"
        )
    return address


def house_wallet() -> Wallet:
    """The house wallet's signing key. The seed must derive CLOSET_HOUSE_WALLET:
    a regular key would sign for another account, so it is refused."""
    address = house_address()
    if not config.CLOSET_HOUSE_SEED:
        raise HouseConfigError("CLOSET_HOUSE_SEED is not set")
    try:
        wallet = Wallet.from_seed(config.CLOSET_HOUSE_SEED)
    except Exception:
        # Never echo the seed, not even inside the library's own message.
        raise HouseConfigError("CLOSET_HOUSE_SEED is not a valid seed") from None
    if wallet.classic_address != address:
        raise HouseConfigError(
            "CLOSET_HOUSE_SEED signs for a different account than CLOSET_HOUSE_WALLET "
            f"({wallet.classic_address} != {address}); a regular key is not supported"
        )
    return wallet
