# tests/test_discord_admin_x_toggle.py
# Discord /admin "X posting: pause/resume" button (Task 7, #41). Mirrors the
# MagicMock/SimpleNamespace-interaction patterns already established for
# Discord-bot UI handlers: the button-callback-direct-invoke style from
# tests/test_discord_buttons.py (view.<button>.callback(ix)) and the
# SimpleNamespace-interaction style from tests/test_discord_register.py.
import asyncio
import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from surfaces._client.errors import ServiceError


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _FakeUser:
    id = 9

    def __str__(self) -> str:
        return "admin#1"


class _SponsoredSvc:
    """Minimal status-aware fake for the sponsored-mint panel controls."""

    def __init__(self, status: dict):
        self.status = status
        self.calls: list[tuple[str, str | None]] = []

    async def sponsored_mint_status(self):
        self.calls.append(("status", None))
        return self.status.copy()

    async def sponsored_mint_start(self, actor: str):
        self.calls.append(("start", actor))
        self.status["state"] = "active"
        return self.status.copy()

    async def sponsored_mint_stop(self, actor: str):
        self.calls.append(("stop", actor))
        self.status["state"] = "stopped"
        return self.status.copy()


def _sponsored_status(state: str = "off") -> dict:
    return {
        "state": state,
        "countdown_seconds": 3661,
        "cap": 100,
        "reserved": 12,
        "minting": 0,
        "minted": 7,
        "offered": 0,
        "accepted": 10,
        "tagged_sponsored_wallets": 10,
        "unique_target": 300,
        "unique_tagged_wallets": 23,
        "lfgo_balance": "17.5",
        "recovery_ready": True,
        "burn_burned": 8,
        "burn_pending": 2,
        "last_operator": "discord:7",
        "changed_at": 1_700_000_000,
    }


def _sponsored_interaction(administrator: bool, *, response_done: bool = False):
    sent: list[tuple[str | None, object | None, bool]] = []
    deferred: list[bool] = []
    edits: list[dict] = []

    async def defer(ephemeral=True):
        deferred.append(ephemeral)

    async def send_message(content=None, embed=None, ephemeral=False):
        sent.append((content, embed, ephemeral))

    async def followup_send(content=None, embed=None, ephemeral=False):
        sent.append((content, embed, ephemeral))

    async def edit_original_response(**kwargs):
        edits.append(kwargs)

    inter = SimpleNamespace(
        user=SimpleNamespace(
            id=9,
            guild_permissions=SimpleNamespace(administrator=administrator),
            __str__=lambda self: "admin#1",
        ),
        client=MagicMock(),
        response=SimpleNamespace(
            defer=defer,
            send_message=send_message,
            is_done=lambda: response_done,
        ),
        followup=SimpleNamespace(send=followup_send),
        edit_original_response=edit_original_response,
    )
    return inter, sent, deferred, edits


def _button_interaction(edit_raises=None):
    """Fake interaction for a component (button) click.

    `edits` records every interaction.edit_original_response(**kwargs) call —
    the deferred-update re-render path that makes the mutated button label
    actually show on the ephemeral panel message. `edit_raises` makes that
    call fail (simulates an expired interaction token / Discord API error).
    """
    sent: list[tuple[str | None, object | None]] = []
    edits: list[dict] = []

    async def defer(ephemeral=True):
        return None

    async def followup_send(content=None, embed=None, ephemeral=True):
        sent.append((content, embed))

    async def edit_original_response(**kwargs):
        if edit_raises is not None:
            raise edit_raises
        edits.append(kwargs)

    inter = SimpleNamespace(
        user=_FakeUser(),
        client=MagicMock(),
        response=SimpleNamespace(defer=defer),
        followup=SimpleNamespace(send=followup_send),
        edit_original_response=edit_original_response,
    )
    return inter, sent, edits


def _command_interaction(order: list[str] | None = None):
    """Fake interaction for the top-level /admin slash command.

    The command must ACK (defer) BEFORE any service call — Discord's ~3s
    initial-response window is far shorter than the SDK's retry/backoff —
    so the fake records call order into `order` for the ordering assert.
    """
    sent: dict[str, object] = {}
    order = order if order is not None else []

    async def defer(ephemeral=True):
        order.append("defer")

    async def followup_send(embed=None, view=None, ephemeral=True):
        order.append("followup")
        sent["embed"] = embed
        sent["view"] = view
        sent["ephemeral"] = ephemeral

    inter = SimpleNamespace(
        user=_FakeUser(),
        response=SimpleNamespace(defer=defer),
        followup=SimpleNamespace(send=followup_send),
    )
    return inter, sent, order


class _FakeSvc:
    """Stands in for the shared LFGServiceClient's x_status/x_pause/x_resume.

    `order` (optional) is the same list a fake interaction records into, so
    tests can assert the ACK/service-call interleaving, not just call counts.
    """

    def __init__(self, paused: bool, order: list[str] | None = None, sponsored_state: str = "off"):
        self.paused = paused
        self.calls: list[str] = []
        self._order = order
        self.sponsored_state = sponsored_state

    async def x_status(self):
        self.calls.append("status")
        if self._order is not None:
            self._order.append("x_status")
        return {"paused": self.paused, "month_posts": 5, "budget": 100, "enabled": True}

    async def x_pause(self):
        self.calls.append("pause")
        self.paused = True
        return {"paused": True}

    async def x_resume(self):
        self.calls.append("resume")
        self.paused = False
        return {"paused": False}

    async def sponsored_mint_status(self):
        return _sponsored_status(self.sponsored_state)


class _FailingSvc:
    """x_status always raises — simulates a down/unreachable lfg_service."""

    async def x_status(self):
        raise ServiceError("service unavailable", status=503)

    async def x_pause(self):
        raise ServiceError("service unavailable", status=503)

    async def x_resume(self):
        raise ServiceError("service unavailable", status=503)

    async def sponsored_mint_status(self):
        raise ServiceError("service unavailable", status=503)


@pytest.fixture
def admin_mod(monkeypatch):
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
    # Plain import, no reload — reloading admin/bot would re-register the
    # @tree.command decorators (test_signing_account.py's precedent).
    import surfaces.discord_bot.admin as admin

    return admin


# ---- pure helper functions (no Discord mocking needed) ----


def test_x_toggle_label_running_offers_pause(admin_mod):
    assert admin_mod._x_toggle_label(paused=False) == "⏸️ Pause X posting"


def test_x_toggle_label_paused_offers_resume(admin_mod):
    assert admin_mod._x_toggle_label(paused=True) == "▶️ Resume X posting"


def test_x_status_embed_fields(admin_mod):
    embed = admin_mod._x_status_embed(
        {"paused": False, "month_posts": 7, "budget": 100, "enabled": True}
    )
    fields = {f.name: f.value for f in embed.fields}
    assert fields["Posting"] == "▶️ Running"
    assert fields["This Month"] == "7 / 100"
    assert fields["Enabled"] == "✅ Yes"


def test_x_status_embed_paused_and_disabled(admin_mod):
    embed = admin_mod._x_status_embed(
        {"paused": True, "month_posts": 0, "budget": 100, "enabled": False}
    )
    fields = {f.name: f.value for f in embed.fields}
    assert fields["Posting"] == "⏸️ Paused"
    assert fields["Enabled"] == "❌ No (dark)"


def test_sponsored_status_embed_exposes_all_operator_metrics(admin_mod):
    embed = admin_mod._sponsored_status_embed(_sponsored_status("active"))
    fields = {field.name: field.value for field in embed.fields}
    assert fields["Countdown"] == "01:01:01"
    assert fields["Admitted"] == "29 / 100"
    assert fields["Confirmed"] == "17 / 100"
    assert fields["Accepted / Tagged"] == "10 / 10"
    assert fields["Unique SourceTag"] == "23 / 300"
    assert fields["LFGO Balance"] == "17.5"
    assert fields["Recovery"] == "✅ Ready"
    assert fields["Burned / Pending"] == "8 / 2"
    assert fields["Last Operator"] == "discord:7"
    assert fields["Last Change"] == "<t:1700000000:f>"


@pytest.mark.parametrize("response_done", [False, True])
def test_non_administrator_is_denied_every_admin_view_component(
    admin_mod, monkeypatch, response_done
):
    fake_svc = _SponsoredSvc(_sponsored_status())
    monkeypatch.setattr(admin_mod, "svc", fake_svc)
    view = admin_mod.AdminView()
    interaction, sent, _deferred, _edits = _sponsored_interaction(
        administrator=False, response_done=response_done
    )

    assert len(view.children) >= 10  # existing panel controls plus sponsored controls
    for _component in view.children:
        assert _run(view.interaction_check(interaction)) is False
    assert sent == [("Administrator permission required.", None, True)] * len(view.children)
    assert fake_svc.calls == []


def test_non_administrator_cannot_submit_admin_modal(admin_mod):
    modal = admin_mod.BurnNFTModal()
    interaction, sent, deferred, _edits = _sponsored_interaction(administrator=False)

    _run(modal.on_submit(interaction))

    assert deferred == []
    assert sent == [("Administrator permission required.", None, True)]


@pytest.mark.parametrize(
    ("button_name", "initial_state", "expected_call", "expected_start_disabled"),
    [
        ("sponsored_start_button", "off", ("start", "discord:9"), True),
        ("sponsored_stop_button", "active", ("stop", "discord:9"), False),
        ("sponsored_refresh_button", "active", ("status", None), True),
    ],
)
def test_administrator_sponsored_controls_use_authoritative_status(
    admin_mod, monkeypatch, button_name, initial_state, expected_call, expected_start_disabled
):
    fake_svc = _SponsoredSvc(_sponsored_status(initial_state))
    monkeypatch.setattr(admin_mod, "svc", fake_svc)
    log_mock = AsyncMock()
    monkeypatch.setattr(admin_mod, "log_admin_action", log_mock)
    view = admin_mod.AdminView()
    interaction, sent, deferred, edits = _sponsored_interaction(administrator=True)

    assert _run(view.interaction_check(interaction)) is True
    _run(getattr(view, button_name).callback(interaction))

    assert deferred == [True]
    assert fake_svc.calls[-1] == ("status", None)
    assert expected_call in fake_svc.calls
    assert view.sponsored_start_button.disabled is expected_start_disabled
    assert sent[-1][1] is not None
    assert sent[-1][2] is True
    assert edits == [{"view": view}]
    if button_name == "sponsored_refresh_button":
        log_mock.assert_not_awaited()
    else:
        log_mock.assert_awaited_once()


# ---- button click: view.x_toggle_button.callback(ix), per test_discord_buttons.py ----


def test_toggle_button_pauses_when_running(admin_mod, monkeypatch):
    fake_svc = _FakeSvc(paused=False)
    monkeypatch.setattr(admin_mod, "svc", fake_svc)
    log_mock = AsyncMock()
    monkeypatch.setattr(admin_mod, "log_admin_action", log_mock)

    view = admin_mod.AdminView()
    ix, sent, edits = _button_interaction()
    _run(view.x_toggle_button.callback(ix))

    assert fake_svc.calls == ["status", "pause"]
    assert view.x_toggle_button.label == "▶️ Resume X posting"
    # The label mutation alone is a dead write — the panel message must be
    # re-rendered (deferred-update edit of the component's own ephemeral
    # message) so the new label actually shows on screen.
    assert edits == [{"view": view}]
    assert len(sent) == 1
    content, embed = sent[0]
    assert embed is not None
    fields = {f.name: f.value for f in embed.fields}
    assert fields["Posting"] == "⏸️ Paused"
    log_mock.assert_awaited_once()
    assert "paused" in log_mock.await_args.args[1]


def test_sponsored_start_logs_success_when_status_refresh_fails(admin_mod, monkeypatch):
    class _StatusFailingSvc:
        async def sponsored_mint_start(self, actor: str):
            self.actor = actor

        async def sponsored_mint_status(self):
            raise ServiceError("service unavailable", status=503)

    fake_svc = _StatusFailingSvc()
    monkeypatch.setattr(admin_mod, "svc", fake_svc)
    log_mock = AsyncMock()
    monkeypatch.setattr(admin_mod, "log_admin_action", log_mock)
    view = admin_mod.AdminView()
    interaction, sent, _deferred, _edits = _sponsored_interaction(administrator=True)

    _run(view.sponsored_start_button.callback(interaction))

    assert fake_svc.actor == "discord:9"
    log_mock.assert_awaited_once()
    assert sent == [("✅ Sponsored mint started, but status refresh failed.", None, True)]


def test_toggle_button_resumes_when_paused(admin_mod, monkeypatch):
    fake_svc = _FakeSvc(paused=True)
    monkeypatch.setattr(admin_mod, "svc", fake_svc)
    monkeypatch.setattr(admin_mod, "log_admin_action", AsyncMock())

    view = admin_mod.AdminView()
    ix, sent, edits = _button_interaction()
    _run(view.x_toggle_button.callback(ix))

    assert fake_svc.calls == ["status", "resume"]
    assert view.x_toggle_button.label == "⏸️ Pause X posting"
    assert edits == [{"view": view}]  # panel re-rendered with the new label
    content, embed = sent[0]
    fields = {f.name: f.value for f in embed.fields}
    assert fields["Posting"] == "▶️ Running"


def test_toggle_button_degrades_ephemerally_on_service_error(admin_mod, monkeypatch):
    monkeypatch.setattr(admin_mod, "svc", _FailingSvc())
    log_mock = AsyncMock()
    monkeypatch.setattr(admin_mod, "log_admin_action", log_mock)

    view = admin_mod.AdminView()
    original_label = view.x_toggle_button.label
    ix, sent, edits = _button_interaction()
    _run(view.x_toggle_button.callback(ix))

    assert len(sent) == 1
    content, embed = sent[0]
    assert embed is None
    assert content is not None and "❌" in content
    # No mutation happened, so the label/panel/audit log must be untouched.
    assert view.x_toggle_button.label == original_label
    assert edits == []
    log_mock.assert_not_awaited()


def test_toggle_button_survives_a_failed_panel_rerender(admin_mod, monkeypatch):
    """The re-render edit is best-effort: if Discord rejects it (expired
    interaction token, API hiccup) the toggle itself already succeeded — the
    status embed followup and the audit log must still go out."""
    import discord

    fake_svc = _FakeSvc(paused=False)
    monkeypatch.setattr(admin_mod, "svc", fake_svc)
    log_mock = AsyncMock()
    monkeypatch.setattr(admin_mod, "log_admin_action", log_mock)

    view = admin_mod.AdminView()
    err = discord.HTTPException(MagicMock(status=401, reason="Unauthorized"), "expired token")
    ix, sent, _edits = _button_interaction(edit_raises=err)
    _run(view.x_toggle_button.callback(ix))  # must not raise

    assert fake_svc.calls == ["status", "pause"]
    assert len(sent) == 1
    _content, embed = sent[0]
    assert embed is not None  # status embed still delivered
    log_mock.assert_awaited_once()


# ---- /admin command wiring: admin_command.callback(ix), same unwrap idiom ----


def test_admin_command_acks_before_status_fetch(admin_mod, monkeypatch):
    # CRITICAL ordering: Discord's initial-response window is ~3s; the SDK's
    # retry/backoff on a down service sleeps far longer. The defer (ACK) must
    # happen BEFORE svc.x_status() is awaited, and the panel goes out on the
    # followup — never response.send_message after a potentially-slow call.
    order: list[str] = []
    monkeypatch.setattr(admin_mod, "svc", _FakeSvc(paused=False, order=order))
    ix, sent, order = _command_interaction(order)
    _run(admin_mod.admin_command.callback(ix))
    assert "defer" in order and "x_status" in order
    assert order.index("defer") < order.index("x_status")
    assert order[-1] == "followup"


def test_admin_command_reflects_paused_state_in_button_label(admin_mod, monkeypatch):
    monkeypatch.setattr(admin_mod, "svc", _FakeSvc(paused=True))
    ix, sent, _order = _command_interaction()
    _run(admin_mod.admin_command.callback(ix))
    assert sent["view"].x_toggle_button.label == "▶️ Resume X posting"
    assert sent["ephemeral"] is True


def test_admin_command_reflects_running_state_in_button_label(admin_mod, monkeypatch):
    monkeypatch.setattr(admin_mod, "svc", _FakeSvc(paused=False))
    ix, sent, _order = _command_interaction()
    _run(admin_mod.admin_command.callback(ix))
    assert sent["view"].x_toggle_button.label == "⏸️ Pause X posting"


def test_admin_command_degrades_when_status_unavailable(admin_mod, monkeypatch):
    monkeypatch.setattr(admin_mod, "svc", _FailingSvc())
    ix, sent, order = _command_interaction()
    # Must not raise — the whole panel must still be sent via the followup
    # (the deferred ACK already went out, so followup is the only valid path).
    _run(admin_mod.admin_command.callback(ix))
    assert order[0] == "defer" and order[-1] == "followup"
    assert sent["view"] is not None
    # Falls back to the view's default (class-defined) label.
    assert sent["view"].x_toggle_button.label == "⏸️ Pause X posting"


def test_admin_command_initializes_sponsored_control_state(admin_mod, monkeypatch):
    fake_svc = _FakeSvc(paused=False, sponsored_state="active")
    monkeypatch.setattr(admin_mod, "svc", fake_svc)
    interaction, sent, _order = _command_interaction()

    _run(admin_mod.admin_command.callback(interaction))

    assert sent["view"].sponsored_start_button.disabled is True
    assert sent["view"].sponsored_stop_button.disabled is False


def test_admin_command_displays_authoritative_sponsored_status(admin_mod, monkeypatch):
    monkeypatch.setattr(admin_mod, "svc", _FakeSvc(paused=False, sponsored_state="active"))
    interaction, sent, _order = _command_interaction()

    _run(admin_mod.admin_command.callback(interaction))

    fields = {field.name: field.value for field in sent["embed"].fields}
    assert fields["Countdown"] == "01:01:01"
    assert fields["Unique SourceTag"] == "23 / 300"
    assert fields["Last Operator"] == "discord:7"
