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
        # Per-wallet standalone trait tokens: maps owner -> list of {nft_id, slot, value}
        self._trait_tokens: dict[str, list[dict[str, Any]]] = {}
        # Counter for fabricating unique trait nft_ids
        self._trait_token_counter: int = 0

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
        trait_tokens = list(self._trait_tokens.get(owner, []))
        return {
            "characters": chars,
            "closet": {"assets": assets, "bodies": bodies, "token": token},
            "trait_order": swap_meta.TRAIT_ORDER,
            "slots": trait_economy.NON_BODY_SLOTS,
            "trait_tokens": trait_tokens,
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

    def equip(self, owner: str, nft_id: str, changes: list[tuple[str, str]]) -> dict[str, Any]:
        char = self._char(nft_id)
        # Validate the whole batch against a working copy before mutating, so a
        # partial apply is impossible (mirrors run_equip's precheck).
        working = dict(self.assets)
        displaced: list[dict[str, str]] = []
        for slot, value in changes:
            if working.get((slot, value), 0) <= 0:
                return {
                    "id": "mock",
                    "state": "failed",
                    "error": "asset not in closet",
                    "displaced": [],
                }
            was = next(a["value"] for a in char["attributes"] if a["trait_type"] == slot)
            displaced.append({"slot": slot, "value": was})
            working[(slot, value)] = working.get((slot, value), 0) - 1
            if was != "None":
                working[(slot, was)] = working.get((slot, was), 0) + 1
        for slot, value in changes:
            next(a for a in char["attributes"] if a["trait_type"] == slot)["value"] = value
        self.assets = working
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

    def assemble_prefill(self, owner: str) -> dict[str, Any]:
        """Dev-mode stand-in for economy_api.assemble_prefill: no affinity
        knowledge here, so just propose the first body + first held asset per
        slot (the mock's assemble accepts anything)."""
        if not self._closet_active(owner):
            raise MockEconomyError("Create and claim your Closet first.")
        if not self.bodies:
            raise MockEconomyError("No bodies in your Closet to assemble.")
        chosen: dict[str, str] = {}
        missing: list[str] = []
        for slot in trait_economy.NON_BODY_SLOTS:
            value = next((v for (s, v), c in self.assets.items() if s == slot and c > 0), None)
            if value is None:
                missing.append(slot)
            else:
                chosen[slot] = value
        return {"edition": self.bodies[0], "body": "male", "chosen": chosen, "missing": missing}

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

    def extract(self, owner: str, body: dict[str, Any]) -> dict[str, Any]:
        """Extract a closet asset into a standalone trait token.

        Gates on an active Closet. Decrements the (slot, value) asset count and
        mints a fake trait NFToken, returning a terminal session-like dict matching
        ``economy_session_dict('extract', ...)`` shape.
        """
        if not self._closet_active(owner):
            raise MockEconomyError("Create and claim your Closet first.")
        slot: str = body["slot"]
        value: str = body["value"]
        if self.assets.get((slot, value), 0) <= 0:
            raise MockEconomyError(f"asset ({slot}, {value}) not in closet")
        self.assets[(slot, value)] -= 1
        self._trait_token_counter += 1
        nft_id = f"DEVTRAIT{self._trait_token_counter}"
        tokens = self._trait_tokens.setdefault(owner, [])
        tokens.append({"nft_id": nft_id, "slot": slot, "value": value})
        return {
            "id": "mock",
            "state": "done",
            "error": None,
            "accept": "https://dev/accept",
            "nft_id": nft_id,
        }

    def deposit(self, owner: str, body: dict[str, Any]) -> dict[str, Any]:
        """Deposit a standalone trait token back into the closet.

        Gates on an active Closet. Removes the trait token by nft_id and credits
        its (slot, value) back into the closet, returning a terminal session-like
        dict matching ``economy_session_dict('deposit', ...)`` shape.
        """
        if not self._closet_active(owner):
            raise MockEconomyError("Create and claim your Closet first.")
        nft_id: str = body["nft_id"]
        tokens = self._trait_tokens.get(owner, [])
        tok = next((t for t in tokens if t["nft_id"] == nft_id), None)
        if tok is None:
            raise MockEconomyError(f"trait token {nft_id!r} not found in wallet")
        tokens.remove(tok)
        slot = tok["slot"]
        value = tok["value"]
        self.assets[(slot, value)] = self.assets.get((slot, value), 0) + 1
        return {
            "id": "mock",
            "state": "done",
            "error": None,
            "slot": slot,
            "value": value,
        }


INSTANCE = MockEconomy()
