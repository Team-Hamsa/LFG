# tests/test_swap_offer_recovery.py
# Covers _create_offer_and_accept's self-offer guard (#136): the reminted
# replacement offer must be SKIPPED when the recipient is the issuer/signing
# account itself — mint_nft always mints Account=SIGNING_ACCOUNT, so the token
# is already in that wallet and a self-directed sell offer is invalid
# (temREDUNDANT). A genuine offer-creation failure still surfaces the admin
# error with no partial result — but only after the #211 on-ledger recheck
# (create_nft_offer collapses indeterminate outcomes into None; a landed
# issuer→swapper offer is adopted instead of failing the session).
#
# Env-guard preamble: importing lfg_core.config freezes its constants at import
# time; set the same defaults test_smoke.py uses so collection order can't
# strand them. (Copy the block verbatim from tests/test_market_ops.py.)
import os

os.environ.setdefault("XUMM_API_KEY", "test")
os.environ.setdefault("XUMM_API_SECRET", "test")
os.environ.setdefault("SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2")
os.environ.setdefault("TOKEN_ISSUER_ADDRESS", "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
os.environ.setdefault("TOKEN_CURRENCY_HEX", "4C46474F00000000000000000000000000000000")
os.environ.setdefault("BUNNY_CDN_ACCESS_KEY", "test")
os.environ.setdefault("BUNNY_CDN_STORAGE_ZONE", "test")
os.environ.setdefault("LAYER_SOURCE", "local")
os.environ.setdefault("BUNNY_PULL_ZONE", "nft.pullzone.example")

import asyncio  # noqa: E402
from decimal import Decimal  # noqa: E402

from lfg_core import config, swap_flow, xrpl_ops, xumm_ops  # noqa: E402

_NEW_NFT_ID = "00191B58B6161690B012F6916ADBBF17A24C4BB687348E21EF54A3CE00459893"


def _run(coro):
    # Repo convention (see tests/test_signing_account.py): a fresh loop that is
    # never set as the thread's current loop, so it doesn't strand loop state
    # for later tests the way asyncio.run() does (asyncio.run sets the current
    # loop to None on exit, breaking webapp/test_smoke's get_event_loop()).
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_session(wallet: str) -> swap_flow.SwapSession:
    s = swap_flow.SwapSession(
        discord_id="d",
        wallet_address=wallet,
        nft1={"name": "n1", "image": "i1"},
        nft2={"name": "n2", "image": "i2"},
        traits_to_swap=["Accessory"],
    )
    s.pay_with = "XRP"
    s.fee_per_nft = Decimal("1")
    return s


def _item() -> dict:
    return {
        "nft": {"name": "Let's Effing Go! #3536"},
        "new_nft_id": _NEW_NFT_ID,
        "image_url": "img",
        "video_url": None,
        "metadata_url": "meta",
    }


def test_self_issuer_recipient_skips_offer_and_marks_delivered(monkeypatch):
    """Recipient == issuer: never attempt a (self-)offer; record it delivered."""
    calls = []

    async def fake_offer(*a, **k):
        calls.append(1)
        return None

    monkeypatch.setattr(xrpl_ops, "create_nft_offer", fake_offer)
    monkeypatch.setattr(config, "SIGNING_ACCOUNT", "rISSUERxxxxxxxxxxxxxxxxxxxxxxxx")

    s = _make_session("rISSUERxxxxxxxxxxxxxxxxxxxxxxxx")
    item = _item()
    ok = _run(swap_flow._create_offer_and_accept(s, item))

    assert ok is True
    assert calls == []  # the offer was never even attempted
    assert s.error is None
    assert item["offer_id"] is None
    assert len(s.results) == 1
    r = s.results[0]
    assert r["modified"] is True  # drives every surface's "no action needed"
    assert r["nft_id"] == _NEW_NFT_ID
    assert "accept_deeplink" not in r
    # #41 T9: the share button needs an edition number without the client
    # regexing the display name — extracted server-side via the existing
    # swap_meta.extract_nft_number, same helper the '#3536' in _item()'s name
    # would be parsed by.
    assert r["nft_number"] == 3536


def test_non_issuer_recipient_creates_priced_offer(monkeypatch):
    """Recipient != issuer: create the sell offer and append the accept link."""

    async def fake_offer(nft_id, dest, amount=None, **kwargs):
        assert dest == "rUSERyyyyyyyyyyyyyyyyyyyyyyyyyy"
        return "OFFER123"

    async def fake_accept(offer_id, return_url=None, user_token=None, **kwargs):
        return {"qr_url": "q", "xumm_url": "x"}

    monkeypatch.setattr(xrpl_ops, "create_nft_offer", fake_offer)
    monkeypatch.setattr(xumm_ops, "create_accept_offer_payload", fake_accept)
    monkeypatch.setattr(config, "SIGNING_ACCOUNT", "rISSUERxxxxxxxxxxxxxxxxxxxxxxxx")

    s = _make_session("rUSERyyyyyyyyyyyyyyyyyyyyyyyyyy")
    item = _item()
    ok = _run(swap_flow._create_offer_and_accept(s, item))

    assert ok is True
    assert item["offer_id"] == "OFFER123"
    assert s.error is None
    assert len(s.results) == 1
    assert s.results[0]["accept_deeplink"] == "x"
    assert s.results[0]["modified"] is False
    assert s.results[0]["nft_number"] == 3536  # #41 T9, see comment above


def test_result_omits_nft_number_when_name_has_no_number(monkeypatch):
    """extract_nft_number returns None for a name without '#<digits>' — the
    result must still carry an explicit nft_number: None (not a missing key)
    so the client's bithomp-URL fallback triggers deliberately, never a
    client-side regex over the display name (#41 T9)."""
    monkeypatch.setattr(config, "SIGNING_ACCOUNT", "rISSUERxxxxxxxxxxxxxxxxxxxxxxxx")

    s = _make_session("rISSUERxxxxxxxxxxxxxxxxxxxxxxxx")
    item = _item()
    item["nft"] = {"name": "Let's Effing Go! (no number)"}
    ok = _run(swap_flow._create_offer_and_accept(s, item))

    assert ok is True
    assert s.results[0]["nft_number"] is None


def test_offer_creation_failure_surfaces_admin_error(monkeypatch):
    """create_nft_offer returning None + no landed offer on-ledger (#211): the
    on-ledger recheck ran (all bounded retry passes), found nothing matching
    (foreign offers are never adopted), and the admin error surfaces with no
    partial result. The raise_on_error kwarg is recorded in the stub and
    asserted in the test body — an in-stub assert would be swallowed by
    _find_landed_offer's broad except and could never fail the test."""
    looked_up = []

    async def fake_offer(*a, **k):
        return None

    async def fake_sell_offers(nft_id, raise_on_error=False):
        looked_up.append((nft_id, raise_on_error))
        return [
            # Same token, wrong destination — someone else's offer, not ours.
            {
                "offer_index": "FOREIGN1",
                "owner": "rISSUERxxxxxxxxxxxxxxxxxxxxxxxx",
                "destination": "rSOMEONEELSEzzzzzzzzzzzzzzzzzzz",
                "amount": "6562500",
            },
            # Right destination, wrong owner — not the issuer's offer.
            {
                "offer_index": "FOREIGN2",
                "owner": "rNOTISSUERwwwwwwwwwwwwwwwwwwwww",
                "destination": "rUSERyyyyyyyyyyyyyyyyyyyyyyyyyy",
                "amount": "6562500",
            },
        ]

    monkeypatch.setattr(xrpl_ops, "create_nft_offer", fake_offer)
    monkeypatch.setattr(xrpl_ops, "get_nft_sell_offers", fake_sell_offers)
    monkeypatch.setattr(config, "SIGNING_ACCOUNT", "rISSUERxxxxxxxxxxxxxxxxxxxxxxxx")
    monkeypatch.setattr(swap_flow, "_LANDED_OFFER_DELAY_SECONDS", 0)

    s = _make_session("rUSERyyyyyyyyyyyyyyyyyyyyyyyyyy")
    item = _item()
    ok = _run(swap_flow._create_offer_and_accept(s, item))

    assert ok is False
    # The recheck ran every bounded pass, each raising on RPC error, not []
    # (raise_on_error=True is the indeterminate-lookups-must-raise contract).
    assert looked_up == [(_NEW_NFT_ID, True)] * swap_flow._LANDED_OFFER_ATTEMPTS
    assert "offer failed" in (s.error or "")
    assert s.results == []


def test_offer_failure_adopts_landed_offer(monkeypatch):
    """#211 core recovery: create_nft_offer collapses an indeterminate outcome
    (raised submit / slow confirm loop) into None even when the offer LANDED —
    the on-ledger recheck finds the issuer→swapper offer and adopts it, so the
    session proceeds to the accept payload instead of failed_offers. Amount is
    deliberately NOT matched: swap offers are fee-priced (non-zero, AMM-quote
    dependent), unlike bulk's amount-0 gift offers."""

    async def fake_offer(*a, **k):
        return None

    async def fake_sell_offers(nft_id, raise_on_error=False):
        assert nft_id == _NEW_NFT_ID
        return [
            {
                "offer_index": "LANDED123",
                "owner": "rISSUERxxxxxxxxxxxxxxxxxxxxxxxx",
                "destination": "rUSERyyyyyyyyyyyyyyyyyyyyyyyyyy",
                "amount": "6562500",  # non-zero: fee-priced, still adopted
            }
        ]

    async def fake_accept(offer_id, return_url=None, user_token=None, **kwargs):
        assert offer_id == "LANDED123"
        return {"qr_url": "q", "xumm_url": "x"}

    monkeypatch.setattr(xrpl_ops, "create_nft_offer", fake_offer)
    monkeypatch.setattr(xrpl_ops, "get_nft_sell_offers", fake_sell_offers)
    monkeypatch.setattr(xumm_ops, "create_accept_offer_payload", fake_accept)
    monkeypatch.setattr(config, "SIGNING_ACCOUNT", "rISSUERxxxxxxxxxxxxxxxxxxxxxxxx")

    s = _make_session("rUSERyyyyyyyyyyyyyyyyyyyyyyyyyy")
    item = _item()
    ok = _run(swap_flow._create_offer_and_accept(s, item))

    assert ok is True
    assert item["offer_id"] == "LANDED123"
    assert s.error is None
    assert len(s.results) == 1
    assert s.results[0]["accept_deeplink"] == "x"


def test_offer_failure_lookup_error_fails_closed(monkeypatch):
    """Recheck lookup raising (RPC blip) is indeterminate: never adopt, retry
    through the blip, then fail exactly as before — the post-burn index write
    (run_swap_session) already persisted the new token, so failed_offers no
    longer strands the edition."""
    attempts = []

    async def fake_offer(*a, **k):
        return None

    async def fake_sell_offers(nft_id, raise_on_error=False):
        attempts.append(nft_id)
        raise RuntimeError("tooBusy")

    monkeypatch.setattr(xrpl_ops, "create_nft_offer", fake_offer)
    monkeypatch.setattr(xrpl_ops, "get_nft_sell_offers", fake_sell_offers)
    monkeypatch.setattr(config, "SIGNING_ACCOUNT", "rISSUERxxxxxxxxxxxxxxxxxxxxxxxx")
    monkeypatch.setattr(swap_flow, "_LANDED_OFFER_DELAY_SECONDS", 0)

    s = _make_session("rUSERyyyyyyyyyyyyyyyyyyyyyyyyyy")
    item = _item()
    ok = _run(swap_flow._create_offer_and_accept(s, item))

    assert ok is False
    assert len(attempts) == swap_flow._LANDED_OFFER_ATTEMPTS  # retried through the blip
    assert "offer failed" in (s.error or "")
    assert s.results == []


def test_offer_failure_adopts_offer_landing_on_retry(monkeypatch):
    """#211 recheck window: submit_and_wait can raise after the tx was
    forwarded, and the offer validates 1-2 ledgers later — the first recheck
    pass sees nothing, but a bounded later pass finds and adopts it instead
    of failing the session while a live, unclaimed offer lands."""
    calls = []

    async def fake_offer(*a, **k):
        return None

    async def fake_sell_offers(nft_id, raise_on_error=False):
        calls.append(nft_id)
        if len(calls) < 2:
            return []  # not validated yet on the first pass
        return [
            {
                "offer_index": "LATE123",
                "owner": "rISSUERxxxxxxxxxxxxxxxxxxxxxxxx",
                "destination": "rUSERyyyyyyyyyyyyyyyyyyyyyyyyyy",
                "amount": "6562500",
            }
        ]

    async def fake_accept(offer_id, return_url=None, user_token=None, **kwargs):
        assert offer_id == "LATE123"
        return {"qr_url": "q", "xumm_url": "x"}

    monkeypatch.setattr(xrpl_ops, "create_nft_offer", fake_offer)
    monkeypatch.setattr(xrpl_ops, "get_nft_sell_offers", fake_sell_offers)
    monkeypatch.setattr(xumm_ops, "create_accept_offer_payload", fake_accept)
    monkeypatch.setattr(config, "SIGNING_ACCOUNT", "rISSUERxxxxxxxxxxxxxxxxxxxxxxxx")
    monkeypatch.setattr(swap_flow, "_LANDED_OFFER_DELAY_SECONDS", 0)

    s = _make_session("rUSERyyyyyyyyyyyyyyyyyyyyyyyyyy")
    item = _item()
    ok = _run(swap_flow._create_offer_and_accept(s, item))

    assert ok is True
    assert item["offer_id"] == "LATE123"
    assert len(calls) == 2  # adopted on the second pass, no third look
    assert s.error is None
