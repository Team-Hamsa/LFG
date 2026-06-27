# webapp/mock_economy.py
# In-memory economy stand-in for WEBAPP_DEV_MODE and endpoint tests. No network,
# no XRPL/XUMM, deterministic. Mirrors economy_api's read/op surface.
from __future__ import annotations

import copy
from typing import Any

from lfg_core import swap_meta, trait_economy

DEV_OWNER = "rDevOwnerLFG000000000000000000000"

# Closet token lifecycle states — mirror lfg_core/closet_token.py constants.
_CLOSET_NONE = "none"
_CLOSET_PENDING = "pending_accept"
_CLOSET_ACTIVE = "active"


class MockEconomyError(Exception):
    """Raised by mock ops when a precondition fails (mirrors EconomyError)."""


def _attrs(**slots: str) -> list[dict[str, str]]:
    return [{"trait_type": s, "value": slots.get(s, "None")} for s in swap_meta.TRAIT_ORDER]


class MockEconomy:
    def __init__(self) -> None:
        self.characters: list[dict[str, Any]] = [
            {
                "nft_id": "MOCK-3537",
                "edition": 3537,
                "body": "male",
                "mutable": True,
                "image_url": "",
                "attributes": _attrs(
                    Body="male",
                    Background="Blue",
                    Clothing="Hoodie",
                    Eyes="Laser",
                    Head="Crown",
                    Mouth="Grin",
                    Eyebrows="Raised",
                ),
            },
            {
                "nft_id": "MOCK-3540",
                "edition": 3540,
                "body": "female",
                "mutable": True,
                "image_url": "",
                "attributes": _attrs(
                    Body="female",
                    Background="Pink",
                    Clothing="Dress",
                    Eyes="Wink",
                    Head="Bow",
                    Mouth="Smile",
                    Eyebrows="Flat",
                ),
            },
        ]
        # Closet assets keyed (slot, value) -> count; only male-compatible for demo.
        self.assets: dict[tuple[str, str], int] = {
            ("Head", "Halo"): 2,
            ("Head", "Tophat"): 1,
            ("Eyes", "Shades"): 1,
            ("Clothing", "Suit"): 1,
        }
        self.bodies: list[int] = [42]
        # Per-wallet closet token state: maps owner -> {status, nft_id}
        self._closet: dict[str, dict[str, Any]] = {}

    # --- reads ---
    def read_state(self, owner: str) -> dict[str, Any]:
        chars = copy.deepcopy(self.characters) if owner == DEV_OWNER else []
        assets = (
            [{"slot": s, "value": v, "count": c} for (s, v), c in self.assets.items() if c > 0]
            if owner == DEV_OWNER
            else []
        )
        bodies = list(self.bodies) if owner == DEV_OWNER else []
        closet_rec = self._closet.get(owner)
        token: dict[str, Any] = {
            "status": closet_rec["status"] if closet_rec else _CLOSET_NONE,
            "nft_id": closet_rec["nft_id"] if closet_rec else None,
        }
        return {
            "characters": chars,
            "closet": {"assets": assets, "bodies": bodies, "token": token},
            "trait_order": swap_meta.TRAIT_ORDER,
            "slots": trait_economy.NON_BODY_SLOTS,
        }

    def _char(self, nft_id: str) -> dict[str, Any]:
        for c in self.characters:
            if c["nft_id"] == nft_id:
                return c
        raise KeyError(nft_id)

    def _closet_active(self, owner: str) -> bool:
        rec = self._closet.get(owner)
        return rec is not None and rec["status"] == _CLOSET_ACTIVE

    # --- ops ---
    def create_closet(self, owner: str) -> dict[str, Any]:
        """Transition the wallet's Closet token through its lifecycle.

        First call:  none → pending_accept  (returns a fake accept link).
        Second call: pending_accept → active (simulates the user accepting).
        Subsequent:  idempotent, returns active record.
        """
        rec = self._closet.get(owner)
        if rec is None:
            # First call: issue a fake pending Closet.
            self._closet[owner] = {"status": _CLOSET_PENDING, "nft_id": "DEV_CLOSET"}
            return {
                "status": _CLOSET_PENDING,
                "nft_id": "DEV_CLOSET",
                "accept": "https://dev/accept",
            }
        if rec["status"] == _CLOSET_PENDING:
            # Second call: simulate the accept — mark active.
            rec["status"] = _CLOSET_ACTIVE
            return {"status": _CLOSET_ACTIVE, "nft_id": rec["nft_id"], "accept": None}
        # Already active — idempotent.
        return {"status": _CLOSET_ACTIVE, "nft_id": rec["nft_id"], "accept": None}

    def equip(self, owner: str, nft_id: str, slot: str, value: str) -> dict[str, Any]:
        char = self._char(nft_id)
        attr = next(a for a in char["attributes"] if a["trait_type"] == slot)
        displaced = attr["value"]
        if self.assets.get((slot, value), 0) <= 0:
            return {
                "id": "mock",
                "state": "failed",
                "error": "asset not in closet",
                "displaced": None,
            }
        attr["value"] = value
        self.assets[(slot, value)] -= 1
        if displaced != "None":
            self.assets[(slot, displaced)] = self.assets.get((slot, displaced), 0) + 1
        return {"id": "mock", "state": "done", "error": None, "displaced": displaced}

    def harvest(self, owner: str, nft_id: str) -> dict[str, Any]:
        if not self._closet_active(owner):
            raise MockEconomyError("Create and claim your Closet first.")
        char = self._char(nft_id)
        moved = []
        for a in char["attributes"]:
            if a["trait_type"] in trait_economy.NON_BODY_SLOTS and a["value"] != "None":
                self.assets[(a["trait_type"], a["value"])] = (
                    self.assets.get((a["trait_type"], a["value"]), 0) + 1
                )
                moved.append((a["trait_type"], a["value"]))
        self.bodies.append(char["edition"])
        self.characters = [c for c in self.characters if c["nft_id"] != nft_id]
        return {"id": "mock", "state": "done", "error": None, "accept": None, "moved_assets": moved}

    def assemble(self, owner: str, edition: int, chosen: dict[str, str]) -> dict[str, Any]:
        if not self._closet_active(owner):
            raise MockEconomyError("Create and claim your Closet first.")
        for slot, value in chosen.items():
            self.assets[(slot, value)] = self.assets.get((slot, value), 0) - 1
        if edition in self.bodies:
            self.bodies.remove(edition)
        self.characters.append(
            {
                "nft_id": f"MOCK-{edition}",
                "edition": edition,
                "body": "male",
                "mutable": True,
                "image_url": "",
                "attributes": [
                    {"trait_type": s, "value": chosen.get(s, "None")} for s in swap_meta.TRAIT_ORDER
                ],
            }
        )
        return {
            "id": "mock",
            "state": "done",
            "error": None,
            "accept": "https://xaman/MOCK",
            "nft_id": f"MOCK-{edition}",
            "image_url": "",
        }


INSTANCE = MockEconomy()
