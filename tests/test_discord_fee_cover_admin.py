"""Discord /admin fee-cover sub-panel (spec §Admin panel)."""

import asyncio
import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from surfaces._client.errors import ServiceError


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _status(**over):
    base = {
        "network": "mainnet",
        "state": "active",
        "campaign": {
            "id": 1,
            "coverage_bps": 10_000,
            "budget_drops": 50_000_000,
            "wallet_cap_drops": 5_000_000,
            "wallet_window_seconds": 2_592_000,
            "min_bid_drops": 1_000_000,
            "ends_at": None,
        },
        "committed_drops": 80_642,
        "paid_drops": 0,
        "remaining_drops": 49_919_358,
        "open_promises": 1,
        "refunds_by_state": {"owed": 1},
        "declines_by_reason": {"below_clearing": 2},
        "top_wallets": [{"wallet": "rHaMsAjoAN21s1XG5TCAM6ErAefzrggsHf", "drops": 80_642}],
        "issuer_balance_drops": 123_000_000,
    }
    base.update(over)
    return base


class _Svc:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    async def fee_cover_status(self):
        self.calls.append(("status",))
        return _status()

    async def fee_cover_start(self, actor, **fields):
        self.calls.append(("start", actor, fields))
        return _status(result="started")

    async def fee_cover_update(self, actor, **fields):
        self.calls.append(("update", actor, fields))
        return _status(result="updated")

    async def fee_cover_stop(self, actor):
        self.calls.append(("stop", actor))
        if self.fail:
            raise ServiceError("service unavailable", status=503)
        return _status(state="stopped", result="stopped")


def _interaction(administrator=True):
    record = {"sent": [], "followups": [], "deferred": 0, "modals": []}

    async def defer(ephemeral=True):
        record["deferred"] += 1

    async def send_message(content=None, embed=None, ephemeral=False):
        record["sent"].append((content, embed))

    async def send_modal(modal):
        record["modals"].append(modal)

    async def followup_send(content=None, embed=None, view=None, ephemeral=True):
        record["followups"].append({"content": content, "embed": embed, "view": view})

    inter = SimpleNamespace(
        user=SimpleNamespace(id=9, guild_permissions=SimpleNamespace(administrator=administrator)),
        client=object(),
        response=SimpleNamespace(
            defer=defer, send_message=send_message, send_modal=send_modal, is_done=lambda: False
        ),
        followup=SimpleNamespace(send=followup_send),
    )
    return inter, record


@pytest.fixture
def mods(monkeypatch):
    for k, v in {
        "DISCORD_BOT_TOKEN": "t",
        "ADMIN_LOG_CHANNEL_ID": "1",
        "LFG_SERVICE_URL": "http://svc",
        "SERVICE_TOKEN_DISCORD": "s",
        "SEED": "sEdSKaCy2JT7JaM7v95H9SxkhP9wS2r",
        "XUMM_API_KEY": "k",
        "XUMM_API_SECRET": "s",
        "TOKEN_ISSUER_ADDRESS": "rIssuer",
        "TOKEN_CURRENCY_HEX": "ABC",
    }.items():
        monkeypatch.setenv(k, v)
    import surfaces.discord_bot.config as cfg

    importlib.reload(cfg)
    import surfaces.discord_bot.admin as admin
    import surfaces.discord_bot.fee_cover_admin as fca

    return admin, fca


def test_parse_form_normalizes_numbers_and_omits_blank_duration(mods):
    _, fca = mods
    assert fca.parse_form(
        coverage="100", budget="50.50", wallet_cap="5", min_bid="0", duration=" "
    ) == {
        "coverage_pct": "100",
        "budget_xrp": "50.5",
        "wallet_cap_xrp": "5",
        "min_bid_xrp": "0",
    }
    assert (
        fca.parse_form(coverage="50", budget="1", wallet_cap="1", min_bid="1", duration="24")[
            "duration_hours"
        ]
        == "24"
    )
    assert (
        fca.parse_form(coverage="50", budget="1", wallet_cap="1", min_bid="0.000001", duration="")[
            "min_bid_xrp"
        ]
        == "0.000001"
    )


@pytest.mark.parametrize(
    "fields",
    [
        {"coverage": "abc", "budget": "1", "wallet_cap": "1", "min_bid": "0", "duration": ""},
        {"coverage": "101", "budget": "1", "wallet_cap": "1", "min_bid": "0", "duration": ""},
        {"coverage": "100", "budget": "0", "wallet_cap": "1", "min_bid": "0", "duration": ""},
        {"coverage": "100", "budget": "1", "wallet_cap": "1", "min_bid": "-1", "duration": ""},
        {"coverage": "100", "budget": "1", "wallet_cap": "1", "min_bid": "0", "duration": "0"},
        {
            "coverage": "100",
            "budget": "1e999999999",
            "wallet_cap": "1",
            "min_bid": "0",
            "duration": "",
        },
        {"coverage": "100", "budget": "1", "wallet_cap": "1", "min_bid": "0", "duration": "100000"},
        {
            "coverage": "100",
            "budget": "1e-9999999999",
            "wallet_cap": "1",
            "min_bid": "0",
            "duration": "",
        },
        {
            "coverage": "100",
            "budget": "1",
            "wallet_cap": "1",
            "min_bid": "0.0000001",
            "duration": "",
        },
        {"coverage": "1e-20", "budget": "1", "wallet_cap": "1", "min_bid": "0", "duration": ""},
    ],
)
def test_parse_form_rejects_bad_input(mods, fields):
    _, fca = mods
    with pytest.raises(ValueError):
        fca.parse_form(**fields)


def test_status_embed_renders_the_operator_numbers(mods):
    _, fca = mods
    fields = {f.name: f.value for f in fca.fee_cover_status_embed(_status()).fields}
    assert fields["Campaign"] == "Active"
    assert fields["Coverage"] == "100% of broker fee"
    assert fields["Budget"] == "0.080642 / 50 XRP committed"
    assert fields["Paid"] == "0 XRP"
    assert fields["Remaining"] == "49.919358 XRP"
    assert fields["Per-wallet cap"] == "5 XRP / 30d"
    assert fields["Min bid"] == "1 XRP"
    assert fields["Ends"] == "No end"
    assert fields["Refunds"] == "owed 1"
    assert fields["Declines"] == "below_clearing 2"
    assert fields["Top wallets"] == "`rHaMsAjoAN…` 0.080642 XRP"
    assert fields["Issuer XRP balance"] == "123 XRP"
    never = {
        f.name: f.value
        for f in fca.fee_cover_status_embed(
            _status(state="never_started", campaign=None, issuer_balance_drops=None)
        ).fields
    }
    assert never["Campaign"] == "Never Started" and "Coverage" not in never
    assert never["Issuer XRP balance"] == "Balance lookup failed"


def test_start_modal_submits_the_form_and_logs(mods, monkeypatch):
    admin, fca = mods
    svc = _Svc()
    log = AsyncMock()
    monkeypatch.setattr(fca, "svc", svc)
    modal = fca.FeeCoverModal("start", admin._admin_interaction_check, log)
    modal.budget._value = "25"
    modal.duration._value = "48"
    inter, record = _interaction()
    _run(modal.on_submit(inter))
    assert svc.calls == [
        (
            "start",
            "discord:9",
            {
                "coverage_pct": "100",
                "budget_xrp": "25",
                "wallet_cap_xrp": "5",
                "min_bid_xrp": "1",
                "duration_hours": "48",
            },
        )
    ]
    log.assert_awaited_once()
    assert record["followups"][-1]["embed"] is not None


def test_update_modal_ignores_duration(mods, monkeypatch):
    admin, fca = mods
    svc = _Svc()
    monkeypatch.setattr(fca, "svc", svc)
    modal = fca.FeeCoverModal("update", admin._admin_interaction_check, AsyncMock())
    modal.duration._value = "48"
    inter, _ = _interaction()
    _run(modal.on_submit(inter))
    assert svc.calls[0][0] == "update" and "duration_hours" not in svc.calls[0][2]


def test_modal_rejects_bad_input_without_calling_the_service(mods, monkeypatch):
    admin, fca = mods
    svc = _Svc()
    monkeypatch.setattr(fca, "svc", svc)
    modal = fca.FeeCoverModal("start", admin._admin_interaction_check, AsyncMock())
    modal.coverage._value = "150"
    inter, record = _interaction()
    _run(modal.on_submit(inter))
    assert svc.calls == []
    assert record["sent"][0][0].startswith("❌")


def test_modal_denies_non_administrators(mods, monkeypatch):
    admin, fca = mods
    svc = _Svc()
    monkeypatch.setattr(fca, "svc", svc)
    modal = fca.FeeCoverModal("start", admin._admin_interaction_check, AsyncMock())
    inter, record = _interaction(administrator=False)
    _run(modal.on_submit(inter))
    assert svc.calls == []
    assert record["sent"] == [("Administrator permission required.", None)]


def test_view_buttons(mods, monkeypatch):
    admin, fca = mods
    svc = _Svc()
    log = AsyncMock()
    monkeypatch.setattr(fca, "svc", svc)
    view = fca.FeeCoverView(admin._admin_interaction_check, log)

    inter, record = _interaction()
    _run(view.start_button.callback(inter))
    assert (
        isinstance(record["modals"][0], fca.FeeCoverModal) and record["modals"][0].mode == "start"
    )

    inter, record = _interaction()
    _run(view.update_button.callback(inter))
    assert record["modals"][0].mode == "update"

    inter, record = _interaction()
    _run(view.stop_button.callback(inter))
    assert svc.calls[-1] == ("stop", "discord:9")
    log.assert_awaited_once()
    assert record["followups"][-1]["embed"] is not None

    inter, record = _interaction()
    _run(view.refresh_button.callback(inter))
    assert svc.calls[-1] == ("status",)
    log.assert_awaited_once()  # refresh does not log


def test_stop_service_error_is_reported(mods, monkeypatch):
    admin, fca = mods
    monkeypatch.setattr(fca, "svc", _Svc(fail=True))
    view = fca.FeeCoverView(admin._admin_interaction_check, AsyncMock())
    inter, record = _interaction()
    _run(view.stop_button.callback(inter))
    assert record["followups"][-1]["content"].startswith("❌")


def test_admin_panel_button_opens_the_sub_panel(mods, monkeypatch):
    admin, fca = mods
    svc = _Svc()
    monkeypatch.setattr(admin, "svc", svc)
    view = admin.AdminView()
    inter, record = _interaction()
    _run(view.fee_cover_button.callback(inter))
    assert svc.calls == [("status",)]
    sent = record["followups"][-1]
    assert isinstance(sent["view"], fca.FeeCoverView)
    assert sent["embed"] is not None


def test_update_modal_is_prefilled_from_the_live_campaign(mods, monkeypatch):
    """Update replaces every knob, so opening the form against the START
    defaults turns "change the coverage" into "reset the budget, cap and
    minimum bid" — a 25 XRP budget would silently become 50."""
    admin, fca = mods

    class _Svc25(_Svc):
        async def fee_cover_status(self):
            self.calls.append(("status",))
            campaign = dict(_status()["campaign"])
            campaign.update(coverage_bps=7_500, budget_drops=25_000_000, min_bid_drops=2_500_000)
            return _status(campaign=campaign)

    svc = _Svc25()
    monkeypatch.setattr(fca, "svc", svc)
    view = fca.FeeCoverView(admin._admin_interaction_check, AsyncMock())
    inter, record = _interaction()
    _run(view.update_button.callback(inter))
    modal = record["modals"][0]
    assert modal.mode == "update"
    assert (modal.coverage.default, modal.budget.default) == ("75", "25")
    assert (modal.wallet_cap.default, modal.min_bid.default) == ("5", "2.5")
    # ...and the Start form still opens on the shipped defaults.
    inter, record = _interaction()
    _run(view.start_button.callback(inter))
    assert record["modals"][0].budget.default == "50"


def test_update_button_refuses_rather_than_opening_a_form_it_cannot_prefill(mods, monkeypatch):
    admin, fca = mods

    class _NoCampaign(_Svc):
        async def fee_cover_status(self):
            return _status(state="never_started", campaign=None)

    class _Broken(_Svc):
        async def fee_cover_status(self):
            raise ServiceError("service unavailable", status=503)

    for svc, expected in ((_NoCampaign(), "❌ No campaign"), (_Broken(), "❌ Fee cover status")):
        monkeypatch.setattr(fca, "svc", svc)
        view = fca.FeeCoverView(admin._admin_interaction_check, AsyncMock())
        inter, record = _interaction()
        _run(view.update_button.callback(inter))
        assert record["modals"] == []
        assert record["sent"][-1][0].startswith(expected)


def test_modal_defaults_is_empty_without_a_campaign(mods):
    _admin, fca = mods
    assert fca.modal_defaults(None) == {}
    assert fca.modal_defaults({}) == {}
