"""Discord /claim view for the BRIX daily drip (#48).

Drives the surface-agnostic handle_claim coroutine with a fake interaction,
mirroring tests/test_discord_buttons.py.
"""

from __future__ import annotations

import asyncio

import pytest

from surfaces._client.errors import ServiceError
from surfaces.discord_bot import claim_view


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _User:
    id = 12345

    def __str__(self):
        return "alice#0001"


class _Response:
    def __init__(self):
        self.deferred = False
        self.ephemeral = None

    async def defer(self, ephemeral=False):
        self.deferred = True
        self.ephemeral = ephemeral


class _Followup:
    def __init__(self):
        self.sent = []

    async def send(self, embed=None, ephemeral=False, **kw):
        self.sent.append(embed)


class _Interaction:
    def __init__(self):
        self.user = _User()
        self.response = _Response()
        self.followup = _Followup()


class _Svc:
    def __init__(self, status=None, claim=None, status_error=None, claim_error=None):
        self._status = status or {}
        self._claim = claim or {}
        self._status_error = status_error
        self._claim_error = claim_error
        self.claim_calls = 0

    async def brix_status(self, user_id, username=""):
        if self._status_error:
            raise self._status_error
        return self._status

    async def brix_claim(self, user_id, username=""):
        self.claim_calls += 1
        if self._claim_error:
            raise self._claim_error
        return self._claim


def _only_embed(interaction):
    assert len(interaction.followup.sent) == 1
    return interaction.followup.sent[0]


def test_claim_pays_out_and_reports_the_amount():
    svc = _Svc(
        status={"claimable": 4, "unlisted_last_epoch": 4},
        claim={"state": "confirmed", "amount": 4, "tx_hash": "HASH"},
    )
    it = _Interaction()
    _run(claim_view.handle_claim(svc, it))
    embed = _only_embed(it)
    assert "claimed" in embed.title.lower()
    assert "4 BRIX" in embed.description


def test_nothing_accrued_shows_the_balance_and_never_calls_claim():
    svc = _Svc(status={"claimable": 0})
    it = _Interaction()
    _run(claim_view.handle_claim(svc, it))
    assert svc.claim_calls == 0
    assert "not" in _only_embed(it).description.lower()


def test_an_open_claim_short_circuits_instead_of_claiming_again():
    """A submitted payout may already have landed; claiming again must not even
    be attempted."""
    svc = _Svc(status={"claimable": 0, "open_claim": {"state": "submitted", "amount": 3}})
    it = _Interaction()
    _run(claim_view.handle_claim(svc, it))
    assert svc.claim_calls == 0
    assert "progress" in _only_embed(it).title.lower()


def test_missing_trustline_points_at_the_trustline_button():
    svc = _Svc(
        status={"claimable": 4},
        claim_error=ServiceError("trustline", code="trustline_required", status=409),
    )
    it = _Interaction()
    _run(claim_view.handle_claim(svc, it))
    embed = _only_embed(it)
    assert "trustline" in embed.title.lower()
    assert "/letsgo" in embed.description


def test_a_failed_payout_tells_the_user_their_balance_is_intact():
    svc = _Svc(status={"claimable": 4}, claim={"state": "failed", "amount": 4})
    it = _Interaction()
    _run(claim_view.handle_claim(svc, it))
    assert "untouched" in _only_embed(it).description


def test_an_unknown_payout_never_tells_the_user_to_retry():
    """The ambiguous window: the BRIX may already be paid. Suggesting a retry
    would invite a support ticket at best and confusion at worst."""
    svc = _Svc(status={"claimable": 4}, claim={"state": "submitted", "amount": 4})
    it = _Interaction()
    _run(claim_view.handle_claim(svc, it))
    description = _only_embed(it).description
    assert "no need to claim again" in description.lower()


def test_a_status_lookup_failure_is_reported_not_raised():
    svc = _Svc(status_error=ServiceError("down", code=None, status=503))
    it = _Interaction()
    _run(claim_view.handle_claim(svc, it))
    assert _only_embed(it) is not None


@pytest.mark.parametrize("claimable", [0, 5])
def test_the_response_is_always_deferred_ephemerally(claimable):
    svc = _Svc(status={"claimable": claimable}, claim={"state": "confirmed", "amount": claimable})
    it = _Interaction()
    _run(claim_view.handle_claim(svc, it))
    assert it.response.deferred
    # Balances are private: asserting only that defer() was called would pass
    # if it silently became public.
    assert it.response.ephemeral is True
