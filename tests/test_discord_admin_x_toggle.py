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

    def __init__(self, paused: bool, order: list[str] | None = None):
        self.paused = paused
        self.calls: list[str] = []
        self._order = order

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


class _FailingSvc:
    """x_status always raises — simulates a down/unreachable lfg_service."""

    async def x_status(self):
        raise ServiceError("service unavailable", status=503)

    async def x_pause(self):
        raise ServiceError("service unavailable", status=503)

    async def x_resume(self):
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
