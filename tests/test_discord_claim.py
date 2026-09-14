"""Discord /claim view for the BRIX daily drip (#48).

Drives the surface-agnostic handle_claim coroutine with a fake interaction,
mirroring tests/test_discord_buttons.py.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import discord
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
    def __init__(self, fail_on=None):
        self.sent = []
        self.calls = []
        # 1-based index of a send that raises, to model an expired webhook token.
        self._fail_on = fail_on

    async def send(self, embed=None, ephemeral=False, **kw):
        if self._fail_on is not None and len(self.calls) + 1 == self._fail_on:
            self.calls.append({"embed": embed, "ephemeral": ephemeral, "failed": True, **kw})
            raise discord.NotFound(MagicMock(status=404, reason="Not Found"), "Unknown Webhook")
        self.sent.append(embed)
        self.calls.append({"embed": embed, "ephemeral": ephemeral, **kw})


class _Interaction:
    def __init__(self, fail_on=None):
        self.user = _User()
        self.response = _Response()
        self.followup = _Followup(fail_on=fail_on)


class _Svc:
    def __init__(
        self,
        status=None,
        claim=None,
        status_error=None,
        claim_error=None,
        trustline=None,
        trustline_error=None,
        trustline_final=None,
    ):
        self._status = status or {}
        self._claim = claim or {}
        self._status_error = status_error
        self._claim_error = claim_error
        self._trustline = trustline or {}
        self._trustline_error = trustline_error
        self._trustline_final = trustline_final or {}
        self.claim_calls = 0
        self.trustline_calls = []
        self.qr_calls = []
        self.waits = []

    async def brix_status(self, user_id, username=""):
        if self._status_error:
            raise self._status_error
        return self._status

    async def brix_claim(self, user_id, username=""):
        self.claim_calls += 1
        if self._claim_error:
            raise self._claim_error
        return self._claim

    async def brix_trustline(self, user_id, username=""):
        self.trustline_calls.append((user_id, username))
        if self._trustline_error:
            raise self._trustline_error
        return self._trustline

    async def qr_png(self, data):
        self.qr_calls.append(data)
        return b"PNG"

    async def wait_for_brix_trustline(self, user_id, uuid, **kw):
        self.waits.append((user_id, uuid, kw))
        if isinstance(self._trustline_final, Exception):
            raise self._trustline_final
        return self._trustline_final


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


def _trustline_required_svc(**kw):
    return _Svc(
        status={"claimable": 4},
        claim_error=ServiceError("trustline", code="trustline_required", status=409),
        **kw,
    )


def test_missing_trustline_never_points_at_the_lfgo_trustline_button():
    """/letsgo's "Set LFGO Trustline" sets a line for TOKEN_CURRENCY_HEX (LFGO
    on mainnet). Drip payouts are on the BRIX pair, so that button cannot clear
    trustline_required and the claim keeps failing."""
    it = _Interaction()
    _run(claim_view.handle_claim(_trustline_required_svc(), it))
    embed = _only_embed(it)
    assert "trustline" in embed.title.lower()
    assert "BRIX" in embed.description
    assert "/letsgo" not in embed.description
    assert "LFGO" not in embed.description


def test_missing_trustline_offers_a_set_brix_trustline_button():
    it = _Interaction()
    _run(claim_view.handle_claim(_trustline_required_svc(), it))
    view = it.followup.calls[0].get("view")
    assert isinstance(view, claim_view.BrixTrustlineView)
    labels = [getattr(item, "label", "") for item in view.children]
    assert any("BRIX Trustline" in (label or "") for label in labels)


def test_the_brix_trustline_button_runs_the_brix_trustline_flow(monkeypatch):
    svc = _trustline_required_svc()
    it = _Interaction()
    _run(claim_view.handle_claim(svc, it))
    view = it.followup.calls[0]["view"]
    pressed = []

    async def fake_handle(s, interaction):
        pressed.append((s, interaction))

    monkeypatch.setattr(claim_view, "handle_brix_trustline", fake_handle)
    press = _Interaction()
    # The decorated button on an instance is bound to the view: interaction only.
    _run(view.set_brix_trustline.callback(press))
    assert pressed == [(svc, press)]


# --- the BRIX trustline button flow ------------------------------------------


def test_trustline_flow_shows_the_xaman_qr_then_confirms_the_line():
    svc = _Svc(
        trustline={"state": "pending", "uuid": "tl-1", "xumm_url": "https://xumm.app/sign/tl-1"},
        trustline_final={"state": "signed", "tx_hash": "TLHASH"},
    )
    it = _Interaction()
    _run(claim_view.handle_brix_trustline(svc, it))

    assert it.response.deferred and it.response.ephemeral is True
    assert svc.trustline_calls == [("12345", "alice#0001")]
    assert svc.qr_calls == ["https://xumm.app/sign/tl-1"]
    assert [w[:2] for w in svc.waits] == [("12345", "tl-1")]

    prompt, done = it.followup.calls
    assert "BRIX" in prompt["embed"].title
    assert "https://xumm.app/sign/tl-1" in prompt["embed"].description
    assert prompt["file"] is not None
    assert prompt["ephemeral"] is True
    assert "set" in done["embed"].title.lower()
    assert "/claim" in done["embed"].description
    assert done["ephemeral"] is True


def test_trustline_flow_poll_stays_inside_the_interaction_token_lifetime():
    """Discord webhook tokens die 15 minutes after the click; the final
    follow-up must be sent before that."""
    svc = _Svc(
        trustline={"state": "pending", "uuid": "tl-1", "xumm_url": "https://xumm.app/sign/tl-1"},
        trustline_final={"state": "signed"},
    )
    _run(claim_view.handle_brix_trustline(svc, _Interaction()))
    timeout = svc.waits[0][2]["timeout"]
    assert 0 < timeout < 15 * 60


def test_trustline_flow_push_delivery_is_mentioned_in_the_prompt():
    svc = _Svc(
        trustline={
            "state": "pending",
            "uuid": "tl-1",
            "xumm_url": "https://xumm.app/sign/tl-1",
            "push": "sent",
        },
        trustline_final={"state": "signed"},
    )
    it = _Interaction()
    _run(claim_view.handle_brix_trustline(svc, it))
    assert "Xaman app" in it.followup.calls[0]["embed"].description


def test_trustline_already_set_says_so_and_never_shows_a_qr():
    svc = _Svc(trustline={"state": "already_set"})
    it = _Interaction()
    _run(claim_view.handle_brix_trustline(svc, it))
    embed = _only_embed(it)
    assert "already" in embed.description.lower()
    assert "/claim" in embed.description
    assert svc.qr_calls == [] and svc.waits == []


def test_trustline_start_failure_is_reported_not_raised():
    svc = _Svc(
        trustline_error=ServiceError(
            "could not reach Xaman", code="signing_unavailable", status=503
        )
    )
    it = _Interaction()
    _run(claim_view.handle_brix_trustline(svc, it))
    embed = _only_embed(it)
    assert "trustline" in embed.title.lower()
    assert svc.qr_calls == [] and svc.waits == []


@pytest.mark.parametrize(
    ("final", "needle"),
    [
        ({"state": "expired"}, "expired"),
        ({"state": "rejected", "code": "signer_mismatch"}, "different wallet"),
        ({"state": "rejected", "code": "tx_failed", "tx_result": "tecNO_LINE"}, "did not confirm"),
        ({"state": "rejected", "code": "tx_unconfirmed"}, "did not confirm"),
        # Poll timed out on a non-terminal state: never claim success or failure.
        ({"state": "opened"}, "still waiting"),
        ({"state": "validating"}, "still waiting"),
    ],
)
def test_trustline_flow_reports_each_non_success_outcome(final, needle):
    svc = _Svc(
        trustline={"state": "pending", "uuid": "tl-1", "xumm_url": "https://xumm.app/sign/tl-1"},
        trustline_final=final,
    )
    it = _Interaction()
    _run(claim_view.handle_brix_trustline(svc, it))
    outcome = it.followup.calls[-1]["embed"]
    assert needle in outcome.description.lower()
    assert "✅" not in outcome.title


def test_losing_track_of_the_request_mid_poll_still_points_back_at_claim():
    """The service keeps trustline requests in memory: a restart mid-poll
    404s. The user may already have approved it, and /claim re-checks the line
    on-ledger — so say that, not a bare "not found"."""
    svc = _Svc(
        trustline={"state": "pending", "uuid": "tl-1", "xumm_url": "https://xumm.app/sign/tl-1"},
        trustline_final=ServiceError("not found", code=None, status=404),
    )
    it = _Interaction()
    _run(claim_view.handle_brix_trustline(svc, it))
    outcome = it.followup.calls[-1]["embed"]
    assert "/claim" in outcome.description
    assert "not found" not in outcome.description.lower()


def test_a_poll_failure_after_the_token_expired_does_not_crash_either():
    """The recovery message after a failed poll is just as late as the normal
    outcome, so its send must be guarded the same way."""
    svc = _Svc(
        trustline={"state": "pending", "uuid": "tl-1", "xumm_url": "https://xumm.app/sign/tl-1"},
        trustline_final=ServiceError("not found", code=None, status=404),
    )
    it = _Interaction(fail_on=2)
    _run(claim_view.handle_brix_trustline(svc, it))  # must not raise
    assert it.followup.calls[-1].get("failed") is True
    assert "/claim" in it.followup.calls[-1]["embed"].description


def test_trustline_flow_survives_an_expired_interaction_token():
    """A long Xaman wait can outlive the webhook token; the final send failing
    must not crash the handler."""
    svc = _Svc(
        trustline={"state": "pending", "uuid": "tl-1", "xumm_url": "https://xumm.app/sign/tl-1"},
        trustline_final={"state": "signed"},
    )
    it = _Interaction(fail_on=2)
    _run(claim_view.handle_brix_trustline(svc, it))  # must not raise
    assert it.followup.calls[-1].get("failed") is True


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
