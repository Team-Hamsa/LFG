# webapp/mock_market.py
# In-memory marketplace stand-in for WEBAPP_DEV_MODE (#44 Task 10) — no
# network, no XRPL/XUMM, no sqlite, deterministic. Mirrors the real
# lfg_service/app.py market handlers' request/response shapes closely enough
# that webapp/client/app.js needs no dev-mode branching of its own (same
# posture as webapp/mock_economy.py for the dressup panel).
#
# Session progression is SCRIPTED across polls (mirrors tests/mock_service.py's
# poll-counter pattern: "state['mint_polls'][sid] += 1; ready = ... >= 2") so a
# human clicking through the mock harness actually sees the QR step, the
# spinner, and the trait wizard's two signatures in sequence — not just an
# instant jump to done. This is deliberately NOT the same shape as
# mock_economy's equip/harvest/assemble/extract/deposit (those complete on the
# very first call): those five ops have no user-facing QR wait in the UI
# (economyState reloads right after), while List/Cancel/Buy/trait-sell each
# render a QR-and-poll screen that the manual verify pass in Task 10's brief
# specifically asks to exercise offline.
from __future__ import annotations

from typing import Any

from lfg_core import brokers, market_ops, market_store
from webapp import mock_economy

DEV_OWNER = mock_economy.DEV_OWNER
OTHER_SELLER_1 = "rMockOtherSeller1111111111111111111"
OTHER_SELLER_2 = "rMockOtherSeller2222222222222222222"

# Polls-until-transition for the scripted session progressions below. Small
# numbers keep a manual click-through snappy (~3s/poll on the client, see
# app.js's pollTimer intervals) while still landing on more than one state.
_LIST_POLLS_TO_PENDING = 1
_LIST_POLLS_TO_DONE = 2
_CANCEL_POLLS_TO_DONE = 1
_BUY_POLLS_TO_PENDING = 1
_BUY_POLLS_TO_DONE = 2
_TRAIT_EXTRACT_POLLS = 1  # -> extract_done
_TRAIT_SIGN1_POLLS = 1  # -> list_pending (extract's own "signature" wait)
_TRAIT_SIGN2_POLLS = 2  # -> listed


class MockMarketError(Exception):
    """Raised by mock market ops for the same preconditions the real handlers
    400/403/404/409 on; the dev-mode caller in lfg_service/app.py turns this
    into a 400 (mirrors mock_economy.MockEconomyError's role for /api/equip
    et al.)."""


def _trait_image_url(slot: str, value: str) -> str:
    # Same same-origin /api/layer convention lfg_service/app.py's real
    # _trait_image_url uses; body is cosmetic in the mock (dev mode's
    # /api/layer path serves whatever local layer tree exists, if any).
    return f"/api/layer?body=male&trait={slot}&value={value}"


class MockMarket:
    def __init__(self) -> None:
        # Seed a few THIRD-PARTY listings so Browse has something to show
        # beyond whatever the dev owner lists during a session, spanning both
        # kinds and a price spread wide enough to manually exercise
        # min/max/sort. One trait row starts NOT live, to exercise the buy
        # 410 "listing_unavailable" path against a stale offer_index.
        self._listings: list[dict[str, Any]] = [
            {
                "nft_id": "MOCK-9001",
                "kind": "character",
                "nft_number": 9001,
                "image": "",
                "attributes": [{"trait_type": "Body", "value": "skeleton"}],
                "amount_drops": 25_000_000,
                "seller": OTHER_SELLER_1,
                "offer_index": "MOCKOFFER-9001",
                "is_live": True,
                "slot": None,
                "value": None,
            },
            {
                "nft_id": "MOCK-9002",
                "kind": "character",
                "nft_number": 9002,
                "image": "",
                "attributes": [{"trait_type": "Body", "value": "female"}],
                "amount_drops": 8_000_000,
                "seller": OTHER_SELLER_2,
                "offer_index": "MOCKOFFER-9002",
                "is_live": True,
                "slot": None,
                "value": None,
            },
            {
                "nft_id": "MOCKTRAIT-9101",
                "kind": "trait",
                "nft_number": None,
                "image": _trait_image_url("Head", "Tophat"),
                "attributes": None,
                "amount_drops": None,
                "amount_brix": "4",
                "seller": OTHER_SELLER_1,
                "offer_index": "MOCKOFFER-9101",
                "is_live": True,
                "slot": "Head",
                "value": "Tophat",
            },
            {
                "nft_id": "MOCKTRAIT-9102",
                "kind": "trait",
                "nft_number": None,
                "image": _trait_image_url("Eyes", "Shades"),
                "attributes": None,
                "amount_drops": None,
                "amount_brix": "15",
                "seller": OTHER_SELLER_2,
                "offer_index": "MOCKOFFER-9102",
                "is_live": True,
                "slot": "Eyes",
                "value": "Shades",
            },
            {
                # #131: an external (brokered) listing — destination-locked to
                # a known broker, read-only in browse, buy refused.
                "nft_id": "MOCK-9003",
                "kind": "character",
                "nft_number": 9003,
                "image": "",
                "attributes": [{"trait_type": "Body", "value": "ape"}],
                "amount_drops": 42_000_000,
                "seller": OTHER_SELLER_2,
                "offer_index": "MOCKOFFER-9003",
                "is_live": True,
                "slot": None,
                "value": None,
                "destination": "rpx9JThQ2y37FaGeeJP7PXDUVEXY3PHZSC",  # xrp.cafe
            },
            {
                "nft_id": "MOCKTRAIT-9199",
                "kind": "trait",
                "nft_number": None,
                "image": _trait_image_url("Mouth", "Grin"),
                "attributes": None,
                "amount_drops": None,
                "amount_brix": "2",
                "seller": OTHER_SELLER_1,
                "offer_index": "MOCKOFFER-9199",
                "is_live": False,  # already sold/cancelled -> buy/cancel 410/404
                "slot": "Mouth",
                "value": "Grin",
            },
        ]
        # #283: seed one live incoming bid on MOCK-9001 (a third-party
        # character) and one outgoing bid shape for the dev owner to exercise
        # both Mine groups + the detail overlay's bid list.
        self._bids: list[dict[str, Any]] = [
            {
                "offer_index": "MOCKBID-9001",
                "nft_id": "MOCK-9001",
                "bidder": OTHER_SELLER_2,
                "amount_drops": 12_000_000,
                "expiration": None,
                "is_live": True,
            },
        ]
        self._sessions: dict[str, dict[str, Any]] = {}
        self._session_counter = 0

    # --- shared helpers ---

    def _next_session_id(self, prefix: str) -> str:
        self._session_counter += 1
        return f"mock-{prefix}-{self._session_counter}"

    def _live(self) -> list[dict[str, Any]]:
        return [r for r in self._listings if r["is_live"]]

    def _find(self, offer_index: str) -> dict[str, Any] | None:
        return next((r for r in self._listings if r["offer_index"] == offer_index), None)

    def _serialize(self, row: dict[str, Any]) -> dict[str, Any]:
        out = {
            "nft_id": row["nft_id"],
            "kind": row["kind"],
            "image": row["image"],
            "seller": row["seller"],
            "offer_index": row["offer_index"],
            "buyable": True,
        }
        # #131 external tagging, mirroring app._serialize_listing_row.
        destination = row.get("destination")
        if destination:
            resolved = brokers.resolve(destination, row["nft_id"])
            out["buyable"] = False
            out["source"] = "external"
            out["destination"] = destination
            out["marketplace"] = resolved["name"] if resolved else f"external ({destination[:8]}…)"
            out["external_url"] = resolved["url"] if resolved else None
        # #239 per-kind denomination, mirroring app._serialize_listing_row.
        if row.get("amount_drops") is not None:
            out["amount_drops"] = row["amount_drops"]
            out["amount_xrp"] = market_ops.drops_to_xrp_str(str(row["amount_drops"]))
        if row.get("amount_brix") is not None:
            out["amount_brix"] = row["amount_brix"]
        if row["kind"] == "character":
            out["nft_number"] = row["nft_number"]
            out["attributes"] = row["attributes"] or []
        else:
            out["slot"] = row["slot"]
            out["value"] = row["value"]
        return out

    # --- reads ---

    def browse(
        self,
        *,
        kind: str,
        trait_filters: dict[str, list[str]],
        min_drops: int | None,
        max_drops: int | None,
        sort: str,
        include_external: bool = False,
    ) -> list[dict[str, Any]]:
        rows = [r for r in self._live() if r["kind"] == kind]
        if not include_external:
            rows = [r for r in rows if not r.get("destination")]
        if min_drops is not None:
            rows = [
                r
                for r in rows
                if r.get("amount_drops") is not None and r["amount_drops"] >= min_drops
            ]
        if max_drops is not None:
            rows = [
                r
                for r in rows
                if r.get("amount_drops") is not None and r["amount_drops"] <= max_drops
            ]
        if trait_filters:
            rows = [
                r
                for r in rows
                if market_store._attributes_match(
                    r["attributes"]
                    if kind == "character"
                    else [{"trait_type": r["slot"], "value": r["value"]}],
                    trait_filters,
                )
            ]
        if sort == "price_asc":
            rows = sorted(rows, key=lambda r: (market_store.listing_price(r), r["offer_index"]))
        elif sort == "price_desc":
            rows = sorted(rows, key=lambda r: (-market_store.listing_price(r), r["offer_index"]))
        else:  # newest: the mock carries no created_ts, so this is stable input order
            rows = list(rows)
        return [self._serialize(r) for r in rows]

    def mine(self, owner: str) -> dict[str, Any]:
        # is_live=True only (mirrors the real handler's "AND is_live = 1" —
        # a cancelled/sold row stays in self._listings as a closed record,
        # never deleted, so this filter is load-bearing).
        my_listings = [r for r in self._listings if r["seller"] == owner and r["is_live"]]
        my_listings = [self._serialize(r) for r in my_listings]

        econ = mock_economy.INSTANCE
        listed_ids = {r["nft_id"] for r in self._live() if r["seller"] == owner}
        unlisted_characters = [
            {
                "nft_id": c["nft_id"],
                "nft_number": c["edition"],
                "image": c["image_url"] or None,
                "attributes": c["attributes"],
            }
            for c in econ.characters
            if c["nft_id"] not in listed_ids
        ]
        unlisted_trait_tokens = [
            {"nft_id": t["nft_id"], "slot": t["slot"], "value": t["value"]}
            for t in econ._trait_tokens.get(owner, [])
            if t["nft_id"] not in listed_ids
        ]
        closet_assets = [
            {"slot": s, "value": v, "count": c} for (s, v), c in econ.assets.items() if c > 0
        ]
        return {
            "listings": my_listings,
            "unlisted_characters": unlisted_characters if owner == DEV_OWNER else [],
            "unlisted_trait_tokens": unlisted_trait_tokens,
            "closet_assets": closet_assets if owner == DEV_OWNER else [],
        }

    def history(
        self, *, nft_id: str | None = None, slot: str | None = None, value: str | None = None
    ) -> dict[str, Any]:
        if nft_id:
            return {"nft_id": nft_id, "events": []}  # no scripted history in the mock
        sales = [
            self._serialize(r)
            for r in self._listings
            if r["kind"] == "trait"
            and r["slot"] == slot
            and r["value"] == value
            and not r["is_live"]
        ]
        return {"slot": slot, "value": value, "sales": sales}

    # --- ops: list ---

    def start_list(
        self,
        owner: str,
        nft_id: str,
        amount_drops: int | None,
        amount_brix: str | None = None,
    ) -> dict[str, Any]:
        # Ownership resolution mirrors the real handler's shape closely
        # enough for manual testing: an unlisted character/trait token the
        # dev owner actually holds (per mock_economy), or already-listed ->
        # reject like the real 409.
        econ = mock_economy.INSTANCE
        char = next((c for c in econ.characters if c["nft_id"] == nft_id), None)
        trait = next((t for t in econ._trait_tokens.get(owner, []) if t["nft_id"] == nft_id), None)
        if char is None and trait is None:
            raise MockMarketError("you do not own that NFT")
        if any(r["nft_id"] == nft_id and r["is_live"] for r in self._listings):
            raise MockMarketError("that NFT is already listed")

        sid = self._next_session_id("list")
        self._sessions[sid] = {
            "kind": "list",
            "polls": 0,
            "state": "awaiting_signature",
            "nft_id": nft_id,
            "listing_kind": "character" if char is not None else "trait",
            "amount_drops": amount_drops if char is not None else None,
            "amount_brix": amount_brix if char is None else None,
            "slot": trait["slot"] if trait else None,
            "value": trait["value"] if trait else None,
            "seller": owner,
            "offer_index": None,
        }
        return self._session_dict(sid)

    def start_cancel(self, owner: str, offer_index: str) -> dict[str, Any]:
        row = self._find(offer_index)
        if row is None or not row["is_live"]:
            # #283: fall through to the caller's own bids.
            bid = self._find_bid(offer_index)
            if bid is None or not bid["is_live"]:
                raise MockMarketError("not found")
            if bid["bidder"] != owner:
                raise MockMarketError("not your listing")
            sid = self._next_session_id("cancel")
            self._sessions[sid] = {
                "kind": "cancel",
                "polls": 0,
                "state": "awaiting_signature",
                "offer_index": offer_index,
                "bid": True,
            }
            return self._session_dict(sid)
        if row["seller"] != owner:
            raise MockMarketError("not your listing")
        sid = self._next_session_id("cancel")
        self._sessions[sid] = {
            "kind": "cancel",
            "polls": 0,
            "state": "awaiting_signature",
            "offer_index": offer_index,
        }
        return self._session_dict(sid)

    def start_buy(self, owner: str, offer_index: str) -> dict[str, Any]:
        row = self._find(offer_index)
        if row is None:
            raise MockMarketError("not found")
        if not row["is_live"]:
            raise MockMarketError("listing_unavailable")
        if row.get("destination"):  # #131: external listings are display-only
            raise MockMarketError("external_listing")
        if row["kind"] == "trait" and not mock_economy.INSTANCE._closet_active(owner):
            raise MockMarketError("closet_required")
        sid = self._next_session_id("buy")
        self._sessions[sid] = {
            "kind": "buy",
            "polls": 0,
            "state": "awaiting_signature",
            "offer_index": offer_index,
            "buyer": owner,
            "instruction": (
                f"Confirm purchase for {row['amount_brix']} BRIX"
                if row.get("amount_brix") is not None
                else f"Confirm purchase for {market_ops.drops_to_xrp_str(str(row['amount_drops']))} XRP"
            ),
        }
        return self._session_dict(sid)

    def start_trait_list(
        self, owner: str, slot: str, value: str, amount_brix: str
    ) -> dict[str, Any]:
        if not mock_economy.INSTANCE._closet_active(owner):
            raise MockMarketError("Create and claim your Closet first.")
        if mock_economy.INSTANCE.assets.get((slot, value), 0) <= 0:
            raise MockMarketError(f"asset ({slot}, {value}) not in closet")
        sid = self._next_session_id("trait_list")
        self._sessions[sid] = {
            "kind": "trait_list",
            "polls": 0,
            "state": "extract_pending",
            "owner": owner,
            "slot": slot,
            "value": value,
            "amount_brix": amount_brix,
            "nft_id": None,
            "offer_index": None,
        }
        return self._session_dict(sid)

    # --- status / advance (scripted progression) ---

    # --- #283: bids ---

    def _serialize_bid(self, b: dict[str, Any]) -> dict[str, Any]:
        return {
            "offer_index": b["offer_index"],
            "nft_id": b["nft_id"],
            "bidder": b["bidder"],
            "amount_drops": b["amount_drops"],
            "amount_xrp": market_ops.drops_to_xrp_str(str(b["amount_drops"])),
            "expiration": b.get("expiration"),
        }

    def bids(self, nft_id: str) -> list[dict[str, Any]]:
        rows = [b for b in self._bids if b["is_live"] and b["nft_id"] == nft_id]
        rows.sort(key=lambda b: -b["amount_drops"])
        return [self._serialize_bid(b) for b in rows]

    def bids_mine(self, owner: str) -> dict[str, Any]:
        mine = [b for b in self._bids if b["is_live"] and b["bidder"] == owner]
        owned_ids = {c["nft_id"] for c in mock_economy.INSTANCE.characters}
        incoming = [
            b
            for b in self._bids
            if b["is_live"] and b["nft_id"] in owned_ids and b["bidder"] != owner
        ]
        return {
            "my_bids": [self._serialize_bid(b) for b in mine],
            "bids_on_my_nfts": [self._serialize_bid(b) for b in incoming],
        }

    def _find_bid(self, offer_index: str) -> dict[str, Any] | None:
        return next((b for b in self._bids if b["offer_index"] == offer_index), None)

    def start_bid(self, owner: str, nft_id: str, amount_drops: int) -> dict[str, Any]:
        # Bids target NFTs, not listings; the only mock-enforced guard is the
        # self-bid check (mirrors the real handler's 400).
        listing = next((r for r in self._listings if r["nft_id"] == nft_id), None)
        if listing is not None and listing["seller"] == owner:
            raise MockMarketError("you already own that NFT")
        sid = self._next_session_id("bid")
        self._sessions[sid] = {
            "kind": "bid",
            "polls": 0,
            "state": "awaiting_signature",
            "offer_index": None,
            "nft_id": nft_id,
            "bidder": owner,
            "amount_drops": amount_drops,
        }
        return self._session_dict(sid)

    def start_bid_accept(self, owner: str, offer_index: str) -> dict[str, Any]:
        bid = self._find_bid(offer_index)
        if bid is None:
            raise MockMarketError("not found")
        if not bid["is_live"]:
            raise MockMarketError("bid_unavailable")
        sid = self._next_session_id("bidaccept")
        self._sessions[sid] = {
            "kind": "bid_accept",
            "polls": 0,
            "state": "awaiting_signature",
            "offer_index": offer_index,
            "owner": owner,
        }
        return self._session_dict(sid)

    def _advance_bid(self, session: dict[str, Any]) -> None:
        if session["state"] not in ("awaiting_signature", "pending"):
            return
        session["polls"] += 1
        if session["state"] == "awaiting_signature" and session["polls"] >= _BUY_POLLS_TO_PENDING:
            session["state"] = "pending"
        if session["polls"] >= _BUY_POLLS_TO_DONE:
            offer_index = f"MOCKBID-{self._next_session_id('placed')}"
            session["offer_index"] = offer_index
            session["state"] = "done"
            self._bids.append(
                {
                    "offer_index": offer_index,
                    "nft_id": session["nft_id"],
                    "bidder": session["bidder"],
                    "amount_drops": session["amount_drops"],
                    "expiration": None,
                    "is_live": True,
                }
            )

    def _advance_bid_accept(self, session: dict[str, Any]) -> None:
        if session["state"] not in ("awaiting_signature", "pending"):
            return
        session["polls"] += 1
        if session["state"] == "awaiting_signature" and session["polls"] >= _BUY_POLLS_TO_PENDING:
            session["state"] = "pending"
        if session["polls"] >= _BUY_POLLS_TO_DONE:
            session["state"] = "done"
            bid = self._find_bid(session["offer_index"])
            if bid is not None:
                bid["is_live"] = False

    def status(self, session_id: str) -> dict[str, Any]:
        session = self._sessions.get(session_id)
        if session is None:
            raise KeyError(session_id)
        kind = session["kind"]
        if kind == "list":
            self._advance_list(session)
        elif kind == "cancel":
            self._advance_cancel(session)
        elif kind == "buy":
            self._advance_buy(session)
        elif kind == "bid":
            self._advance_bid(session)
        elif kind == "bid_accept":
            self._advance_bid_accept(session)
        elif kind == "trait_list":
            self._advance_trait_list(session)
        return self._session_dict(session_id)

    def _advance_list(self, session: dict[str, Any]) -> None:
        if session["state"] not in ("awaiting_signature", "pending"):
            return
        session["polls"] += 1
        if session["state"] == "awaiting_signature" and session["polls"] >= _LIST_POLLS_TO_PENDING:
            session["state"] = "pending"
        if session["polls"] >= _LIST_POLLS_TO_DONE:
            offer_index = f"MOCKOFFER-{self._next_session_id('listed')}"
            session["offer_index"] = offer_index
            session["state"] = "done"
            self._listings.append(
                {
                    "nft_id": session["nft_id"],
                    "kind": session["listing_kind"],
                    "nft_number": None,
                    "image": (
                        _trait_image_url(session["slot"], session["value"])
                        if session["listing_kind"] == "trait"
                        else ""
                    ),
                    "attributes": [],
                    "amount_drops": session.get("amount_drops"),
                    "amount_brix": session.get("amount_brix"),
                    "seller": session["seller"],
                    "offer_index": offer_index,
                    "is_live": True,
                    "slot": session["slot"],
                    "value": session["value"],
                }
            )
            # Reflect the character/trait as no-longer-unlisted, mirroring
            # the real listener updating market_listings + the caller's next
            # /api/market/mine read excluding it via listed_char_ids/listed_trait_ids.
            econ = mock_economy.INSTANCE
            if session["listing_kind"] == "trait":
                tokens = econ._trait_tokens.get(session["seller"], [])
                econ._trait_tokens[session["seller"]] = [
                    t for t in tokens if t["nft_id"] != session["nft_id"]
                ]

    def _advance_cancel(self, session: dict[str, Any]) -> None:
        if session["state"] != "awaiting_signature":
            return
        session["polls"] += 1
        if session["polls"] >= _CANCEL_POLLS_TO_DONE:
            session["state"] = "done"
            if session.get("bid"):  # #283: cancelling the caller's own bid
                bid = self._find_bid(session["offer_index"])
                if bid is not None:
                    bid["is_live"] = False
                return
            row = self._find(session["offer_index"])
            if row is not None:
                row["is_live"] = False
                # Give the un-listed item back to the seller's wallet pool so
                # Mine's "unlisted" groups pick it back up, mirroring the real
                # listener's close_listing(reason='cancelled').
                if row["kind"] == "trait":
                    mock_economy.INSTANCE._trait_tokens.setdefault(row["seller"], []).append(
                        {"nft_id": row["nft_id"], "slot": row["slot"], "value": row["value"]}
                    )

    def _advance_buy(self, session: dict[str, Any]) -> None:
        if session["state"] not in ("awaiting_signature", "pending"):
            return
        session["polls"] += 1
        if session["state"] == "awaiting_signature" and session["polls"] >= _BUY_POLLS_TO_PENDING:
            session["state"] = "pending"
        if session["polls"] >= _BUY_POLLS_TO_DONE:
            session["state"] = "done"
            row = self._find(session["offer_index"])
            if row is not None:
                row["is_live"] = False
                if row["kind"] == "trait":
                    # Settlement (spec §Q7): the sold trait is immediately
                    # burned back into the buyer's Closet — same terminal
                    # outcome the real settlement sweep converges to.
                    mock_economy.INSTANCE.assets[(row["slot"], row["value"])] = (
                        mock_economy.INSTANCE.assets.get((row["slot"], row["value"]), 0) + 1
                    )

    def _advance_trait_list(self, session: dict[str, Any]) -> None:
        state = session["state"]
        if state == "extract_pending":
            session["polls"] += 1
            if session["polls"] >= _TRAIT_EXTRACT_POLLS:
                econ = mock_economy.INSTANCE
                result = econ.extract(
                    session["owner"], {"slot": session["slot"], "value": session["value"]}
                )
                session["nft_id"] = result["nft_id"]
                session["state"] = "extract_done"
                session["polls"] = 0
        elif state == "extract_done":
            session["polls"] += 1
            if session["polls"] >= _TRAIT_SIGN1_POLLS:
                session["state"] = "list_pending"
                session["polls"] = 0
        elif state == "list_pending":
            session["polls"] += 1
            if session["polls"] >= _TRAIT_SIGN2_POLLS:
                offer_index = f"MOCKOFFER-{self._next_session_id('traitlisted')}"
                session["offer_index"] = offer_index
                session["state"] = "listed"
                self._listings.append(
                    {
                        "nft_id": session["nft_id"],
                        "kind": "trait",
                        "nft_number": None,
                        "image": _trait_image_url(session["slot"], session["value"]),
                        "attributes": [],
                        "amount_drops": None,
                        "amount_brix": session["amount_brix"],
                        "seller": session["owner"],
                        "offer_index": offer_index,
                        "is_live": True,
                        "slot": session["slot"],
                        "value": session["value"],
                    }
                )
                # The freshly-extracted token was briefly a loose wallet
                # trait token (see MockEconomy.extract); it is listed now,
                # not unlisted, so drop it from that Mine group.
                econ = mock_economy.INSTANCE
                tokens = econ._trait_tokens.get(session["owner"], [])
                econ._trait_tokens[session["owner"]] = [
                    t for t in tokens if t["nft_id"] != session["nft_id"]
                ]

    def _session_dict(self, session_id: str) -> dict[str, Any]:
        session = self._sessions[session_id]
        kind = session["kind"]
        base = {"id": session_id, "state": session["state"], "error": None}
        if kind == "list":
            return {
                **base,
                "qr_url": "https://dev/mock-qr",
                "xumm_url": "https://dev/mock-xumm",
                "offer_index": session["offer_index"],
            }
        if kind in ("bid", "bid_accept"):
            return {
                **base,
                "reason": None,
                "qr_url": "https://dev/mock-qr",
                "xumm_url": "https://dev/mock-xumm",
                "offer_index": session["offer_index"],
            }
        if kind == "cancel":
            return {
                **base,
                "qr_url": "https://dev/mock-qr",
                "xumm_url": "https://dev/mock-xumm",
                "offer_index": session["offer_index"],
            }
        if kind == "buy":
            return {
                **base,
                "reason": None,
                "qr_url": "https://dev/mock-qr",
                "xumm_url": "https://dev/mock-xumm",
                "instruction": session["instruction"],
                "offer_index": session["offer_index"],
            }
        # trait_list
        extract_ready = session["state"] not in ("extract_pending",)
        list_ready = session["state"] in ("list_pending", "listed")
        return {
            **base,
            "nft_id": session["nft_id"],
            "offer_index": session["offer_index"],
            "extract_qr_url": "https://dev/mock-qr-extract" if extract_ready else None,
            "extract_xumm_url": "https://dev/mock-xumm-extract" if extract_ready else None,
            "list_qr_url": "https://dev/mock-qr-list" if list_ready else None,
            "list_xumm_url": "https://dev/mock-xumm-list" if list_ready else None,
        }


INSTANCE = MockMarket()
